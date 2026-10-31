"""
Which books mention a plant: a small read-only JSON API over the DuckDB index.

    python build_index.py && python build_books.py && python build_api_tables.py
    uvicorn books_api:app --host 0.0.0.0 --port 7360

Entry points are the same identifiers the RDF export uses (see build_rdf.py -
plant_uri() has exactly two shapes, and the DuckDB tables carry the same two keys):

    GET /books?name=Quercus+robur                                  (any case, x/× normalised)
    GET /books?iri=https://worldfloraonline.org/taxon/wfo-0000292879
    GET /books?iri=https://plantdb.example.org/taxon/crassula%20helmsii
    GET /books?iri=wfo-0000292879                                  (bare key also works)

Options: dutch_only=true keeps titles flagged Dutch; limit (default 1000, max
5000) and offset page the citation list.

Semantics are identical to the hand-over's SPARQL Query C: a mention found
under a synonym name rolls up to the accepted plant (same re-keying as
build_rdf.py: coalesce(accepted_wfo_id, wfo_id), synonym wfo_ids collected
from plant_index). Verified against HANDOVER.md expectations: Quercus robur
2,302 books, Heracleum mantegazzianum 134, Crassula helmsii 26.

Speed comes from build_api_tables.py: mentions re-keyed to the accepted plant
(ekey) and sorted, so a point lookup reads a handful of row groups instead of
scanning 27M rows. Citation fields follow the plant_books_full view.
"""
import queue
import time
import urllib.parse
from contextlib import contextmanager
from pathlib import Path

import duckdb
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "plant_data"
DB = DATA / "plant_index.duckdb"
NAMES = DATA / "plant_names_by_nname.parquet"
NAMES_FULL = DATA / "plant_names_by_full_name.parquet"
CLUSTERS = DATA / "plant_clusters.parquet"
BOOKS = DATA / "plant_books_by_acc.parquet"
UNLINKED = DATA / "plant_books_unlinked.parquet"

WFO = "https://worldfloraonline.org/taxon/"
TAXON = "https://plantdb.example.org/taxon/"

for p in (DB, NAMES, NAMES_FULL, CLUSTERS, BOOKS, UNLINKED):
    if not p.exists():
        raise SystemExit(f"missing {p} - run build_index.py, build_books.py "
                         f"and build_api_tables.py first")

_db = duckdb.connect(str(DB), read_only=True)
_db.execute("SET threads = 2")
_db.execute("SET memory_limit = '1GB'")
_db.execute("SET parquet_metadata_cache = true")

# A small pool of cursors instead of thread-local ones: uvicorn runs sync
# endpoints on a ~40 thread pool, so thread-local cursors mean a fresh
# connection (and cold parquet metadata) for nearly every request. The pool
# keeps a handful warm; the startup ping below reads the footers once.
POOL_SIZE = 2
_pool: "queue.Queue" = queue.Queue()
_WARMUP = [f"SELECT count(*) FROM '{NAMES}' WHERE nname = 'quercus robur'",
           f"SELECT count(*) FROM '{NAMES_FULL}' WHERE nname_full = 'quercus robur l.'",
           f"SELECT count(*) FROM '{CLUSTERS}' WHERE acc = 'wfo-0000292858'",
           f"SELECT count(*) FROM '{BOOKS}' WHERE ekey = 'wfo-0000292858'",
           f"SELECT count(*) FROM '{UNLINKED}' WHERE nname = 'quercus robur'"]


def _warmup():
    for _ in range(POOL_SIZE):
        cur = _db.cursor()
        for sql in _WARMUP:
            cur.execute(sql).fetchall()
        _pool.put(cur)


@contextmanager
def _cursor():
    cur = _pool.get()          # blocks when the pool is saturated -> backpressure
    try:
        yield cur
    finally:
        _pool.put(cur)


def norm(s: str) -> str:
    """Same normalisation as plant_lookup.py / the nname column of plant_books."""
    return " ".join(str(s).replace("×", "x ").split()).lower()


SUMMARY_SQL = f"""
SELECT count(*) mentions, count(DISTINCT title_id) books, sum(pages) page_mentions,
       count(DISTINCT title_id) FILTER (WHERE dutch <> '') books_dutch
FROM '{BOOKS}'
WHERE ekey = $acc AND (NOT $dutch OR dutch <> '')"""

CITATIONS_SQL = f"""
SELECT title_id, nname AS mention_name,
       title, short_title,
       -- TL2Author is populated for only 231 of 205k BHL titles; BHL usually
       -- puts the author at the end of FullTitle after " / "
       coalesce(nullif(book_author, ''),
                nullif(regexp_extract(title, ' / ([^/]{{3,}})$', 1), '')) AS book_author,
       coalesce(start_year, first_year) AS pub_year,
       pages, copies, first_year, last_year, start_year, end_year,
       language_code, published_place, dutch,
       volume, institution, call_number, item_url, pdf_url, title_url
FROM '{BOOKS}'
WHERE ekey = $acc AND (NOT $dutch OR dutch <> '')
ORDER BY pub_year NULLS LAST, pages DESC, title
LIMIT $limit OFFSET $offset"""

UNLINKED_SQL = f"""
SELECT title_id, nname AS mention_name,
       title, short_title,
       coalesce(nullif(book_author, ''),
                nullif(regexp_extract(title, ' / ([^/]{{3,}})$', 1), '')) AS book_author,
       coalesce(start_year, first_year) AS pub_year,
       pages, copies, first_year, last_year, start_year, end_year,
       language_code, published_place, dutch,
       volume, institution, call_number, item_url, pdf_url, title_url
FROM '{UNLINKED}'
WHERE nname = $nname AND (NOT $dutch OR dutch <> '')
ORDER BY pub_year NULLS LAST, pages DESC, title
LIMIT $limit OFFSET $offset"""


def cluster_ids(c, acc: str) -> list[str]:
    """Every wfo_id whose mentions roll up to this accepted record - the same
    synonym re-keying build_rdf.py applies when it exports the mention edges."""
    row = c.execute(f"SELECT wfo_ids FROM '{CLUSTERS}' WHERE acc = ?", [acc]).fetchone()
    return list(row[0]) if row else [acc]


class Timer:
    """Optional per-request stage timings (?debug=true) - find slow stages fast."""
    def __init__(self):
        self.start = time.perf_counter()
        self.laps = {}

    def lap(self, name):
        now = time.perf_counter()
        self.laps[name] = round((now - self.start) * 1000, 1)
        self.start = now


def plant_block(c, acc: str, matched_name: str, matched_status: str,
                dutch_only: bool, limit: int, offset: int, t: Timer | None = None) -> dict:
    t = t or Timer()
    label = c.execute("SELECT name, author, rank, family FROM plant_index WHERE wfo_id = ?",
                      [acc]).fetchone()
    t.lap("label")
    ids = cluster_ids(c, acc)   # metadata only now - the ekey file is already rolled up
    t.lap("cluster")
    summary = dict(zip(("mentions", "books", "page_mentions", "books_dutch"),
                       c.execute(SUMMARY_SQL, {"acc": acc, "dutch": dutch_only}).fetchone()))
    t.lap("summary")
    rows = c.execute(CITATIONS_SQL, {"acc": acc, "dutch": dutch_only,
                                     "limit": limit, "offset": offset}).fetchall()
    t.lap("citations")
    cols = ["title_id", "mention_name", "title", "short_title", "book_author", "pub_year",
            "pages", "copies", "first_year", "last_year", "start_year", "end_year",
            "language_code", "published_place", "dutch", "volume", "institution",
            "call_number", "item_url", "pdf_url", "title_url"]
    return {
        "wfo_id": acc,
        "iri": WFO + acc,
        "name": label[0] if label else None,
        "author": label[1] if label else None,
        "rank": label[2] if label else None,
        "family": label[3] if label else None,
        "matched_name": matched_name,
        "matched_status": matched_status,
        "cluster_size": len(ids),
        **summary,
        "citations": [dict(zip(cols, r)) for r in rows],
    }


def by_name(c, nname: str, dutch_only: bool, limit: int, offset: int,
            t: Timer | None = None) -> list[dict]:
    """Accepted plants whose own name or synonym list contains this name.
    Second pass on "name + author" so the RDF altLabel form also resolves."""
    t = t or Timer()
    hits = c.execute(f"""SELECT wfo_id, name, status, accepted_wfo_id
                         FROM '{NAMES}' WHERE nname = ?""", [nname]).fetchall()
    if not hits:
        hits = c.execute(f"""SELECT wfo_id, name, status, accepted_wfo_id
                             FROM '{NAMES_FULL}' WHERE nname_full = ?""",
                         [nname]).fetchall()
    t.lap("names")
    if not hits:
        return []
    accs = sorted({a for *_, a in hits if a})
    blocks = []
    for acc in accs:
        hit = next((h for h in hits if h[3] == acc), None)
        blocks.append(plant_block(c, acc, hit[1] if hit else nname,
                                  hit[2] if hit else "Accepted",
                                  dutch_only, limit, offset, t))
    if len(blocks) > 1:
        for b in blocks:
            b["note"] = "this name is homonymous; one group per accepted plant"
    return blocks


def by_unlinked_name(c, nname: str, dutch_only: bool, limit: int, offset: int,
                     t: Timer | None = None) -> list[dict]:
    """Mentions recorded under a name that never linked to one WFO record
    (homonyms). These exist in plant_books but are not in the RDF export."""
    t = t or Timer()
    rows = c.execute(UNLINKED_SQL, {"nname": nname, "dutch": dutch_only,
                                    "limit": limit, "offset": offset}).fetchall()
    t.lap("unlinked")
    if not rows:
        return []
    cols = ["title_id", "mention_name", "title", "short_title", "book_author", "pub_year",
            "pages", "copies", "first_year", "last_year", "start_year", "end_year",
            "language_code", "published_place", "dutch", "volume", "institution",
            "call_number", "item_url", "pdf_url", "title_url"]
    return [{
        "wfo_id": None,
        "iri": TAXON + urllib.parse.quote(nname),
        "name": nname,
        "matched_name": nname,
        "matched_status": "Unlinked",
        "books": len(rows),
        "citations": [dict(zip(cols, r)) for r in rows],
        "note": "name not uniquely linked to one WFO record; "
                "these mentions are in plant_books but not in the RDF export",
    }]


def resolve_iri(iri: str) -> tuple[str, str]:
    """(kind, key) for both plant_uri() shapes - or a bare wfo key."""
    iri = iri.strip()
    if iri.startswith(WFO):
        key = iri[len(WFO):]
    elif iri.startswith(TAXON):
        key = urllib.parse.unquote(iri[len(TAXON):])
    else:
        key = iri
    if key.lower().startswith("wfo-"):
        return "wfo", key.lower()
    if "://" in key:
        raise HTTPException(400, f"not a plant IRI this index knows: {iri}")
    return "name", norm(key)


app = FastAPI(title="plant book citations", version="1.0")
_warmup()


@app.get("/")
def root():
    demo = ROOT / "demo" / "index.html"
    if demo.exists():
        return FileResponse(demo)
    return {"endpoints": ["/books?name=...", "/books?iri=<plant iri>", "/health"],
            "examples": ["/books?name=Quercus%20robur",
                         "/books?iri=https://worldfloraonline.org/taxon/wfo-0000292879",
                         "options: dutch_only=true, limit, offset"]}


@app.get("/health")
def health():
    with _cursor() as c:
        return {"ok": True,
                "plants": c.execute("SELECT count(*) FROM plant_index").fetchone()[0]}


@app.get("/books")
def books(name: str = Query(None, description="scientific name, any case"),
          iri: str = Query(None, description="plant IRI (WFO or plantdb taxon)"),
          dutch_only: bool = False,
          limit: int = Query(1000, ge=1, le=5000),
          offset: int = Query(0, ge=0),
          debug: bool = False):
    if bool(name) == bool(iri):
        raise HTTPException(400, "give exactly one of name= or iri=")
    t = Timer()
    with _cursor() as c:
        t.lap("pool")
        if iri:
            kind, key = resolve_iri(iri)
            if kind == "wfo":
                acc = c.execute("""SELECT coalesce(nullif(accepted_wfo_id, ''), wfo_id)
                                   FROM plant_index WHERE wfo_id = ?""", [key]).fetchone()
                if acc is None or acc[0] is None:
                    raise HTTPException(404, f"no plant_index record for {key}")
                t.lap("wfo")
                plants = [plant_block(c, acc[0], key, "Accepted", dutch_only, limit, offset, t)]
            else:
                plants = by_name(c, key, dutch_only, limit, offset, t) \
                         or by_unlinked_name(c, key, dutch_only, limit, offset, t)
            query = iri
        else:
            nname = norm(name)
            plants = by_name(c, nname, dutch_only, limit, offset, t) \
                     or by_unlinked_name(c, nname, dutch_only, limit, offset, t)
            query = nname
    if not plants:
        raise HTTPException(404, f"no book mentions found for {query!r}")
    out = {"query": query, "dutch_only": dutch_only,
           "limit": limit, "offset": offset, "plants": plants}
    if debug:
        t.lap("total")
        out["timings"] = t.laps
    return out

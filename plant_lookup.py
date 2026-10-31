"""
Name lookup library: messy Latin names -> WFO name + GBIF / NCBI / Wikidata / WCVP ids.

    from plant_lookup import lookup
    df = lookup(["Quercus robor", "Bellis perenis L.", "Taraxacum officinale"])

1. Exact match against plant_index (DuckDB, instant).
2. Names not found exactly go to WorldFlora's fuzzy matcher in R (via rpy2).
   The WFO table is loaded into R once per Python session (~1 min), then each
   batch is quick. Send names in one list rather than one call per name.
3. Synonyms are resolved to the accepted name; ids come from plant_index.

Returns a pandas DataFrame, one row per query:
    query, match (exact / fuzzy / none), candidates, fuzzy_dist,
    matched_name, matched_wfo_id, matched_status,
    wfo_id, name, author, family, rank (accepted name), gbif_id, ncbi_taxid,
    wikidata_qid, wcvp_id, ipni_id, powo_id, in_megatree
Needs build_index.py to have run. Read-only: never changes plant_index.duckdb.
"""
import tempfile
from pathlib import Path

import duckdb
import pandas as pd

DATA = Path(__file__).resolve().parent / "plant_data"
DB = DATA / "plant_index.duckdb"
WFO_FOR_R = DATA / "wfo_for_r.tsv"     # quote-free copy of wfo_raw for R, made once
NORM = "lower(trim(regexp_replace(replace({c}, '×', 'x '), '\\s+', ' ', 'g')))"
ID_COLS = ["wfo_id", "name", "author", "family", "rank", "gbif_id", "ncbi_taxid",
           "wikidata_qid", "wcvp_id", "ipni_id", "powo_id", "in_megatree"]

_r_ready = False


def _con():
    return duckdb.connect(str(DB), read_only=True)


def _norm(s):
    return " ".join(str(s).replace("×", "x ").split()).lower()


def _exact(con, queries):
    """Exact normalised-name matches; prefers Accepted when a name has several rows."""
    rows = con.execute(f"""
        WITH q AS (SELECT unnest(?::VARCHAR[]) AS nname)
        SELECT q.nname, p.wfo_id, p.name, p.status, p.accepted_wfo_id,
               count(*) OVER (PARTITION BY q.nname) AS candidates,
               row_number() OVER (PARTITION BY q.nname
                   ORDER BY (p.status = 'Accepted') DESC, p.wfo_id) AS rk
        FROM q JOIN plant_index p ON {NORM.format(c='p.name')} = q.nname
        QUALIFY rk = 1""", [sorted({_norm(x) for x in queries})]).df()
    return rows.set_index("nname")


def _ensure_r():
    """Load WorldFlora and the WFO table into R once per Python session."""
    global _r_ready
    if _r_ready:
        return
    if not WFO_FOR_R.exists():
        tmp = WFO_FOR_R.with_suffix(".part")
        with _con() as con:
            con.execute(f"COPY wfo_raw TO '{tmp}' "
                        "(FORMAT csv, DELIMITER '\t', HEADER true, QUOTE '', ESCAPE '')").fetchall()
        tmp.rename(WFO_FOR_R)
    import rpy2.robjects as ro
    print("loading WFO into R (once per session, ~1 min) ...", flush=True)
    ro.r(f"""
    suppressPackageStartupMessages({{ library(WorldFlora); library(data.table) }})
    .wfo <- data.table::fread("{WFO_FOR_R}", sep = "\\t", quote = "", encoding = "UTF-8",
                              colClasses = "character", showProgress = FALSE)
    """)
    _r_ready = True


def _fuzzy(names, fuzzy):
    """WorldFlora WFO.match + WFO.one for names not matched exactly."""
    _ensure_r()
    import rpy2.robjects as ro
    with tempfile.TemporaryDirectory() as d:
        src, out = Path(d) / "q.tsv", Path(d) / "m.tsv"
        pd.DataFrame({"spec.name": names}).to_csv(src, sep="\t", index=False)
        ro.r(f"""
        q <- data.table::fread("{src}", sep = "\\t", quote = "", encoding = "UTF-8")
        m <- WFO.match(spec.data = as.data.frame(q), WFO.data = .wfo,
                       Fuzzy = {fuzzy}, verbose = FALSE, counter = 1e9)
        o <- WFO.one(m, verbose = FALSE)
        keep <- intersect(c("spec.name", "taxonID", "scientificName", "taxonomicStatus",
                            "Fuzzy", "Fuzzy.dist", "Old.ID", "Old.name", "Old.status"),
                          names(o))
        data.table::fwrite(as.data.frame(o)[, keep, drop = FALSE], "{out}", sep = "\\t")
        """)
        return pd.read_csv(out, sep="\t", dtype=str)


def lookup(names, fuzzy=0.1):
    """names: list of strings. fuzzy: WorldFlora fuzzy threshold (0 = exact only)."""
    names = list(dict.fromkeys(str(n).strip() for n in names if str(n).strip()))
    res = pd.DataFrame({"query": names})
    res["nname"] = res["query"].map(_norm)

    with _con() as con:
        ex = _exact(con, names)
    hit = res["nname"].isin(ex.index)
    res.loc[hit, "match"] = "exact"
    for col, src in [("matched_wfo_id", "wfo_id"), ("matched_name", "name"),
                     ("matched_status", "status"), ("accepted_wfo_id", "accepted_wfo_id"),
                     ("candidates", "candidates")]:
        res.loc[hit, col] = res.loc[hit, "nname"].map(ex[src])

    todo = res.loc[~hit, "query"].tolist()
    if todo and fuzzy > 0:
        fz = _fuzzy(todo, fuzzy).drop_duplicates("spec.name").set_index("spec.name")
        for c in ["taxonID", "scientificName", "taxonomicStatus", "Old.ID", "Old.name",
                  "Old.status", "Fuzzy.dist"]:
            if c not in fz:
                fz[c] = None
        fz = fz.replace("", None)
        fz = fz[fz["taxonID"].notna()]
        # WFO.one gives the accepted name; Old.* is the name actually matched (a synonym)
        fz["m_id"] = fz["Old.ID"].fillna(fz["taxonID"])
        fz["m_name"] = fz["Old.name"].fillna(fz["scientificName"])
        fz["m_status"] = fz["Old.status"].fillna(fz["taxonomicStatus"])
        idx = res.index[~hit & res["query"].isin(fz.index)]
        q = res.loc[idx, "query"]
        res.loc[idx, "match"] = "fuzzy"
        res.loc[idx, "accepted_wfo_id"] = q.map(fz["taxonID"])
        res.loc[idx, "matched_wfo_id"] = q.map(fz["m_id"])
        res.loc[idx, "matched_name"] = q.map(fz["m_name"])
        res.loc[idx, "matched_status"] = q.map(fz["m_status"])
        res.loc[idx, "fuzzy_dist"] = q.map(fz["Fuzzy.dist"])
    res["match"] = res["match"].fillna("none")

    # accepted id: exact matches use accepted_wfo_id when the matched name is a synonym
    acc = res["accepted_wfo_id"].where(res["accepted_wfo_id"].fillna("") != "",
                                       res["matched_wfo_id"])
    res["wfo_id"] = acc
    ids = [x for x in res["wfo_id"].dropna().unique()]
    with _con() as con:
        info = con.execute(f"SELECT {', '.join(ID_COLS)} FROM plant_index "
                           "WHERE wfo_id IN (SELECT unnest(?::VARCHAR[]))", [ids]).df()
    out = res.drop(columns=["nname", "accepted_wfo_id"]).merge(info, on="wfo_id", how="left")
    front = ["query", "match", "candidates", "fuzzy_dist", "matched_name",
             "matched_wfo_id", "matched_status"]
    return out[[c for c in front if c in out] + [c for c in ID_COLS if c in out]]


if __name__ == "__main__":
    import sys
    print(lookup(sys.argv[1:] or ["Quercus robor", "Bellis perennis", "Taraxacum officinale"])
          .to_string(index=False))
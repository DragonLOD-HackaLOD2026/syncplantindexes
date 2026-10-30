"""
Build one combined plant name index from the files in plant_data/.

    python build_index.py

Output: plant_data/plant_index.duckdb (all tables) + plant_data/plant_index.parquet
One row per WFO name with: WFO id, name, author, rank, status, accepted WFO id,
IPNI / POWO / WCVP ids, Wikidata QID, GBIF id, NCBI taxid, and whether the
species is a tip in the megatree. *_src columns say how each id was found
("wikidata" = linked on the same Wikidata item, "name" = unique exact name match).

Rerunnable: each source is converted/loaded once; set REBUILD = True to redo.
Needs: pip install duckdb pandas requests (rpy2 only for the one-time WCVP export)
"""
import csv
import re
import sqlite3
import zipfile
from pathlib import Path

import duckdb

# --- settings ---------------------------------------------------------------
DATA = Path(__file__).resolve().parent / "plant_data"
DB = DATA / "plant_index.duckdb"
QLEVER = "https://qlever.cs.uni-freiburg.de/api/wikidata"  # or your local QLever
COMMON_NAME_LANGS = ["en", "nl"]
REBUILD = False   # True = reload every source table


def log(*a):
    print(*a, flush=True)


# --- 1. one-time conversions to plain files (skipped if the output exists) ---
def fetch_wikidata():
    """Plant items with their ids, as a long table (item, prop, value)."""
    import requests

    queries = {
        "wikidata_ids.csv": """
            PREFIX wdt: <http://www.wikidata.org/prop/direct/>
            SELECT ?item ?prop ?value WHERE {
              { SELECT DISTINCT ?item WHERE {
                  { ?item wdt:P7715 ?w } UNION { ?item wdt:P961 ?i } } }
              VALUES ?prop { wdt:P225 wdt:P405 wdt:P7715 wdt:P961 wdt:P5037 wdt:P846 wdt:P685 }
              ?item ?prop ?value .
            }""",
        "wikidata_common_names.csv": """
            PREFIX wdt: <http://www.wikidata.org/prop/direct/>
            SELECT ?item ?lang ?name WHERE {
              { SELECT DISTINCT ?item WHERE {
                  { ?item wdt:P7715 ?w } UNION { ?item wdt:P961 ?i } } }
              ?item wdt:P1843 ?n .
              BIND(LANG(?n) AS ?lang) BIND(STR(?n) AS ?name)
              FILTER(?lang IN (%s))
            }""" % ", ".join(f'"{l}"' for l in COMMON_NAME_LANGS),
    }
    for fname, q in queries.items():
        out = DATA / fname
        if out.exists():
            log("skip", out.name)
            continue
        log("querying QLever ->", out.name)
        tmp = out.with_suffix(".part")
        try:
            # 10 s to connect (fails fast if QLever is down), up to 1 h to stream
            with requests.post(QLEVER, data={"query": q}, headers={"Accept": "text/csv"},
                               stream=True, timeout=(10, 3600)) as r:
                r.raise_for_status()
                with open(tmp, "wb") as f:
                    for chunk in r.iter_content(1 << 20):
                        f.write(chunk)
            tmp.rename(out)
        except requests.RequestException as e:
            tmp.unlink(missing_ok=True)
            log(f"!! QLever unavailable, building without Wikidata ({e.__class__.__name__}). "
                "Rerun later to add it.")
            return


def export_wcvp():
    """rWCVPdata (R package) -> two CSVs, once."""
    outs = {"wcvp_names": DATA / "wcvp_names.csv",
            "wcvp_distributions": DATA / "wcvp_distributions.csv"}
    if all(p.exists() for p in outs.values()):
        log("skip wcvp csvs")
        return
    import rpy2.robjects as ro
    for obj, path in outs.items():
        if path.exists():
            continue
        log("exporting", obj, "from rWCVPdata")

        ro.r(f"""
        x <- rWCVPdata::{obj}
        x$geometry <- NULL
        out <- "{path}.part"
        if (requireNamespace("data.table", quietly = TRUE)) {{
            data.table::fwrite(x, out)
        }} else {{
            utils::write.csv(x, out, row.names = FALSE)
        }}
        """)


        Path(f"{path}.part").rename(path)


def extract_gbif_plantae():
    """Stream Taxon.tsv out of gbif_backbone.zip, keeping only Plantae rows."""
    out = DATA / "gbif_plantae.tsv"
    if out.exists():
        log("skip", out.name)
        return
    with zipfile.ZipFile(DATA / "gbif_backbone.zip") as z:
        member = next((n for n in z.namelist() if n.lower().endswith("taxon.tsv"Can)), None)
        if member is None:
            raise SystemExit(f"No Taxon.tsv in gbif_backbone.zip. Members: {z.namelist()}")
        log("filtering Plantae from", member, "(a few minutes)")
        tmp = out.with_suffix(".part")
        with z.open(member) as src, open(tmp, "w", encoding="utf-8") as dst:
            header = src.readline().decode("utf-8")
            k = header.rstrip("\n").split("\t").index("kingdom")
            dst.write(header)
            for raw in src:
                line = raw.decode("utf-8")
                cols = line.split("\t")
                if len(cols) > k and cols[k].strip() == "Plantae":
                    dst.write(line)
    tmp.rename(out)


# --- 2. load tables into DuckDB (skipped if the table exists) -----------------
def has_table(con, name):
    return con.execute("SELECT count(*) FROM information_schema.tables WHERE table_name = ?",
                       [name]).fetchone()[0] > 0


def load(con, name, sql_or_fn):
    if has_table(con, name) and not REBUILD:
        log("skip table", name)
        return
    log("loading table", name)
    if callable(sql_or_fn):
        sql_or_fn(con)
    else:
        con.execute(f"CREATE OR REPLACE TABLE {name} AS {sql_or_fn}")


def load_optional(con, name, path, sql, empty_cols):
    """Load from file if present; otherwise an empty placeholder table, which is
    replaced automatically on a later run once the file exists."""
    if path.exists():
        empty = has_table(con, name) and con.execute(f"SELECT count(*) FROM {name}").fetchone()[0] == 0
        if has_table(con, name) and not empty and not REBUILD:
            log("skip table", name)
            return
        log("loading table", name)
        con.execute(f"CREATE OR REPLACE TABLE {name} AS {sql}")
    elif not has_table(con, name):
        log(f"no {path.name} - empty {name} table for now")
        cols = ", ".join(f"{c} VARCHAR" for c in empty_cols)
        con.execute(f"CREATE TABLE {name} ({cols})")


def read_tsv_or_csv(path):
    """WFO's classification.csv may be tab-separated despite the name."""
    first = open(path, encoding="utf-8", errors="replace").readline()
    if "\t" in first:
        return f"read_csv('{path}', delim='\t', quote='', header=true, all_varchar=true)"
    return f"read_csv('{path}', header=true, all_varchar=true)"


def load_ncbi(con):
    """Viridiplantae (taxid 33090) names + synonyms from ETE's taxa.sqlite."""
    import pandas as pd
    s = sqlite3.connect(DATA / "taxa.sqlite")
    plants = "(',' || track || ',') LIKE '%,33090,%'"
    names = pd.read_sql(f"""
        SELECT taxid, spname AS name, rank, 'scientific' AS kind FROM species WHERE {plants}
        UNION ALL
        SELECT y.taxid, y.spname, s.rank, 'synonym' FROM synonym y
        JOIN species s USING (taxid) WHERE {plants.replace('track', 's.track')}""", s)
    s.close()
    con.register("ncbi_df", names)
    con.execute("CREATE OR REPLACE TABLE ncbi AS SELECT * FROM ncbi_df")
    con.unregister("ncbi_df")


def load_megatree(con):
    text = (DATA / "megatree_GBOTB.tre").read_text()
    tips = {t.strip().strip("'").replace("_", " ")
            for t in re.findall(r"[(,]([^(),:;]+)", text)}
    con.execute("CREATE OR REPLACE TABLE megatree_tips (name VARCHAR)")
    con.executemany("INSERT INTO megatree_tips VALUES (?)", [(t,) for t in tips])


# normalised name for exact matching: lower case, single spaces, no hybrid sign
NORM = "lower(trim(regexp_replace(replace({c}, '×', 'x '), '\\s+', ' ', 'g')))"


def n(col):
    return NORM.format(c=col)


INDEX_SQL = f"""
CREATE OR REPLACE TABLE plant_index AS
WITH
wfo AS (
  SELECT taxonID AS wfo_id, scientificName AS name, scientificNameAuthorship AS author,
         taxonRank AS rank, taxonomicStatus AS status, acceptedNameUsageID AS accepted_wfo_id,
         family, nullif(regexp_extract(scientificNameID, '(\\d+-\\d)', 1), '') AS ipni_id,
         {n('scientificName')} AS nname
  FROM wfo_raw),
wd AS (   -- Wikidata long table -> one row per item
  SELECT regexp_extract(item, 'Q\\d+') AS qid,
    any_value(value) FILTER (WHERE prop LIKE '%P7715') AS wfo,
    any_value(value) FILTER (WHERE prop LIKE '%P961')  AS ipni,
    any_value(value) FILTER (WHERE prop LIKE '%P5037') AS powo,
    any_value(value) FILTER (WHERE prop LIKE '%P846')  AS gbif,
    any_value(value) FILTER (WHERE prop LIKE '%P685')  AS ncbi
  FROM wikidata_ids GROUP BY 1),
wd_by_wfo  AS (SELECT wfo,  any_value(qid) qid, any_value(gbif) gbif, any_value(ncbi) ncbi,
                     any_value(powo) powo FROM wd WHERE wfo  IS NOT NULL GROUP BY 1),
wd_by_ipni AS (SELECT ipni, any_value(qid) qid, any_value(gbif) gbif, any_value(ncbi) ncbi,
                     any_value(powo) powo FROM wd WHERE ipni IS NOT NULL GROUP BY 1),
wcvp AS (SELECT CAST(ipni_id AS VARCHAR) ipni_id, any_value(CAST(plant_name_id AS VARCHAR)) wcvp_id,
                any_value(CAST(powo_id AS VARCHAR)) powo_id
         FROM wcvp_names WHERE ipni_id IS NOT NULL GROUP BY 1),
-- name fallbacks only where the name is unique in that source (avoids homonyms)
gbif_u AS (SELECT {n('canonicalName')} nname, any_value(taxonID) gbif_id FROM gbif
           GROUP BY 1 HAVING count(DISTINCT taxonID) = 1),
gbif_a AS (SELECT {n('canonicalName')} nname, {n('scientificNameAuthorship')} nauth,
                  any_value(taxonID) gbif_id FROM gbif
           GROUP BY 1, 2 HAVING count(DISTINCT taxonID) = 1),
ncbi_u AS (SELECT {n('name')} nname, any_value(CAST(taxid AS VARCHAR)) taxid FROM ncbi
           GROUP BY 1 HAVING count(DISTINCT taxid) = 1),
tips AS (SELECT DISTINCT {n('name')} nname FROM megatree_tips)
SELECT
  w.wfo_id, w.name, w.author, w.rank, w.status, w.accepted_wfo_id, w.family,
  coalesce(w.ipni_id, i.ipni)                       AS ipni_id,
  c.wcvp_id,
  coalesce(c.powo_id, f.powo, i.powo)               AS powo_id,
  coalesce(f.qid, i.qid)                            AS wikidata_qid,
  coalesce(f.gbif, i.gbif, ga.gbif_id, gu.gbif_id)  AS gbif_id,
  CASE WHEN coalesce(f.gbif, i.gbif) IS NOT NULL THEN 'wikidata'
       WHEN ga.gbif_id IS NOT NULL THEN 'name+author'
       WHEN gu.gbif_id IS NOT NULL THEN 'name' END   AS gbif_src,
  coalesce(f.ncbi, i.ncbi, nu.taxid)                AS ncbi_taxid,
  CASE WHEN coalesce(f.ncbi, i.ncbi) IS NOT NULL THEN 'wikidata'
       WHEN nu.taxid IS NOT NULL THEN 'name' END     AS ncbi_src,
  t.nname IS NOT NULL                               AS in_megatree
FROM wfo w
LEFT JOIN wd_by_wfo  f  ON f.wfo  = w.wfo_id
LEFT JOIN wd_by_ipni i  ON i.ipni = w.ipni_id AND f.wfo IS NULL
LEFT JOIN wcvp       c  ON c.ipni_id = coalesce(w.ipni_id, i.ipni)
LEFT JOIN gbif_a     ga ON ga.nname = w.nname AND ga.nauth = {n('w.author')}
LEFT JOIN gbif_u     gu ON gu.nname = w.nname
LEFT JOIN ncbi_u     nu ON nu.nname = w.nname
LEFT JOIN tips       t  ON t.nname  = w.nname
"""


def main():
    fetch_wikidata()
    export_wcvp()
    extract_gbif_plantae()

    con = duckdb.connect(str(DB))
    load(con, "wfo_raw", f"SELECT * FROM {read_tsv_or_csv(DATA / 'classification.csv')}")
    load(con, "wcvp_names", f"SELECT * FROM read_csv('{DATA / 'wcvp_names.csv'}', all_varchar=true)")
    load(con, "wcvp_distributions",
         f"SELECT * FROM read_csv('{DATA / 'wcvp_distributions.csv'}', all_varchar=true)")
    load(con, "gbif", f"SELECT * FROM read_csv('{DATA / 'gbif_plantae.tsv'}', delim='\t', "
                      "quote='', header=true, all_varchar=true)")
    load_optional(con, "wikidata_ids", DATA / "wikidata_ids.csv",
                  f"SELECT * FROM read_csv('{DATA / 'wikidata_ids.csv'}', all_varchar=true)",
                  ["item", "prop", "value"])
    load_optional(con, "wikidata_common_names", DATA / "wikidata_common_names.csv",
                  f"SELECT regexp_extract(item, 'Q\\d+') AS qid, lang, name "
                  f"FROM read_csv('{DATA / 'wikidata_common_names.csv'}', all_varchar=true)",
                  ["qid", "lang", "name"])
    load(con, "ncbi", load_ncbi)
    load(con, "megatree_tips", load_megatree)

    log("building plant_index")
    con.execute(INDEX_SQL)
    con.execute(f"COPY plant_index TO '{DATA / 'plant_index.parquet'}' (FORMAT parquet)")
    log(con.execute("""
        SELECT count(*) names, count(wikidata_qid) wikidata, count(gbif_id) gbif,
               count(ncbi_taxid) ncbi, count(wcvp_id) wcvp, sum(in_megatree::INT) megatree
        FROM plant_index""").df().to_string(index=False))
    con.close()


if __name__ == "__main__":
    main()
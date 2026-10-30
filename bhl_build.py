"""
BHL build: which plant names appear in BHL, how often, and when.

    python build_index.py   # first (creates plant_index)
    python bhl_fetch.py     # downloads + extracts
    python bhl_build.py     # this

1. Converts the four BHL TSVs to slim Parquet once (only the needed columns).
2. Keeps only BHL names that occur in plant_index, then counts pages/titles
   and first/last year per name.
3. Writes small tables bhl_name_summary and plant_index_bhl into
   plant_index.duckdb, plus plant_index_bhl.parquet.

The big BHL data stays in Parquet files, not in the database. DuckDB is capped
(THREADS, MEMORY) and spills to disk, so the machine stays usable.
"""
from pathlib import Path

import duckdb

DATA = Path(__file__).resolve().parent / "plant_data"
BHL = DATA / "bhl"
DB = DATA / "plant_index.duckdb"
THREADS = 4
MEMORY = "8GB"          # keep well below your RAM

# column -> wanted columns (from BHL's export schema)
TABLES = {
    "pagename": ["NameConfirmed", "PageID"],
    "page":     ["PageID", "ItemID", "Year"],
    "item":     ["ItemID", "TitleID", "Year"],
    "title":    ["TitleID", "StartYear"],
}
# same name normalisation as build_index.py
NORM = "lower(trim(regexp_replace(replace({c}, '×', 'x '), '\\s+', ' ', 'g')))"


def log(*a):
    print(*a, flush=True)


def connect():
    tmp = BHL / "duckdb_tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(DB))
    con.execute(f"SET threads = {THREADS}")
    con.execute(f"SET memory_limit = '{MEMORY}'")
    con.execute(f"SET temp_directory = '{tmp}'")
    con.execute("SET preserve_insertion_order = false")
    return con


def to_parquet(con):
    """TSV -> Parquet with only the needed columns. Streams; skipped if done."""
    for name, cols in TABLES.items():
        out = BHL / f"{name}.parquet"
        if out.exists():
            log("skip", out.name)
            continue
        src = next((p for p in BHL.glob("*.txt") if p.stem.lower() == name), None)
        if src is None:
            raise SystemExit(f"Missing {name}.txt in {BHL} - run bhl_fetch.py first")
        log("converting", src.name, "->", out.name)
        sel = ", ".join(f'"{c}"' for c in cols)
        tmp = out.with_suffix(".parquet.part")
        con.execute(f"""COPY (SELECT {sel} FROM read_csv('{src}', delim='\t', quote='',
                         header=true, all_varchar=true, ignore_errors=true))
                        TO '{tmp}' (FORMAT parquet)""")
        tmp.rename(out)


def main():
    con = connect()
    if not con.execute("SELECT count(*) FROM information_schema.tables "
                       "WHERE table_name = 'plant_index'").fetchone()[0]:
        raise SystemExit(f"No plant_index in {DB} - run build_index.py first.")
    to_parquet(con)

    p = {n: f"'{BHL / (n + '.parquet')}'" for n in TABLES}
    log("building bhl_name_summary (plant names only)")
    con.execute(f"""
    CREATE OR REPLACE TABLE bhl_name_summary AS
    WITH plants AS (SELECT DISTINCT {NORM.format(c='name')} AS nname FROM plant_index),
    hits AS (                      -- filter first: only names in the index
      SELECT n.nname, n.PageID
      FROM (SELECT {NORM.format(c='NameConfirmed')} AS nname, PageID FROM {p['pagename']}) n
      SEMI JOIN plants USING (nname))
    , dated AS (
      SELECT h.nname, h.PageID, i.TitleID,
             try_cast(regexp_extract(
               coalesce(nullif(pg.Year, ''), nullif(i.Year, ''), t.StartYear),
               '(1[5-9]\\d\\d|20\\d\\d)', 1) AS INT) AS y
      FROM hits h
      JOIN {p['page']}  pg ON pg.PageID = h.PageID
      JOIN {p['item']}  i  ON i.ItemID  = pg.ItemID
      LEFT JOIN {p['title']} t ON t.TitleID = i.TitleID)
    SELECT nname,
           count(DISTINCT PageID)  AS bhl_pages,
           count(DISTINCT TitleID) AS bhl_titles,
           min(y) AS bhl_first_year,
           max(y) AS bhl_last_year
    FROM dated
    GROUP BY nname""")

    con.execute(f"""
    CREATE OR REPLACE TABLE plant_index_bhl AS
    SELECT x.*, b.bhl_pages, b.bhl_titles, b.bhl_first_year, b.bhl_last_year
    FROM plant_index x
    LEFT JOIN bhl_name_summary b ON b.nname = {NORM.format(c='x.name')}""")
    con.execute(f"COPY plant_index_bhl TO '{DATA / 'plant_index_bhl.parquet'}' (FORMAT parquet)")
    log(con.execute("""SELECT count(*) names, count(bhl_pages) in_bhl,
                       min(bhl_first_year) earliest FROM plant_index_bhl""").df().to_string(index=False))
    con.close()


if __name__ == "__main__":
    main()
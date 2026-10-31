"""
Sorted Parquet slices that make books_api.py a millisecond lookup.

    python build_index.py     # first (plant_index)
    python build_books.py     # first (plant_books.parquet)
    python build_api_tables.py   # this

plant_index.duckdb answers "nname = ?" only by scanning (the name column is
mixed case, ~330 ms), and plant_books.parquet is physically in hash-aggregate
order, so a citation lookup scans all 27M rows (~130 ms). Sorting once here
lets DuckDB's row-group zonemaps prune to the few groups that matter:

    plant_names_by_nname.parquet    nname -> (wfo_id, accepted_wfo_id, status)
    plant_names_by_full_name.parquet  same rows keyed by the normalised
                                    "name + author" form (the RDF altLabel)
    plant_clusters.parquet          accepted wfo_id -> every wfo_id in the
                                    name cluster (the accepted record plus
                                    all its synonyms) - the same re-keying
                                    build_rdf.py applies to the mention edges
    plant_books_by_acc.parquet      the citation answer pre-joined and
                                    re-keyed: mentions rolled up like
                                    build_rdf.py (ekey = coalesce(
                                    accepted_wfo_id, wfo_id)) + title/item
                                    text denormalised in, sorted by ekey -
                                    one plant = one contiguous run of rows
    plant_books_unlinked.parquet    the ~4.8M rows with no wfo_id (homonyms,
                                    not in the RDF export) sorted by nname

(plant_books.parquet itself stays slim and dimension-joined - that is the
pipeline artefact; the denormalised serving copy costs ~2 GB extra on disk.)

Rebuild after build_books.py changes (~20 s). Read-only on the source db.
"""
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "plant_data"
DB = DATA / "plant_index.duckdb"
BOOKS_IN = DATA / "plant_books.parquet"

NAMES = DATA / "plant_names_by_nname.parquet"
NAMES_FULL = DATA / "plant_names_by_full_name.parquet"
CLUSTERS = DATA / "plant_clusters.parquet"
BOOKS = DATA / "plant_books_by_acc.parquet"
UNLINKED = DATA / "plant_books_unlinked.parquet"

NORM = "lower(trim(regexp_replace(replace({c}, '\u00d7', 'x '), '\\s+', ' ', 'g')))"


def main():
    if not DB.exists() or not BOOKS_IN.exists():
        raise SystemExit(f"Run build_index.py and build_books.py first "
                         f"(missing {DB if not DB.exists() else BOOKS_IN})")
    con = duckdb.connect()
    con.execute("SET threads = 2")
    con.execute("SET memory_limit = '2GB'")
    con.execute(f"SET temp_directory = '{DATA / 'duckdb_tmp'}'")
    con.execute("SET preserve_insertion_order = false")
    con.execute(f"ATTACH '{DB}' AS src (READ_ONLY)")

    con.execute(f"""COPY (
        SELECT {NORM.format(c='name')} AS nname, wfo_id, name, author, rank, status,
               coalesce(nullif(accepted_wfo_id, ''), wfo_id) AS accepted_wfo_id
        FROM src.plant_index
        ORDER BY nname, wfo_id) TO '{NAMES}' (FORMAT parquet)""")
    print(f"  {NAMES.name}")

    # same rows sorted by the "name + author" form - the RDF skos:altLabel
    # (build_rdf.py: f"{{sname}} {{sauthor}}") so authors can be queried too
    con.execute(f"""COPY (
        SELECT {NORM.format(c="name || ' ' || coalesce(author, '')")} AS nname_full,
               wfo_id, name, author, rank, status,
               coalesce(nullif(accepted_wfo_id, ''), wfo_id) AS accepted_wfo_id
        FROM src.plant_index
        ORDER BY nname_full, wfo_id) TO '{NAMES_FULL}' (FORMAT parquet)""")
    print(f"  {NAMES_FULL.name}")

    con.execute(f"""COPY (
        WITH c AS (SELECT coalesce(nullif(accepted_wfo_id, ''), wfo_id) AS acc, wfo_id
                   FROM src.plant_index
                   WHERE coalesce(nullif(accepted_wfo_id, ''), wfo_id) IS NOT NULL)
        SELECT acc, list(wfo_id) AS wfo_ids
        FROM c GROUP BY acc ORDER BY acc) TO '{CLUSTERS}' (FORMAT parquet)""")
    print(f"  {CLUSTERS.name}")

    # citation slices, denormalised once: titles/items joined in, so a request
    # never re-builds a hash join over the 205k-row dimension; mentions re-keyed
    # to the accepted plant exactly as build_rdf.py exports them, sorted so one
    # plant's citations sit in one contiguous run of rows
    con.execute(f"""COPY (
        SELECT coalesce(nullif(p.accepted_wfo_id, ''), b.wfo_id) AS ekey,
               b.nname, b.wfo_id, b.title_id,
               t."FullTitle"  AS title, t."ShortTitle" AS short_title,
               t."TL2Author"  AS book_author, t."TitleURL" AS title_url,
               b.pages, b.copies, b.first_year, b.last_year,
               b.start_year, b.end_year, b.language_code,
               b.published_place, b.dutch,
               i."VolumeInfo" AS volume, i."InstitutionName" AS institution,
               i."CallNumber" AS call_number,
               i."ItemURL" AS item_url, i."ItemPDFURL" AS pdf_url
        FROM '{BOOKS_IN}' b
        JOIN src.plant_index p USING (wfo_id)      -- inner join: unlinked rows drop out
        JOIN src.bhl_titles t ON t."TitleID" = b.title_id
        LEFT JOIN src.bhl_items i ON i."ItemID" = b.item_id
        ORDER BY ekey, nname, title_id) TO '{BOOKS}' (FORMAT parquet)""")
    print(f"  {BOOKS.name}")

    con.execute(f"""COPY (
        SELECT b.nname, b.title_id,
               t."FullTitle"  AS title, t."ShortTitle" AS short_title,
               t."TL2Author"  AS book_author, t."TitleURL" AS title_url,
               b.pages, b.copies, b.first_year, b.last_year,
               b.start_year, b.end_year, b.language_code,
               b.published_place, b.dutch,
               i."VolumeInfo" AS volume, i."InstitutionName" AS institution,
               i."CallNumber" AS call_number,
               i."ItemURL" AS item_url, i."ItemPDFURL" AS pdf_url
        FROM '{BOOKS_IN}' b
        JOIN src.bhl_titles t ON t."TitleID" = b.title_id
        LEFT JOIN src.bhl_items i ON i."ItemID" = b.item_id
        WHERE b.wfo_id IS NULL
        ORDER BY nname, title_id) TO '{UNLINKED}' (FORMAT parquet)""")
    print(f"  {UNLINKED.name}")
    con.close()


if __name__ == "__main__":
    main()

"""
Which books mention which plant: the BHL name hits folded up per title.

    python build_index.py     # first (plant_index)
    python bhl_fetch.py       # BHL TSV export
    python bhl_build.py       # slim Parquet of the big BHL tables + name summary
    python build_books.py     # this

Output: table `plant_books` in plant_data/plant_index.duckdb and
plant_data/plant_books.parquet - one row per *plant name x BHL title*:

    nname, wfo_id, name, author, rank, title_id, pages, copies, first_year, last_year,
    language_code, published_place, publication_details, start_year, end_year,
    dutch_language, dutch_published, dutch, item_id

The title's own text (FullTitle, TL2Author, CallNumber, InstitutionName, URLs) is *not*
duplicated across the ~27M rows; it stays in the small `bhl_titles` / `bhl_items` tables
and the view `plant_books_full` joins it for ad-hoc SQL. plant_books is the fact table,
those two are its dimensions - that is what keeps the answer fast without re-scanning
217M name-page rows every time.

Reading the counts: `pages` is page-mentions summed over all digitised **copies** of that
title (`copies` says how many), `first_year`/`last_year` are the years of the pages that
mention it, and `wfo_id` is filled only when the name is unique in plant_index - homonyms
stay queryable by `nname`, never guessed.

"Dutch" is two different questions, so it gets two flags (see seed/nl_places.csv):
    dutch_language   LanguageCode IN ('DUT','NLA')          -> 435 BHL titles
    dutch_published  PublicationDetails matches an imprint place in seed/nl_places.csv
                                                              -> 988 BHL titles
    dutch            'both' / 'language' / 'published' / ''
Most books printed in the Netherlands are Latin-language, not Dutch-language, so a
search for "Dutch books" that only filters on language misses most of them.

Deliberately NOT treated as Dutch: Antverpiae/Antwerp (Spanish Netherlands, not the
modern one), bare Lugduni (Lyon), Hafniae (Copenhagen), Lipsiae (Leipzig), Londini,
Parisiis. Rebuilding is cheap (three minutes), so curating is a matter of editing
seed/nl_places.csv and rerunning.
"""
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "plant_data"
BHL = DATA / "bhl"
DB = DATA / "plant_index.duckdb"
PLACES = ROOT / "seed" / "nl_places.csv"
BOOKS_PARQUET = DATA / "plant_books.parquet"
THREADS = 4
MEMORY = "8GB"
TMP = DATA / "duckdb_tmp"

NORM = "lower(trim(regexp_replace(replace({c}, '\u00d7', 'x '), '\\s+', ' ', 'g')))"
# first plausible printing year in a date string (BHL years are often partial)
RYEAR = r"'(1[5-9]\d\d|20\d\d)'"


def run(con, sql, *params):
    """Execute and fully consume the result (newer DuckDB refuses further
    work on a connection with an unfinished result)."""
    return con.execute(sql, *params).fetchall()


def log(*a):
    print(*a, flush=True)


def n(c):
    return NORM.format(c=c)


def main():
    for f in ("pagename", "page"):
        if not (BHL / f"{f}.parquet").exists():
            raise SystemExit(f"Missing {BHL / (f + '.parquet')} - run bhl_fetch.py and bhl_build.py first")
    if not PLACES.exists():
        raise SystemExit(f"Missing {PLACES} - the curated Dutch imprint-place list")

    con = duckdb.connect(str(DB))
    run(con, f"SET threads = {THREADS}")
    run(con, f"SET memory_limit = '{MEMORY}'")
    TMP.mkdir(exist_ok=True)
    run(con, f"SET temp_directory = '{TMP}'")
    run(con, "SET preserve_insertion_order = false")
    for need in ("plant_index", "bhl_titles", "bhl_items"):
        if not run(con, "SELECT count(*) FROM information_schema.tables WHERE table_name = ?",
                   [need])[0][0]:
            raise SystemExit(f"No {need} in {DB} - run build_index.py and bhl_build.py first")

    # imprint places, as a tiny table so the list is data, not code
    run(con, f"""CREATE OR REPLACE TEMP TABLE nl_places AS
                SELECT lower(trim(pattern)) pattern, trim(place_en) place_en
                FROM read_csv('{PLACES}', header=true, all_varchar=true)""")
    log(f"{run(con, 'SELECT count(*) FROM nl_places')[0][0]} imprint patterns loaded")

    # flag each title once (205k titles), so the 27M-row fact table never joins the
    # place list itself
    run(con, f"""
    CREATE OR REPLACE TEMP TABLE title_flags AS
    SELECT t."TitleID",
           lower(coalesce(any_value(t."LanguageCode"), '')) IN ('dut', 'nla') dutch_language,
           bool_or(pos.pattern IS NOT NULL) dutch_published,
           any_value(t."LanguageCode")     language_code,
           any_value(pos.place_en)         published_place,
           any_value(t."PublicationDetails") publication_details,
           any_value(try_cast(t."StartYear" AS INT)) start_year,
           any_value(try_cast(t."EndYear" AS INT))   end_year
    FROM bhl_titles t
    LEFT JOIN nl_places pos
           ON lower(coalesce(t."PublicationDetails", '')) LIKE '%' || pos.pattern || '%'
    GROUP BY 1""")
    log(con.execute("""SELECT count(*) titles, count(*) FILTER (WHERE dutch_language) dutch_language,
                       count(*) FILTER (WHERE dutch_published) dutch_published
                       FROM title_flags""").df().to_string(index=False))

    # Staged, one temp table per step. Each step can spill to disk on its own; as one
    # giant query DuckDB ran the 217M-row scan and the aggregations at once and OOMed
    # at 7.4 GiB even with temp_directory set.
    log("step 1/4: which BHL pages mention a plant name (217M rows -> ~71M)")
    run(con, f"""
    CREATE OR REPLACE TEMP TABLE hits AS
    SELECT DISTINCT h.nname, h."PageID"
    FROM (SELECT {n('"NameConfirmed"')} nname, "PageID" FROM '{BHL / 'pagename.parquet'}') h
    SEMI JOIN (SELECT DISTINCT {n('name')} nname FROM plant_index) p USING (nname)""")
    log(f"  {run(con, 'SELECT count(*) FROM hits')[0][0]:,} name x page pairs")

    log("step 2/4: page -> digitised copy -> title")
    run(con, f"""
    CREATE OR REPLACE TEMP TABLE dated AS
    SELECT h.nname, i."TitleID", i."ItemID", h."PageID",
           try_cast(regexp_extract(
             coalesce(nullif(pg."Year", ''), nullif(i."Year", ''), t."StartYear"),
             {RYEAR}, 1) AS INT) y
    FROM hits h
    JOIN '{BHL / 'page.parquet'}' pg ON pg."PageID" = h."PageID"
    JOIN bhl_items i ON i."ItemID" = pg."ItemID"
    LEFT JOIN bhl_titles t ON t."TitleID" = i."TitleID" """)
    run(con, "DROP TABLE hits")

    log("step 3/4: count per name x copy (pages, first/last year)")
    run(con, f"""
    CREATE OR REPLACE TEMP TABLE per_copy AS
    SELECT nname, "TitleID", "ItemID", count(*) pages, min(y) first_year, max(y) last_year
    FROM dated GROUP BY 1, 2, 3""")
    run(con, "DROP TABLE dated")

    log("step 4/4: count per name x title, then attach names and Dutch flags")
    run(con, f"""
    CREATE OR REPLACE TEMP TABLE books_agg AS
    SELECT nname, "TitleID", cast(sum(pages) AS BIGINT) pages,
           count(DISTINCT "ItemID") copies,
           min(first_year) first_year, max(last_year) last_year,
           -- representative copy: the one where the name appears most
           arg_max("ItemID", pages) item_id
    FROM per_copy GROUP BY 1, 2""")
    run(con, "DROP TABLE per_copy")

    run(con, f"""
    CREATE OR REPLACE TABLE plant_books AS
    WITH by_name AS (                -- names that resolve to exactly one WFO record
        SELECT {n('name')} nname, any_value(wfo_id) wfo_id, any_value(name) name,
               any_value(author) author, any_value(rank) rank
        FROM plant_index GROUP BY 1 HAVING count(*) = 1)
    SELECT a.nname,
           b.wfo_id, b.name, b.author, b.rank,
           a."TitleID" AS title_id,
           a.pages, a.copies, a.first_year, a.last_year,
           tf.language_code, tf.published_place, tf.publication_details,
           tf.start_year, tf.end_year,
           tf.dutch_language, tf.dutch_published,
           CASE WHEN tf.dutch_language AND tf.dutch_published THEN 'both'
                WHEN tf.dutch_language THEN 'language'
                WHEN tf.dutch_published THEN 'published' ELSE '' END AS dutch,
           a.item_id
    FROM books_agg a
    JOIN title_flags tf ON tf."TitleID" = a."TitleID"
    LEFT JOIN by_name b ON b.nname = a.nname""")
    run(con, "DROP TABLE books_agg")

    run(con, f"""
    CREATE OR REPLACE VIEW plant_books_full AS
    SELECT pb.nname, pb.wfo_id, pb.name, pb.author, pb.rank, pb.title_id,
           t."FullTitle" full_title, t."ShortTitle" short_title, t."TL2Author" author_of_book,
           pb.start_year, pb.end_year, pb.language_code, pb.publication_details,
           pb.published_place, pb.dutch_language, pb.dutch_published, pb.dutch,
           pb.pages, pb.copies, pb.first_year, pb.last_year,
           i."CallNumber" call_number, i."InstitutionName" institution,
           i."VolumeInfo" volume, i."ItemURL" item_url, i."ItemPDFURL" pdf_url,
           t."TitleURL" title_url
    FROM plant_books pb
    JOIN bhl_titles t ON t."TitleID" = pb.title_id
    LEFT JOIN bhl_items i ON i."ItemID" = pb.item_id""")

    run(con, f"COPY plant_books TO '{BOOKS_PARQUET}' (FORMAT parquet)")

    log(con.execute("""SELECT count(*) rows, count(DISTINCT nname) names,
                       count(DISTINCT title_id) titles, count(wfo_id) linked_to_wfo,
                       count(*) FILTER (WHERE dutch_language) dutch_language_rows,
                       count(*) FILTER (WHERE dutch_published) dutch_published_rows
                       FROM plant_books""").df().to_string(index=False))
    log(con.execute("""SELECT count(DISTINCT title_id) dutch_titles,
                       count(DISTINCT nname) names_in_dutch_books
                       FROM plant_books WHERE dutch <> ''""").df().to_string(index=False))
    con.close()


if __name__ == "__main__":
    main()

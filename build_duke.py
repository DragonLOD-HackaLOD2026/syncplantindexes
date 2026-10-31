"""
Dr. Duke's ethnobotanical database: a parallel store of medicinal plant uses.

    python build_index.py     # first (plant_index, for the parquet export)
    python build_duke.py      # this

Output: plant_data/duke.duckdb (+ one parquet per table next to it). plant_index.duckdb
is never opened or modified - plant_index is read from plant_index.parquet, so this can
run while another process holds the main database. Delete duke.duckdb and run again to
start over; everything is rebuilt from the source CSVs in seconds.

Source: plant_data/dr_duke/Duke-Source-CSV.zip (Dr. Duke's Phytochemical and
Ethnobotanical Databases, USDA). Only the use/context tables are taken; the chemical
composition (FARMACY, CHEMICALS, AGGREGAC...), dosage/assay and crop-yield tables are
parked - see sources.md.

Tables:
  duke_uses           82,873 use-claims, one row per plant x activity x country:
                        use_id, plant_name, authority, infraspecific, activity,
                        superactivity, country, reference
                      (plant_name is the GENUS+SPECIES binomial: the 200-odd
                      infraspecific rows roll up to their species, the odd variant
                      epithet is kept in `infraspecific`)
  duke_flag           2,376 curated taxa with Duke's usage flag:
                        fnf_id, plant_name, authority, family, useage
                      (M medicinal 1,711, F food 295, G general 127, C cosmetic)
  duke_activity_vocab 2,432 canonical bioactivities (activity, activity_upper)
  duke_superact       activity -> broad category (many-to-many: 1,065 activities
                      under 153 categories, one activity may sit under many, so the
                      full map lives here and duke_uses carries one representative)
  duke_reference_vocab 2,043 citations: short form -> full reference (provenance)
  duke_links          one row per distinct Duke plant name (ETHNOBOT + FNFTAX):
                        plant_name, authority, wfo_id, accepted_wfo_id, link_src
                      linking, tried in order like build_ranges.py: name+author
                      (unique), name+author ignoring the parenthetical author, then
                      unique name alone; finally canonicalised through plant_index's
                      accepted_wfo_id so synonyms land on the accepted taxon.
                      wfo_id is NULL when nothing matched - the row is kept for
                      review; the triple export inner-joins on wfo_id NOT NULL, so
                      unlinked Duke plants produce no triples.
"""
from pathlib import Path
import zipfile

import duckdb

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "plant_data"
DUKE = DATA / "dr_duke"
CSV_DIR = DUKE / "duke_extracted"
DB = DATA / "duke.duckdb"
PLANT_INDEX = DATA / "plant_index.parquet"
NEEDED = ["ETHNOBOT.csv", "FNFTAX.csv", "ACTIVITIES.csv", "SUPERACT.csv", "REFERENCES.csv"]
THREADS = 4
MEMORY = "8GB"
TMP = DATA / "duckdb_tmp"

NORM = "lower(trim(regexp_replace(replace({c}, '×', 'x '), '\\s+', ' ', 'g')))"


def run(con, sql, *params):
    """Execute and fully consume the result (newer DuckDB refuses further
    work on a connection with an unfinished result)."""
    return con.execute(sql, *params).fetchall()


def log(*a):
    print(*a, flush=True)


def n(c):
    return NORM.format(c=c)


def csv(path):
    """Standard CSV. Quote/escape are set explicitly: DuckDB's sniffer only looks at the
    first rows and misses quoted fields further down."""
    return (f"read_csv('{path}', header=true, all_varchar=true, delim=',', "
            f"quote='\"', escape='\"')")


def unparen(c):
    """Drop the parenthetical (recombined) author part, as in build_ranges.py."""
    return f"regexp_replace({c}, '\\([^)]*\\)', '', 'g')"


def extract():
    """Unpack only the tables we need, once, next to the zip."""
    CSV_DIR.mkdir(exist_ok=True)
    missing = [name for name in NEEDED if not (CSV_DIR / name).exists()]
    if not missing:
        return
    z = DUKE / "Duke-Source-CSV.zip"
    if not z.exists():
        raise SystemExit(f"missing {z} and {missing} - download Dr. Duke first (see sources.md)")
    with zipfile.ZipFile(z) as zf:
        for name in missing:
            zf.extract(name, CSV_DIR)
            log(f"   extracted {name}")


def main():
    extract()
    if not PLANT_INDEX.exists():
        raise SystemExit(f"no {PLANT_INDEX} - run build_index.py first")

    con = duckdb.connect(str(DB))
    run(con, f"SET threads = {THREADS}")
    run(con, f"SET memory_limit = '{MEMORY}'")
    TMP.mkdir(exist_ok=True)
    run(con, f"SET temp_directory = '{TMP}'")
    run(con, "SET preserve_insertion_order = false")

    ethno = csv(CSV_DIR / "ETHNOBOT.csv")
    fnftax = csv(CSV_DIR / "FNFTAX.csv")
    activities = csv(CSV_DIR / "ACTIVITIES.csv")
    superact = csv(CSV_DIR / "SUPERACT.csv")
    references = csv(CSV_DIR / "REFERENCES.csv")

    log("building duke_superact, duke_activity_vocab, duke_reference_vocab")
    run(con, f"""
    CREATE OR REPLACE TABLE duke_superact AS
    SELECT DISTINCT trim(ACTIVITY) activity, trim(SUPERACT) superactivity
    FROM {superact} WHERE ACTIVITY IS NOT NULL AND SUPERACT IS NOT NULL""")
    run(con, f"""
    CREATE OR REPLACE TABLE duke_activity_vocab AS
    SELECT DISTINCT trim(ACTIVITY) activity, trim(ACTUPPER) activity_upper
    FROM {activities} WHERE ACTIVITY IS NOT NULL""")
    run(con, f"""
    CREATE OR REPLACE TABLE duke_reference_vocab AS
    SELECT trim(REFERENCE) reference, any_value(nullif(trim(LONGREF), '')) long_reference,
           any_value(nullif(trim(NOTE), '')) note
    FROM {references} WHERE REFERENCE IS NOT NULL GROUP BY 1""")

    log("building duke_uses")
    # one representative superactivity per activity (the map itself is many-to-many)
    run(con, f"""
    CREATE OR REPLACE TABLE duke_uses AS
    WITH sup AS (SELECT activity, any_value(superactivity) superactivity
                 FROM duke_superact GROUP BY 1)
    SELECT try_cast(e.ETHNO AS INT) use_id,
           nullif(trim(e.GENUS || ' ' || e.SPECIES), '') plant_name,
           nullif(trim(e.SPAUT), '') authority,
           nullif(trim(e.SPXNAM), '') infraspecific,
           nullif(trim(e.ACTIVITY), '') activity,
           s.superactivity,
           nullif(trim(e.COUNTRY), '') country,
           nullif(trim(e.REFERENCE), '') reference
    FROM {ethno} e
    LEFT JOIN sup s ON s.activity = trim(e.ACTIVITY)
    WHERE e.GENUS IS NOT NULL AND e.SPECIES IS NOT NULL""")

    log("building duke_flag")
    run(con, f"""
    CREATE OR REPLACE TABLE duke_flag AS
    SELECT try_cast(FNFNUM AS INT) fnf_id,
           nullif(trim(TAXON), '') plant_name,
           nullif(trim(TAXAUTHOR), '') authority,
           nullif(trim(FAMILY), '') family,
           nullif(trim(USEAGE), '') useage
    FROM {fnftax} WHERE TAXON IS NOT NULL""")

    log("building duke_links")
    # names: ETHNOBOT binomials + FNFTAX taxa; authorities kept for the name+author attempt
    run(con, f"""
    CREATE OR REPLACE TABLE duke_links AS
    WITH pi AS (SELECT * FROM read_parquet('{PLANT_INDEX}')),
    by_name_auth AS (SELECT {n("name || ' ' || coalesce(author, '')")} k,
                            any_value(wfo_id) wfo_id
                     FROM pi WHERE nullif(trim(coalesce(author, '')), '') IS NOT NULL
                     GROUP BY 1 HAVING count(*) = 1),
    by_name_bare AS (
        SELECT wfo_id, k FROM (
            SELECT wfo_id, {n(unparen("name || ' ' || coalesce(author, '')"))} k,
                   row_number() OVER (PARTITION BY
                       {n(unparen("name || ' ' || coalesce(author, '')"))}
                       ORDER BY CASE lower(status) WHEN 'accepted' THEN 0
                                                   WHEN 'unchecked' THEN 1 ELSE 2 END) rn
            FROM pi WHERE nullif(trim(coalesce(author, '')), '') IS NOT NULL)
        WHERE rn = 1),
    by_name AS (SELECT {n("name")} k, any_value(wfo_id) wfo_id
                FROM pi GROUP BY 1 HAVING count(*) = 1),
    accepted AS (SELECT wfo_id, any_value(coalesce(nullif(accepted_wfo_id, ''), wfo_id)) accepted
                 FROM pi GROUP BY 1),
    names AS (
        SELECT DISTINCT plant_name, authority FROM (
            SELECT nullif(trim(GENUS || ' ' || SPECIES), '') plant_name,
                   nullif(trim(SPAUT), '') authority
            FROM {ethno} WHERE GENUS IS NOT NULL AND SPECIES IS NOT NULL
            UNION ALL
            SELECT nullif(trim(TAXON), ''), nullif(trim(TAXAUTHOR), '')
            FROM {fnftax} WHERE TAXON IS NOT NULL)
        WHERE plant_name IS NOT NULL)
    SELECT nm.plant_name, nm.authority,
           coalesce(ac.accepted, a.wfo_id, z.wfo_id, b.wfo_id) wfo_id,
           CASE WHEN a.wfo_id IS NOT NULL THEN 'name+author'
                WHEN z.wfo_id IS NOT NULL THEN 'name+author(no parenthetical)'
                WHEN b.wfo_id IS NOT NULL THEN 'name' END link_src
    FROM names nm
    LEFT JOIN by_name_auth a ON a.k = {n("coalesce(nm.plant_name, '') || ' ' || coalesce(nm.authority, '')")}
    LEFT JOIN by_name_bare z ON a.wfo_id IS NULL
         AND z.k = {n(unparen("coalesce(nm.plant_name, '') || ' ' || coalesce(nm.authority, '')"))}
    LEFT JOIN by_name b ON a.wfo_id IS NULL AND z.wfo_id IS NULL
         AND b.k = {n("nm.plant_name")}
    LEFT JOIN accepted ac ON ac.wfo_id = coalesce(a.wfo_id, z.wfo_id, b.wfo_id)""")

    run(con, "CREATE INDEX duke_links_wfo ON duke_links (wfo_id)")
    for t in ["duke_uses", "duke_flag", "duke_activity_vocab", "duke_superact",
              "duke_reference_vocab", "duke_links"]:
        run(con, f"COPY {t} TO '{DATA / (t + '.parquet')}' (FORMAT parquet)")

    log()
    log(con.execute("""SELECT 'duke_uses' t, count(*) rows, count(DISTINCT plant_name) plants FROM duke_uses
                       UNION ALL SELECT 'duke_flag', count(*), count(DISTINCT plant_name) FROM duke_flag
                       UNION ALL SELECT 'duke_links', count(*), count(wfo_id) FROM duke_links
                       UNION ALL SELECT 'duke_activity_vocab', count(*), NULL FROM duke_activity_vocab
                       UNION ALL SELECT 'duke_superact', count(*), NULL FROM duke_superact
                       UNION ALL SELECT 'duke_reference_vocab', count(*), NULL FROM duke_reference_vocab
                       ORDER BY 1""").df().to_string(index=False))
    log()
    log(con.execute("SELECT coalesce(link_src, 'unlinked') link_src, count(*) "
                    "FROM duke_links GROUP BY 1 ORDER BY 2 DESC").df().to_string(index=False))
    log()
    log(con.execute("""SELECT count(*) use_rows,
                              sum(plant_name IN (SELECT plant_name FROM duke_links
                                                 WHERE wfo_id IS NOT NULL)) use_rows_linked_to_wfo
                       FROM duke_uses""").df().to_string(index=False))
    con.close()


if __name__ == "__main__":
    main()

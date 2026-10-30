"""
Build one long table of where each plant is native / introduced / invasive.

    python build_index.py    # first (creates plant_index + exports WCVP csvs)
    python build_ranges.py

Output: table `ranges` in plant_data/plant_index.duckdb and plant_data/ranges.parquet
One row per taxon x region x source:
    source, wfo_id, wcvp_id, name, region_scheme, region_code, region_name,
    status (native / introduced / naturalized / ...), invasive, extinct,
    first_record_year
Sources (each skipped with a message if its files are missing):
    WCVP distributions  plant_data/wcvp_distributions.csv  (native + introduced, TDWG level 3)
    GloNAF              plant_data/glonaf/                 (naturalized / invasive, GloNAF regions)
    SInAS               plant_data/sinas/SInAS_*.csv       (native + alien ranges, first record year)
    GRIIS               plant_data/griis/*.zip             (introduced / invasive per country)
Linking: WCVP and GloNAF by WCVP id; SInAS and GRIIS by exact name (unique names only).
Rebuilt on every run (seconds to minutes); inputs are never modified.
"""
import zipfile
from pathlib import Path

import duckdb

DATA = Path(__file__).resolve().parent / "plant_data"
DB = DATA / "plant_index.duckdb"
THREADS = 4
MEMORY = "8GB"

NORM = "lower(trim(regexp_replace(replace({c}, '×', 'x '), '\\s+', ' ', 'g')))"
COLS = ("source, wfo_id, wcvp_id, name, region_scheme, region_code, region_name, "
        "status, invasive, extinct, first_record_year")


def run(con, sql, *params):
    """Execute and fully consume the result (newer DuckDB refuses further
    work on a connection with an unfinished result)."""
    return con.execute(sql, *params).fetchall()


def log(*a):
    print(*a, flush=True)


def n(c):
    return NORM.format(c=c)


def csv(path, **kw):
    opts = ", ".join(f"{k}={v}" for k, v in {"header": "true", "all_varchar": "true", **kw}.items())
    return f"read_csv('{path}', {opts})"


def tsv(path):
    return csv(path, delim="'\t'", quote="''")


def pick(con, rel, *candidates):
    """First matching column name (case-insensitive) in a relation, or None."""
    cols = [r[0] for r in con.execute(f"DESCRIBE SELECT * FROM {rel}").fetchall()]
    low = {c.lower(): c for c in cols}
    for c in candidates:
        if c.lower() in low:
            return f'"{low[c.lower()]}"'
    log(f"   none of {candidates} in {cols}")
    return None


# --- sources: each returns a SELECT producing COLS, or None ------------------
def wcvp(con):
    f = DATA / "wcvp_distributions.csv"
    if not f.exists():
        log("skip WCVP: no wcvp_distributions.csv (run build_index.py)")
        return None
    return f"""
    SELECT 'wcvp' source, NULL wfo_id, d.plant_name_id wcvp_id, NULL name,
           'tdwg3' region_scheme, d.area_code_l3 region_code, d.area region_name,
           CASE WHEN d.introduced = '1' THEN 'introduced' ELSE 'native' END status,
           NULL::BOOLEAN invasive, d.extinct = '1' extinct, NULL::INT first_record_year
    FROM {csv(f)} d"""


def glonaf(con):
    g = DATA / "glonaf"
    need = ["glonaf_flora2.csv", "glonaf_taxon_wcvp.csv", "glonaf_region.csv"]
    if not all((g / x).exists() for x in need):
        log("skip GloNAF: need", need, "in", g)
        return None
    return f"""
    SELECT 'glonaf' source, NULL wfo_id, nullif(t.accepted_plant_name_id, 'NA') wcvp_id,
           nullif(t.taxa_accepted, 'NA') name,
           'glonaf' region_scheme, r.code region_code, r.name region_name,
           lower(f.status) status, f.status = 'Invasive' invasive,
           NULL::BOOLEAN extinct, NULL::INT first_record_year
    FROM {csv(g / 'glonaf_flora2.csv')} f
    JOIN {csv(g / 'glonaf_taxon_wcvp.csv')} t ON t.id = f.taxon_wcvp_id
    JOIN {csv(g / 'glonaf_region.csv')} r ON r.id = f.region_id"""


def sinas(con):
    files = sorted((DATA / "sinas").glob("SInAS_*.csv"))
    if not files:
        log("skip SInAS: no plant_data/sinas/SInAS_*.csv")
        return None
    rel = csv(files[-1])
    taxon = pick(con, rel, "scientificName", "taxon", "Taxon", "species")
    loc = pick(con, rel, "location", "Location", "locality", "region")
    status = pick(con, rel, "establishmentMeans", "Status", "status")
    year = pick(con, rel, "eventDate", "FirstRecord", "first_record", "firstRecord")
    kingdom = pick(con, rel, "kingdom", "Kingdom")
    invasive = pick(con, rel, "degreeOfEstablishment", "invasionStatus", "InvasionStatus")
    if not (taxon and loc and status):
        log("skip SInAS: columns above not recognised - paste them to me")
        return None
    inv_sql = f"lower({invasive}) LIKE '%invasive%'" if invasive else "NULL::BOOLEAN"
    year_sql = ("try_cast(regexp_extract(" + year + r", '(\d{4})', 1) AS INT)"
                if year else "NULL::INT")
    where = f"WHERE {kingdom} = 'Plantae'" if kingdom else ""
    return f"""
    SELECT 'sinas' source, NULL wfo_id, NULL wcvp_id, {taxon} name,
           'sinas' region_scheme, {loc} region_code, {loc} region_name,
           lower({status}) status, {inv_sql} invasive,
           NULL::BOOLEAN extinct, {year_sql} first_record_year
    FROM {rel}
    {where}"""


def griis(con):
    zips = sorted((DATA / "griis").glob("*.zip"))
    if not zips:
        log("skip GRIIS: no plant_data/griis/*.zip")
        return None
    parts = []
    for z in zips:
        out = z.with_suffix("")            # griis/griis_nl/
        if not out.exists():
            zipfile.ZipFile(z).extractall(out)
        files = {p.stem.lower(): p for p in out.rglob("*.txt")}
        if not {"taxon", "distribution"} <= files.keys():
            log(f"   {z.name}: no taxon/distribution file, found {list(files)}")
            continue
        prof = (f"LEFT JOIN {tsv(files['speciesprofile'])} p USING (id)"
                if "speciesprofile" in files else "")
        inv = "lower(p.isInvasive) IN ('invasive', 'true', 'yes')" if prof else "NULL::BOOLEAN"
        parts.append(f"""
        SELECT 'griis' source, NULL wfo_id, NULL wcvp_id, t.scientificName name,
               'iso2' region_scheme, d.countryCode region_code, d.locality region_name,
               lower(d.establishmentMeans) status, {inv} invasive,
               NULL::BOOLEAN extinct, NULL::INT first_record_year
        FROM {tsv(files['taxon'])} t
        JOIN {tsv(files['distribution'])} d USING (id)
        {prof}
        WHERE t.kingdom = 'Plantae'""")
    return " UNION ALL ".join(parts) if parts else None


def main():
    con = duckdb.connect(str(DB))
    run(con, f"SET threads = {THREADS}")
    run(con, f"SET memory_limit = '{MEMORY}'")
    if not con.execute("SELECT count(*) FROM information_schema.tables "
                       "WHERE table_name = 'plant_index'").fetchall()[0][0]:
        raise SystemExit(f"No plant_index in {DB} - run build_index.py first.")

    selects = []
    for name, fn in [("WCVP", wcvp), ("GloNAF", glonaf), ("SInAS", sinas), ("GRIIS", griis)]:
        log("source", name)
        sql = fn(con)
        if sql:
            selects.append(f"SELECT {COLS} FROM ({sql})")
    if not selects:
        raise SystemExit("No range sources found.")

    log("building ranges")
    run(con, f"""
    CREATE OR REPLACE TABLE ranges AS
    WITH raw AS ({' UNION ALL '.join(selects)}),
    by_wcvp AS (SELECT wcvp_id, any_value(wfo_id) wfo_id, any_value(name) name
                FROM plant_index WHERE wcvp_id IS NOT NULL GROUP BY 1),
    by_name AS (SELECT {n('name')} nname, any_value(wfo_id) wfo_id, any_value(wcvp_id) wcvp_id
                FROM plant_index GROUP BY 1 HAVING count(*) = 1)
    SELECT r.source,
           coalesce(w.wfo_id, b.wfo_id)            AS wfo_id,
           coalesce(r.wcvp_id, b.wcvp_id)          AS wcvp_id,
           coalesce(r.name, w.name)                AS name,
           r.region_scheme, r.region_code, r.region_name,
           r.status, r.invasive, r.extinct, r.first_record_year
    FROM raw r
    LEFT JOIN by_wcvp w ON w.wcvp_id = r.wcvp_id
    LEFT JOIN by_name b ON r.wcvp_id IS NULL AND b.nname = {n('r.name')}
    ORDER BY wfo_id, source""")
    run(con, f"COPY ranges TO '{DATA / 'ranges.parquet'}' (FORMAT parquet)")
    log(con.execute("""SELECT source, count(*) rows, count(wfo_id) linked_to_wfo,
                       count(DISTINCT coalesce(wfo_id, name)) taxa
                       FROM ranges GROUP BY 1 ORDER BY 1""").df().to_string(index=False))
    con.close()


if __name__ == "__main__":
    main()
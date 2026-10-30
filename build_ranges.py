"""
Build one long table of where each plant is native / introduced / invasive.

    python build_index.py    # first (creates plant_index + exports WCVP csvs)
    python build_ranges.py

Output: table `ranges` in plant_data/plant_index.duckdb and plant_data/ranges.parquet
One row per taxon x region x source:
    source, wfo_id, wcvp_id, name, src_name, link_src, region_scheme, region_code,
    region_name, status (native / introduced / naturalized / ...), invasive, extinct,
    first_record_year, pathway
Sources (each skipped with a message if its files are missing):
    WCVP distributions  plant_data/wcvp_distributions.csv  (native + introduced, TDWG level 3)
    GloNAF              plant_data/glonaf/                 (naturalized / invasive, GloNAF regions)
    SInAS               plant_data/sinas/SInAS_*.csv       (native + alien, first record year,
                                                           pathway; space-delimited, all kingdoms
                                                           so only rows matching a plant_index
                                                           name are kept)
    GRIIS               plant_data/griis/*.zip             (introduced/invasive per country; the
                                                           zip is unpacked once, crash-safely,
                                                           next to itself)
Linking, tried in this order (recorded per row in `link_src`): WCVP id; name+author when
that is unique in plant_index; name+author ignoring the parenthetical author (checklists
drop it, and WFO sometimes holds the same name twice); then name alone when the name alone
is unique. GRIIS names carry authorship ("Acer negundo L."), so `name` is cut to the rank's
binomial for display while `src_name` keeps the source string for matching (plant_index
keeps name and author apart). Homonyms are only ever linked by id or by name+author, never
by name alone.
Rebuilt on every run (seconds to minutes); the input files are never modified.
"""
import zipfile
from pathlib import Path

import duckdb

DATA = Path(__file__).resolve().parent / "plant_data"
DB = DATA / "plant_index.duckdb"
THREADS = 4
MEMORY = "8GB"
TMP = DATA / "duckdb_tmp"

NORM = "lower(trim(regexp_replace(replace({c}, '×', 'x '), '\\s+', ' ', 'g')))"
COLS = ("source, wfo_id, wcvp_id, name, src_name, region_scheme, region_code, region_name, "
        "status, invasive, extinct, first_record_year, pathway")


def run(con, sql, *params):
    """Execute and fully consume the result (newer DuckDB refuses further
    work on a connection with an unfinished result)."""
    return con.execute(sql, *params).fetchall()


def log(*a):
    print(*a, flush=True)


def n(c):
    return NORM.format(c=c)


def csv(path, **kw):
    """Standard CSV. Quote/escape are set explicitly: DuckDB's sniffer only looks at the
    first rows and misses quoted fields further down (e.g. GloNAF line 192702)."""
    base = {"header": "true", "all_varchar": "true", "delim": "','",
            "quote": "'\"'", "escape": "'\"'"}
    opts = ", ".join(f"{k}={v}" for k, v in {**base, **kw}.items())
    return f"read_csv('{path}', {opts})"


def tsv(path):
    return csv(path, delim="'\t'")


def spc(path):
    """SInAS: fields separated by a literal space, quoted with double quotes."""
    return csv(path, delim="' '")


def year(col):
    """First 4-digit year in a (often partial) date column."""
    return f"try_cast(regexp_extract({col}, '([0-9]{{4}})', 1) AS INT)"


def unparen(c):
    """Drop the parenthetical (recombined) author part: GRIIS writes 'Erigeron annuus
    Pers.' for what WFO records as 'Erigeron annuus (L.) Pers.'. One backslash only -
    DuckDB string literals do not process escapes, so '\\\\(' would look for a backslash."""
    return f"regexp_replace({c}, '\\([^)]*\\)', '', 'g')"


def pick(con, rel, *candidates, quiet=False):
    """First matching column name (case-insensitive) in a relation, or None.
    quiet=True for optional columns, so their absence is not worth a warning."""
    cols = [r[0] for r in con.execute(f"DESCRIBE SELECT * FROM {rel}").fetchall()]
    low = {c.lower(): c for c in cols}
    for c in candidates:
        if c.lower() in low:
            return f'"{low[c.lower()]}"'
    if not quiet:
        log(f"   none of {candidates} in {cols}")
    return None


# --- sources: each returns a SELECT producing COLS, or None ------------------
def wcvp(con):
    f = DATA / "wcvp_distributions.csv"
    if not f.exists():
        log("skip WCVP: no wcvp_distributions.csv (run build_index.py)")
        return None
    return f"""
    SELECT 'wcvp' source, NULL wfo_id, d.plant_name_id wcvp_id, NULL name, NULL src_name,
           'tdwg3' region_scheme, d.area_code_l3 region_code, d.area region_name,
           CASE WHEN d.introduced = '1' THEN 'introduced' ELSE 'native' END status,
           NULL::BOOLEAN invasive, d.extinct = '1' extinct, NULL::INT first_record_year,
           NULL::VARCHAR pathway
    FROM {csv(f)} d"""


def glonaf(con):
    g = DATA / "glonaf"
    need = ["glonaf_flora2.csv", "glonaf_taxon_wcvp.csv", "glonaf_region.csv"]
    if not all((g / x).exists() for x in need):
        log("skip GloNAF: need", need, "in", g)
        return None
    return f"""
    SELECT 'glonaf' source, NULL wfo_id, nullif(t.accepted_plant_name_id, 'NA') wcvp_id,
           nullif(t.taxa_accepted, 'NA') name, nullif(t.taxa_accepted, 'NA') src_name,
           'glonaf' region_scheme, r.code region_code, r.name region_name,
           lower(f.status) status, f.status = 'Invasive' invasive,
           NULL::BOOLEAN extinct, NULL::INT first_record_year, NULL::VARCHAR pathway
    FROM {csv(g / 'glonaf_flora2.csv')} f
    JOIN {csv(g / 'glonaf_taxon_wcvp.csv')} t ON t.id = f.taxon_wcvp_id
    JOIN {csv(g / 'glonaf_region.csv')} r ON r.id = f.region_id"""


def sinas(con):
    """SInAS: one row per taxon x location, every kingdom. The file is space-delimited,
    has no kingdom column, and names carry no authorship - so the plant filter is
    'the name is in plant_index', applied in main()."""
    files = sorted((DATA / "sinas").glob("SInAS_*.csv"))
    if not files:
        log("skip SInAS: no plant_data/sinas/SInAS_*.csv")
        return None
    parts = []
    for f in files:
        rel = spc(f)
        taxon = pick(con, rel, "taxon", "scientificName", "species")
        loc = pick(con, rel, "location")
        locid = pick(con, rel, "locationID", quiet=True)
        status = pick(con, rel, "establishmentMeans", "Status", "status")
        if not (taxon and loc and status):
            log(f"skip SInAS {f.name}: columns above not recognised")
            continue
        year_c = pick(con, rel, "eventDate", "FirstRecord", "first_record", quiet=True)
        degree = pick(con, rel, "degreeOfEstablishment", "invasionStatus", quiet=True)
        pathway = pick(con, rel, "pathway", "vector", quiet=True)
        inv = f"lower({degree}) LIKE '%invasive%'" if degree else "NULL::BOOLEAN"
        parts.append(f"""
        SELECT 'sinas' source, NULL wfo_id, NULL wcvp_id, {taxon} name, {taxon} src_name,
               'sinas' region_scheme, coalesce({locid}, {loc}) region_code, {loc} region_name,
               lower({status}) status, {inv} invasive,
               NULL::BOOLEAN extinct, {year(year_c) if year_c else 'NULL::INT'} first_record_year,
               {pathway if pathway else 'NULL::VARCHAR'} pathway
        FROM {rel}""")
        log(f"   {f.name}: taxon={taxon} location={loc} status={status} "
            f"year={year_c or '-'} pathway={pathway or '-'}")
    return " UNION ALL ".join(parts) if parts else None


def griis(con):
    """GRIIS Darwin Core Archives: taxon.txt (core) + distribution.txt + speciesprofile.txt.
    Unzipped once into <zip name>/ (via .part, so an interrupted unzip is never trusted)."""
    zips = sorted((DATA / "griis").glob("*.zip"))
    if not zips:
        log("skip GRIIS: no plant_data/griis/*.zip")
        return None
    parts = []
    for z in zips:
        out = z.with_suffix("")                       # griis/griis-nl/
        if not out.exists():
            tmp = out.with_name(out.name + ".part")
            if tmp.exists():
                import shutil
                shutil.rmtree(tmp)
            tmp.mkdir(parents=True)
            zipfile.ZipFile(z).extractall(tmp)
            tmp.rename(out)                            # only a complete unzip is visible
            log(f"   unzipped {z.name} -> {out.name}/")
        files = {p.stem.lower(): p for p in out.rglob("*.txt")}
        if not {"taxon", "distribution"} <= files.keys():
            log(f"   {z.name}: no taxon/distribution file, found {list(files)}")
            continue
        prof = (f"LEFT JOIN {tsv(files['speciesprofile'])} p USING (id)"
                if "speciesprofile" in files else "")
        inv = "lower(p.isInvasive) = 'invasive'" if prof else "NULL::BOOLEAN"
        dist = files["distribution"]
        yr = year('"eventDate"') if "eventDate" in [r[0] for r in con.execute(
            f"DESCRIBE SELECT * FROM {tsv(dist)}").fetchall()] else "NULL::INT"
        # GRIIS names carry authorship: keep only the words the rank needs, so
        # "Acer negundo L." matches plant_index.name = 'Acer negundo'.
        bare = ("CASE WHEN lower(t.taxonRank) = 'genus' THEN regexp_extract(t.scientificName, "
                r"'^(\S+)', 1) ELSE regexp_extract(t.scientificName, '^(\S+\s+\S+)', 1) END")
        parts.append(f"""
        SELECT 'griis' source, NULL wfo_id, NULL wcvp_id, {bare} name, t.scientificName src_name,
               'iso' region_scheme, d.countryCode region_code, d.locationID region_name,
               lower(d.establishmentMeans) status, {inv} invasive,
               NULL::BOOLEAN extinct, {yr} first_record_year,
               NULL::VARCHAR pathway
        FROM {tsv(files['taxon'])} t
        JOIN {tsv(dist)} d USING (id)
        {prof}
        WHERE t.kingdom = 'Plantae'""")
    return " UNION ALL ".join(parts) if parts else None


def main():
    con = duckdb.connect(str(DB))
    run(con, f"SET threads = {THREADS}")
    run(con, f"SET memory_limit = '{MEMORY}'")
    TMP.mkdir(exist_ok=True)
    run(con, f"SET temp_directory = '{TMP}'")
    run(con, "SET preserve_insertion_order = false")
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
    by_name_auth AS (SELECT {n("coalesce(name, '') || ' ' || coalesce(author, '')")} k,
                             any_value(wfo_id) wfo_id, any_value(wcvp_id) wcvp_id
                      FROM plant_index WHERE author IS NOT NULL
                      GROUP BY 1 HAVING count(*) = 1),
    -- same name and author, but ignoring the parenthetical part, and when the key is
    -- held by several WFO records (the same name written twice) prefer the accepted one
    by_name_bare AS (
        SELECT wfo_id, wcvp_id, k FROM (
            SELECT wfo_id, wcvp_id, {n(unparen("coalesce(name, '') || ' ' || coalesce(author, '')"))} k,
                   row_number() OVER (PARTITION BY
                       {n(unparen("coalesce(name, '') || ' ' || coalesce(author, '')"))}
                       ORDER BY CASE lower(status) WHEN 'accepted' THEN 0
                                                   WHEN 'unchecked' THEN 1 ELSE 2 END) rn
            FROM plant_index WHERE author IS NOT NULL) WHERE rn = 1),
    by_name AS (SELECT {n('name')} nname, any_value(wfo_id) wfo_id, any_value(wcvp_id) wcvp_id
                FROM plant_index GROUP BY 1 HAVING count(*) = 1)
    SELECT r.source,
           coalesce(w.wfo_id, a.wfo_id, z.wfo_id, b.wfo_id)          AS wfo_id,
           coalesce(r.wcvp_id, a.wcvp_id, z.wcvp_id, b.wcvp_id)      AS wcvp_id,
           coalesce(r.name, w.name)                                 AS name,
           r.src_name,
           CASE WHEN w.wfo_id  IS NOT NULL THEN 'wcvp_id'
                WHEN a.wfo_id  IS NOT NULL THEN 'name+author'
                WHEN z.wfo_id  IS NOT NULL THEN 'name+author(no parenthetical)'
                WHEN b.wfo_id  IS NOT NULL THEN 'name' END          AS link_src,
           r.region_scheme, r.region_code, r.region_name,
           r.status, r.invasive, r.extinct, r.first_record_year, r.pathway
    FROM raw r
    LEFT JOIN by_wcvp w ON w.wcvp_id = r.wcvp_id
    -- then name+author (exact, unique): resolves the homonymous names that the
    -- name-only guard below has to refuse (Quercus rubra L., Prunus serotina Ehrh., ...)
    LEFT JOIN by_name_auth a ON w.wfo_id IS NULL
         AND a.k = {n("coalesce(r.src_name, '')")}
    -- then name+author ignoring the parenthetical author (checklists drop it)
    LEFT JOIN by_name_bare z ON (w.wfo_id IS NULL AND a.wfo_id IS NULL)
         AND z.k = {n(unparen("coalesce(r.src_name, '')"))}
    -- then unique name alone, for rows without a WCVP id *or* whose id does not resolve
    -- here (GloNAF ships WCVP v12 ids); WCVP rows have NULL name, so they cannot match
    LEFT JOIN by_name b ON (w.wfo_id IS NULL AND a.wfo_id IS NULL AND z.wfo_id IS NULL)
         AND b.nname = {n('r.name')}
    -- SInAS covers every kingdom; keep only what is a plant name in this index
    WHERE NOT (r.source = 'sinas'
               AND coalesce(w.wfo_id, a.wfo_id, z.wfo_id, b.wfo_id) IS NULL)
    ORDER BY wfo_id, source""")
    run(con, f"COPY ranges TO '{DATA / 'ranges.parquet'}' (FORMAT parquet)")
    log(con.execute("""SELECT source, count(*) rows, count(wfo_id) linked_to_wfo,
                       count(DISTINCT coalesce(wfo_id, name)) taxa,
                       count(pathway) with_pathway
                       FROM ranges GROUP BY 1 ORDER BY 1""").df().to_string(index=False))
    log(con.execute("SELECT coalesce(link_src, 'unlinked') link_src, count(*) FROM ranges "
                    "GROUP BY 1 ORDER BY 2 DESC").df().to_string(index=False))
    con.close()


if __name__ == "__main__":
    main()

"""
Where in the Netherlands has each plant actually been recorded?

    python gbif_nl.py --admin 2         # first: the GBIF download (asks for a login)
    python build_index.py               # first (plant_index)
    python build_obs.py                 # this
    python build_obs.py --force         # rebuild after a newer GBIF download

Three derived tables plus a view, each of the tables also written as Parquet next to
the database. Nothing here modifies `plant_index` or the GBIF/GADM downloads.

  observations_nl  the fact table: one row per *plant x area* (GADM level 2, the 352
                   municipalities plus the IJsselmeer and two other water areas), with
                   how many GBIF records fall in that area and the first/last year seen.
                       key, gbif_key, gbif_name, wfo_id, name, author, rank, family,
                       link_src, area_id, area_name, province, in_nl,
                       occurrences, first_year, last_year
  obs_areas        one row per area: its GADM id, Dutch name, province, whether it is in
                   the Netherlands, its centroid (lat/lng) and bounding box, and how many
                   plants/records were counted there. The centroid + bbox is what makes a
                   map cheap: a choropleth joins these, no geometry needed.
  nl_totals        one row per plant: how many areas it was found in, total records,
                   first/last year, the bbox of its records (a quick map extent), and how
                   many records could not be placed in an area at all.
  observations_nl_full
                   the fact table joined to the area centroids/bboxes, for plotting.

Why aggregates rather than 33 million points. GBIF's SQL download API does the GROUP BY
on GBIF's side, so the whole Netherlands arrives as 538 thousand rows / 38 MB instead of
a few GB of coordinates (issue #7 asked for "a list (or map) of the relevant
observations"; this answers the list and the map at municipality level). The cost: no
per-record coordinates, so a coordinate + radius search inside a municipality is not
possible from this table, and #8's GADM level 3 (wards) is deliberately not built -
GBIF has no level-3 area for ~1 in 4 of NL's subdivisions that hold records, and the user
chose to keep the database small. GADM 4.1's GeoJSON distribution stops at level 2 for
the Netherlands anyway (gadm41_NLD_3.json does not exist).

The points GBIF puts *outside* the Netherlands are kept, flagged: `countrycode = 'NL'`
means the publisher said Netherlands, while GADM says where the coordinate actually
falls - 1,067,595 records land in Belgian Limburg or on the German border (border points
and GBIF's default centroid for poorly georeferenced herbarium slips), and 1,987,303
have no area at all because they were digitised without coordinates (coastal and dune
plants mostly: *Cakile maritima*, *Hippophae rhamnoides*). `in_nl` / `area_id IS NULL`
let a query decide whether to count them.

Linking to plant_index is by name, not by id: GBIF's SQL API returns an opaque
`specieskey` (like `4MDK3`) which has no relation to the GBIF backbone keys stored in
plant_index, measured: 0 of 12,467 match. So a normalised exact name match wins, and
second-best a match on the bare binomial of a subspecies/variety. 10,443 of 12,310 names
link (1,816 do not: mostly algae, which the WFO plant backbone does not hold, and notho-
hybrids like *Persicaria lapathifolia x maculosa*); `link_src` records which route each
link came by.
"""
import argparse
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "plant_data"
DB = DATA / "plant_index.duckdb"
GADM_DIR = DATA / "gadm"
GADM2 = GADM_DIR / "gadm41_NLD_2.json"
THREADS = 4
MEMORY = "8GB"
TMP = DATA / "duckdb_tmp"

NORM = "lower(trim(regexp_replace(replace({c}, '\u00d7', 'x '), '\\s+', ' ', 'g')))"
# "Salix x sepulcralis Simon E.G." -> "Salix x sepulcralis": infraspecific ranks and
# author citations dropped, so a name plant_index writes without them still matches
BARE = "array_to_string(list_slice(str_split({c}, ' '), 1, 2), ' ')"
# plant_index.status ranked, as in build_status.py: a homonym resolves to Accepted first
RSTATUS = "CASE lower({c}) WHEN 'accepted' THEN 3 WHEN 'unchecked' THEN 2 " \
          "WHEN 'synonym' THEN 1 ELSE 0 END"


def run(con, sql, *params):
    """Execute and fully consume the result (newer DuckDB refuses further
    work on a connection with an unfinished result)."""
    return con.execute(sql, *params).fetchall()


def log(*a):
    print(*a, flush=True)


def download_csv():
    """The newest GBIF species x municipality TSV the gbif_nl.py download left behind."""
    hits = sorted(DATA.glob("gbif_nl/*.csv"), key=lambda p: p.stat().st_size, reverse=True)
    return hits[0] if hits else None


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--force", action="store_true",
                    help="rebuild even if observations_nl already exists")
    force = ap.parse_args().force
    csv = download_csv()
    if csv is None:
        raise SystemExit("No GBIF download in plant_data/gbif_nl/ - run gbif_nl.py first "
                         "(it asks for a GBIF login and does the GROUP BY on GBIF's side).")
    if not GADM2.exists():
        raise SystemExit(f"Missing {GADM2} - GADM 4.1 Netherlands level 2 boundaries "
                         "(https://geodata.ucdavis.edu/gadm/gadm4.1/json/gadm41_NLD_2.json)")
    con = duckdb.connect(str(DB))
    run(con, f"SET threads = {THREADS}")
    run(con, f"SET memory_limit = '{MEMORY}'")
    TMP.mkdir(exist_ok=True)
    run(con, f"SET temp_directory = '{TMP}'")
    run(con, "SET preserve_insertion_order = false")
    if not run(con, "SELECT count(*) FROM information_schema.tables WHERE table_name = "
                    "'plant_index'")[0][0]:
        raise SystemExit(f"No plant_index in {DB} - run build_index.py first")
    try:
        run(con, "INSTALL spatial")
    except Exception:
        pass                                  # offline: an existing build is enough
    try:
        run(con, "LOAD spatial")
    except Exception as e:
        raise SystemExit("build_obs.py needs DuckDB's spatial extension to read the GADM "
                         f"GeoJSON (and it needs the centroids for mapping):\n{e}")

    if run(con, "SELECT count(*) FROM information_schema.tables WHERE table_name = "
                "'observations_nl'")[0][0] and not force:
        log("observations_nl already built - skipping (rerun with --force to rebuild "
            "from a newer GBIF download)")
        con.close()
        return

    # ------------------------------------------------------------- the GBIF download
    log(f"reading {csv.relative_to(ROOT)}")
    run(con, f"""CREATE OR REPLACE TEMP TABLE gb AS
                SELECT cast(specieskey AS VARCHAR) gbif_key, species, family,
                       level2gid area_id, level2name area_name,
                       cast(occurrences AS BIGINT) occurrences,
                       cast(first_year AS INTEGER) first_year,
                       cast(last_year AS INTEGER) last_year
                FROM read_csv('{csv}', header=true, sep='\t')""")
    rows, taxa, areas, occ = run(con, """SELECT count(*), count(DISTINCT species),
        count(DISTINCT area_id), sum(occurrences) FROM gb""")[0]
    log(f"  {rows:,} rows: {taxa:,} GBIF names x {areas:,} areas, {occ:,} records")

    # ---------------------------------------------------------------- the GADM areas
    log("reading GADM 4.1 Netherlands level 2 (municipalties)")
    run(con, f"""CREATE OR REPLACE TEMP TABLE gadm AS
                SELECT GID_2 area_id, NAME_2 area_name, NAME_1 province, TYPE_2 area_type,
                       HASC_2 hasc, ST_X(ST_Centroid(geom)) lng, ST_Y(ST_Centroid(geom)) lat,
                       ST_XMin(geom) xmin, ST_XMax(geom) xmax, ST_YMin(geom) ymin,
                       ST_YMax(geom) ymax
                FROM st_read('{GADM2}')""")
    log(f"  {run(con, 'SELECT count(*) FROM gadm')[0][0]:,} areas "
        f"({run(con, 'SELECT count(DISTINCT province) FROM gadm')[0][0]:,} provinces)")

    # one row per area, whichever side it came from: GBIF knows the names of Belgian and
    # German areas too, GADM only the Dutch ones (and has the centroid/bbox)
    log("building obs_areas")
    run(con, """CREATE OR REPLACE TABLE obs_areas AS
                WITH seen AS (SELECT area_id, arg_max(area_name, (occurrences, area_name))
                                    gbif_name,
                          count(*) n_names, sum(occurrences)::BIGINT occurrences
                          FROM gb WHERE area_id IS NOT NULL GROUP BY 1)
                SELECT s.area_id id, coalesce(g.area_name, s.gbif_name) name,
                       g.province, coalesce(g.area_type, 'area outside GADM/Netherlands') type,
                       g.hasc, g.lat, g.lng, g.xmin, g.xmax, g.ymin, g.ymax,
                       (g.area_id IS NOT NULL) in_nl, s.n_names, s.occurrences
                FROM seen s LEFT JOIN gadm g USING (area_id)
                ORDER BY 1""")
    run(con, "CREATE INDEX obs_areas_id ON obs_areas (id)")
    log(f"  {run(con, 'SELECT count(*) FROM obs_areas')[0][0]:,} areas, "
        f"{run(con, 'SELECT count(*) FILTER (WHERE in_nl) FROM obs_areas')[0][0]:,} in the "
        f"Netherlands, {run(con, 'SELECT count(*) FILTER (WHERE NOT in_nl) FROM obs_areas')[0][0]:,} "
        "where GBIF placed a 'Netherlands' record outside the country")

    # ---------------------------------------------------------------- link to the index
    log("linking GBIF names to plant_index")
    run(con, f"""CREATE OR REPLACE TEMP TABLE pin AS
                SELECT {NORM.format(c='name')} n,
                       arg_max(coalesce(accepted_wfo_id, wfo_id), {RSTATUS.format(c='status')}) wfo_id,
                       count(*) n_index_rows
                FROM plant_index WHERE coalesce(accepted_wfo_id, wfo_id) IS NOT NULL
                GROUP BY 1""")
    # candidate identity per name, from plant_index through the accepted WFO record, and
    # failing that from the GBIF download's own name/family
    run(con, f"""CREATE OR REPLACE TEMP TABLE ident AS
                WITH g AS (SELECT gbif_name, any_value(family) family,
                                  any_value({BARE.format(c='gbif_name')}) bare
                           FROM (SELECT {NORM.format(c='species')} gbif_name, species, family
                                 FROM gb GROUP BY 1, 2, 3) GROUP BY 1),
                c AS (SELECT g.gbif_name, g.family,
                             coalesce(pn.wfo_id, pb.wfo_id) wfo_id,
                             CASE WHEN pn.n IS NOT NULL THEN 'name'
                                  WHEN pb.n IS NOT NULL THEN 'accepted_name' END link_src,
                             row_number() OVER (PARTITION BY g.gbif_name ORDER BY
                               (pn.n IS NULL), (pn.n_index_rows > 1), g.gbif_name) rn
                      FROM g
                      LEFT JOIN pin pn ON pn.n = g.gbif_name
                      -- a subspecies/variety name plant_index simply does not have: fall
                      -- back to the binomial, but only when that binomial is not a homonym
                      LEFT JOIN pin pb ON pb.n = g.bare AND pb.n_index_rows = 1),
                best AS (SELECT * FROM c WHERE rn = 1),
                -- display fields come from the record the name resolved to, not from the
                -- download's spelling: GBIF already normalises its species names, so
                -- 'quercus robur' in the download resolves to the accepted WFO record
                -- wfo-0000292858 (GBIF had sunk several Quercus names into it)
                disp AS (SELECT b.gbif_name,
                               coalesce(pi.name, b.gbif_name) name,
                               pi.author, pi.rank, coalesce(pi.family, b.family) family
                        FROM best b LEFT JOIN plant_index pi ON pi.wfo_id = b.wfo_id)
                SELECT b.gbif_name nname, b.wfo_id, b.link_src, d.name, d.author, d.rank,
                       d.family
                FROM best b JOIN disp d USING (gbif_name)""")
    run(con, "CREATE INDEX ident_name ON ident (nname)")
    for src, n in run(con, "SELECT coalesce(link_src, 'none'), count(*) FROM ident "
                           "GROUP BY 1 ORDER BY 2 DESC"):
        log(f"  {src}: {n:,} names")

    # ------------------------------------------------------------------- observations_nl
    log("building observations_nl (one row per plant x area)")
    run(con, f"""CREATE OR REPLACE TABLE observations_nl AS
                WITH g AS (SELECT gbif_key, {NORM.format(c='species')} nname, area_id,
                                    sum(occurrences) occurrences, min(first_year) first_year,
                                    max(last_year) last_year
                             FROM gb GROUP BY 1, 2, 3)
                -- grouped by (key, area), not by the download's name: GBIF has already
                -- normalised its species names, so 'quercus robur' and 'quercus hispanica'
                -- in the download are records of one accepted WFO record and must share
                -- one municipality row instead of sitting in two
                SELECT coalesce(i.wfo_id, g.nname) key,
                       -- a name for the row, chosen deterministically: the GBIF spelling
                       -- that holds most records, ties broken alphabetically
                       arg_max(g.nname, (g.occurrences, g.nname)) gbif_name,
                       any_value(g.gbif_key) gbif_key, i.wfo_id, max(i.name) name,
                       max(i.author) author, max(i.rank) rank, max(i.family) family,
                       arg_max(i.link_src, (g.occurrences, g.nname)) link_src,
                       g.area_id, max(a.name) area_name, max(a.province) province,
                       max(a.in_nl) in_nl, sum(g.occurrences)::BIGINT occurrences,
                       min(g.first_year) first_year, max(g.last_year) last_year
                FROM g
                JOIN ident i USING (nname)
                LEFT JOIN obs_areas a ON a.id = g.area_id
                GROUP BY coalesce(i.wfo_id, g.nname), i.wfo_id, g.area_id
                ORDER BY occurrences DESC""")
    run(con, "CREATE INDEX obs_key ON observations_nl (key)")
    run(con, "CREATE INDEX obs_area ON observations_nl (area_id)")

    log("building nl_totals (one row per plant)")
    run(con, f"""CREATE OR REPLACE TABLE nl_totals AS
                SELECT key, any_value(gbif_name) gbif_name, max(wfo_id) wfo_id,
                       max(o.name) name, max(o.author) author, max(o.rank) rank,
                       max(o.family) family, max(link_src) link_src,
                       count(DISTINCT o.area_id) n_areas,
                       count(DISTINCT CASE WHEN o.in_nl THEN o.area_id END) n_areas_nl,
                       sum(CASE WHEN o.in_nl THEN o.occurrences END)::BIGINT occurrences_nl,
                       sum(o.occurrences)::BIGINT occurrences_all,
                       sum(CASE WHEN o.area_id IS NULL THEN o.occurrences END)::BIGINT
                         occurrences_no_area,
                       sum(CASE WHEN NOT o.in_nl THEN o.occurrences END)::BIGINT
                         occurrences_outside_nl,
                       min(o.first_year) first_year, max(o.last_year) last_year,
                       min(a.lat) lat_min, max(a.lat) lat_max,
                       min(a.lng) lng_min, max(a.lng) lng_max
                FROM observations_nl o LEFT JOIN obs_areas a ON a.id = o.area_id
                GROUP BY key""")
    run(con, "CREATE INDEX totals_key ON nl_totals (key)")

    run(con, """CREATE OR REPLACE VIEW observations_nl_full AS
                SELECT o.*, a.type, a.hasc, a.lat, a.lng,
                       a.xmin, a.xmax, a.ymin, a.ymax
                FROM observations_nl o LEFT JOIN obs_areas a ON a.id = o.area_id""")

    for t in ("observations_nl", "obs_areas", "nl_totals"):
        run(con, f"COPY {t} TO '{DATA / (t + '.parquet')}' (FORMAT parquet)")

    log(con.execute("""SELECT count(*) rows, count(DISTINCT key) taxa,
        count(*) FILTER (WHERE wfo_id IS NOT NULL) linked_to_index,
        count(*) FILTER (WHERE area_id IS NULL) unplaced_rows,
        sum(CASE WHEN in_nl THEN occurrences END)::BIGINT records_in_netherlands,
        sum(CASE WHEN NOT in_nl THEN occurrences END)::BIGINT records_outside,
        sum(CASE WHEN area_id IS NULL THEN occurrences END)::BIGINT records_no_coordinates,
        min(first_year) first_record, max(last_year) last_record
        FROM observations_nl""").df().to_string(index=False))
    log(con.execute("""SELECT link_src, count(DISTINCT gbif_name) names,
        sum(occurrences)::BIGINT records
        FROM observations_nl GROUP BY 1 ORDER BY 2 DESC""").df().to_string(index=False))
    con.close()


if __name__ == "__main__":
    main()

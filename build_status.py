"""
Where is each plant from, and what is it doing in the Netherlands?

    python build_index.py     # first (plant_index)
    python build_ranges.py    # first (ranges)
    python build_status.py    # this

Three derived tables, each also written as Parquet next to the database. `ranges` and
the raw sources are never modified.

  taxon_ranges  one row per taxon: how much of the world it is native to and where it
                has been introduced, so origin("Quercus robur") answers without
                rescanning 2.0M WCVP distribution rows.
                    wfo_id, name, author, rank, family, n_native_regions,
                    native_regions (TDWG region names, up to 12 of them), native_groups
                    (TDWG level-2 region names, for wider ranges),
                    n_introduced_regions, n_regions_total, sources
                The native range comes from WCVP only: it is the one source with global
                TDWG level-3 coverage, while GloNAF covers the naturalised alien flora
                and SInAS numbers its own region list (its '8' is Curaçao, not a TDWG
                code), so mixing them into one region list would be meaningless.

  tdwg_regions  the region gazetteer: TDWG level-3 code -> name, its level-2 region and
                its continent. Free, because WCVP's distribution table carries all three
                levels; it is what makes a wide native range readable rather than a
                string of two-letter prefixes.

  nl_status     one row per taxon seen in the Netherlands, every source's verdict kept
                side by side plus a resolved one-word answer:
                    wfo_id, key, name, author, rank, status, status_source, conflict,
                    wcvp_status, glonaf_status, sinas_status, griis_status,
                    n_sources, n_sources_native, n_sources_alien,
                    invasive, extinct, arrival_year, arrival_source

Why the four sources need a rule. WCVP answers phytogeography at TDWG level 3 and covers
the most taxa (3,548 in the Netherlands, 2,848 of them called native); GloNAF records only
the *naturalised* alien flora (anything it lists is alien by construction, 433 taxa); SInAS
gives native and alien per country (2,323 taxa, and the only source with arrival years);
GRIIS-NL is the Dutch alien checklist (235 taxa, all species rank). They disagree for 88
taxa - typically GloNAF or SInAS calling a species alien while WCVP still calls it native
to the region. So every verdict stays visible and the headline answer is resolved by
authority, not by counting votes:

    GRIIS (the Dutch checklist)  >  SInAS  >  GloNAF  >  WCVP (coarsest)

with one override: any source saying *invasive* makes the answer invasive, because
"not invasive" is only the absence of a record. `conflict` marks every taxon where some
source says native and another says alien, so the disagreements stay queryable instead of
being quietly averaged away.

Taxon identity: `ranges.wfo_id` when the row linked to one, otherwise the source's name
string - so the same unlinked name in two sources stays one taxon instead of two. WCVP
rows carry no name of their own (they are keyed by plant_name_id), so their name comes
from plant_index through the wfo_id. A name that is homonymous in plant_index is resolved
to the **Accepted** record when exactly one candidate is accepted (plant_index holds both
`Quercus robur L.` and an unrelated `Quercus robur` Asso synonym); with no accepted
candidate the wfo_id stays NULL and the row is only ever reachable by name. Then every id
is canonicalised to its accepted record (`accepted_wfo_id`), because a source can link to
a WFO synonym - GRIIS puts *Crassula helmsii* on wfo-0001300375, a synonym of
wfo-0000625092 that GloNAF and SInAS use - and one plant must be one row.

Region filters read seed/nl_regions.csv, never a fuzzy match: 'Netherlands' as a string
also matches Netherlands Antilles (1,068 WCVP rows), and the code 'ANT' means Netherlands
Antilles in GloNAF but Antarctica in TDWG3.

Rerunnable: everything is rebuilt from `ranges` in about three seconds, so there is
nothing to skip - delete the tables and run it again if you change the seed file.
"""
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "plant_data"
DB = DATA / "plant_index.duckdb"
REGIONS = ROOT / "seed" / "nl_regions.csv"
THREADS = 4
MEMORY = "8GB"
TMP = DATA / "duckdb_tmp"

NORM = "lower(trim(regexp_replace(replace({c}, '\u00d7', 'x '), '\\s+', ' ', 'g')))"

# the sources' own status strings, mapped onto two buckets
NATIVE = "lower(status) = 'native'"
ALIEN = "(lower(status) LIKE 'introduced%' OR lower(status) = 'naturalized' " \
        "OR lower(status) = 'invasive' OR lower(status) LIKE 'cryptogenic%')"
# who gets the last word about the Netherlands (see the docstring)
PRIORITY = "CASE source WHEN 'griis' THEN 1 WHEN 'sinas' THEN 2 WHEN 'glonaf' THEN 3 " \
           "ELSE 4 END"
# plant_index.status, ranked: a homonymous name resolves to its Accepted record first
RSTATUS = "CASE lower({c}) WHEN 'accepted' THEN 3 WHEN 'unchecked' THEN 2 " \
          "WHEN 'synonym' THEN 1 ELSE 0 END"


def run(con, sql, *params):
    """Execute and fully consume the result (newer DuckDB refuses further
    work on a connection with an unfinished result)."""
    return con.execute(sql, *params).fetchall()


def log(*a):
    print(*a, flush=True)


TAXA = f"""
    WITH t AS (SELECT DISTINCT CASE WHEN wfo_id IS NOT NULL THEN wfo_id
                                    ELSE {NORM.format(c='name')} END k, wfo_id, name
               FROM ranges
               -- WCVP rows that linked to no WFO id have no name either (they are keyed by
               -- plant_name_id), so they cannot become a taxon here
               WHERE wfo_id IS NOT NULL OR name IS NOT NULL),
    cand AS (
        SELECT t.k, COALESCE(t.wfo_id, pi.wfo_id, pid.wfo_id) wfo_id,
               COALESCE(pi.name, pid.name, t.name) name,
               COALESCE(pi.author, pid.author) author,
               COALESCE(pi.rank, pid.rank) rank,
               COALESCE(pi.family, pid.family) family,
               COALESCE({RSTATUS.format(c='pi.status')}, pid.rs, 0) rstatus
        FROM t
        -- wfo_id is the reliable route (WCVP rows have no name column of their own)
        LEFT JOIN plant_index pi ON {NORM.format(c='pi.name')} = {NORM.format(c='t.name')}
        LEFT JOIN (SELECT wfo_id, arg_max(name, {RSTATUS.format(c='status')}) name,
                   arg_max(author, {RSTATUS.format(c='status')}) author,
                   arg_max(rank, {RSTATUS.format(c='status')}) rank,
                   arg_max(family, {RSTATUS.format(c='status')}) family,
                   max({RSTATUS.format(c='status')}) rs
                   FROM plant_index WHERE wfo_id IS NOT NULL GROUP BY 1) pid ON pid.wfo_id = t.k),
    -- one row per taxon. A homonymous name matches several plant_index rows: keep the
    -- id only when all candidates share one id or exactly one of them is Accepted, and
    -- take the display name/author from that same best candidate (never mix columns
    -- from different candidates).
    best AS (
        SELECT k, wfo_id, name, author, rank, family,
               count(DISTINCT wfo_id) OVER (PARTITION BY k) n_ids,
               sum(CASE WHEN rstatus = 3 THEN 1 ELSE 0 END) OVER (PARTITION BY k) n_accepted,
               row_number() OVER (PARTITION BY k ORDER BY rstatus DESC, name, author) rn
        FROM cand)
    SELECT k, CASE WHEN n_ids = 1 OR n_accepted = 1 THEN wfo_id END wfo_id,
           name, author, rank, family
    FROM best WHERE rn = 1
"""


def main():
    if not REGIONS.exists():
        raise SystemExit(f"Missing {REGIONS} - the region-code crosswalk")
    con = duckdb.connect(str(DB))
    run(con, f"SET threads = {THREADS}")
    run(con, f"SET memory_limit = '{MEMORY}'")
    TMP.mkdir(exist_ok=True)
    run(con, f"SET temp_directory = '{TMP}'")
    run(con, "SET preserve_insertion_order = false")
    for need in ("plant_index", "ranges", "wcvp_distributions"):
        if not run(con, "SELECT count(*) FROM information_schema.tables WHERE table_name = ?",
                   [need])[0][0]:
            raise SystemExit(f"No {need} in {DB} - run build_index.py and build_ranges.py first")

    # the Netherlands is a set of codes, one per scheme, and that list is data
    run(con, f"""CREATE OR REPLACE TEMP TABLE nl_regions AS
                SELECT region_scheme, code
                FROM read_csv('{REGIONS}', header=true, all_varchar=true)
                WHERE lower(include) = 'yes'""")
    log("NL region codes: " + ", ".join(f"{s}={c}" for s, c in
                                        run(con, "SELECT region_scheme, code FROM nl_regions")))

    log("building taxa (one identity per taxon mentioned anywhere in ranges)")
    run(con, f"CREATE OR REPLACE TEMP TABLE taxa AS {TAXA}")
    log(f"  {run(con, 'SELECT count(*) FROM taxa')[0][0]:,} taxa, "
        f"{run(con, 'SELECT count(wfo_id) FROM taxa')[0][0]:,} with a WFO id")
    # One plant, one row. A species can reach us under two spellings: one source's row
    # linked to a WFO id, another only matched by name; and a source can link to a WFO
    # *synonym* record instead of the accepted one (GRIIS links Crassula helmsii to
    # wfo-0001300375, a synonym of wfo-0000625092). So the canonical key is the accepted
    # WFO id when there is one, otherwise the resolved id, otherwise the name.
    run(con, """CREATE OR REPLACE TEMP TABLE acc AS
               SELECT wfo_id, coalesce(accepted_wfo_id, wfo_id) canonical
               FROM plant_index WHERE wfo_id IS NOT NULL""")
    run(con, """CREATE OR REPLACE TEMP TABLE xref AS
               SELECT t.k, coalesce(a.canonical, t.wfo_id, t.k) k2
               FROM taxa t LEFT JOIN acc a ON a.wfo_id = t.wfo_id""")
    run(con, "CREATE INDEX xref_k ON xref (k)")
    merged = run(con, "SELECT count(*) FROM (SELECT k2 FROM xref GROUP BY 1 HAVING count(*) > 1)")[0][0]
    log(f"  {merged:,} canonical taxa absorb more than one spelling (id / synonym id / name)")
    # display fields for the canonical key: the accepted WFO record wins when the key is
    # an id, so the name and author shown are the ones plant_index calls Accepted
    run(con, """CREATE OR REPLACE TEMP TABLE ident AS
               WITH c AS (SELECT x.k2, t.name, t.author, t.rank, t.family,
                                row_number() OVER (PARTITION BY x.k2
                                    ORDER BY (t.wfo_id IS NULL), t.name) rn
                         FROM taxa t JOIN xref x ON x.k = t.k)
               SELECT c.k2, CASE WHEN c.k2 LIKE 'wfo-%' THEN c.k2 END wfo_id,
                      coalesce(pi.name, c.name) name, coalesce(pi.author, c.author) author,
                      coalesce(pi.rank, c.rank) rank, coalesce(pi.family, c.family) family
               FROM c LEFT JOIN plant_index pi ON pi.wfo_id = c.k2 WHERE c.rn = 1""")

    # ------------------------------------------------------------ TDWG region gazetteer
    # WCVP's own distribution table carries the three TDWG levels together, so the
    # code -> name lookup is free. Without it a wide native range reads as "CA E N EUP
    # NAS" instead of "Caucasia, Northern Europe, temperate Europe".
    log("building tdwg_regions (region code -> name, from the WCVP distributions table)")
    run(con, """CREATE OR REPLACE TABLE tdwg_regions AS
               SELECT area_code_l3 code, any_value(area) name,
                      any_value(region_code_l2) l2_code, any_value(region) l2_name,
                      any_value(continent_code_l1) l1_code, any_value(continent) l1_name
               FROM wcvp_distributions WHERE area_code_l3 IS NOT NULL
               GROUP BY 1""")
    odd = run(con, """SELECT count(*) FROM (SELECT area_code_l3 FROM wcvp_distributions
                     WHERE area_code_l3 IS NOT NULL GROUP BY 1
                     HAVING count(DISTINCT region_code_l2) > 1)""")[0][0]
    log(f"  {run(con, 'SELECT count(*) FROM tdwg_regions')[0][0]:,} level-3 regions, "
        f"{run(con, 'SELECT count(DISTINCT l2_code) FROM tdwg_regions')[0][0]:,} level-2 "
        f"groups, {odd} level-3 codes in more than one level-2 group")

    # ---------------------------------------------------------------- taxon_ranges
    log("building taxon_ranges (native / introduced range per taxon)")
    run(con, f"""
    CREATE OR REPLACE TABLE taxon_ranges AS
    WITH src AS (SELECT CASE WHEN wfo_id IS NOT NULL THEN wfo_id
                             ELSE {NORM.format(c='name')} END k,
                        source, region_code, region_name, status
                 FROM ranges WHERE wfo_id IS NOT NULL OR name IS NOT NULL),
    r AS (SELECT x.k2 k, src.source, src.region_code, src.region_name,
               coalesce(g.l2_name, src.region_name) group_name,
               src.status FROM src JOIN xref x ON x.k = src.k
               LEFT JOIN tdwg_regions g ON g.code = src.region_code),
    per_taxon AS (
        -- All three counts are TDWG regions from WCVP, the only source with global
        -- level-3 coverage. GloNAF and SInAS use their own region lists (SInAS numbers
        -- them: its '8' is Curaçao), so their codes would be counted as extra "regions"
        -- for places WCVP already names. They still show up in `sources`, and they are
        -- the authority for nl_status, where the region is fixed to the Netherlands.
        -- DuckDB has no FILTER on the aggregate argument, so conditions live in the CASE.
        SELECT k,
               count(DISTINCT CASE WHEN source = 'wcvp' AND {NATIVE} THEN region_code END)
                 n_native_regions,
               count(DISTINCT CASE WHEN source = 'wcvp' AND {ALIEN} THEN region_code END)
                 n_introduced_regions,
               count(DISTINCT CASE WHEN source = 'wcvp' THEN region_code END) n_regions,
               string_agg(DISTINCT CASE WHEN source = 'wcvp' AND {NATIVE} THEN region_name END,
                          ', ') native_region_names,
               string_agg(DISTINCT CASE WHEN source = 'wcvp' AND {NATIVE} THEN group_name END,
                          ', ') native_group_names,
               string_agg(DISTINCT source, ', ') sources
        FROM r GROUP BY k)
    SELECT x.wfo_id, x.name, x.author, x.rank, x.family,
           r.n_native_regions,
           -- a list of region names reads well up to about a dozen; past that the same
           -- range is summarised by its TDWG level-2 regions, which is what a person
           -- scanning "where is this from" wants to see
           CASE WHEN r.n_native_regions <= 12 THEN r.native_region_names END native_regions,
           CASE WHEN r.n_native_regions > 12 THEN r.native_group_names END native_groups,
           r.n_introduced_regions, r.n_regions n_regions_total, r.sources
    FROM per_taxon r JOIN ident x ON x.k2 = r.k""")

    # ------------------------------------------------------------------- nl_status
    log("building nl_status (one row per taxon recorded in the Netherlands)")
    run(con, f"""
    CREATE OR REPLACE TABLE nl_status AS
    WITH raw AS (                     -- the Netherlands by code + scheme, never by name
        SELECT CASE WHEN wfo_id IS NOT NULL THEN wfo_id
                    ELSE {NORM.format(c='name')} END k,
               source, status, invasive, extinct, first_record_year
        FROM ranges r
        WHERE (wfo_id IS NOT NULL OR name IS NOT NULL)
          -- pair, not two separate INs: '8' is a SInAS locationID for Curaçao and could
          -- otherwise match a code in some other scheme
          AND EXISTS (SELECT 1 FROM nl_regions g
                      WHERE g.region_scheme = r.region_scheme AND g.code = r.region_code)),
    nl AS (SELECT x.k2 k, raw.source, raw.status, raw.invasive, raw.extinct,
                  raw.first_record_year FROM raw JOIN xref x ON x.k = raw.k),
    per_source AS (                  -- one verdict per taxon x source
        SELECT k, source,
               string_agg(DISTINCT lower(status), '; ') statuses,
               bool_or({NATIVE}) is_native, bool_or({ALIEN}) is_alien,
               bool_or(invasive) is_invasive, bool_or(extinct) is_extinct,
               min(first_record_year) first_year
        FROM nl GROUP BY k, source),
    each AS (                        -- pivot: one column per source, no self-joins
        SELECT k,
               max(CASE WHEN source = 'wcvp' THEN statuses END)   wcvp_status,
               max(CASE WHEN source = 'glonaf' THEN statuses END) glonaf_status,
               max(CASE WHEN source = 'sinas' THEN statuses END)  sinas_status,
               max(CASE WHEN source = 'griis' THEN statuses END)  griis_status,
               arg_min(statuses, {PRIORITY}) status,
               arg_min(source, {PRIORITY}) status_source,
               count(*) n_sources,
               sum(CASE WHEN is_native THEN 1 ELSE 0 END) n_sources_native,
               sum(CASE WHEN is_alien THEN 1 ELSE 0 END) n_sources_alien,
               bool_or(is_native) any_native, bool_or(is_alien) any_alien,
               bool_and(is_native) all_native,
               bool_or(is_invasive) invasive, bool_or(is_extinct) extinct,
               min(first_year) arrival_year,
               arg_min(source, first_year) arrival_source
        FROM per_source GROUP BY k)
    SELECT x.wfo_id, e.k key, x.name, x.author, x.rank,
           -- 'invasive' from any source outranks the hierarchy: not-invasive is only the
           -- absence of a record, invasive is a positive one
           CASE WHEN e.invasive THEN 'invasive' ELSE e.status END status,
           CASE WHEN e.invasive THEN 'invasive' ELSE e.status_source END status_source,
           e.any_native AND e.any_alien conflict,
           e.wcvp_status, e.glonaf_status, e.sinas_status, e.griis_status,
           e.n_sources, e.n_sources_native, e.n_sources_alien,
           e.invasive, e.extinct, e.arrival_year, e.arrival_source, e.all_native
    FROM each e JOIN ident x ON x.k2 = e.k""")

    for t in ("taxon_ranges", "nl_status", "tdwg_regions"):
        run(con, f"COPY {t} TO '{DATA / (t + '.parquet')}' (FORMAT parquet)")

    log(con.execute("""SELECT status, count(*) taxa, count(*) FILTER (WHERE conflict) conflicting,
                       count(arrival_year) with_arrival_year
                       FROM nl_status GROUP BY 1 ORDER BY 2 DESC""").df().to_string(index=False))
    log(con.execute("""SELECT count(*) taxa, count(*) FILTER (WHERE n_native_regions > 0) with_native,
                       count(*) FILTER (WHERE n_introduced_regions > 0) with_introduced,
                       max(n_native_regions) widest_native_range,
                       count(*) FILTER (WHERE native_regions IS NOT NULL) shown_as_names,
                       count(*) FILTER (WHERE native_groups IS NOT NULL) shown_as_groups
                       FROM taxon_ranges""").df().to_string(index=False))
    con.close()


if __name__ == "__main__":
    main()

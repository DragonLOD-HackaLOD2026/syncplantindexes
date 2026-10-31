"""
Quality report: what is actually in the database, what joined well, what is broken.

    python status.py

Read-only: opens plant_data/plant_index.duckdb with read_only=True and never writes
to it (run it any time, but not while another process is building). Writes
plant_data/quality_report.md and prints the same scorecard.

Four verdicts:
    ok      check passed
    warn    known gap, fixable later - each one prints the command that fixes it
    fail    wrong or regressed - the index is not trustworthy
    parked  decided against (issue #2), reported so it stops looking like a bug

Exit code is 1 if anything FAILed.
"""
import csv
import sys
from datetime import datetime
from pathlib import Path

import duckdb

DATA = Path(__file__).resolve().parent / "plant_data"
DB = DATA / "plant_index.duckdb"
REPORT = DATA / "quality_report.md"

NORM = ("lower(trim(regexp_replace(replace({c}, '×', 'x '), '\\s+', ' ', 'g')))")

# --- what we expect. Measured on 2026-10-30: floor = regression alarm, ---------
#     expected = what a good run gives, so noticeably less is a warning. --------
COVERAGE = [
    # label,          column,        SQL expression,                         floor, expected, meaning
    ("wcvp_id",       "wcvp_id",     "count(wcvp_id)",                       0.70, 0.74, "linked to WCVP (and so POWO)"),
    ("ipni_id",       "ipni_id",     "count(ipni_id)",                       0.75, 0.81, "IPNI LSID taken from the WFO record"),
    ("gbif_id",       "gbif_id",     "count(gbif_id)",                       0.65, 0.70, "GBIF backbone key"),
    ("ncbi_taxid",    "ncbi_taxid",  "count(ncbi_taxid)",                    0.25, 0.32, "NCBI taxid (species mainly; synonyms miss)"),
    ("in_megatree",   "in_megatree", "sum(CASE WHEN in_megatree THEN 1 ELSE 0 END)", 0.03, 0.04, "name is a tip of the GBOTB megatree"),
]
# wikidata_qid is deliberately absent: parked (issue #2), reported under Parked.

# files: label, candidate paths (any one satisfies), level, how to fix if missing
#   required = the index cannot be built or rebuilt without it
#   wanted   = a derived table is missing without it
#   later    = belongs to an open issue, expected to be absent today
FILES = [
    ("WFO backbone (cleaned)", ["wfo_clean.tsv"], "required", "run build_index.py"),
    ("WFO backbone (raw)", ["classification.csv"], "wanted",
     "Zenodo 7460932 -> classification.csv in plant_data/"),
    ("WCVP names", ["wcvp_names.csv"], "required", "run build_index.py"),
    ("WCVP distributions", ["wcvp_distributions.csv"], "required", "run build_index.py"),
    ("GBIF backbone", ["gbif_plantae.tsv", "gbif_backbone.zip", "_DwC_backbone_R.zip"],
     "required", "run download.py"),
    ("NCBI taxonomy (ETE)", ["taxa.sqlite"], "required", "run download.py"),
    ("Megatree", ["megatree_GBOTB.tre"], "required", "run download.py"),
    ("BHL export", ["bhl/title.txt", "bhl/pagename.txt"], "wanted", "run bhl_fetch.py"),
    ("GloNAF", ["glonaf/glonaf_flora2.csv", "glonaf/glonaf_taxon_wcvp.csv",
                "glonaf/glonaf_region.csv"], "wanted", "see sources.md (#3)"),
    ("SInAS", ["sinas/SInAS_*.csv"], "wanted",
     "SInAS_3.2.csv from zenodo.org/records/21933976 into plant_data/sinas/, then "
     "build_ranges.py (#3)"),
    ("GRIIS Netherlands", ["griis/*.zip"], "wanted",
     "checklist from cloud.gbif.org/griis/resource?r=griis-nl into plant_data/griis/ (#3)"),
    ("GBIF NL download (#7)", ["gbif_nl"], "wanted",
     "python gbif_nl.py --admin 2 (asks for a GBIF login; GROUP BY runs on GBIF's side; "
     "cite the DOI it prints), then build_obs.py"),
    ("GADM Netherlands (#8)", ["gadm/gadm41_NLD_2.json"], "wanted",
     "GADM 4.1 boundaries from geodata.ucdavis.edu/gadm/gadm4.1/json/gadm41_NLD_2.json "
     "into plant_data/gadm/ (level 3 does not exist for the Netherlands)"),
    ("plant_index.parquet", ["plant_index.parquet"], "wanted", "run build_index.py"),
    ("ranges.parquet", ["ranges.parquet"], "wanted", "run build_ranges.py"),
    ("plant_index_bhl.parquet", ["plant_index_bhl.parquet"], "wanted", "run bhl_build.py"),
    ("plant_books.parquet", ["plant_books.parquet"], "wanted", "run build_books.py (#4)"),
    ("taxon_ranges.parquet", ["taxon_ranges.parquet"], "wanted", "run build_status.py (#5)"),
    ("nl_status.parquet", ["nl_status.parquet"], "wanted", "run build_status.py (#5)"),
    ("tdwg_regions.parquet", ["tdwg_regions.parquet"], "wanted", "run build_status.py (#5)"),
    ("observations_nl.parquet", ["observations_nl.parquet"], "wanted", "run build_obs.py (#7)"),
    ("obs_areas.parquet", ["obs_areas.parquet"], "wanted", "run build_obs.py (#7)"),
    ("nl_totals.parquet", ["nl_totals.parquet"], "wanted", "run build_obs.py (#7)"),
    ("seed/nl_regions.csv", ["../seed/nl_regions.csv"], "wanted",
     "curated Netherlands region codes per scheme, committed in seed/ - build_status.py "
     "and status.py both read it (#5)"),
    ("seed/nl_places.csv", ["../seed/nl_places.csv"], "wanted",
     "curated Dutch imprint places, committed in seed/ - build_books.py needs it (#4)"),
]

# tables: name, what it is for, level (as above)
TABLES = [
    ("plant_index", "the master index", "required"),
    ("wfo_raw", "WFO backbone as loaded", "required"),
    ("wcvp_names", "WCVP/POWO names", "required"),
    ("wcvp_distributions", "WCVP native/introduced per TDWG area", "wanted"),
    ("gbif", "GBIF backbone", "required"),
    ("ncbi", "NCBI plant taxonomy", "required"),
    ("megatree_tips", "GBOTB megatree tip names", "required"),
    ("ranges", "taxon x region x status", "wanted"),
    ("bhl_names", "BHL name mentions", "wanted"),
    ("bhl_pages", "BHL pages", "wanted"),
    ("bhl_items", "BHL physical copies", "wanted"),
    ("bhl_titles", "BHL titles (LanguageCode, PublicationDetails)", "wanted"),
    ("bhl_name_summary", "pages/titles per BHL name", "wanted"),
    ("plant_index_bhl", "index + literature counts", "wanted"),
    ("plant_books", "plant name x BHL title (#4)", "wanted"),
    ("plant_books_full", "plant_books + title/copy text", "wanted"),
    ("taxon_ranges", "native / introduced range per taxon (#5)", "wanted"),
    ("nl_status", "one row per taxon recorded in the Netherlands (#5)", "wanted"),
    ("tdwg_regions", "TDWG region code -> name gazetteer (#5)", "wanted"),
    ("observations_nl", "plant x municipality record counts (#7)", "wanted"),
    ("obs_areas", "municipality centroids + bounding boxes (#8)", "wanted"),
    ("nl_totals", "one row per plant: how much of the NL it is found in (#7)", "wanted"),
    ("observations_nl_full", "observations_nl + area centroids, for maps (#8)", "wanted"),
    ("wikidata_ids", "parked (issue #2)", "parked"),
    ("wikidata_common_names", "parked (issue #2)", "parked"),
]

# names any botanist expects to be heavily documented in BHL
SPOT_NAMES = ["Typha latifolia", "Quercus robur", "Zea mays", "Solanum lycopersicum",
              "Taraxacum officinale"]


def log(*a):
    print(*a, flush=True)


def norm(c):
    return NORM.format(c=c)


def one(con, sql, params=None, default=None):
    """First value of a query, or default when it returns nothing/NULL."""
    r = con.execute(sql, params or []).fetchall()
    return r[0][0] if r and r[0] and r[0][0] is not None else default


def has_table(con, name):
    return bool(con.execute("SELECT count(*) FROM information_schema.tables "
                            "WHERE table_name = ?", [name]).fetchone()[0])


def has_col(con, table, col):
    if not has_table(con, table):
        return False
    return col in [r[0] for r in con.execute(f"DESCRIBE {table}").fetchall()]


def human(v):
    return f"{v:,}" if isinstance(v, int) else ("n/a" if v is None else str(v))


def pct(part, whole):
    return f"{100.0 * part / whole:.1f}%" if whole else "-"


def nl_pairs(include="yes"):
    """
    (region_scheme, code) rows from seed/nl_regions.csv.

    "The Netherlands" is a set of codes, one per source's region scheme, and the pair is
    what matters: ANT is Netherlands Antilles in GloNAF and Antarctica in TDWG3. Keeping
    the list in one curated file means no query can quietly define it differently.
    """
    path = DATA.parent / "seed" / "nl_regions.csv"
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as fh:
        return [(r["region_scheme"], r["code"]) for r in csv.DictReader(fh)
                if r["include"].strip().lower() == include]


def nl_values(pairs=None):
    """Those pairs as a SQL VALUES list, for EXISTS (scheme, code) checks."""
    pairs = pairs if pairs is not None else nl_pairs()
    return ", ".join(f"('{s}', '{c}')" for s, c in pairs)


# --- sections: each returns [(verdict, label, value, note)] -------------------
def check_files(con):
    out = []
    for label, names, level, fix in FILES:
        found = [x for x in names if (DATA / x).exists() or list(DATA.glob(x))]
        if found:
            p = DATA / found[0]
            if not p.exists():                       # a glob pattern: report the matches
                hits = sorted(DATA.glob(found[0]))
                detail = f"{found[0]} ({len(hits)} file(s), " \
                         f"{sum(h.stat().st_size for h in hits) / 1e6:.0f} MB)"
                out.append(("ok", label, None, detail))
                continue
            if p.is_dir():
                detail = f"{found[0]}/ ({sum(1 for f in p.rglob('*') if f.is_file())} files)"
            else:
                detail = f"{found[0]} ({p.stat().st_size / 1e6:.0f} MB, " \
                         f"{datetime.fromtimestamp(p.stat().st_mtime):%Y-%m-%d})"
            out.append(("ok", label, None, detail))
        else:
            verdict = {"required": "fail", "wanted": "warn", "later": "warn"}[level]
            out.append((verdict, label, None, f"missing -> {fix}"))
    return out


def check_tables(con):
    out = []
    for name, purpose, level in TABLES:
        if not has_table(con, name):
            verdict = {"required": "fail", "wanted": "warn", "parked": "parked"}[level]
            out.append((verdict, name, None, f"absent - {purpose}"))
            continue
        rows = one(con, f"SELECT count(*) FROM {name}", default=0)
        if level == "parked":
            out.append(("parked", name, rows, purpose))
        elif rows == 0 and level == "required":
            out.append(("fail", name, rows, purpose + " - EMPTY"))
        else:
            out.append(("ok", name, rows, purpose))
    return out


def check_coverage(con):
    if not has_table(con, "plant_index"):
        return [("fail", "plant_index", None, "no index to measure")]
    total = one(con, "SELECT count(*) FROM plant_index", default=0)
    out = [("ok", "names in plant_index", total, "one row per WFO name")]
    for label, col, expr, floor, expected, meaning in COVERAGE:
        if not has_col(con, "plant_index", col):
            out.append(("fail", label, None, meaning + " - column missing"))
            continue
        got = one(con, f"SELECT {expr} FROM plant_index", default=0) or 0
        frac = got / total if total else 0
        if frac < floor:
            out.append(("fail", label, frac, f"{meaning} - below floor {floor:.0%}"))
        elif frac < expected - 0.05:
            out.append(("warn", label, frac,
                        f"{meaning} - below expected {expected:.0%} of {human(total)}"))
        else:
            out.append(("ok", label, frac, f"{meaning} ({human(got)} of {human(total)})"))
    return out


def check_links(con):
    n_name = norm("name")
    checks = [
        ("gbif_id -> gbif", "gbif",
         "SELECT count(*) FROM plant_index p LEFT JOIN gbif g ON g.taxonID = p.gbif_id "
         "WHERE p.gbif_id IS NOT NULL AND g.taxonID IS NULL",
         "index GBIF ids missing from the backbone table -> backbone and index are from "
         "different snapshots: reload gbif (REBUILD=True in build_index.py)"),
        ("ncbi_taxid -> ncbi", "ncbi",
         "SELECT count(*) FROM plant_index p LEFT JOIN ncbi n ON n.taxid = p.ncbi_taxid "
         "WHERE p.ncbi_taxid IS NOT NULL AND n.taxid IS NULL",
         "index taxids missing from the NCBI table -> reload ncbi (REBUILD=True)"),
        ("wcvp_id -> wcvp_names", "wcvp_names",
         "SELECT count(*) FROM plant_index p LEFT JOIN wcvp_names w "
         "ON w.plant_name_id = p.wcvp_id "
         "WHERE p.wcvp_id IS NOT NULL AND w.plant_name_id IS NULL",
         "index WCVP ids missing from wcvp_names -> rerun build_index.py"),
        ("megatree -> index", "megatree_tips",
         f"SELECT count(*) FILTER (WHERE p.x IS NULL), "
         f"count(*) FILTER (WHERE p.x IS NULL AND t.name LIKE '% sp.%'), "
         f"count(*) FILTER (WHERE p.x IS NULL AND (t.name LIKE '% ssp.%' OR t.name LIKE '% var.%')), "
         f"count(*) FILTER (WHERE p.x IS NULL AND t.name NOT LIKE '% sp.%' "
         f"AND t.name NOT LIKE '% ssp.%' AND t.name NOT LIKE '% var.%') FROM megatree_tips t "
         f"LEFT JOIN (SELECT DISTINCT {n_name} x FROM plant_index) p ON p.x = {norm('t.name')}",
         "megatree tips with no index name: (sp. placeholders, infraspecific ssp./var., "
         "plain names not in WFO)"),
        ("ranges -> index", "ranges",
         "SELECT count(*) FROM ranges r LEFT JOIN plant_index p ON p.wfo_id = r.wfo_id "
         "WHERE r.wfo_id IS NOT NULL AND p.wfo_id IS NULL",
         "range rows whose WFO id is not in the index -> ranges was built against a "
         "different plant_index: rerun build_ranges.py"),
    ]
    out = []
    for label, table, sql, fix in checks:
        if not has_table(con, table):
            out.append(("warn", label, None, f"cannot check - no {table} table"))
            continue
        if label.startswith("megatree"):
            # four counts: unmatched, of which 'sp.' placeholders, infraspecific, plain
            unmatched, sp, infra, plain = con.execute(sql).fetchone()[0:4]
            tips = one(con, "SELECT count(*) FROM megatree_tips", default=0)
            real = unmatched - sp          # 'sp.' entries can never be in a backbone
            frac = real / tips if tips else 0
            note = (f"{human(unmatched)} of {human(tips)} tips unmatched: {human(sp)} are 'sp.' "
                    f"placeholders (never in WFO), {human(infra)} infraspecific (ssp./var.), "
                    f"{human(plain)} plain names that are simply not in the WFO backbone")
            if frac > 0.03:
                out.append(("warn", label, frac, note + " -> check name normalisation "
                            "(hybrid x, ssp. vs subsp., accents)"))
            else:
                out.append(("ok", label, frac, note + " - drift under 3%"))
            continue
        bad = con.execute(sql).fetchone()[0] or 0
        if bad == 0:
            out.append(("ok", label, 0, "dangling ids: none"))
        else:
            out.append(("fail", label, bad, fix))
    return out


def check_ranges(con):
    if not has_table(con, "ranges"):
        return [("warn", "ranges", None, "not built -> run build_ranges.py (#3)")]
    out = []
    for src, rows, linked in con.execute(
            "SELECT source, count(*), count(wfo_id) FROM ranges GROUP BY 1 ORDER BY 1").fetchall():
        frac = linked / rows if rows else 0
        note = f"{human(rows)} rows, {human(linked)} linked to WFO ({pct(linked, rows)})"
        regions = one(con, "SELECT count(DISTINCT region_code) FROM ranges WHERE source = ?",
                      [src], default=0)
        note += f", {human(regions)} regions"
        if frac < 0.85:
            out.append(("warn", f"ranges:{src}", frac, note + " - under 85% linked"))
        else:
            out.append(("ok", f"ranges:{src}", frac, note))
    present = {s for (s,) in con.execute("SELECT DISTINCT source FROM ranges").fetchall()}
    for missing, issue in [("sinas", "#3"), ("griis", "#3")]:
        if missing not in present:
            out.append(("warn", f"ranges:{missing}", None,
                        f"source absent from ranges (files not downloaded) - see {issue}"))
    if has_col(con, "ranges", "link_src"):
        mix = con.execute("SELECT coalesce(link_src, 'unlinked'), count(*) FROM ranges "
                          "GROUP BY 1 ORDER BY 2 DESC").fetchall()
        out.append(("ok", "how ranges linked", None,
                    ", ".join(f"{s} {human(c)}" for s, c in mix)
                    + " (unlinked WCVP rows are names the WFO backbone does not hold)"))
    # what the Netherlands looks like in each source: the number #5 #6 #7 depend on
    pairs = nl_pairs()
    if pairs:
        nl = con.execute(f"""SELECT source, count(*), count(DISTINCT coalesce(wfo_id, name)) taxa
            FROM ranges r WHERE EXISTS (SELECT 1 FROM (VALUES {nl_values(pairs)})
                                        v(scheme, code)
            WHERE v.scheme = r.region_scheme AND v.code = r.region_code)
            GROUP BY 1 ORDER BY 1""").fetchall()
        out.append(("ok", "ranges:Netherlands", None,
                    ", ".join(f"{s} {human(r)} rows/{human(t)} taxa" for s, r, t in nl)
                    + " (region codes " + "/".join(c for _, c in pairs)
                    + " paired with their scheme - never ILIKE '%netherlands%', which also "
                      "returns Netherlands Antilles)"))
    else:
        out.append(("warn", "seed/nl_regions.csv", None,
                    "missing -> every Netherlands filter needs its (scheme, code) pairs"))
    # SInAS has a pathway column, but for plants it is empty - matters for #6
    path = one(con, "SELECT count(pathway) FROM ranges", default=0)
    if path == 0 and "sinas" in present:
        out.append(("warn", "ranges:pathway", 0,
                    "no pathway values for any plant: SInAS fills pathway for 2,810 rows, "
                    "all of them animals, and GRIIS-NL has no pathway column (its metadata "
                    "says pathways are 'planned') -> #6 needs a curated seed table"))
    # GloNAF records are per synonym, so one taxon x region can appear many times with
    # different statuses. Not a bug, but the derived status table (#5) must aggregate.
    if has_col(con, "ranges", "wfo_id"):
        dup = con.execute("""SELECT count(*) FILTER (WHERE n > 1), count(*) FILTER (WHERE c > 1)
            FROM (SELECT wfo_id, region_code, count(*) n, count(DISTINCT status) c FROM ranges
                  WHERE source = 'glonaf' AND wfo_id IS NOT NULL GROUP BY 1, 2)""").fetchone()
        if dup[0]:
            out.append(("ok", "ranges:glonaf duplicates", None,
                        f"{human(dup[0])} taxon x region pairs have several records "
                        f"({human(dup[1])} disagree on status, e.g. naturalized vs invasive) - "
                        "GloNAF records are per synonym, so nl_status (#5) aggregates them"))
    return out


def check_bhl(con):
    if not has_table(con, "bhl_name_summary"):
        return [("warn", "BHL", None, "no BHL tables -> run bhl_fetch.py then bhl_build.py")]
    out = []
    named = one(con, "SELECT count(*) FROM bhl_name_summary WHERE bhl_pages > 0", default=0)
    out.append(("ok", "BHL names", named, "BHL names with at least one page hit"))
    if has_table(con, "plant_index_bhl"):
        withlit = one(con, "SELECT count(*) FROM plant_index_bhl WHERE coalesce(bhl_pages, 0) > 0",
                      default=0)
        total = one(con, "SELECT count(*) FROM plant_index_bhl", default=0)
        frac = withlit / total if total else 0
        verdict = "warn" if frac < 0.4 else "ok"
        out.append((verdict, "index x BHL", frac,
                    f"index names with BHL hits ({human(withlit)} of {human(total)})"))
    if has_col(con, "bhl_name_summary", "nname"):
        missing = [nm for nm in SPOT_NAMES
                   if not one(con, "SELECT coalesce(max(bhl_pages), 0) FROM bhl_name_summary "
                                   "WHERE nname = ?", [nm.lower()], default=0)]
        if missing:
            out.append(("warn", "BHL spot check", len(missing),
                        "well-known names with no hits: " + ", ".join(missing)
                        + " -> name normalisation problem"))
        else:
            out.append(("ok", "BHL spot check", None, "spot names all found: "
                        + ", ".join(SPOT_NAMES)))
    if has_table(con, "bhl_titles"):
        langs = one(con, "SELECT count(DISTINCT LanguageCode) FROM bhl_titles", default=0)
        out.append(("ok", "BHL titles", one(con, "SELECT count(*) FROM bhl_titles", default=0),
                    f"{langs} distinct LanguageCodes - language filter for #4 is usable"))
    return out


def check_books(con):
    """plant_books: plant name x BHL title, and the two Dutch flags (#4)."""
    if not has_table(con, "plant_books"):
        return [("warn", "plant_books", None, "not built -> run build_books.py (#4)")]
    out = []
    rows, names, titles, wfo = con.execute(
        "SELECT count(*), count(DISTINCT nname), count(DISTINCT title_id), count(wfo_id) "
        "FROM plant_books").fetchone()
    out.append(("ok", "plant_books", rows,
                f"{human(names)} names x {human(titles)} titles, "
                f"{pct(wfo, rows)} of the rows resolve to one WFO record "
                "(the rest are homonyms or names plant_index does not hold)"))
    # one row per name x title, never two
    dup = one(con, "SELECT count(*) FROM (SELECT nname, title_id, count(*) c FROM plant_books "
                   "GROUP BY 1, 2 HAVING count(*) > 1)", default=0)
    out.append(("ok" if not dup else "fail", "plant_books key", dup,
                "duplicate (name, title) rows - must be 0"))
    # same titles as the independent summary: a cheap agreement check between two builds
    if has_table(con, "bhl_name_summary"):
        bad = one(con, """SELECT count(*) FROM (SELECT nname, bhl_titles t FROM bhl_name_summary) s
            JOIN (SELECT nname, count(*) t FROM plant_books GROUP BY 1) b USING (nname)
            WHERE s.t <> b.t""", default=0)
        out.append(("ok" if not bad else "warn", "plant_books vs bhl_name_summary", bad,
                    "names whose title count disagrees between the two builds - must be 0 "
                    "(their page counts differ on purpose: the summary counts distinct pages, "
                    "plant_books sums them over digitised copies)"))
    if has_col(con, "plant_books", "dutch"):
        mix = con.execute("SELECT dutch, count(DISTINCT title_id) FROM plant_books "
                          "GROUP BY 1 ORDER BY 2 DESC").fetchall()
        by_title = {d: t for d, t in mix}
        flagged = sum(t for d, t in mix if d)
        out.append(("ok", "Dutch flags", flagged,
                    "titles flagged: "
                    + ", ".join((f"{d or 'none'} {human(t)}") for d, t in mix)
                    + " - dutch_language is LanguageCode DUT/NLA, dutch_published matches an "
                      "imprint place in seed/nl_places.csv; most books printed in the "
                      "Netherlands are Latin, not Dutch, so filtering on language alone "
                      "misses most of them"))
        if not by_title.get("published") and not by_title.get("both"):
            out.append(("warn", "Dutch imprints", 0,
                        "no title matched seed/nl_places.csv - check the pattern list"))
        places = DATA.parent / "seed" / "nl_places.csv"
        if places.exists():
            n_pat = len(places.read_text(encoding="utf-8").strip().splitlines()) - 1
            out.append(("ok", "nl_places.csv", n_pat,
                        "curated imprint patterns (Latin forms matter: Amstelodami, "
                        "Lugduni Batavorum, Trajecti); Antwerp/Antverpiae is deliberately "
                        "not counted as the Netherlands"))
        else:
            out.append(("warn", "nl_places.csv", None, f"missing -> {places}"))
    return out


def check_status(con):
    """taxon_ranges / nl_status: where a plant is from, what it does in the NL (#5)."""
    if not has_table(con, "nl_status"):
        return [("warn", "nl_status", None, "not built -> run build_status.py (#5)")]
    out = []
    pairs = nl_pairs()
    rows = one(con, "SELECT count(*) FROM nl_status", default=0)
    raw = one(con, f"""SELECT count(*) FROM (SELECT DISTINCT
                          CASE WHEN r.wfo_id IS NOT NULL THEN r.wfo_id ELSE {norm('r.name')} END k
                      FROM ranges r WHERE EXISTS (SELECT 1 FROM (VALUES {nl_values(pairs)})
                          v(scheme, code) WHERE v.scheme = r.region_scheme
                          AND v.code = r.region_code))""", default=0)
    out.append(("ok", "nl_status", rows,
                f"taxa recorded in the Netherlands from {human(raw)} raw id/name spellings "
                f"in ranges - a WFO synonym id, the accepted id and a bare name are one plant"))
    dup = one(con, "SELECT count(*) FROM (SELECT key FROM nl_status GROUP BY 1 HAVING count(*) > 1)",
              default=0)
    out.append(("ok" if not dup else "fail", "nl_status key", dup,
                "duplicate taxon rows - must be 0"))
    # every answer word must come from the sources' own vocabulary
    known = {"native", "introduced", "naturalized", "invasive", "cryptogenic", "uncertain"}
    used = {w.strip() for (s,) in con.execute("SELECT DISTINCT status FROM nl_status").fetchall()
            for w in str(s).replace("|", ";").split(";")}
    if used - known:
        out.append(("fail", "nl_status vocabulary", len(used - known),
                    "unexpected status words: " + ", ".join(sorted(used - known))))
    else:
        mix = con.execute("SELECT status, count(*) FROM nl_status GROUP BY 1 ORDER BY 2 DESC")\
                 .fetchall()
        out.append(("ok", "NL status mix", None,
                    ", ".join(f"{s} {human(c)}" for s, c in mix)))
    conflict = one(con, "SELECT count(*) FILTER (WHERE conflict) FROM nl_status", default=0)
    out.append(("ok" if conflict else "warn", "sources disagree", conflict,
                f"{pct(conflict, rows)} of NL taxa: some source calls them native and another "
                "alien. Kept as `conflict` instead of averaged away - usually GloNAF/SInAS "
                "calling an alien while WCVP still calls the species native to the region"))
    arrival, lo, hi, srcs = con.execute(
        """SELECT count(arrival_year), min(arrival_year), max(arrival_year),
                  string_agg(DISTINCT arrival_source, ',') FROM nl_status""").fetchone()
    out.append(("ok" if arrival else "warn", "arrival years", arrival,
                f"{lo}-{hi} ({', '.join(str(s) for s in srcs.split(',')) if srcs else 'none'})"
                " - only SInAS dates arrivals; GRIIS-NL has eventDate on 1 of 376 rows"))
    extinct = one(con, "SELECT count(*) FILTER (WHERE extinct) FROM nl_status", default=0)
    out.append(("ok", "extinct in NL", extinct,
                "records whose source flags the taxon as extinct in the Netherlands"))
    # origin(): the range must be readable, not just counted
    if has_table(con, "taxon_ranges"):
        t_rows, with_native, shown = con.execute(
            """SELECT count(*), count(*) FILTER (WHERE n_native_regions > 0),
                      count(*) FILTER (WHERE native_regions IS NOT NULL
                                       OR native_groups IS NOT NULL)
               FROM taxon_ranges""").fetchone()
        out.append(("ok", "taxon_ranges", t_rows,
                    f"{human(with_native)} taxa have a native range in WCVP, "
                    f"{human(shown)} of them with a readable list (region names up to 12, "
                    "then the TDWG level-2 regions they fall in)"))
        blind = one(con, """SELECT count(*) FROM taxon_ranges WHERE n_native_regions > 0
                          AND native_regions IS NULL AND native_groups IS NULL""", default=0)
        out.append(("ok" if not blind else "fail", "range readable", blind,
                    "taxa counted as native somewhere but with no region shown - must be 0"))
        if has_table(con, "tdwg_regions"):
            missing = one(con, """SELECT count(DISTINCT r.region_code) FROM ranges r
                              WHERE r.source = 'wcvp' AND NOT EXISTS
                              (SELECT 1 FROM tdwg_regions g WHERE g.code = r.region_code)""",
                        default=0)
            gaz = one(con, "SELECT count(*) FROM tdwg_regions", default=0)
            out.append(("ok" if not missing else "warn", "tdwg_regions", gaz,
                        f"region codes -> names ({missing} WCVP codes not in the gazetteer)"))
    return out


def check_obs(con):
    """observations_nl / obs_areas / nl_totals: where plants were seen in the NL (#7 #8)."""
    if not has_table(con, "observations_nl"):
        return [("warn", "observations_nl", None,
                 "not built -> run gbif_nl.py (the GBIF download) then build_obs.py")]
    out = []
    rows, taxa, nl_areas = con.execute("""SELECT count(*), count(DISTINCT key),
        count(DISTINCT CASE WHEN in_nl THEN area_id END) FROM observations_nl""").fetchone()
    out.append(("ok", "observations_nl", rows,
                f"{human(taxa)} plants seen in {nl_areas} Dutch areas (GADM level 2: the "
                "municipalities plus the IJsselmeer and two other water areas)"))
    dup = one(con, "SELECT count(*) FROM (SELECT key, coalesce(area_id, '') FROM "
                   "observations_nl GROUP BY 1, 2 HAVING count(*) > 1)", default=0)
    out.append(("ok" if not dup else "fail", "one row per plant x area", dup,
                "duplicate (plant, area) rows - must be 0"))
    # no record may be lost or invented on the way in: the download's totals must add up
    total = one(con, "SELECT sum(occurrences) FROM observations_nl", default=0)
    inside = one(con, "SELECT sum(CASE WHEN in_nl THEN occurrences END) FROM observations_nl",
                 default=0)
    outside = one(con, "SELECT sum(CASE WHEN NOT in_nl THEN occurrences END) "
                       "FROM observations_nl", default=0)
    nocoord = one(con, "SELECT sum(CASE WHEN area_id IS NULL THEN occurrences END) "
                       "FROM observations_nl", default=0)
    out.append(("ok" if inside + outside + nocoord == total else "fail",
                "records add up", total,
                f"{human(inside)} inside the Netherlands + {human(outside)} whose coordinate "
                "lands in Belgium/Germany (border slips and GBIF's default centroid for "
                f"badly georeferenced labels) + {human(nocoord)} digitised without any "
                "coordinates - kept and flagged, never dropped"))
    raw = list(DATA.glob("gbif_nl/*.csv"))
    if raw:
        newest = max(raw, key=lambda p: p.stat().st_size)
        download = one(con, f"SELECT sum(cast(occurrences AS BIGINT)) FROM read_csv "
                            f"('{newest}', header=true, sep='\t')", default=0)
        out.append(("ok" if download == total else "fail", "matches GBIF download", download,
                    f"sum(occurrences) in {newest.name} - every record of the download is "
                    "in observations_nl"))
    linked = one(con, "SELECT count(DISTINCT key) FILTER (WHERE wfo_id IS NOT NULL) "
                      "FROM observations_nl", default=0)
    out.append(("ok", "linked to plant_index", linked,
                f"{pct(linked, taxa)} of the {human(taxa)} GBIF names resolve to a WFO "
                "record by name (GBIF's SQL specieskey matches no id in the index: 0 of "
                "12,310). The rest are mostly algae and nothohybrids the plant backbone "
                "does not hold; they keep their own name as key"))
    if has_table(con, "obs_areas"):
        areas, innl, nofix = con.execute("""SELECT count(*), count(*) FILTER (WHERE in_nl),
            count(*) FILTER (WHERE in_nl AND lat IS NULL) FROM obs_areas""").fetchone()
        out.append(("ok" if not nofix else "fail", "obs_areas", areas,
                    f"{innl} Dutch + {areas - innl} foreign areas GBIF placed NL records in; "
                    "each Dutch one has a centroid + bbox for mapping "
                    f"({nofix} missing coordinates)"))
        named = one(con, "SELECT count(*) FROM obs_areas WHERE name IS NULL", default=0)
        out.append(("ok" if not named else "warn", "area names", named,
                    "areas without a name - must be 0 for the maps to be readable"))
    if has_table(con, "nl_totals"):
        t_rows, t_taxa = con.execute("SELECT count(*), count(DISTINCT key) FROM nl_totals")\
                          .fetchone()
        out.append(("ok" if t_rows == t_taxa == taxa else "fail", "nl_totals", t_rows,
                    "one row per plant, matching observations_nl"))
    out.append(("ok", "level 3 (wards)", 0,
                "deliberately not built (issue #8): GBIF has no level-3 area for about 1 in "
                "4 NL subdivisions holding records, GADM 4.1's GeoJSON has no gadm41_NLD_3 "
                "file at all, and the agreed answer was to keep the database small"))
    return out


def check_matching(con):
    """How much of the index can be matched by name alone - drives every fallback."""
    if not has_table(con, "plant_index"):
        return []
    uniq, distinct = con.execute(f"SELECT count(*) FILTER (WHERE n = 1), count(*) FROM "
                                 f"(SELECT {norm('name')} k, count(*) n FROM plant_index "
                                 f"GROUP BY 1)").fetchone()
    rows = [("ok", "name uniqueness", None,
             f"{human(uniq)} of {human(distinct)} distinct names are unique in the index "
             f"({pct(uniq, distinct)}); homonyms are never matched by name alone")]
    if has_table(con, "plant_index"):
        src = con.execute("SELECT coalesce(gbif_src,'none'), count(*) FROM plant_index "
                          "GROUP BY 1 ORDER BY 2 DESC").fetchall()
        rows.append(("ok", "gbif_src mix", None,
                     ", ".join(f"{s} {human(c)}" for s, c in src)
                     + " ('wikidata' stays empty - parked, issue #2)"))
    return rows


def check_parked(con):
    wd = one(con, "SELECT count(wikidata_qid) FROM plant_index", default=0) if \
        has_col(con, "plant_index", "wikidata_qid") else 0
    return [("parked", "Wikidata", wd,
             "0 ids is the agreed state (issue #2, parked): no QLever fetch, no Wikidata "
             "common names, gbif_src='wikidata' stays empty. Not a bug - do not re-attempt.")]


SECTIONS = [
    ("Source files", check_files),
    ("Tables in the database", check_tables),
    ("Index coverage (share of names holding each id)", check_coverage),
    ("Cross-reference integrity", check_links),
    ("Distribution ranges", check_ranges),
    ("BHL literature", check_bhl),
    ("Books per plant (#4)", check_books),
    ("Origin and Netherlands status (#5)", check_status),
    ("Observations in the Netherlands (#7 #8)", check_obs),
    ("Matching notes", check_matching),
    ("Parked - decided against, not bugs", check_parked),
]

ICON = {"ok": "ok", "warn": "WARN", "fail": "FAIL", "parked": "parked"}


def fmt(value):
    if value is None:
        return ""
    return f"{value:.1%}" if isinstance(value, float) else human(value)


def main():
    if not DB.exists():
        raise SystemExit(f"No database at {DB} - run build_index.py first.")
    try:
        con = duckdb.connect(str(DB), read_only=True)
    except duckdb.IOException as e:
        raise SystemExit(f"Cannot open {DB} read-only (is a build running?):\n{e}")

    log(f"quality report for plant_data/{DB.name}   {datetime.now():%Y-%m-%d %H:%M}\n")
    results, counts = [], {"ok": 0, "warn": 0, "fail": 0, "parked": 0}
    for title, fn in SECTIONS:
        rows = fn(con)
        results.append((title, rows))
        log(f"## {title}")
        for verdict, label, value, note in rows:
            counts[verdict] += 1
            log(f"  [{ICON[verdict]:<6}] {label:<24} {fmt(value):>9}  {note}")
        log("")

    fails = [r for _, rows in results for r in rows if r[0] == "fail"]
    warns = [r for _, rows in results for r in rows if r[0] == "warn"]
    log(f"summary: {counts['ok']} ok, {counts['warn']} warnings, {counts['fail']} failures, "
        f"{counts['parked']} parked")
    if warns:
        log("\nwarnings, with the fix for each:")
        for _, label, _, note in warns:
            log(f"  - {label}: {note}")

    md = ["# Quality report", "",
          f"`plant_data/{DB.name}` - generated by `python status.py` on "
          f"{datetime.now():%Y-%m-%d %H:%M}.", "",
          f"**{counts['ok']} ok / {counts['warn']} warnings / {counts['fail']} failures / "
          f"{counts['parked']} parked.**", "",
          "Wikidata is *parked* (issue #2): empty Wikidata ids are the agreed state, not a gap."]
    for title, rows in results:
        md += [f"## {title}", "", "| | check | value | note |", "|---|---|---:|---|"]
        md += [f"| `{ICON[v]}` | {l} | {fmt(val)} | {note} |" for v, l, val, note in rows]
        md.append("")
    if fails:
        md += ["## Failing - fix these first", ""]
        md += [f"- **{l}**: {note}" for _, l, _, note in fails] + [""]
    REPORT.write_text("\n".join(md), encoding="utf-8")
    log(f"\nwrote {REPORT.relative_to(DATA.parent)}")
    con.close()
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()

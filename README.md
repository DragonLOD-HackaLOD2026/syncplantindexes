# syncplantindexes — one index for plant names across biodiversity databases

**The idea:** every biodiversity database identifies plants by its own id — WFO, IPNI, POWO,
GBIF, NCBI, Wikidata, … — and cross-referencing them is a pain. This repo builds one
master index, keyed on the **World Flora Online (WFO)** backbone, that pulls all those ids
together, adds how much literature each name has in the **Biodiversity Heritage Library
(BHL)** (how many pages and titles mention it, earliest and latest year), and adds where
each species is **native, introduced, naturalized or invasive** from four distribution
sources (WCVP, GloNAF, SInAS, GRIIS).

Everything runs locally, everything is **safe to re-run**: each step skips work that is
already done, and no download or conversion is ever repeated unless you delete its output.

```
 WFO backbone ─┐
 WCVP (POWO)  ─┤
 GBIF backbone─┼──► build_index.py ──► plant_index        one row per WFO name:
 NCBI taxonomy─┤                       (DuckDB + Parquet)  WFO/IPNI/POWO/WCVP/Wikidata/GBIF/NCBI ids,
 Wikidata*    ─┤                                           megatree tip flag, provenance
 megatree     ─┘
                                     bhl_fetch.py ──► BHL TSV export (4 big tables)
 BHL books    ──── bhl_fetch.py ────► bhl_build.py ──► plant_index_bhl
                                     (DuckDB + Parquet)  + page/title counts + first/last year

 WCVP distributions ─┐
 GloNAF              ─┼──► build_ranges.py ──► ranges    long table: one row per taxon × region
 SInAS               ─┤      (DuckDB + Parquet)           × source, with native/introduced/
 GRIIS               ─┘                                  invasive status + first record year

 status.py      ─── reads everything above and prints/writes a quality scorecard

 plant_index ──┐
 bhl + seed/  ──┴──► build_books.py ──► plant_books     one row per *plant name × BHL title*:
                                     (DuckDB + Parquet)  pages, copies, years, and the two
                                                         Dutch flags (language / imprint)

 plant_query.py ── read-only Python questions over all of it:
                   plants(), books(), origin(), nl_status(), observations()

 GBIF NL download ┐
 (GROUP BY on     ├─► build_obs.py ──► observations_nl  one row per plant × municipality:
 GBIF's server)   │     (DuckDB + Parquet) record counts, first/last year
 + GADM borders   └──►  obs_areas + nl_totals  area centroids/bboxes for maps,
                                               totals per plant

 ranges ─────────┐
 plant_index    ─┼─► build_status.py ──► taxon_ranges   one row per taxon: how much of the
 + seed/nl_...   │     (DuckDB + Parquet) world it is native to, where it has been introduced
                 └──►  nl_status          + tdwg_regions (region code → name);
                                          one row per taxon recorded in the Netherlands:
                                          native/introduced/naturalized/invasive, each
                                          source's verdict, arrival year
```

<sub>\* Wikidata is **parked** (issue #2): the QLever fetch is switched off, so the
Wikidata columns exist but stay empty. Nothing else in the pipeline needs them.</sub>

## What you get

| Output | What it is |
|---|---|
| `plant_data/plant_index.duckdb` | All raw source tables plus the final `plant_index`, `plant_index_bhl`, `ranges`, `plant_books`, `taxon_ranges`, `nl_status` and `tdwg_regions` |
| `plant_data/plant_index.parquet` | `plant_index` as a single Parquet file (easy for pandas/polars/DuckDB anywhere) |
| `plant_data/plant_index_bhl` (table + `.parquet`) | `plant_index` enriched with BHL literature counts per name |
| `plant_data/ranges.parquet` | `ranges`: who lives where — native / introduced / naturalized / invasive per taxon × region × source |
| `plant_data/plant_books.parquet` | `plant_books`: which BHL title mentions which plant name, how many pages, when, and whether the book is Dutch |
| `plant_data/taxon_ranges.parquet` | `taxon_ranges`: native / introduced range per taxon (TDWG regions, from WCVP) |
| `plant_data/nl_status.parquet` | `nl_status`: one row per taxon recorded in the Netherlands — resolved status plus each source's verdict and arrival year |
| `plant_data/tdwg_regions.parquet` | `tdwg_regions`: TDWG level-3 code → name, its level-2 region and continent |
| `plant_data/observations_nl.parquet` | `observations_nl`: one row per plant × Dutch municipality — GBIF record counts, first/last year seen (33.6 M records compressed into 529 K rows) |
| `plant_data/obs_areas.parquet` | `obs_areas`: one row per area — GADM id, Dutch name, province, centroid + bounding box (what a choropleth needs, no geometry required) |
| `plant_data/nl_totals.parquet` | `nl_totals`: one row per plant — how many of the 355 Dutch areas it was found in, total records, its bounding box in the NL |
| `plant_data/quality_report.md` | The `status.py` scorecard: what is loaded, coverage per id, broken links, and how to fix each gap |
| `plant_data/bhl/*.parquet` | The slim BHL tables (only the columns needed) |

Each `plant_index` row: WFO id, name, author, rank, status, accepted name, family, IPNI id,
WCVP/POWO ids, Wikidata QID (empty — parked, see below), GBIF id, NCBI taxid, `in_megatree`
(is it a tip of the GBOTB megatree?), and `*_src` columns saying **how** each id was found
(`"name+author"` / `"name"` = unique exact name match — homonyms are never matched by name
alone; the `"wikidata"` value exists in the schema but never fires, issue #2).

Every build script ends with a coverage summary (how many names got each id, how many
range rows link to WFO, how many names are in BHL), so you can see at a glance how well
the linking worked.

Each `plant_books` row is one *plant name × BHL title*: `pages` (page mentions, summed over
all digitised **copies** — `copies` says how many), `first_year`/`last_year` of the pages
mentioning it, the title's `language_code`, `publication_details` and `published_place`,
plus the two Dutch flags. Title text (full title, author of the book, call number, holding
institution, URLs) is not duplicated across the 27 M rows; the view `plant_books_full`
joins it when you want it.

**"Dutch books" is two different questions**, so `plant_books` keeps both flags:

| flag | meaning | BHL titles |
|---|---|---:|
| `dutch_language` | `LanguageCode IN ('DUT','NLA')` | 435 |
| `dutch_published` | imprint place in `seed/nl_places.csv` (`Amstelodami`, `Lugduni Batavorum`, `Trajecti`, …) | 988 |
| `dutch` | `'both'` / `'language'` / `'published'` / `''` | 838 flagged in total |

Most books printed in the Netherlands are **Latin**, not Dutch — filtering on language
alone misses most of them, and the Latin imprint forms are what find them. Antwerp
(*Antverpiae*) is deliberately **not** counted as the Netherlands.

**Where a plant is from, and what it is doing in the Netherlands** (`build_status.py`):

- `taxon_ranges` — one row per taxon. `native_regions` lists the TDWG regions when the
  range is small enough to read (up to 12); wider ranges are summarised by their TDWG
  level-2 regions in `native_groups`. The counts come from **WCVP only**: it is the one
  source with global level-3 coverage, GloNAF records only the naturalised alien flora,
  and SInAS numbers its own region list (its `8` is Curaçao, not a TDWG code).
  `tdwg_regions` is the code → name gazetteer that makes them readable.
- `nl_status` — one row per taxon recorded in the Netherlands (4,806 of them: 2,752
  native, 1,830 introduced, 116 invasive, 63 naturalised). Every source's verdict keeps
  its own column and `conflict` marks the **88** taxa where one source says native and
  another says alien, instead of averaging the disagreement away. The headline `status`
  is resolved by authority — **GRIIS** (the Dutch checklist) > **SInAS** > **GloNAF** >
  **WCVP** (coarsest) — with one override: any source saying *invasive* wins, because
  "not invasive" is only the absence of a record. `arrival_year` comes from SInAS
  (165 taxa, 1599–2019); GRIIS-NL has a date on 1 of its 376 rows.
- One plant, one row. A source can link to a WFO **synonym** record, so every id is
  canonicalised to its accepted record: GRIIS puts *Crassula helmsii* on
  `wfo-0001300375`, a synonym of the `wfo-0000625092` that GloNAF and SInAS use, and
  those become one taxon rather than two.
- "The Netherlands" is a list of `(scheme, code)` pairs in `seed/nl_regions.csv`
  (`tdwg3:NET`, `glonaf:NLD`, `iso:NL`, `sinas:184`, and the codes that must stay out:
  `NLA`/`ANT`/`CUW`/`SUR`). The pair matters: `ANT` is Netherlands Antilles in GloNAF and
  **Antarctica** in TDWG3, and `ILIKE '%netherlands%'` silently adds 1,068 Netherlands
  Antilles rows.

**Where each plant was actually recorded in the Netherlands** (`gbif_nl.py` + `build_obs.py`):

- GBIF holds 33.6 M plant records for the Netherlands. Downloading the points would take
  gigabytes, so `gbif_nl.py` asks GBIF's SQL API to do the `GROUP BY species × GADM area`
  on **GBIF's own server**: the whole country arrives as 538 K rows / 38 MB, and
  `build_obs.py` turns that into `observations_nl` — one row per plant × municipality
  (529 K rows, 12,106 plants × 355 Dutch areas — 352 municipalities plus the IJsselmeer
  and two other water areas), with `obs_areas` holding each area's centroid and bounding
  box for maps.
- **Level 2 (municipalties), not level 3 (wards)**, deliberately: GBIF has no level-3 area
  for roughly 1 in 4 NL subdivisions that hold records, GADM 4.1's GeoJSON has no
  `gadm41_NLD_3` file at all, and level 3 would roughly triple the table for little gain.
- GBIF's SQL API returns an opaque `specieskey` that matches **no** GBIF backbone key in
  the index (measured: 0 of 12,310), so linking runs through **names**: 10,290 of 12,106
  names (85%) resolve to a WFO record, the rest are mostly algae and nothohybrids the
  plant backbone does not hold — they keep their own name as key instead of disappearing.
- Points whose **coordinate** falls outside the Netherlands (1.07 M records in Belgian
  Limburg and along the German border — publishers said `countrycode = NL`, georeferencing
  did not agree) and points digitised **without coordinates** (1.99 M, mostly coastal and
  dune plants) are kept and flagged (`in_nl`, `area_id IS NULL`), never silently dropped.

`seed/` holds hand-curated reference tables that the scripts read (committed on purpose,
unlike the gitignored `plant_data/`); `seed/README.md` explains each one.

## The scripts, in the order you'd run them

| Script | What it does | Time / space |
|---|---|---|
| `download.py` | Installs missing Python & R packages; sets up the WFO backbone, V.PhyloMaker2 megatree, ETE/NCBI taxonomy SQLite, GBIF backbone archive, (optionally) a GBIF species list | ~20 GB disk, downloads an hour or two |
| `build_index.py` | Loads every source into DuckDB and builds `plant_index` with all the cross-links | minutes |
| `bhl_fetch.py` | Downloads BHL's monthly TSV export (~2.5 GB zip) and extracts the four tables needed: `pagename`, `page`, `item`, `title` | ~15 GB unpacked |
| `bhl_build.py` | Converts the BHL TSVs to slim Parquet, keeps only names present in `plant_index`, and builds `bhl_name_summary` + `plant_index_bhl` | tens of minutes, DuckDB capped at 4 threads / 8 GB and spills to disk |
| `build_ranges.py` | Stacks the four distribution sources into one long `ranges` table linked back to `plant_index` — by WCVP id, else name+author, else name+author ignoring the parenthetical author, else unique exact name (`link_src` says which). Skips SInAS/GRIIS with a message while their files are missing | seconds to minutes; rebuilt from scratch on every run |
| `build_books.py` | Folds the 217 M BHL name-page rows into `plant_books`, one row per plant name × title, with the Dutch language/imprint flags from `seed/nl_places.csv` | ~3 min, staged temp tables so each step can spill to disk |
| `build_status.py` | Turns `ranges` into answers: `taxon_ranges` (native / introduced range per taxon), `nl_status` (what each source says about the Netherlands, plus one resolved verdict) and the `tdwg_regions` gazetteer | ~3 s, reads `seed/nl_regions.csv` |
| `gbif_nl.py` | Asks GBIF's SQL download API for all plant occurrences in the Netherlands **grouped by species × GADM area** — the GROUP BY runs on GBIF's server, so the whole country comes back as 538 K rows / 38 MB instead of tens of millions of points. Needs a GBIF login; prints the DOI to cite | ~5–10 min on GBIF's side, 38 MB |
| `build_obs.py` | Folds that download into `observations_nl` (plant × municipality), `obs_areas` (names, centroids, bboxes from the GADM GeoJSON) and `nl_totals`; links GBIF names to `plant_index` by name | ~2 s |
| `build_rdf.py` | Exports the index as **N-Triples** for SPARQL serving: plants (synonyms become `skos:altLabel`, never entities), the 355 gemeenten as `schema:Place`, observation facts with pre-computed prevalence scores (zeros included), and BHL books as `schema:CreativeWork` with mentions rolled up to the accepted plant. Every `plantdb:` fact carries its own label + plain-language definition in the file. `--slice 200` exports just the most-observed plants + edge cases (the prototype) | slice: ~20 s, 2.3 M triples; full run: issue #15 |
| `status.py` | **Quality check**: prints a scorecard of every source file, table, id coverage, and cross-reference (and writes `plant_data/quality_report.md`). Read-only, so run it as often as you like; exit code 1 means something failed | seconds |
| `plant_query.py` | The questions as Python functions: `plants(name_or_id)`, `books(name, dutch=…)`, `origin(name)`, `nl_status(name, status=…)`, `observations(name, area=…, status=…)`, `obs_totals()`, `areas()`. Read-only, resolves names and ids, suggests close names on a typo | about a second per question |

```bash
python download.py        # one-time data setup
python build_index.py     # the cross-reference index
python bhl_fetch.py       # BHL raw data
python bhl_build.py       # literature counts
python build_ranges.py    # native / introduced / invasive per region
python build_books.py     # which books mention which plant (and which are Dutch)
python build_status.py    # origin + native/alien/invasive in the Netherlands
python gbif_nl.py --admin 2   # where each plant was recorded in the NL (asks for a login)
python build_obs.py       # plant × municipality record counts + areas for maps
python build_rdf.py --slice 200   # RDF for SPARQL: a slice first, no flag = everything
python status.py          # what is in there, and did it join well?
```

## Prerequisites

- Python 3.9+ with `pip` (the heavy lifting is done by `duckdb`, `pandas`, `requests`, `ete4`, `pygbif`, `rpy2`)
- R installed (needed for the WCVP data export and the megatree; `rpy2` and the R packages are installed for you)
- ~25–35 GB free disk for `plant_data/`
- Optionally a free [GBIF account](https://www.gbif.org) (`GBIF_USER/GBIF_PWD/GBIF_EMAIL`) for the species-list download — skipped if unset
- **No Wikidata.** The QLever fetch is switched off (`WIKIDATA = False` in `build_index.py`) — see *Parked* below. Everything works from WFO/WCVP/GBIF/NCBI ids and unique name matching.

## Data sources

| Source | How it gets here |
|---|---|
| WFO backbone | manual: download the DwC backbone zip from [Zenodo](https://zenodo.org/records/7460932) and put `classification.csv` in `plant_data/` (the built-in WFO URL is dead) |
| WCVP / POWO | `rWCVPdata` R package, exported to CSV once (names **and** TDWG-level-3 distributions) |
| GBIF backbone | taxonomy archive from `hosted-datasets.gbif.org` |
| NCBI taxonomy | full taxdump, stored in an ETE SQLite DB (`taxa.sqlite`) |
| Phylogenetic megatree | `V.PhyloMaker2` (GBOTB extended) → Newick |
| ~~Wikidata ids + common names~~ | **parked** (issue #2) — the QLever query is written and disabled (`WIKIDATA = False`); those columns stay empty on purpose |
| BHL | monthly TSV export at <https://www.biodiversitylibrary.org/data/TSV/data.zip> |
| GloNAF 2.02 | manual download into `plant_data/glonaf/` (`glonaf_flora2`, `glonaf_taxon_wcvp`, `glonaf_region` are the three files used) |
| SInAS 3.2 | `SInAS_3.2.csv` into `plant_data/sinas/` — [Zenodo 21933976](https://zenodo.org/records/21933976); **space-delimited** despite the `.csv` name, and it covers every kingdom, so only rows whose name is in `plant_index` are kept |
| GRIIS Netherlands | Darwin Core Archive zip into `plant_data/griis/` — [cloud.gbif.org/griis/resource?r=griis-nl](https://cloud.gbif.org/griis/resource?r=griis-nl), unpacked once by `build_ranges.py` |
| GBIF occurrences (NL) | `python gbif_nl.py --admin 2` — GBIF's SQL download API (`api.gbif.org/v1/occurrence/download/request/0018549-...`), species × GADM area aggregated **server-side**. Needs a GBIF login; it prints a DOI on completion — ours is [10.15468/dl.jzdpzg](https://doi.org/10.15468/dl.jzdpzg), 12,310 names × 426 areas, 33,601,025 records, 1510–2026 |
| GADM 4.1 boundaries | free: `gadm41_NLD_{1,2}.json` from [geodata.ucdavis.edu](https://geodata.ucdavis.edu/gadm/gadm4.1/json/) into `plant_data/gadm/`. Level 2 = the 355 municipal areas; a level-3 file does not exist for the Netherlands |
| IPNI ids | extracted from WFO's `scientificNameID` LSIDs — no separate download |
| Dutch imprint places | hand-curated `seed/nl_places.csv` — no source lists where a book was printed, so this list *is* the curation |
| Netherlands region codes | hand-curated `seed/nl_regions.csv` — which code means "the Netherlands" in each source's scheme, and the codes (`NLA`/`ANT`/`CUW`/`SUR`) that must stay out |
| TDWG region names | free: WCVP's own distribution table carries level 3 → level 2 → continent, so `tdwg_regions` is derived, not downloaded |

SInAS and GRIIS checklists round out the distribution data; their download links (and what
they actually contain) are in `sources.md`. Drop the files into `plant_data/sinas/` and
`plant_data/griis/` and `build_ranges.py` picks them up automatically — until then it just
skips them with a friendly message.

What they add, measured: WCVP gives native/introduced for 3,866 rows / 3,548 taxa in the
Netherlands (TDWG level 3 `NET`), GloNAF 1,076 rows / 433 taxa naturalized or invasive
(`NLD`), GRIIS-NL 235 introduced species (`NL`), SInAS 2,380 rows / 2,323 taxa with
first-record years 1599–2019 (`184`). `build_status.py` folds that into **4,806 taxa with
one resolved Netherlands verdict each** — and the sources genuinely disagree for 88 of
them, which stays visible as `conflict` rather than being averaged away. Two honest gaps:
SInAS has a `pathway` column with a proper controlled vocabulary, but every one of its 2,810
filled rows is an **animal** — no plant pathways anywhere — and GRIIS-NL has no pathway
column and no arrival dates (`eventDate` filled for 1 of 376 rows).

## Playing with the result

The shortest route is `plant_query.py` (read-only, so it is safe while a build runs):

```python
from plant_query import plants, books, origin, nl_status

plants("Typha latifolia")                    # ids, family, megatree flag
books("Typha latifolia")                     # every BHL title mentioning it
books("Typha latifolia", dutch=True)         # anything Dutch: 47 titles for Typha
books("Quercus robur", dutch="published")    # printed in the Netherlands only
books("Quercus roburr")                      # LookupError: closest is Quercus robur
origin("Quercus robur")                      # native in 41 TDWG regions, introduced in 19
nl_status("Heracleum mantegazzianum")        # invasive in NL, arrived 1900, 4 sources agree
nl_status(status="invasive")                 # the whole Dutch invasive list: 116 taxa
observations("Heracleum mantegazzianum")     # every municipality it is recorded in + counts
observations(area="Zuid-Holland", status="invasive")  # invasives in one province
observations("Amelanchier lamarckii")        # GBIF-only names work too (no WFO record)
obs_totals("Quercus robur")                  # 353 areas, 185,697 records, its NL extent
areas("amst")                                # find an area: Amsterdam + Amstelveen + ...
```

Or straight SQL (open the DB read-only, or use the Parquet files):

```bash
python -c "import duckdb; duckdb.sql(\"SELECT * FROM 'plant_data/plant_index.parquet' LIMIT 5\").show()"
```

```sql
-- everything the index knows about a genus, including literature counts
SELECT name, gbif_id, ncbi_taxid, wcvp_id, bhl_pages, bhl_first_year
FROM plant_index_bhl WHERE name ILIKE 'Typha%';

-- the most-documented plants in BHL
SELECT name, bhl_pages, bhl_titles, bhl_first_year, bhl_last_year
FROM plant_index_bhl ORDER BY bhl_pages DESC LIMIT 10;

-- how well did the linking work?
SELECT gbif_src, count(*) FROM plant_index GROUP BY 1;

-- where is this species native, and where has it turned up as an alien?
SELECT region_scheme, region_name, status, invasive
FROM ranges WHERE name ILIKE 'Heracleum%mantegazzian%';

-- most invasive species per distribution source
SELECT source, name, count(*) AS regions
FROM ranges WHERE invasive GROUP BY 1, 2 ORDER BY regions DESC LIMIT 10;

-- is it an alien in the Netherlands, and which source says so? the resolved answer:
SELECT name, author, status, status_source, conflict, wcvp_status, glonaf_status,
       sinas_status, griis_status, arrival_year
FROM nl_status WHERE lower(name) = 'rosa rugosa';

-- the underlying rows, if you want to see them per source and region:
-- (region codes only, and paired with their scheme: tdwg3 NET, glonaf NLD, iso NL,
--  sinas 184 — ILIKE '%netherlands%' would also pull in Netherlands Antilles)
SELECT source, region_scheme, region_code, status, invasive, first_record_year, link_src
FROM ranges r
WHERE name = 'Rosa rugosa' AND EXISTS (SELECT 1 FROM (VALUES
    ('tdwg3','NET'), ('glonaf','NLD'), ('iso','NL'), ('sinas','184'))
    v(scheme, code) WHERE v.scheme = r.region_scheme AND v.code = r.region_code);

-- the Dutch alien/invasive list, with arrival dates where a source has them
SELECT name, author, status, arrival_year FROM nl_status
WHERE status <> 'native' ORDER BY arrival_year NULLS LAST, name;

-- which Dutch books mention this plant? use `dutch <> ''`: most books printed in the
-- Netherlands are Latin-language, so filtering LanguageCode = 'DUT' alone misses most
SELECT short_title, author_of_book, language_code, published_place, dutch,
       pages, first_year, last_year
FROM plant_books_full
WHERE nname = 'typha latifolia' AND dutch <> ''
ORDER BY pages DESC;

-- the best-documented plants in Dutch books
SELECT nname, count(DISTINCT title_id) AS dutch_titles, sum(pages) AS pages
FROM plant_books WHERE dutch <> '' GROUP BY 1 ORDER BY dutch_titles DESC LIMIT 10;

-- where in the Netherlands has this plant been recorded, and how often?
SELECT area_name, province, occurrences, first_year, last_year
FROM observations_nl WHERE lower(name) = 'heracleum mantegazzianum' AND in_nl
ORDER BY occurrences DESC;

-- the invasives observed in one province, joined to their NL status and arrival year
SELECT o.name, a.name AS municipality, o.occurrences, s.status, s.arrival_year
FROM observations_nl o JOIN obs_areas a ON a.id = o.area_id
LEFT JOIN nl_status s ON s.key = o.key
WHERE a.province = 'Zuid-Holland' AND s.status = 'invasive'
ORDER BY o.occurrences DESC LIMIT 20;

-- for a map: record counts per municipality with the centroid to place a label on
SELECT a.id, a.name, a.lat, a.lng, sum(o.occurrences) AS records
FROM observations_nl o JOIN obs_areas a ON a.id = o.area_id
WHERE o.in_nl GROUP BY 1, 2, 3, 4 ORDER BY records DESC LIMIT 10;
```

## Re-running, redoing, resetting

- **Re-run anything at any time.** Each step checks for its output file / table first and prints `skip ...` if present. Partial downloads never pollute: they go to `.part` files and are renamed only on success.
- **Redo one step:** delete that step's output (e.g. `rm plant_data/gbif_plantae.tsv`, or `DROP TABLE gbif` in the DuckDB file) and run the script again.
- **Full reload of source tables:** set `REBUILD = True` in `build_index.py`.
- `plant_data/` is git-ignored; delete it wholesale to start from scratch.

## Parked: Wikidata (QLever)

Decided 2026-10-30 — issue #2 is closed *not planned*: **do not fetch Wikidata.** `WIKIDATA = False`
in `build_index.py`, so `fetch_wikidata()` prints a skip line and the Wikidata tables stay empty.
Nothing here needs them: GBIF and NCBI ids come from exact name(+author) matching, and every
question the repo answers is answerable without Wikidata.

`Qleverfile` is kept only as a record of how a local Wikidata index would be built (~110 GB
compressed, ~500 GB index, about a day of work). Switching it back on is a decision to make,
not a bug to fix — `status.py` reports Wikidata as `parked`, never as a gap.

## Known quirks (learned the hard way)

- WFO `classification.csv` contains a few invalid UTF-8 bytes and unbalanced quotes — `build_index.py` cleans a copy once (`wfo_clean.tsv`) rather than dropping rows.
- GBIF moved its backbone to the Catalogue of Life XR in 2026. Our GBIF ids come from the backbone archive, never from Wikidata (parked), so the P846 re-key question does not apply here — but `status.py` still checks that every `gbif_id` in the index actually exists in the `gbif` table.
- Filtering distributions for the Netherlands must use **region codes**: `region_code = 'NET'` for WCVP (TDWG level 3) and `'NLD'` for GloNAF. `ILIKE '%netherlands%'` also returns *Netherlands Antilles* (`NLA` / `ANT`).
- GBIF's SQL download API returns an opaque `specieskey` (`4MDK3`) that has nothing to do with GBIF backbone keys — 0 of 12,310 match `plant_index.gbif_id`. Observation tables link to the index **by name**.
- GBIF normalises its species names, so `quercus robur` and `quercus hispanica` in a download are records of one accepted WFO record; observations must be grouped by `(plant, area)`, not by the download's name, or a municipality gets two rows for one plant.
- `countrycode = 'NL'` on a GBIF record means the *publisher* said Netherlands — 1.07 M of those coordinates actually fall in Belgium or Germany, and 1.99 M records have no coordinates at all. `in_nl` / `area_id IS NULL` flag both.
- WorldFlora's built-in download URL is dead — that's why the WFO backbone comes from Zenodo.
- ETE and rWCVPdata only download/build when their output is missing; later uses of `taxa.sqlite` should pass `update=False`.

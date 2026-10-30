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
```

<sub>\* Wikidata is **parked** (issue #2): the QLever fetch is switched off, so the
Wikidata columns exist but stay empty. Nothing else in the pipeline needs them.</sub>

## What you get

| Output | What it is |
|---|---|
| `plant_data/plant_index.duckdb` | All raw source tables plus the final `plant_index`, `plant_index_bhl` and `ranges` |
| `plant_data/plant_index.parquet` | `plant_index` as a single Parquet file (easy for pandas/polars/DuckDB anywhere) |
| `plant_data/plant_index_bhl` (table + `.parquet`) | `plant_index` enriched with BHL literature counts per name |
| `plant_data/ranges.parquet` | `ranges`: who lives where — native / introduced / naturalized / invasive per taxon × region × source |
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

## The scripts, in the order you'd run them

| Script | What it does | Time / space |
|---|---|---|
| `download.py` | Installs missing Python & R packages; sets up the WFO backbone, V.PhyloMaker2 megatree, ETE/NCBI taxonomy SQLite, GBIF backbone archive, (optionally) a GBIF species list | ~20 GB disk, downloads an hour or two |
| `build_index.py` | Loads every source into DuckDB and builds `plant_index` with all the cross-links | minutes |
| `bhl_fetch.py` | Downloads BHL's monthly TSV export (~2.5 GB zip) and extracts the four tables needed: `pagename`, `page`, `item`, `title` | ~15 GB unpacked |
| `bhl_build.py` | Converts the BHL TSVs to slim Parquet, keeps only names present in `plant_index`, and builds `bhl_name_summary` + `plant_index_bhl` | tens of minutes, DuckDB capped at 4 threads / 8 GB and spills to disk |
| `build_ranges.py` | Stacks the four distribution sources into one long `ranges` table linked back to `plant_index` — by WCVP id, else name+author, else name+author ignoring the parenthetical author, else unique exact name (`link_src` says which). Skips SInAS/GRIIS with a message while their files are missing | seconds to minutes; rebuilt from scratch on every run |
| `status.py` | **Quality check**: prints a scorecard of every source file, table, id coverage, and cross-reference (and writes `plant_data/quality_report.md`). Read-only, so run it as often as you like; exit code 1 means something failed | seconds |

```bash
python download.py        # one-time data setup
python build_index.py     # the cross-reference index
python bhl_fetch.py       # BHL raw data
python bhl_build.py       # literature counts
python build_ranges.py    # native / introduced / invasive per region
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
| IPNI ids | extracted from WFO's `scientificNameID` LSIDs — no separate download |

SInAS and GRIIS checklists round out the distribution data; their download links (and what
they actually contain) are in `sources.md`. Drop the files into `plant_data/sinas/` and
`plant_data/griis/` and `build_ranges.py` picks them up automatically — until then it just
skips them with a friendly message.

What they add, measured: WCVP gives native/introduced for 3,866 taxa in the Netherlands
(TDWG level 3 `NET`), GloNAF 433 naturalized/invasive (`NLD`), GRIIS-NL 235 introduced
species (`NL`), SInAS 2,323 taxa with first-record years 1599–2019 (`184`). Two honest gaps:
SInAS has a `pathway` column with a proper controlled vocabulary, but every one of its 2,810
filled rows is an **animal** — no plant pathways anywhere — and GRIIS-NL has no pathway
column and no arrival dates (`eventDate` filled for 1 of 376 rows).

## Playing with the result

```bash
python -c "import duckdb; duckdb.sql(\"SELECT * FROM 'plant_data/plant_index.parquet' LIMIT 5\").show()"
```

Or some friendly SQL (open the DB read-only, or use the Parquet file):

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

-- is it an alien in the Netherlands, and which source says so?
-- (region codes only: NET = TDWG level 3, NLD = GloNAF, NL = GRIIS, 184 = SInAS;
--  ILIKE '%netherlands%' would also pull in Netherlands Antilles)
SELECT source, region_code, status, invasive, first_record_year, link_src
FROM ranges
WHERE name = 'Rosa rugosa' AND region_code IN ('NET', 'NLD', 'NL', '184');
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
- WorldFlora's built-in download URL is dead — that's why the WFO backbone comes from Zenodo.
- ETE and rWCVPdata only download/build when their output is missing; later uses of `taxa.sqlite` should pass `update=False`.

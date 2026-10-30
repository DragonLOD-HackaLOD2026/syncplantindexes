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
 Wikidata     ─┤                                           megatree tip flag, provenance
 megatree     ─┘
                                     bhl_fetch.py ──► BHL TSV export (4 big tables)
 BHL books    ──── bhl_fetch.py ────► bhl_build.py ──► plant_index_bhl
                                     (DuckDB + Parquet)  + page/title counts + first/last year

 WCVP distributions ─┐
 GloNAF              ─┼──► build_ranges.py ──► ranges    long table: one row per taxon × region
 SInAS (optional)    ─┤      (DuckDB + Parquet)           × source, with native/introduced/
 GRIIS (optional)    ─┘                                  invasive status + first record year
```

## What you get

| Output | What it is |
|---|---|
| `plant_data/plant_index.duckdb` | All raw source tables plus the final `plant_index`, `plant_index_bhl` and `ranges` |
| `plant_data/plant_index.parquet` | `plant_index` as a single Parquet file (easy for pandas/polars/DuckDB anywhere) |
| `plant_data/plant_index_bhl` (table + `.parquet`) | `plant_index` enriched with BHL literature counts per name |
| `plant_data/ranges.parquet` | `ranges`: who lives where — native / introduced / naturalized / invasive per taxon × region × source |
| `plant_data/bhl/*.parquet` | The slim BHL tables (only the columns needed) |

Each `plant_index` row: WFO id, name, author, rank, status, accepted name, family, IPNI id,
WCVP/POWO ids, Wikidata QID, GBIF id, NCBI taxid, `in_megatree` (is it a tip of the
GBOTB megatree?), and `*_src` columns saying **how** each id was found
(`"wikidata"` = linked on the same Wikidata item, `"name"` / `"name+author"` = unique
exact name match — homonyms are never matched by name alone).

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
| `build_ranges.py` | Stacks the distribution sources into one long `ranges` table linked back to `plant_index` (by WCVP id where available, else unique exact name). SInAS and GRIIS are skipped until their files are in `plant_data/` (links in `sources.md`) | seconds to minutes; rebuilt from scratch on every run |

```bash
python download.py        # one-time data setup
python build_index.py     # the cross-reference index
python bhl_fetch.py       # BHL raw data
python bhl_build.py       # literature counts
python build_ranges.py    # native / introduced / invasive per region
```

## Prerequisites

- Python 3.9+ with `pip` (the heavy lifting is done by `duckdb`, `pandas`, `requests`, `ete4`, `pygbif`, `rpy2`)
- R installed (needed for the WCVP data export and the megatree; `rpy2` and the R packages are installed for you)
- ~25–35 GB free disk for `plant_data/`
- Optionally a free [GBIF account](https://www.gbif.org) (`GBIF_USER/GBIF_PWD/GBIF_EMAIL`) for the species-list download — skipped if unset
- Wikidata ids come from a public [QLever](https://qlever.cs.uni-freiburg.de/api/wikidata) endpoint by default; if it's down, the build continues without Wikidata and you can re-run later to add it (or index Wikidata yourself — see below)

## Data sources

| Source | How it gets here |
|---|---|
| WFO backbone | manual: download the DwC backbone zip from [Zenodo](https://zenodo.org/records/7460932) and put `classification.csv` in `plant_data/` (the built-in WFO URL is dead) |
| WCVP / POWO | `rWCVPdata` R package, exported to CSV once (names **and** TDWG-level-3 distributions) |
| GBIF backbone | taxonomy archive from `hosted-datasets.gbif.org` |
| NCBI taxonomy | full taxdump, stored in an ETE SQLite DB (`taxa.sqlite`) |
| Phylogenetic megatree | `V.PhyloMaker2` (GBOTB extended) → Newick |
| Wikidata ids + common names | SPARQL/QLever: items with a WFO (P7715) or IPNI (P961) id, then P225/P405/P5037/P846/P685 and P1843 (English/Dutch common names) |
| BHL | monthly TSV export at <https://www.biodiversitylibrary.org/data/TSV/data.zip> |
| GloNAF 2.02 | manual download into `plant_data/glonaf/` (`glonaf_flora2`, `glonaf_taxon_wcvp`, `glonaf_region` are the three files used) |
| IPNI ids | extracted from WFO's `scientificNameID` LSIDs — no separate download |

SInAS and GRIIS checklists round out the distribution data; their download links are in
`sources.md`. Drop the files into `plant_data/sinas/` and `plant_data/griis/` and
`build_ranges.py` picks them up automatically — until then it just skips them with a
friendly message.

## Playing with the result

```bash
python -c "import duckdb; duckdb.sql(\"SELECT * FROM 'plant_data/plant_index.parquet' LIMIT 5\").show()"
```

Or some friendly SQL (open the DB read-only, or use the Parquet file):

```sql
-- everything the index knows about a genus, including literature counts
SELECT name, gbif_id, ncbi_taxid, wikidata_qid, bhl_pages, bhl_first_year
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
```

## Re-running, redoing, resetting

- **Re-run anything at any time.** Each step checks for its output file / table first and prints `skip ...` if present. Partial downloads never pollute: they go to `.part` files and are renamed only on success.
- **Redo one step:** delete that step's output (e.g. `rm plant_data/gbif_plantae.tsv`, or `DROP TABLE gbif` in the DuckDB file) and run the script again.
- **Full reload of source tables:** set `REBUILD = True` in `build_index.py`.
- `plant_data/` is git-ignored; delete it wholesale to start from scratch.

## Optional: your own Wikidata (QLever)

`Qleverfile` in this repo indexes the complete Wikidata locally (~110 GB compressed,
~500 GB index — one day of work):

```bash
pip install qlever
qlever get-data    # ~7 h
qlever index       # ~5 h
qlever start       # then point QLEVER in build_index.py at http://localhost:7001
```

## Known quirks (learned the hard way)

- WFO `classification.csv` contains a few invalid UTF-8 bytes and unbalanced quotes — `build_index.py` cleans a copy once (`wfo_clean.tsv`) rather than dropping rows.
- GBIF moved its backbone to the Catalogue of Life XR in 2026; Wikidata's GBIF property (P846) refers to the old keys. The index prefers id links where they exist and falls back to unique name(+author) matches, and the `*_src` columns tell you which is which.
- WorldFlora's built-in download URL is dead — that's why the WFO backbone comes from Zenodo.
- ETE and rWCVPdata only download/build when their output is missing; later uses of `taxa.sqlite` should pass `update=False`.

# Data sources

All external sources behind `plant_data/` and the built tables, with URL and license
where known. Empty license cell = still to be looked up.
(Notes on how each source is used live in `sources.md`, `README.md` and `status.py`.)

## Used for RDF
| Source | Files in `plant_data/` | URL | License |
|---|---|---|---|
| **World Flora Online (WFO) Taxonomic Backbone** — the backbone the whole index is keyed on | `classification.csv`, `_DwC_backbone_R.zip` (cleaned copy: `wfo_clean.tsv`) | https://zenodo.org/records/7460932 (the built-in WFO URL is dead) | Public domain (Zenodo record `other-pd`) |
| **WCVP / POWO** — names + TDWG level-3 distributions, via the `rWCVPdata` R package | `wcvp_names.csv`, `wcvp_distributions.csv` | package: https://github.com/matildabrown/rWCVPdata (data from https://powo.science.kew.org) | |
| **GBIF backbone taxonomy** — GBIF ids (Plantae subset: `gbif_plantae.tsv`) | `gbif_backbone.zip`, `gbif_plantae.tsv` | https://hosted-datasets.gbif.org/datasets/backbone/current/backbone.zip | |
| **NCBI taxonomy** — NCBI taxids, via ETE's taxdump SQLite | `taxa.sqlite` (+ `.traverse.pkl`) | taxdump from https://ftp.ncbi.nlm.nih.gov/taxonomy/ (fetched by ETE) | Public domain (US government) |
| **V.PhyloMaker2** (GBOTB extended megatree) — `in_megatree` flag | `megatree_GBOTB.tre` | https://github.com/jinyizju/V.PhyloMaker2 | |
| **Biodiversity Heritage Library (BHL)** — monthly TSV export: page/name hits, titles, items → `plant_index_bhl`, `plant_books` | `bhl/data.zip` → `pagename/page/item/title` (txt + parquet) | https://www.biodiversitylibrary.org/data/TSV/data.zip (schema: https://www.biodiversitylibrary.org/data/TSV/BHLExportSchema.pdf) | |
| **GloNAF 2.02** — naturalized/invasive per region → `ranges`, `nl_status` | `glonaf/` (`glonaf_flora2.csv`, `glonaf_taxon_wcvp.csv`, `glonaf_region.csv` + rest) | https://zenodo.org/records/17105725 | CC BY 4.0 |
| **SInAS 3.2** — native/alien distributions + arrival years → `ranges`, `nl_status` | `sinas/SInAS_3.2.csv` (space-delimited despite `.csv`) | https://zenodo.org/records/21933976 | CC BY 4.0 |
| **GRIIS — Global Register of Introduced and Invasive Species, Netherlands** (DwC-A v1.2, GBIF UUID `6e74aa5e-d156-4fd9-9299-049307da6fe8`) → `ranges`, `nl_status` | `griis/griis-nl.zip` (+ unpacked) | https://cloud.gbif.org/griis/resource?r=griis-nl — direct: https://cloud.gbif.org/griis/archive.do?r=griis-nl&v=1.2 | |
| **GBIF occurrences, Netherlands** — SQL download aggregated server-side (`gbif_nl.py --admin 2`), DOI **10.15468/dl.jzdpzg** (0018549-261023074918325) → `observations_nl`, `nl_totals` | `gbif_nl/0018549-261023074918325.csv` (+ `.zip`, `.doi.txt`) | API: https://api.gbif.org/v1/occurrence/download/request — cite the download: https://doi.org/10.15468/dl.jzdpzg | mixed (per-publisher rights statements inside one download — check the DOI page) |
| **GADM 4.1** — NL administrative boundaries (provinces + 355 gemeenten) → `obs_areas` | `gadm/gadm41_NLD_1.json`, `gadm41_NLD_2.json` (the `_3.json` file is a saved 404 page — it does not exist) | https://geodata.ucdavis.edu/gadm/gadm4.1/json/ | free for non-commercial use |


## Used in the pipeline

| Source | Files in `plant_data/` | URL | License |
|---|---|---|---|
| **World Flora Online (WFO) Taxonomic Backbone** — the backbone the whole index is keyed on | `classification.csv`, `_DwC_backbone_R.zip` (cleaned copy: `wfo_clean.tsv`) | https://zenodo.org/records/7460932 (the built-in WFO URL is dead) | Public domain (Zenodo record `other-pd`) |
| **WCVP / POWO** — names + TDWG level-3 distributions, via the `rWCVPdata` R package | `wcvp_names.csv`, `wcvp_distributions.csv` | package: https://github.com/matildabrown/rWCVPdata (data from https://powo.science.kew.org) | |
| **GBIF backbone taxonomy** — GBIF ids (Plantae subset: `gbif_plantae.tsv`) | `gbif_backbone.zip`, `gbif_plantae.tsv` | https://hosted-datasets.gbif.org/datasets/backbone/current/backbone.zip | |
| **NCBI taxonomy** — NCBI taxids, via ETE's taxdump SQLite | `taxa.sqlite` (+ `.traverse.pkl`) | taxdump from https://ftp.ncbi.nlm.nih.gov/taxonomy/ (fetched by ETE) | Public domain (US government) |
| **V.PhyloMaker2** (GBOTB extended megatree) — `in_megatree` flag | `megatree_GBOTB.tre` | https://github.com/jinyizju/V.PhyloMaker2 | |
| **Biodiversity Heritage Library (BHL)** — monthly TSV export: page/name hits, titles, items → `plant_index_bhl`, `plant_books` | `bhl/data.zip` → `pagename/page/item/title` (txt + parquet) | https://www.biodiversitylibrary.org/data/TSV/data.zip (schema: https://www.biodiversitylibrary.org/data/TSV/BHLExportSchema.pdf) | |
| **GloNAF 2.02** — naturalized/invasive per region → `ranges`, `nl_status` | `glonaf/` (`glonaf_flora2.csv`, `glonaf_taxon_wcvp.csv`, `glonaf_region.csv` + rest) | https://zenodo.org/records/17105725 | CC BY 4.0 |
| **SInAS 3.2** — native/alien distributions + arrival years → `ranges`, `nl_status` | `sinas/SInAS_3.2.csv` (space-delimited despite `.csv`) | https://zenodo.org/records/21933976 | CC BY 4.0 |
| **GRIIS — Global Register of Introduced and Invasive Species, Netherlands** (DwC-A v1.2, GBIF UUID `6e74aa5e-d156-4fd9-9299-049307da6fe8`) → `ranges`, `nl_status` | `griis/griis-nl.zip` (+ unpacked) | https://cloud.gbif.org/griis/resource?r=griis-nl — direct: https://cloud.gbif.org/griis/archive.do?r=griis-nl&v=1.2 | |
| **GBIF occurrences, Netherlands** — SQL download aggregated server-side (`gbif_nl.py --admin 2`), DOI **10.15468/dl.jzdpzg** (0018549-261023074918325) → `observations_nl`, `nl_totals` | `gbif_nl/0018549-261023074918325.csv` (+ `.zip`, `.doi.txt`) | API: https://api.gbif.org/v1/occurrence/download/request — cite the download: https://doi.org/10.15468/dl.jzdpzg | mixed (per-publisher rights statements inside one download — check the DOI page) |
| **GADM 4.1** — NL administrative boundaries (provinces + 355 gemeenten) → `obs_areas` | `gadm/gadm41_NLD_1.json`, `gadm41_NLD_2.json` (the `_3.json` file is a saved 404 page — it does not exist) | https://geodata.ucdavis.edu/gadm/gadm4.1/json/ | free for non-commercial use |
| **Dr. Duke's Phytochemical and Ethnobotanical Databases (USDA)** — ethnobotanical use-claims → `duke.duckdb` (parallel store) | `dr_duke/Duke-Source-CSV.zip` (+ extracted CSVs) | https://phytochem.nal.usda.gov/ — catalog: https://catalog.data.gov/dataset/dr-dukes-phytochemical-and-ethnobotanical-databases | Public domain (US government work) |
| **Flora Batava (1800–1934)** — Dutch illustrated flora records. ⚠ Provenance of our exact file is *not documented in the repo*: columns match the published "Flora Batava: people, plants, locations" dataset (Teixeira-Costa et al.), ours additionally carries `species.wikidata`, Dutch names and plate-image URLs | `flora-data-with-plates-and-names.csv` | published dataset: https://doi.org/10.5281/zenodo.17713394 (also https://doi.org/10.5281/zenodo.18375794; IPT: https://www.verspreidingsatlas.nl/ipt/resource?r=flora-batava) | CC BY 4.0 for the published dataset — **verify for our enriched variant** |

## Downloaded but parked (not in any output table)

| Source | Files in `plant_data/` | URL | License |
|---|---|---|---|
| **Wikidata dumps** (`latest-all`, `latest-lexemes`) | `latest-all.ttl.bz2`, `latest-lexemes.ttl.bz2` (+ `wikidata.download-log.txt`) | https://dumps.wikimedia.org | CC0 |
| Wikidata via QLever — query written but disabled (`WIKIDATA = False`, issue #2) | — | https://qlever.cs.uni-freiburg.de/api/wikidata (see `Qleverfile`) | CC0 (the underlying data) |
| **The LOTUS Initiative — Wikidata query results** (chemical data) | `lotus/` (`taxa.tsv`, `compounds.tsv`, `references.tsv`, `compound_reference_taxon.tsv`) | https://zenodo.org/records/5668855 | CC BY 4.0 |
| **Amidha Ayurveda Herb Database v2.0** | `herb-database-main/herb.json` | https://github.com/sciencewithsaucee-sudo/herb-database — Zenodo DOI 10.5281/zenodo.20581467 | CC BY 4.0 (repo LICENSE + CITATION.cff) |
| **Plants For A Future (PFAF)** — unofficial SQLite snapshot scraped from the PFAF site | — (not currently in `plant_data/`) | https://github.com/saulshanabrook/pfaf-data | repo code is MIT, but the PFAF data itself is subject to PFAF's own copyright terms |

## Made by us (no external source)

- `seed/nl_places.csv`, `seed/nl_regions.csv` — hand-curated, committed in the repo.
- `tdwg_regions` — derived from WCVP's own distribution table, not downloaded.
- IPNI ids — extracted from WFO's `scientificNameID` LSIDs, no separate download.

## Licenses still to look up

- WCVP / POWO (via `rWCVPdata`)
- GBIF backbone taxonomy
- V.PhyloMaker2 (no license declared on the GitHub repo)
- BHL TSV export
- GRIIS Netherlands
- GBIF NL occurrence download 10.15468/dl.jzdpzg (per-dataset rights)
- Flora Batava enriched variant (published base dataset is CC BY 4.0)

Global Naturalized Alien Flora (GloNAF). Open access data to support research on understanding global plant invasions.
https://zenodo.org/records/17105725
Used: glonaf_flora2.csv (region x taxon status), glonaf_taxon_wcvp.csv, glonaf_region.csv.
Records are per *synonym*, so one accepted taxon x region can appear many times and the
statuses can disagree (naturalized vs invasive) - aggregate when deriving a status table.
Its WCVP ids are from an older WCVP release, so ~3.5% do not resolve and fall back to names.

SInAS: A global dataset of native and alien distributions of alien species
https://zenodo.org/records/21933976
Used: SInAS_3.2.csv (125 MB) - **space-delimited with quoted fields**, despite the .csv name.
425,601 rows, every kingdom; 4,683 for the Netherlands (locationID 184), 228,519 rows are
plant names we can link. eventDate gives first-record years (1,599-2019 for NL, filled for
about 10% of NL rows). The pathway column uses a proper vocabulary (Huderle et al.) but is
filled for 2,810 rows and **every one of them is an animal** - no plant pathways at all.
Also available: SInAS_Locations_v3.2.zip (TDWG region codes for its locations).

Global Register of Introduced and Invasive Species - Netherlands 
https://cloud.gbif.org/griis/resource?r=griis-nl
Direct archive: https://cloud.gbif.org/griis/archive.do?r=griis-nl&v=1.2 (22 KB, GBIF UUID
6e74aa5e-d156-4fd9-9299-049307da6fe8, v1.2 published 2026-08-23). DwC-A: taxon.txt,
distribution.txt, speciesprofile.txt, 376 records, of which 235 are plants (209 accepted,
26 synonyms; all species rank). establishmentMeans = introduced (370) / cryptogenic|uncertain (6);
isInvasive = invasive for 61. **eventDate is filled for 1 of 376 rows**, so no arrival years,
and there is **no pathway column** - the metadata says pathways are only "planned".
Names carry authorship ("Acer negundo L.") and the file contains doubly-encoded UTF-8
("LidÃ©n"), which does not affect name matching.

Plants For A Future (PFAF) Data
An unofficial SQLite snapshot scraped from the PFAF website. The data and images are subject to PFAF’s copyright terms.
https://github.com/saulshanabrook/pfaf-data

The LOTUS Initiative for Open Natural Products Research: wikidata query results
https://zenodo.org/records/5668855

Amidha Ayurveda Herb Database v2.0
https://github.com/sciencewithsaucee-sudo/herb-database

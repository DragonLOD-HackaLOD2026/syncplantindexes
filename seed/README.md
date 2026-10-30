Hand-curated reference tables that the build scripts read.

These are **inputs we wrote by hand**, not downloads, so they are committed on
purpose. Anything downloaded or generated belongs in `plant_data/`, which is
gitignored and rebuildable from scratch.

| file | read by | what it is |
|---|---|---|
| `nl_places.csv` | `build_books.py` | imprint places that count as "printed in the Netherlands" for the `dutch_published` flag. `pattern` is matched case-insensitively against BHL's `PublicationDetails`, `place_en` is the modern English name it is reported under, `note` explains the Latin form or a deliberate exclusion |

Two rules that keep the flag honest:

- **Latin forms matter.** Early Dutch imprints are printed in Latin — *Amstelodami*,
  *Lugduni Batavorum*, *Trajecti*, *Hagae Comitis*. A list of modern Dutch city names
  alone finds almost none of them.
- **The modern Netherlands only.** *Antverpiae*/Antwerp is excluded (Spanish Netherlands),
  and bare *Lugduni* is excluded because that is Lyon — only *Lugduni Batavorum* counts.
  *Hafniae* (Copenhagen), *Lipsiae* (Leipzig), *Londini*, *Parisiis* are excluded too.

Edit a list and rerun the script that uses it — the builds are idempotent and a rebuild
of `plant_books` takes about three minutes. Issue #6 will add `pathways_nl.csv` here:
no distribution source carries introduction pathways for plants, so that table is
necessarily curated, and every row in it must carry a citation.

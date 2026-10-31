Hand-curated reference tables that the build scripts read.

These are **inputs we wrote by hand**, not downloads, so they are committed on
purpose. Anything downloaded or generated belongs in `plant_data/`, which is
gitignored and rebuildable from scratch.

| file | read by | what it is |
|---|---|---|
| `nl_places.csv` | `build_books.py` | imprint places that count as "printed in the Netherlands" for the `dutch_published` flag. `pattern` is matched case-insensitively against BHL's `PublicationDetails`, `place_en` is the modern English name it is reported under, `note` explains the Latin form or a deliberate exclusion |
| `nl_regions.csv` | `build_status.py`, `status.py` | which region code means "the Netherlands" in each source's region scheme (`tdwg3:NET`, `glonaf:NLD`, `iso:NL`, `sinas:184`), plus the codes that must **not** be swept in with it — `NLA`/`ANT`/`CUW` (Netherlands Antilles, 1,068 WCVP rows) and `SUR` (Suriname). One row per (scheme, code), with `include` yes/no, so every query uses the same definition |
| `nl_regions.csv` | `build_status.py` | which region code means "the Netherlands" in each source's scheme (`tdwg3: NET`, `glonaf: NLD`, `iso: NL`, `sinas: 184`), and the codes that must **not** be swept in with it (`NLA`/`ANT`/`CUW` = Netherlands Antilles, `SUR` = Suriname). Every row carries `include` yes/no, so one definition is shared by all queries |

Two rules that keep the flag honest:

- **Latin forms matter.** Early Dutch imprints are printed in Latin — *Amstelodami*,
  *Lugduni Batavorum*, *Trajecti*, *Hagae Comitis*. A list of modern Dutch city names
  alone finds almost none of them.
- **The modern Netherlands only.** *Antverpiae*/Antwerp is excluded (Spanish Netherlands),
  and bare *Lugduni* is excluded because that is Lyon — only *Lugduni Batavorum* counts.
  *Hafniae* (Copenhagen), *Lipsiae* (Leipzig), *Londini*, *Parisiis* are excluded too.
- **A region code means nothing without its scheme.** `ANT` is Netherlands Antilles in
  GloNAF and Antarctica in TDWG3; `8` is Curaçao in SInAS's numbered list. Every filter
  matches the `(region_scheme, code)` pair, and `ILIKE '%netherlands%'` is never used —
  it silently adds the 1,068 Netherlands Antilles rows.
- **Region filters need the scheme *and* the code.** The same letters mean different places
  in different sources: `ANT` is Netherlands Antilles in GloNAF and Antarctica in TDWG3,
  and a bare `ILIKE '%netherlands%'` silently adds 1,068 Netherlands Antilles rows. That is
  why `nl_regions.csv` rows are (scheme, code) pairs.

Edit a list and rerun the script that uses it — the builds are idempotent and a rebuild
of `plant_books` takes about three minutes. Issue #6 will add `pathways_nl.csv` here:
no distribution source carries introduction pathways for plants, so that table is
necessarily curated, and every row in it must carry a citation.

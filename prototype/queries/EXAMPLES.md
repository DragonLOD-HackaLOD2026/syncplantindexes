# Example answers to the three app queries

These tables are **actual query results** (generated 2026-10-31 from
this export, Oxigraph reference engine). Row counts are identical in
the full files; only the engine differs. Query texts are in
`A.rq` / `B.rq` / `C.rq` - substitution points in `{braces}`.

## A - all facts for one plant: `Crassula helmsii`

45 rows back (shown: a representative selection). The NL status
comes back as the term's **English label**, not a URI. Looking this
plant up by one of its synonyms instead - e.g.
`"Crassula recurva (Hook.f.) Ostenf."` (a `skos:altLabel`) - returns
the same 45 rows: one entity answers every name it goes by.

```
| field | label | value |
|---|---|---|
| nlAreaCount | Areas seen in NL | 258 |
| bhlTitleCount | Books mentioning this plant | 26 |
| firstObservationYear | First NL observation | 1995 |
| introducedRegionCount | Introduced region count | 0 |
| invasive | Invasive in NL | true |
| nativeRegionCount | Native region count | 0 |
| normalizedName | Normalised name | crassula helmsii |
| nlOccurrenceCount | Observations in NL | 8039 |
| bhlPageMentions | Page mentions in books | 71 |
| nlStatus | Status in the Netherlands | Invasive in NL |
| family | http://rs.tdwg.org/dwc/terms/family | Crassulaceae |
| scientificName | http://rs.tdwg.org/dwc/terms/scientificName | Crassula helmsii |
| scientificNameAuthorship | http://rs.tdwg.org/dwc/terms/scientificNameAuthorship | (Kirk) Cockayne |
| taxonRank | http://rs.tdwg.org/dwc/terms/taxonRank | species |
| identifier | https://schema.org/identifier | 300471 |
| identifier | https://schema.org/identifier | 77199135-1 |
... 45 rows total; took 900 ms
```

## B - prevalence map: `Heracleum mantegazzianum`

Exactly **355 rows** - every Dutch gemeente, never-seen areas
included with `0` counts and `0.000000` score (a plant with a long
tail of zeros: `amelanchier lamarckii`, lower-case on purpose - the
name match is case-insensitive and also matches synonyms).

| **areaName** | **province** | **occurrences** | **areaTotal** | **score** |
|---|---|---|---|---|
| Lelystad | Flevoland | 1141 | 56470 | 0.020205 |
| Huizen | Noord-Holland | 252 | 13658 | 0.018451 |
| Zeewolde | Flevoland | 390 | 34779 | 0.011214 |
| Pekela | Groningen | 63 | 6129 | 0.010279 |
| Rotterdam | Zuid-Holland | 3507 | 417812 | 0.008394 |
| Dronten | Flevoland | 353 | 51860 | 0.006807 |
| Almere | Flevoland | 404 | 60013 | 0.006732 |
| Vlaardingen | Zuid-Holland | 533 | 88352 | 0.006033 |
| Haarlem | Noord-Holland | 289 | 53040 | 0.005449 |
| Middelburg | Zeeland | 354 | 68837 | 0.005143 |
| ... | | | | |
| Opmeer | Noord-Holland | 0 | 2159 | 0.000000 |
| Papendrecht | Zuid-Holland | 0 | 7906 | 0.000000 |
| Simpelveld | Limburg | 0 | 7489 | 0.000000 |

355 rows total; took 3865 ms

## C - books mentioning `Quercus robur`, Dutch only (`{DUTCH}` = true)

**34 rows** with the filter on, **2,302** with `{DUTCH}` = false.
The Dutch filter is part of the query (`plantdb:dutchRelevance`),
and every citation carries its BHL link.

| **pages** | **title** | **author** | **yearFrom** | **yearTo** | **bhlUrl** |
|---|---|---|---|---|---|
| 31 | Verzamelde geschriften van M. W. Beijerinck ter gelegenheid van zijn 70sten verjaardag, met medewerking der Ne |  | 1921 |  | https://www.biodiversitylibrary.org/bibliography/1479 |
| 20 | Nederlandsch kruidkundig archief |  | 1846 | 1951 | https://www.biodiversitylibrary.org/bibliography/3629 |
| 15 | Archives néerlandaises des sciences exactes et naturelles |  | 1866 | 1911 | https://www.biodiversitylibrary.org/bibliography/82374 |
| 10 | Tijdschrift voor entomologie |  | 1857 |  | https://www.biodiversitylibrary.org/bibliography/10088 |
| 7 | Album der Natuur |  |  |  | https://www.biodiversitylibrary.org/bibliography/16227 |
| 7 | Tijdschrift over plantenziekten |  | 1895 | 1956 | https://www.biodiversitylibrary.org/bibliography/15200 |
| 6 | Verhandelingen der Koninklijke Akademie van Wetenschappen |  | 1854 | 1892 | https://www.biodiversitylibrary.org/bibliography/8938 |
| ... 34 rows total ... | | | | | |

Edge case worth knowing: `Crassula helmsii` with `{DUTCH}` = true
returns **0 rows** - legitimately (26 books mention it, none Dutch).

took 14271 ms / 14347 ms

## Notes on these examples (read before wiring the app)

- **The three queries are verified correct**, not just plausible:
  A = 45 facts, B = exactly 355 rows, C = 34 Dutch / 2,302 total for
  Quercus robur - every number here was checked set-identical against
  the source database (golden fixtures in the source repo).
- One claim was **wrong in an earlier draft of this page**: Crassula
  helmsii is *not* found via a synonym label - its own
  `dwc:scientificName` matches directly. The synonym route works too
  (see A above); both were re-checked against the data.
- In query A the `?fieldLabel` column falls back to the full
  predicate URI for standard terms (`dwc:`, `schema:`); only our
  `plantdb:` fields carry English + Dutch labels in the graph. The
  app should expect both shapes.
- In query C `?author` is OPTIONAL and frequently empty: most Dutch
  hits are journals and proceedings, which have no personal author.
- Timings are Oxigraph on the development slice - a correctness
  reference, **not** a QLever performance benchmark.


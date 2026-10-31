# Plant RDF - hand-over guide

The four `.nt` files in this folder **are the deliverable**. They are plain
N-Triples (SPARQL standard format, the native input format of QLever - no
conversion needed) and contain the complete computed plant index: identity
and taxonomy, literature counts from BHL, origin, Netherlands status, and
per-gemeente observation facts. This document tells the **QLever people**
how to load and serve the data (§1-2) and the **app developers** exactly
which queries to send and what they answer (§3-5).

Export date and exact counts are in `manifest.json`. Everything below was
measured on this export, not estimated.

| file | contains | size |
|---|---|---|
| `plants.nt` | plant entities, synonyms, origin, NL status, per-plant totals + the self-describing vocabulary | 2.2 GB |
| `areas.nt` | the 355 Dutch gemeenten | 0.6 MB |
| `observations.nt` | plant x gemeente observation facts (full grid, zeros included) | 4.4 GB |
| `books.nt` | BHL titles + plant-mention edges with page counts | 12.4 GB |
| `manifest.json` | machine-readable counts for this exact export | - |
| `slice.nt` | small development slice - NOT part of the hand-over, ignore it | - |

Totals: **122,563,663 triples** - 639,573 plants, 150,175 books,
19,234,994 mentions, 4,297,630 observation facts, 355 areas,
1,026,257 synonym labels.

## 1. Loading into QLever

From inside this folder, with the QLever CLI (`pip install qlever`):

```
qlever index --name plantdb --format nt \
  --input-file plants.nt --input-file areas.nt \
  --input-file observations.nt --input-file books.nt \
  --server-port 7350
qlever start -d plantdb        # UI + SPARQL endpoint at http://localhost:7350
```

Sizing guidance: ~19 GB of N-Triples; plan for an index roughly the size of
the raw files on disk, and give the indexer as much RAM as you can spare
(`--stxxl-memory` if you must constrain it). A text index (`--text-index`,
languages `en` and `nl`) is optional and only enables QLever's fuzzy
`MATCH` bonus - no query in this document depends on it.

## 2. Data model in one page

| thing | RDF class | URI pattern | count |
|---|---|---|---|
| plant | `dwc:Taxon` + `schema:BioChemEntity` | `https://worldfloraonline.org/taxon/wfo-…` or `https://plantdb.example.org/taxon/<key>` | 639,573 |
| gemeente | `schema:Place` | `https://plantdb.example.org/area/<id>` | 355 |
| observation fact | `qb:Observation` | `https://plantdb.example.org/obs/<plant-key>/<area-id>` | 4,297,630 |
| book | `schema:CreativeWork` | `https://www.biodiversitylibrary.org/bibliography/<title-id>` | 150,175 |
| plant-in-book mention | `plantdb:TaxonMention` | `https://plantdb.example.org/mention/<plant-key>/<title-id>` | 19,234,994 |
| NL status term | `schema:DefinedTerm` | `https://plantdb.example.org/status/<term>` | 8 |

Every plant answers to three name forms (always search all three):
`dwc:scientificName` (as displayed), `plantdb:normalizedName` (lower-case),
and `skos:altLabel` (its synonyms - a synonym is a name on the accepted
plant, never a separate entity).

External ids on plants: `owl:sameAs` to WFO / GBIF / NCBI / IPNI / POWO plus
`schema:identifier` literals.

**The vocabulary describes itself.** Every `plantdb:` predicate carries an
English and Dutch `rdfs:label` and an English `skos:definition` inside
`plants.nt` - the app can render field tooltips straight from the graph:

```sparql
SELECT ?predicate ?label ?definition WHERE {
  ?predicate rdfs:label ?label ; skos:definition ?definition .
  FILTER (lang(?label) = "en")
}
```

## 3. The three app queries

They are plain **SPARQL 1.1** - no vendor extensions, identical results on
Oxigraph, jena, or QLever. Canonical copies are in the `queries/` folder
next to this document; `{NAME}` / `{DUTCH}` mark the substitution points.
Expected sizes below were verified against the source database.

### Query A - all facts about one plant (`queries/A.rq`)

One row per fact: predicate, English label, value. Identity, external ids,
origin, NL status (returned as the term's label, not a bare URI), BHL
counts, NL observation totals. Lookup is case-insensitive and also matches
synonyms ("Crassula recurva (Hook.f.) Ostenf.", a synonym of Crassula
helmsii, returns the same 45 facts). Expected on this export: 45 facts for
"Crassula helmsii" (verified).

```sparql
PREFIX rdf:    <http://www.w3.org/1999-02-22-rdf-syntax-ns#>
PREFIX rdfs:   <http://www.w3.org/2000/01/rdf-schema#>
PREFIX skos:   <http://www.w3.org/2004/02/skos/core#>
PREFIX dwc:    <http://rs.tdwg.org/dwc/terms/>
PREFIX plantdb: <https://plantdb.example.org/def/>
SELECT DISTINCT ?predicate ?fieldLabel ?valueOut WHERE {
  VALUES ?lookup { "Crassula helmsii" }
  ?plant (dwc:scientificName | plantdb:normalizedName | skos:altLabel) ?found .
  FILTER (lcase(str(?found)) = lcase(?lookup))
  ?plant ?predicate ?value .
  FILTER (?predicate != rdf:type)
  OPTIONAL { ?predicate rdfs:label ?pl FILTER (lang(?pl) = 'en') }
  OPTIONAL { ?value rdfs:label ?vl FILTER (lang(?vl) = 'en') }
  BIND(COALESCE(STR(?vl), STR(?value)) AS ?valueOut)
  BIND(COALESCE(STR(?pl), STR(?predicate)) AS ?fieldLabel)
}
ORDER BY ?fieldLabel
```

If the name is unknown or misspelled, A returns zero rows - use §4 first.

### Query B - prevalence map: all 355 gemeenten for one plant (`queries/B.rq`)

Returns **every** gemeente, including where the plant was never seen
(occurrences 0, score 0). `?score` is pre-computed: occurrences divided by
the area's total plant records, 0-1, rounded to 6 decimals; `?score > 0`
exactly when `?occurrences > 0`. Expected: exactly 355 rows for any known
plant; e.g. Heracleum mantegazzianum nonzero in 349 of 355, amelanchier
lamarckii nonzero in 334 of 355 (lower-case name also works). Sample rows
for both are in `queries/EXAMPLES.md`.

```sparql
PREFIX qb:     <http://purl.org/linked-data/cube#>
PREFIX schema: <https://schema.org/>
PREFIX dwc:    <http://rs.tdwg.org/dwc/terms/>
PREFIX skos:   <http://www.w3.org/2004/02/skos/core#>
PREFIX plantdb: <https://plantdb.example.org/def/>
SELECT DISTINCT ?areaName ?province ?occurrences ?areaTotal ?score WHERE {
  VALUES ?lookup { "Heracleum mantegazzianum" }
  ?plant (dwc:scientificName | plantdb:normalizedName | skos:altLabel) ?found .
  FILTER (lcase(str(?found)) = lcase(?lookup))
  ?fact a qb:Observation ;
        plantdb:aboutPlant ?plant ;
        plantdb:observedIn ?area ;
        plantdb:occurrences ?occurrences ;
        plantdb:prevalenceScore ?score .
  ?area schema:name ?areaName ;
        schema:administrativeArea ?province ;
        plantdb:areaTotalOccurrences ?areaTotal .
}
ORDER BY DESC(?score) ?areaName
```

### Query C - books mentioning a plant, Dutch filter inside the query (`queries/C.rq`)

The citation includes its BHL link (`?bhlUrl`). `?dutchOnly = true` keeps
titles flagged Dutch (written in Dutch OR published in the Netherlands);
`false` keeps everything - the flag lives in the data
(`plantdb:dutchRelevance`), so the app decides at query time. Expected on
this export: Quercus robur **2,302 books / 34 Dutch**; Heracleum
mantegazzianum **134 / 1**; Crassula helmsii **26 / 0** (an empty Dutch
answer is legitimate and common).

```sparql
PREFIX schema: <https://schema.org/>
PREFIX dwc:    <http://rs.tdwg.org/dwc/terms/>
PREFIX skos:   <http://www.w3.org/2004/02/skos/core#>
PREFIX plantdb: <https://plantdb.example.org/def/>
SELECT DISTINCT ?pages ?title ?author ?yearFrom ?yearTo ?bhlUrl WHERE {
  VALUES ?lookup { "Quercus robur" }
  VALUES ?dutchOnly { true }        # or false
  ?plant (dwc:scientificName | plantdb:normalizedName | skos:altLabel) ?found .
  FILTER (lcase(str(?found)) = lcase(?lookup))
  ?mention a plantdb:TaxonMention ;
           plantdb:aboutPlant ?plant ;
           plantdb:mentionsBook ?book ;
           plantdb:pages ?pages .
  ?book schema:name ?title ;
        plantdb:dutchRelevance ?dutch .
  FILTER (?dutch = ?dutchOnly || ?dutchOnly = false)
  OPTIONAL { ?book plantdb:authorOfBook ?author }
  OPTIONAL { ?book plantdb:coverageStartYear ?yearFrom }
  OPTIONAL { ?book plantdb:coverageEndYear ?yearTo }
  OPTIONAL { ?book schema:url ?bhlUrl }
}
ORDER BY DESC(?pages) ?title
```

## 4. Did-you-mean before A/B/C (`queries/didyoumean.rq`)

Portable substring search over every name form. Returns candidate plant
URIs to plug back into A/B/C. Expected: fragment "helmsii" -> 44 name hits.

```sparql
PREFIX dwc:    <http://rs.tdwg.org/dwc/terms/>
PREFIX skos:   <http://www.w3.org/2004/02/skos/core#>
PREFIX plantdb: <https://plantdb.example.org/def/>
SELECT DISTINCT ?plant ?name WHERE {
  VALUES ?fragment { "helmsii" }
  ?plant (dwc:scientificName | plantdb:normalizedName | skos:altLabel) ?name .
  FILTER (CONTAINS(lcase(str(?name)), lcase(?fragment)))
}
LIMIT 200
```

With a QLever text index, `MATCH (?plant, "crassula helmsi", ?score)` is a
drop-in fuzzy upgrade of this one query. The query above must work with or
without the text index - it is the contract.

## 5. Semantics and gotchas (read once before writing the app)

- **Names are case-insensitive** in the query pattern (`lcase`), and a
  user-typed synonym resolves to the accepted plant. A plant that is not
  in WFO (some GBIF-only observation names) still exists as an entity
  under `plantdb.example.org/taxon/...`.
- **prevalenceScore** is share of ALL plant observations in that gemeente,
  not of area size. Zero occurrences <=> score 0. `?areaTotal` is the
  denominator if the app wants to show raw context.
- **Pages**: one integer per (plant, book) mention, summed over copies;
  the per-plant `plantdb:bhlTitleCount`/`bhlPageMentions` facts agree with
  what C returns (same underlying edges).
- **Status vocabulary**: `plantdb:nlStatus` points at one of 8
  `schema:DefinedTerm`s under
  `https://plantdb.example.org/vocabulary/nl-status` - labels/definitions
  in the graph, never hardcode the list in the app.
- Language tags: area names `@nl`, book titles `@mul`, vocabulary labels
  `@en`/`@nl`, plant names untagged.
- Deliberate scope choices, not bugs: synonyms are never entities; book
  mentions of synonyms roll up to the accepted plant; 4,281 BHL titles
  that only occur on unlinked name rows are not exported; foreign areas
  appear only as counts on plants, only the 355 Dutch gemeenten are
  `schema:Place` nodes.

## 6. What was validated before hand-over

- SHACL conformance against the Netherlands Digital Heritage Schema.org
  profile + query-contract shapes: 0 violations (slice run, `shacl/` +
  `validate_rdf.py` in the source repo).
- Every query answer verified set-identical against the source database
  (golden fixtures): B 355/355 rows for two plants, C 34/134/0 Dutch
  splits, A fact counts, pages totals (64,813,736 page mentions exactly),
  cross-file referential integrity (every mention/observation resolves to
  an exported plant/book/area).
- Count reconciliation against independently measured database numbers:
  plants 639,573, mentions 19,234,994, observations 12,106 x 355,
  books 150,175 (see issue #15: the 154,456 figure in the issue included
  4,281 titles that exist only on unlinked rows, excluded by design).

Regenerating the whole export from the source database is one command in
the source repo: `python3 build_rdf.py --full` (~4 minutes).

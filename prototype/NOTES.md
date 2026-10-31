# Query prototype notes (issue #13)

The three app queries run locally against the slice export through Oxigraph
(in-memory, `pip install pyoxigraph`) - no server. Every answer is compared
against the same number computed from DuckDB directly, so "the RDF is right"
means "SPARQL over the .nt == ground truth", and a later QLever serving the
same files must answer identically (the queries are plain SPARQL 1.1).

## How to run

    python3 build_rdf.py --slice 200 --force    # rebuild the slice export
    python3 validate_rdf.py                     # SHACL + contracts (issue #12)
    python3 prototype/run_queries.py            # all cases green, or exit 1
    python3 prototype/run_queries.py --only B_heracleum   # one case
    python3 prototype/run_queries.py --show B   # print an answer

`--update` regenerates the golden fixtures in `prototype/expected/` from the
DuckDB ground truth; only do that together with reviewing the diff.

## Slice results (2026-10-31, plant_data/rdf/slice.nt)

2,320,939 unique triples as loaded (the exporter counts 2,321,129 emitted
lines; ~190 identical duplicate lines collapse under RDF set semantics).

| check | rows | time |
|---|---|---|
| load 2.3M triples (Oxigraph) | - | 4 s |
| A_crassula (45 facts, spot-checked vs DuckDB) | 45 | 0.6 s |
| B_heracleum / B_amelanchier (355 areas, zeros included) | 355 | 2.5 s |
| C_quercus_dutch (34) / C_heracleum_all (134) / C_crassula_dutch (0) | - | 9-12 s |
| didyoumean "quercus robur" (56 suggestions, set-equal to DuckDB) | 56 | <1 s |

Edge cases covered: Heracleum (355/355 facts), Amelanchier lamarckii (name-key
entity: looked up by a name WFO lists as a synonym), Quercus robur (201
synonyms; rolled-up synonym mentions present), Crassula helmsii (Dutch filter
legitimately returns 0 books), empty did-you-mean fragments (empty result, no
crash - `LIMIT 200` caps suggestions).

Comparison method: answers are compared as SETS in a harness-chosen order
(engines order language-tagged strings differently; the ORDER BY inside the
.rq files is for the app, not the contract). Literals are normalised: 6-decimal
decimals, integers unquoted, unbound OPTIONALs as empty strings.

## Full-scale projection (live table sizes, measured today)

Measured now, not projected:

    mention edges (synonyms rolled up, deduped)     19,234,994
    plant_books_full rows                         28,534,974   (5,040,596 NULL-wfo, not exported)
    observation rows                                 528,916
    plant entities (nl_totals)                       12,106
    BHL title nodes exported                         ~154,500  (of 205,133 rows)
    areas 355 NL + 71 foreign

Triple budget:

    mentions  19.23M edges x 4 (type, about, book, pages)   ~ 76.9M
    obs facts 12,106 entities x 355 areas x 6 triples        ~ 25.8M
              (deliberate zero-filled grid, so Query B is one join)
    books     ~154.5k titles x ~10                          ~  1.5M
    plants    ~12.1k entities x ~40 (facts + synonym labels) ~  0.5M
    areas     426 x ~12                                     ~  5k
    ------------------------------------------------ total   ~105M triples (~6-8 GB .nt)

Load: Oxigraph took 4 s for 2.3M; linear scaling says ~3 min and roughly
10 GB RAM for 105M in memory. That is fine for validation on a big box, but
serving stays on QLever (disk-backed, text index) - outside this repo.

Query times at full scale: A and B scale with one plant's rows (seconds).
C is the worst: the mention join touched 334k edges in 9 s on the slice,
19.2M edges at full scale - Oxigraph in memory would take minutes, QLever's
indexed engine is the answer. If C is too slow anywhere, the fix is at
serving time (index/planning), not in the data: the query is already the
simplest shape (star + one OPTIONAL set).

## Engine-neutral vs QLever-specific

Neutral (must run anywhere, and do on Oxigraph): property-path alternation
for name lookup, VALUES, COALESCE, lcase/str, CONTAINS, ORDER BY, OPTIONAL.

QLever-specific bonus (never in these files): the fuzzy MATCH operator.
The portable did-you-mean floor is `didyoumean.rq` (case-insensitive
CONTAINS over every label). QLever MATCH, where the dump is eventually
served, is a serving-layer decision outside this repo.

## What re-validation is expected at full scale (input for #15)

- `validate_rdf.py` batched checks: O(graph) via Oxigraph, minutes at 105M - fine.
- pySHACL part: rdflib-bound and slow (137 s for 2.3M, super-linear); at full
  scale validate a LIMIT-ed slice instead (issue #12 decision: no sharding).
- run_queries.py cases are name-keyed, so they run unchanged at full scale.

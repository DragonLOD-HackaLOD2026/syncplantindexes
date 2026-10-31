#!/usr/bin/env python3
"""Validate an N-Triples export against SHACL, locally, no server (issue #12).

Checks, fastest first:
  1. Prevalence contract as ONE batched SPARQL query via Oxigraph (Rust):
     score = occurrences / area total, and never-seen <=> score 0.
     (shacl/plantdb-components.ttl contains the same rules as real SHACL
     constraint components; per-node SHACL execution would mean one SPARQL
     query per observation, which crawls - so the big graph gets the batched
     form, and the self-test exercises the component form.)
  2. Self-description via SPARQL: every plantdb: predicate USED in the data
     carries rdfs:label and skos:definition; every status term has a label;
     no schema:Person / schema:Organization nodes anywhere (authors are
     plain literals by design, so those NDE shapes must stay silent).
  3. pySHACL (rdflib): the vendored NDE SCHEMA-AP shapes plus our query-
     contract shapes (shacl/plantdb-shapes.ttl).

python validate_rdf.py                 # validates plant_data/rdf/slice.nt
python validate_rdf.py --graph FILE    # validate another .nt (sample later)
python validate_rdf.py --self-test     # the broken example MUST fail: proves
                                       # the harness actually catches problems
"""

import argparse
import sys
import time

import pyoxigraph
from rdflib import Graph, RDF, RDFS, SKOS, URIRef

DATA = "plant_data/rdf/slice.nt"
SHAPES = ["shacl/nde-schema-ap.ttl", "shacl/plantdb-shapes.ttl"]
COMPONENTS = ["shacl/plantdb-components.ttl"]  # only for the self-test
PLANTDB = "https://plantdb.example.org/def/"
SCHEMA = "https://schema.org/"

# what shacl/examples/bad.nt plants, by property path - the self-test fails if
# any of these does not produce a violation (a harness that only passes is
# worthless)
EXPECTED_BAD_PATHS = {
    SCHEMA + "name",                       # book without title (NDE)
    SCHEMA + "sdDatePublished",            # book without stamp (NDE)
    PLANTDB + "dutchRelevance",            # book without Dutch filter flag
    "http://rs.tdwg.org/dwc/terms/scientificName",   # plant without name
    PLANTDB + "occurrences",               # observation without count
    PLANTDB + "prevalenceScore",           # range violation
    PLANTDB + "areaTotalOccurrences",      # area without denominator
    PLANTDB + "pages",                     # mention without pages
    str(RDFS.label),                       # term without label
}
EXPECTED_CONSTRAINTS = {  # custom SHACL components that must fire on bad.nt
    "ZeroImpliesZeroConstraint",
    "ScoreFormulaConstraint",
}

# same rules as plantdb-components.ttl, but ONE query for the whole graph.
# 0.0000005 = half of the last kept decimal: build_rdf.py rounds scores to 6.
PREVALENCE_QUERY = f"""
PREFIX plantdb: <{PLANTDB}>
PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>
SELECT ?f ?o ?t ?s WHERE {{
    ?f plantdb:occurrences ?o ;
       plantdb:areaTotalOccurrences ?t ;
       plantdb:prevalenceScore ?s .
    FILTER ( abs(xsd:double(?s) - xsd:double(?o) / xsd:double(?t)) > 0.0000005
             || (?o = 0 && ?s != 0)
             || (?o > 0 && xsd:double(?s) < 0.0000005) )
}} LIMIT 6"""

SELF_DESCRIBE_QUERIES = {
    "Person/Organization nodes (must be none)": f"""
        PREFIX schema: <{SCHEMA}>
        SELECT (COUNT(*) AS ?n) WHERE {{ VALUES ?c {{ schema:Person schema:Organization }}
                                        ?s a ?c }}""",
    "plantdb: predicates used without rdfs:label": f"""
        PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
        SELECT DISTINCT ?bad WHERE {{
            ?s ?p ?o . FILTER(STRSTARTS(STR(?p), "{PLANTDB}"))
            FILTER NOT EXISTS {{ ?p rdfs:label ?l }} }} LIMIT 6""",
    "plantdb: predicates used without skos:definition": f"""
        PREFIX skos: <http://www.w3.org/2004/02/skos/core#>
        SELECT DISTINCT ?bad WHERE {{
            ?s ?p ?o . FILTER(STRSTARTS(STR(?p), "{PLANTDB}"))
            FILTER NOT EXISTS {{ ?p skos:definition ?d }} }} LIMIT 6""",
    "status terms without rdfs:label": f"""
        PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
        SELECT DISTINCT ?bad WHERE {{
            ?s <{PLANTDB}nlStatus> ?t .
            FILTER NOT EXISTS {{ ?t rdfs:label ?l }} }} LIMIT 6""",
}


def load_shapes(extra=()) -> Graph:
    shapes = Graph()
    for path in SHAPES + list(extra):
        shapes.parse(path, format="turtle")
    return shapes


def report_violations(report: Graph):
    """Yield (severity, source_component, source_shape, path, message) per result."""
    SH = "http://www.w3.org/ns/shacl#"
    for v in report.subjects(RDF.type, URIRef(SH + "ValidationResult")):
        sev = report.value(v, URIRef(SH + "resultSeverity"))
        comp = report.value(v, URIRef(SH + "sourceConstraintComponent"))
        shape = report.value(v, URIRef(SH + "sourceShape"))
        path = report.value(v, URIRef(SH + "resultPath")) or \
            report.value(v, URIRef(SH + "path"))
        msg = report.value(v, URIRef(SH + "resultMessage"))
        yield (str(sev or SH + "Violation"), str(comp or ""), str(shape or ""),
               str(path or ""), str(msg or ""))


def self_test() -> int:
    shapes = load_shapes(extra=COMPONENTS)  # small graph: components run per node
    g = Graph()
    g.parse("shacl/examples/bad.nt", format="nt")
    t0 = time.time()
    from pyshacl import validate
    conforms, report, _ = validate(g, shacl_graph=shapes, advanced=True,
                                   serialize_report="graph")
    print(f"--self-test: broken example validated in {time.time() - t0:.1f}s")
    if conforms:
        print("FAIL: the broken example PASSED - the harness is blind")
        return 1
    seen_paths, seen_constraints, rows = set(), set(), []
    for sev, comp, shape, path, msg in report_violations(report):
        if not sev.endswith("Violation"):
            continue
        rows.append((comp, shape, path, msg))
        if path:
            seen_paths.add(path)
        for name in EXPECTED_CONSTRAINTS:
            if name in comp or name in shape:
                seen_constraints.add(name)
    print(f"  violations found: {len(rows)}")
    for comp, shape, path, msg in sorted(rows):
        src = (comp or shape).split("/")[-1].split("#")[-1]
        print(f"  [{src}] {path.split('/')[-1] or path} : {msg[:90]}")
    missing_paths = EXPECTED_BAD_PATHS - seen_paths
    missing_constraints = EXPECTED_CONSTRAINTS - seen_constraints
    ok = True
    if missing_paths:
        print("FAIL: expected violations NOT reported for:", *missing_paths, sep="\n  ")
        ok = False
    if missing_constraints:
        print("FAIL: custom constraints never fired:", *missing_constraints, sep="\n  ")
        ok = False
    print("self-test:", "PASS - every planted error was caught" if ok else "FAIL")
    return 0 if ok else 1


def oxigraph_checks(path: str) -> int:
    """Batched prevalence contract + self-description, via Oxigraph (fast)."""
    t0 = time.time()
    store = pyoxigraph.Store()
    with open(path, "rb") as fh:
        store.load(fh, format=pyoxigraph.RdfFormat.N_TRIPLES)
    n = next(store.query("SELECT (COUNT(*) AS ?n) WHERE { ?s ?p ?o }"))["n"]
    print(f"oxigraph: loaded {int(n.value):,} triples in {time.time()-t0:.0f}s")

    rc = 0
    t0 = time.time()
    bad = list(store.query(PREVALENCE_QUERY))
    print(f"prevalence contract (score = occ/total, zeros exact): "
          f"{time.time()-t0:.1f}s -> {'PASS' if not bad else 'FAIL'}")
    for row in bad:
        print(f"  - {row['f']}: {row['o']}/{row['t']} but score {row['s']}")
        rc = 1

    for title, q in SELF_DESCRIBE_QUERIES.items():
        rows = list(store.query(q))
        if title.startswith("Person/Organization"):
            count = int(next(iter(rows))["n"].value) if rows else 0
            problem = count > 0
            detail = f"{count} nodes - authors/places must not be Person/" \
                     f"Organization entities (issue #11 decision 4)"
        else:
            problem = bool(rows)
            detail = ", ".join(str(r["bad"]) for r in rows)
        print(f"self-describe: {title}: {'FAIL' if problem else 'PASS'}"
              + (f" ({detail})" if problem else ""))
        if problem:
            rc = 1
    return rc


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--graph", default=DATA, help="N-Triples file to validate")
    ap.add_argument("--self-test", action="store_true",
                    help="validate the broken example instead: it must fail")
    args = ap.parse_args()

    if args.self_test:
        sys.exit(self_test())

    rc = oxigraph_checks(args.graph)

    t0 = time.time()
    g = Graph()
    g.parse(args.graph, format="nt")
    print(f"rdflib: loaded {len(g):,} triples in {time.time()-t0:.0f}s")
    shapes = load_shapes()  # NOT COMPONENTS: those ran batched, above
    t0 = time.time()
    from pyshacl import validate
    conforms, report, _ = validate(g, shacl_graph=shapes)
    print(f"pySHACL: {time.time()-t0:.0f}s -> {'conforms' if conforms else 'results'}")
    # count by severity: the NDE profile uses sh:Warning deliberately
    # (e.g. "add a MediaObject when media is available" - our books have no
    # media, so the warning fires for every book and means nothing). Only
    # sh:Violation results fail the export.
    tally = {}
    for sev, comp, shape, path, msg in report_violations(report):
        key = (sev.split("#")[-1], (comp or shape).split("#")[-1], msg[:80])
        tally[key] = tally.get(key, 0) + 1
    hard = 0
    for (sev, comp, msg), n in sorted(tally.items()):
        print(f"  {sev:9} x {n:>6}  [{comp}] {msg}")
        if sev == "Violation":
            hard += n
    if hard:
        print(f"shapes: FAIL ({hard} violations)")
        sys.exit(1)
    print("shapes: PASS (NDE SCHEMA-AP + plantdb query contract)")
    sys.exit(rc)


if __name__ == "__main__":
    main()

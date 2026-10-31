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
python validate_rdf.py --graph FILE    # validate another single .nt file
python validate_rdf.py --full          # the four full-export files: batched
                                       # contract queries over the WHOLE graph
                                       # (disk-backed Oxigraph store), plus
                                       # pySHACL over the slice (rdflib cannot
                                       # hold 100M+ triples - issue #12 decision)
python validate_rdf.py --self-test     # the broken example MUST fail: proves
                                       # the harness actually catches problems
"""

import argparse
import json
import os
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


# the same shapes the SHACL run checks, expressed as batched SPARQL so they
# can run over the WHOLE full export (--full); one query per rule, each
# returns offending subjects (LIMIT-ed).
FULL_CONTRACT_QUERIES = {
    "book: exactly one schema:name": f"""
        PREFIX schema: <{SCHEMA}>
        SELECT ?b WHERE {{ ?b a <{SCHEMA}CreativeWork> .
            {{ SELECT ?b (COUNT(?n) AS ?c) WHERE {{ ?b schema:name ?n }}
              GROUP BY ?b }} FILTER (?c <> 1) }} LIMIT 6""",
    "book: exactly one schema:sdDatePublished": f"""
        PREFIX schema: <{SCHEMA}>
        SELECT ?b WHERE {{ ?b a <{SCHEMA}CreativeWork> .
            {{ SELECT ?b (COUNT(?d) AS ?c) WHERE {{ ?b schema:sdDatePublished ?d }}
              GROUP BY ?b }} FILTER (?c <> 1) }} LIMIT 6""",
    "book: dutchRelevance flag present": f"""
        PREFIX schema: <{SCHEMA}>
        SELECT ?b WHERE {{ ?b a <{SCHEMA}CreativeWork> .
            FILTER NOT EXISTS {{ ?b <{PLANTDB}dutchRelevance> ?x }} }} LIMIT 6""",
    "plant: exactly one dwc:scientificName": """
        PREFIX dwc: <http://rs.tdwg.org/dwc/terms/>
        SELECT ?p WHERE { ?p a <http://rs.tdwg.org/dwc/terms/Taxon> .
            { SELECT ?p (COUNT(?n) AS ?c) WHERE { ?p dwc:scientificName ?n }
              GROUP BY ?p } FILTER (?c <> 1) } LIMIT 6""",
    "plant: normalizedName present": f"""
        PREFIX dwc: <http://rs.tdwg.org/dwc/terms/>
        SELECT ?p WHERE {{ ?p a <http://rs.tdwg.org/dwc/terms/Taxon> .
            FILTER NOT EXISTS {{ ?p <{PLANTDB}normalizedName> ?n }} }} LIMIT 6""",
    "mention: type+aboutPlant+mentionsBook+pages all present": f"""
        SELECT ?m WHERE {{ ?m a <{PLANTDB}TaxonMention> .
            FILTER (NOT EXISTS {{ ?m <{PLANTDB}aboutPlant> ?p }}
                 OR NOT EXISTS {{ ?m <{PLANTDB}mentionsBook> ?b }}
                 OR NOT EXISTS {{ ?m <{PLANTDB}pages> ?n }}) }} LIMIT 6""",
    "observation: occurrences+total+score present": f"""
        SELECT ?o WHERE {{ ?o a <http://purl.org/linked-data/cube#Observation> .
            FILTER (NOT EXISTS {{ ?o <{PLANTDB}occurrences> ?x }}
                 OR NOT EXISTS {{ ?o <{PLANTDB}areaTotalOccurrences> ?t }}
                 OR NOT EXISTS {{ ?o <{PLANTDB}prevalenceScore> ?s }}) }} LIMIT 6""",
    "place: name+geo+areaTotal present": f"""
        PREFIX schema: <{SCHEMA}>
        SELECT ?a WHERE {{ ?a a <{SCHEMA}Place> .
            FILTER (NOT EXISTS {{ ?a schema:name ?n }}
                 OR NOT EXISTS {{ ?a schema:geo ?g }}
                 OR NOT EXISTS {{ ?a <{PLANTDB}areaTotalOccurrences> ?t }}) }} LIMIT 6""",
    # the four files must stitch together - a mention in books.nt that points
    # at a plant missing from plants.nt would silently shrink Query A/C
    "no dangling mention -> plant": f"""
        PREFIX dwc: <http://rs.tdwg.org/dwc/terms/>
        SELECT ?m WHERE {{ ?m a <{PLANTDB}TaxonMention> ;
            <{PLANTDB}aboutPlant> ?p .
            FILTER NOT EXISTS {{ ?p a <http://rs.tdwg.org/dwc/terms/Taxon> }} }} LIMIT 6""",
    "no dangling mention -> book": f"""
        PREFIX schema: <{SCHEMA}>
        SELECT ?m WHERE {{ ?m a <{PLANTDB}TaxonMention> ;
            <{PLANTDB}mentionsBook> ?b .
            FILTER NOT EXISTS {{ ?b a <{SCHEMA}CreativeWork> }} }} LIMIT 6""",
    "no dangling observation -> plant": f"""
        PREFIX dwc: <http://rs.tdwg.org/dwc/terms/>
        SELECT ?o WHERE {{ ?o a <http://purl.org/linked-data/cube#Observation> ;
            <{PLANTDB}aboutPlant> ?p .
            FILTER NOT EXISTS {{ ?p a <http://rs.tdwg.org/dwc/terms/Taxon> }} }} LIMIT 6""",
    "no dangling observation -> area": f"""
        PREFIX schema: <{SCHEMA}>
        SELECT ?o WHERE {{ ?o a <http://purl.org/linked-data/cube#Observation> ;
            <{PLANTDB}observedIn> ?a .
            FILTER NOT EXISTS {{ ?a a <{SCHEMA}Place> }} }} LIMIT 6""",
}


STORE_DIR = "/tmp/opencode/oxigraph-full-store"


def store_marker():
    return STORE_DIR, os.path.join(STORE_DIR, "plantdb-marker.json")


def open_full_store(paths, reload=False):
    """Open an on-disk Oxigraph store holding the given .nt files (the full
    export does not fit in RAM). Reuses the store when a marker file still
    matches the files' size/mtime, so validate --full and handover_check.py
    load once. Returns (store, loaded_now)."""
    import shutil
    marker_path = os.path.join(STORE_DIR, "plantdb-marker.json")
    state = {p: [os.path.getsize(p), int(os.path.getmtime(p))] for p in paths}
    if not reload and os.path.exists(marker_path) \
            and json.load(open(marker_path)) == state:
        return pyoxigraph.Store(STORE_DIR), False
    shutil.rmtree(STORE_DIR, ignore_errors=True)
    os.makedirs(STORE_DIR, exist_ok=True)
    store = pyoxigraph.Store(STORE_DIR)
    for p in paths:
        with open(p, "rb") as fh:
            store.load(fh, format=pyoxigraph.RdfFormat.N_TRIPLES)
    json.dump(state, open(marker_path, "w"))
    return store, True


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


def oxigraph_checks(paths, full=False) -> int:
    """Batched prevalence contract + self-description (+ full-graph shape
    rules when full), via Oxigraph. --full uses a DISK-BACKED store: the
    full export is too big to hold in RAM."""
    if isinstance(paths, str):
        paths = [paths]
    t0 = time.time()
    if full:
        store, loaded = open_full_store(paths)
        if not loaded:
            print("oxigraph: reusing on-disk store (marker matches the files)")
    else:
        store = pyoxigraph.Store()
        for path in paths:
            with open(path, "rb") as fh:
                store.load(fh, format=pyoxigraph.RdfFormat.N_TRIPLES)
    n = next(store.query("SELECT (COUNT(*) AS ?n) WHERE { ?s ?p ?o }"))["n"]
    print(f"oxigraph: {int(n.value):,} triples from {len(paths)} file(s) "
          f"in {time.time()-t0:.0f}s")

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

    if full:
        for title, q in FULL_CONTRACT_QUERIES.items():
            t0 = time.time()
            rows = list(store.query(q))
            problem = bool(rows)
            detail = ", ".join(", ".join(map(str, r.values())) for r in rows[:3])
            print(f"contract ({time.time()-t0:.0f}s): {title}: "
                  f"{'FAIL' if problem else 'PASS'}"
                  + (f" ({detail})" if problem else ""))
            if problem:
                rc = 1
    return rc


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--graph", default=DATA, help="N-Triples file to validate")
    ap.add_argument("--full", action="store_true",
                    help="validate the four full-export files (batched "
                         "contract queries over the whole graph; pySHACL "
                         "then re-runs on the slice)")
    ap.add_argument("--self-test", action="store_true",
                    help="validate the broken example instead: it must fail")
    args = ap.parse_args()

    if args.self_test:
        sys.exit(self_test())

    if args.full:
        d = "plant_data/rdf"
        paths = [f"{d}/{f}" for f in
                 ("plants.nt", "areas.nt", "observations.nt", "books.nt")]
        missing = [p for p in paths if not os.path.exists(p)]
        if missing:
            sys.exit(f"missing: {missing} - run build_rdf.py --full first")
        rc = oxigraph_checks(paths, full=True)
        print(f"pySHACL over the full export is out of reach for rdflib - "
              f"same shapes re-checked on the slice:")
        graph_path = DATA
    else:
        rc = oxigraph_checks(args.graph)
        graph_path = args.graph

    t0 = time.time()
    g = Graph()
    g.parse(graph_path, format="nt")
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

#!/usr/bin/env python3
"""Targeted quality checks on the FULL export (issue #15 hand-over).

The slice harness (run_queries.py) already proves the queries answer exactly
on 2.3M triples. This script asks only the questions that can go wrong at
full scale, against an on-disk Oxigraph store (the 122M-triple export does
not fit in RAM):

  1. per-class counts in the graph == manifest.json == the DuckDB tables
  2. pages contract: SUM(plantdb:pages) over mentions ==
     SUM(plantdb:bhlPageMentions) over plants == DuckDB SUM(pages)
     (catches the plant_books_full view fan-out class of bug)
  3. the golden fixtures B/C still answer identically over the full store
  4. Query A for a plant OUTSIDE the slice (never exercised before)
  5. did-you-mean over ALL entities (no slice restriction)

Run validate_rdf.py --full first: the store it leaves in
/tmp/opencode/oxigraph-full-store is reused (marker file matches the files).

    python3 prototype/handover_check.py
"""

import argparse
import csv
import json
import os
import sys
import time

import duckdb
import pyoxigraph

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from run_queries import (CASES, EXPECTED, QUERY_VARS, compare, duck_a_facts,
                         run_query, term_of)
from validate_rdf import (FULL_CONTRACT_QUERIES, PREVALENCE_QUERY,
                          SELF_DESCRIBE_QUERIES, PLANTDB, store_marker,
                          open_full_store)

RDF = "plant_data/rdf"
FILES = ["plants.nt", "areas.nt", "observations.nt", "books.nt"]
DB = "plant_data/plant_index.duckdb"
P = PLANTDB.rstrip("/")


def count_of(store, q):
    """Single-scalar SELECT: return the one binding as int."""
    sol = next(iter(store.query(q)))
    return int(next(iter(sol)).value)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--force-store", action="store_true",
                    help="rebuild the on-disk Oxigraph store even if the "
                         "marker says it is current")
    args = ap.parse_args()

    man = json.load(open(os.path.join(RDF, "manifest.json")))
    store, loaded = open_full_store(
        [os.path.join(RDF, f) for f in FILES], reload=args.force_store)
    print(f"store: {'REBUILT' if loaded else 'reused (validate_rdf.py --full)'}"
          f" at {store_marker()[0]}")
    con = duckdb.connect(DB, read_only=True)
    rc = 0

    def check(label, got, want):
        nonlocal rc
        ok = got == want
        rc |= 0 if ok else 1
        print(f"  {'PASS' if ok else 'FAIL'} {label}: graph={got} "
              f"expected={want}")

    print("\n1. counts: graph == manifest == DuckDB tables")
    one = lambda t, pred: count_of(
        store, f"SELECT (COUNT(DISTINCT ?s) AS ?n) WHERE {{ ?s a <{t}> }}")
    check("plants (dwc:Taxon)", one("http://rs.tdwg.org/dwc/terms/Taxon", ""),
          man["counts"]["plants"])
    check("books (schema:CreativeWork)", one("https://schema.org/CreativeWork", ""),
          man["counts"]["books"])
    check("mentions (plantdb:TaxonMention)", one(P + "/TaxonMention", ""),
          man["counts"]["mentions"])
    check("observations (qb:Observation)",
          one("http://purl.org/linked-data/cube#Observation", ""),
          man["counts"]["observations"])
    check("areas (schema:Place)", one("https://schema.org/Place", ""),
          man["counts"]["areas"])
    check("plants table", con.execute(
        "SELECT count(*) FROM export_entities").fetchone()[0],
        man["counts"]["plants"])
    check("plant_area_scores table", con.execute(
        "SELECT count(*) FROM plant_area_scores").fetchone()[0],
        man["counts"]["observations"])

    print("\n2. pages contract (view fan-out would inflate these)")
    sql_total = con.execute("SELECT sum(pages) FROM mention_edges").fetchone()[0]
    t0 = time.time()
    g_mentions = count_of(
        store, f"SELECT (SUM(?p) AS ?s) WHERE {{ ?m a <{P}/TaxonMention> ; "
               f"<{P}pages> ?p }}")
    g_plants = count_of(
        store, f"SELECT (SUM(?p) AS ?s) WHERE {{ ?p <{P}bhlPageMentions> ?p2 }}")
    print(f"  (SUM over 19M mentions took {time.time()-t0:.0f}s)")
    check("SUM pages on mentions", g_mentions, sql_total)
    check("SUM bhlPageMentions on plants", g_plants, sql_total)

    print("\n3. golden fixtures over the full store (same names, same rows)")
    for label, (path, kwargs, _truth, key) in CASES.items():
        rows, dt = run_query(store, path, **kwargs)
        want = [r for r in csv.reader(open(os.path.join(EXPECTED, label + ".csv")))]
        print(f"  {label}: {len(rows):,} rows in {dt*1000:.0f} ms")
        rc |= compare(rows, want, label, key)

    print("\n4. Query A for a plant that was NEVER in the slice")
    name = "Salvia pratensis"
    wfo, facts = duck_a_facts(con, name)
    rows, dt = run_query(store, "A.rq", name)
    got = {p.split("/")[-1]: v for p, _lbl, v in rows}
    print(f"  {name}: {len(rows)} facts in {dt*1000:.0f} ms")
    check("  mention facts present", got.get("bhlPageMentions") is not None, True)
    n_titles = con.execute(
        "SELECT count(*) FROM mention_edges WHERE ekey=?", [wfo]).fetchone()[0]
    check("  bhlTitleCount", int(got.get("bhlTitleCount", -1)), n_titles)
    n_areas = con.execute(
        "SELECT count(*) FROM plant_area_scores WHERE ekey=? AND occurrences>0",
        [wfo]).fetchone()[0]
    check("  nlAreaCount", int(got.get("nlAreaCount", -1)), n_areas)

    print("\n5. did-you-mean over ALL entities")
    fragment = "helmsii"
    q = open("prototype/queries/didyoumean.rq").read().replace("{{NAME}}", fragment)
    got = {(term_of(s["plant"]), term_of(s["name"]).lower()) for s in store.query(q)}
    want = set()
    for ekey, label in con.execute("""
            SELECT m.ekey, k.name FROM entity_map m JOIN nl_totals k ON k.key = m.okey
            WHERE lower(k.name) LIKE ?
            UNION
            SELECT e.ekey, trim(coalesce(s.name,'') || ' ' || coalesce(s.author,''))
            FROM plant_index s JOIN export_entities e ON e.ekey = s.accepted_wfo_id
            WHERE s.status = 'Synonym'
              AND lower(trim(coalesce(s.name,'') || ' ' || coalesce(s.author,''))) LIKE ?
        """, [f"%{fragment}%", f"%{fragment}%"]).fetchall():
        base = ("https://worldfloraonline.org/taxon/" if ekey.startswith("wfo-")
                else "https://plantdb.example.org/taxon/")
        want.add((base + ekey, label.lower()))
    ok = got == want
    rc |= 0 if ok else 1
    print(f"  {'PASS' if ok else 'FAIL'} didyoumean '{fragment}': "
          f"{len(got)} suggestions (duckdb {len(want)})")
    if not ok:
        print(f"    only in sparql: {sorted(got - want)[:3]}")
        print(f"    only in duckdb: {sorted(want - got)[:3]}")

    print("\nexit:", "ALL GREEN - hand-over ready" if rc == 0 else "FAILURES above")
    sys.exit(rc)


if __name__ == "__main__":
    main()

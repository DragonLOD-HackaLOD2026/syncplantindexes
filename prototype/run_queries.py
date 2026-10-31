#!/usr/bin/env python3
"""Run the three app queries (prototype/queries/*.rq) against the RDF export,
locally, via Oxigraph in memory - no server (issue #13).

The point is the CONTRACT: every SPARQL answer is compared against the same
number computed from DuckDB directly (the ground truth the exporter was built
from). If the RDF answer matches the DuckDB answer, a future QLever serving
the same .nt files must give the same answers - the queries are plain
SPARQL 1.1, no vendor extensions.

python run_queries.py            # run all cases, compare, PASS/FAIL
python run_queries.py --update   # (re)write the expected CSVs from DuckDB
python run_queries.py --show B   # just print the answer of case B_heracleum
"""

import argparse
import csv
import os
import sys
import time

import duckdb
import pyoxigraph

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from build_rdf import STATUS_TERMS, pick_slice  # same vocabulary/slice the exporter uses

SLICE = 200  # must match: build_rdf.py --slice 200 produced plant_data/rdf/slice.nt

DB = "plant_data/plant_index.duckdb"
NT = "plant_data/rdf/slice.nt"
QUERIES = "prototype/queries"
EXPECTED = "prototype/expected"

P = "https://plantdb.example.org/def/"


def store():
    st = pyoxigraph.Store()
    with open(NT, "rb") as fh:
        st.load(fh, format=pyoxigraph.RdfFormat.N_TRIPLES)
    return st


# variable order per query file: iterating a pyoxigraph solution returns
# bindings in alphabetical order, NOT the order of the SELECT
QUERY_VARS = {
    "A.rq": ["predicate", "fieldLabel", "valueOut"],
    "B.rq": ["areaName", "province", "occurrences", "areaTotal", "score"],
    "C.rq": ["pages", "title", "author", "yearFrom", "yearTo", "bhlUrl"],
}


def run_query(st, path, name, dutch=None):
    text = open(os.path.join(QUERIES, path)).read()
    text = text.replace("{{NAME}}", name)
    if dutch is not None:
        text = text.replace("{{DUTCH}}", "true" if dutch else "false")
    t0 = time.time()
    rows = [[term_of(sol[v]) for v in QUERY_VARS[path]] for sol in st.query(text)]
    return rows, time.time() - t0


def term_of(t):
    """Normalise one SPARQL result term to a plain string.

    Unbound OPTIONALs come back as None; decimals are compared at the 6
    decimals the export keeps; integers without quotes.
    """
    if t is None:
        return ""
    if isinstance(t, pyoxigraph.Literal):
        v = t.value
        dt = str(t.datatype or "")
        if "decimal" in dt or "double" in dt or "float" in dt:
            return f"{float(v):.6f}"
        if "integer" in dt:
            return str(int(v))
        return v
    if hasattr(t, "value") and not isinstance(t, pyoxigraph.Triple):
        return t.value
    return str(t)


# ---------------------------------------------------------------- ground truth
# DuckDB-side answers: same rules the exporter documents (roll mentions up to
# the accepted name, prevalence = plant records / area total, 6 decimals),
# but computed independently of build_rdf.py.
def duck_areas(con, name):
    """Query B rows: area, province, occurrences, areaTotal, score (6 dp)."""
    rows = con.execute("""
        WITH keys AS (
            -- exactly how the exporter names entities: the display name of a
            -- nl_totals row, or a synonym label (skos:altLabel) hanging off
            -- that row's accepted WFO record
            SELECT k.key FROM nl_totals k WHERE lower(k.name) = lower(?)
            UNION
            SELECT k.key FROM nl_totals k
            JOIN plant_index s ON s.status = 'Synonym'
                               AND s.accepted_wfo_id = k.wfo_id
            WHERE lower(trim(concat_ws(' ', s.name, s.author))) = lower(?)
        )
        SELECT a.name, a.province, coalesce(sum(o.occurrences), 0), a.occurrences,
               round(coalesce(sum(o.occurrences), 0)::DOUBLE / a.occurrences, 6)
        FROM obs_areas a
        LEFT JOIN observations_nl o ON o.area_id = a.id AND o.key IN (SELECT key FROM keys)
        WHERE a.in_nl
        GROUP BY a.id, a.name, a.province, a.occurrences
        ORDER BY 5 DESC, 1""", [name, name]).fetchall()
    return [[n, prov, str(int(occ)), str(int(total)), f"{score:.6f}"]
            for n, prov, occ, total, score in rows]


def duck_books(con, name, dutch):
    """Query C rows: pages, title, author, yearFrom, yearTo, url."""
    rows = con.execute("""
        WITH keyed AS (
            SELECT coalesce(nullif(p.accepted_wfo_id, ''), b.wfo_id) AS ekey,
                   b.title_id, sum(b.pages) AS pages, bool_or(b.dutch <> '') AS d_any
            FROM plant_books b JOIN plant_index p USING (wfo_id)
            GROUP BY 1, 2),
        named AS (
            SELECT k.title_id, k.pages, k.d_any
            FROM keyed k JOIN plant_index acc ON acc.wfo_id = k.ekey
            WHERE lower(acc.name) = lower(?))
        SELECT k.pages, coalesce(t."FullTitle", ''), coalesce(t."TL2Author", ''),
               coalesce(t."StartYear", ''), coalesce(t."EndYear", ''),
               coalesce(t."TitleURL", '')
        FROM named k LEFT JOIN bhl_titles t ON t."TitleID" = k.title_id
        WHERE (NOT ? OR k.d_any)      -- ? = dutchOnly: true keeps Dutch only
        ORDER BY 1 DESC, 2""", [name, bool(dutch)]).fetchall()
    return [[str(int(pg)), t, a, str(y1), str(y2), u]
            for pg, t, a, y1, y2, u in rows]


def duck_a_facts(con, name):
    """Query A spot checks: (predicate-local-name, expected string value)."""
    wfo = con.execute(
        "SELECT wfo_id FROM plant_index WHERE lower(name)=? AND status<>'Synonym' LIMIT 1",
        [name.lower()]).fetchone()
    if not wfo:
        raise SystemExit(f"duck_a_facts: {name} has no non-synonym WFO record")
    wfo = wfo[0]
    status = con.execute(
        "SELECT status FROM nl_status WHERE wfo_id=?", [wfo]).fetchone()
    n_syn = con.execute(
        "SELECT count(*) FROM plant_index WHERE status='Synonym' AND accepted_wfo_id=?",
        [wfo]).fetchone()[0]
    n_titles = con.execute("""
        SELECT count(*) FROM (
          SELECT b.title_id FROM plant_books b JOIN plant_index p USING (wfo_id)
          WHERE coalesce(nullif(p.accepted_wfo_id,''), b.wfo_id)=? GROUP BY 1)""",
        [wfo]).fetchone()[0]
    n_areas = con.execute("""
        SELECT count(DISTINCT area_id) FROM observations_nl o
        WHERE o.in_nl AND o.occurrences > 0
          AND (o.wfo_id=? OR EXISTS (SELECT 1 FROM nl_totals k WHERE k.key=o.key
               AND coalesce(nullif(k.wfo_id,''),k.key)=?))""",
        [wfo, wfo]).fetchone()[0]
    facts = {
        "normalizedName": name.lower(),
        "bhlTitleCount": str(n_titles),
        "synonymLabels": str(n_syn),          # counted below, not a fact value
        "nlAreaCount": str(n_areas),
    }
    if status and status[0] in STATUS_TERMS:
        facts["nlStatusLabel"] = STATUS_TERMS[status[0]][0]
    return wfo, facts


# ------------------------------------------------------------------- the cases
# each case: (query file, runner kwargs, duckdb ground truth, row sort key).
# SPARQL engines and DuckDB order language-tagged strings differently, so the
# harness compares as SETS in its own deterministic order; the ORDER BY inside
# the .rq files is for the app, not part of the contract.
CASES = {
    "B_heracleum": ("B.rq", dict(name="Heracleum mantegazzianum"),
                    lambda con: duck_areas(con, "Heracleum mantegazzianum"),
                    lambda r: (r[0],)),
    "B_amelanchier": ("B.rq", dict(name="amelanchier lamarckii"),
                      lambda con: duck_areas(con, "amelanchier lamarckii"),
                      lambda r: (r[0],)),
    "C_quercus_dutch": ("C.rq", dict(name="Quercus robur", dutch=True),
                        lambda con: duck_books(con, "Quercus robur", True),
                        lambda r: (-int(r[0]), r[1])),
    "C_heracleum_all": ("C.rq", dict(name="Heracleum mantegazzianum", dutch=False),
                        lambda con: duck_books(con, "Heracleum mantegazzianum", False),
                        lambda r: (-int(r[0]), r[1])),
    "C_crassula_dutch": ("C.rq", dict(name="Crassula helmsii", dutch=True),
                         lambda con: duck_books(con, "Crassula helmsii", True),
                         lambda r: (-int(r[0]), r[1])),
}


def compare(got, want, label, key):
    if sorted(got, key=lambda r: (key(r), r)) == sorted(want, key=lambda r: (key(r), r)):
        print(f"  PASS {label}: {len(got):,} rows")
        return 0
    print(f"  FAIL {label}: sparql {len(got):,} rows vs duckdb {len(want):,}")
    gs = sorted(got, key=lambda r: (key(r), r))
    ws = sorted(want, key=lambda r: (key(r), r))
    for g, w in zip(gs, ws):
        if g != w:
            print(f"    first diff:\n      sparql {g}\n      duckdb {w}")
            break
    return 1


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--update", action="store_true", help="write expected CSVs")
    ap.add_argument("--show", metavar="CASE", help="print this case's answer and exit")
    ap.add_argument("--only", metavar="CASE", help="run just this case")
    ap.add_argument("--name", default=None,
                    help="with --show A: query A for this plant name")
    args = ap.parse_args()

    st = store()
    con = duckdb.connect(DB, read_only=True)

    if args.show:
        if args.show.upper() == "A":
            rows, dt = run_query(st, "A.rq", args.name or "Crassula helmsii")
        else:
            path, kwargs, _, _ = CASES[args.show]
            rows, dt = run_query(st, path, **kwargs)
        for r in rows[:40]:
            print(" | ".join(r))
        print(f"({len(rows):,} rows in {dt*1000:.0f} ms)")
        return

    os.makedirs(EXPECTED, exist_ok=True)
    rc = 0
    cases = {k: v for k, v in CASES.items() if not args.only or k == args.only}
    for label, (path, kwargs, truth, key) in cases.items():
        rows, dt = run_query(st, path, **kwargs)
        csv_path = os.path.join(EXPECTED, label + ".csv")
        if args.update:
            want = truth(con)
            with open(csv_path, "w", newline="") as fh:
                csv.writer(fh).writerows(want)
            print(f"  wrote {csv_path} ({len(want):,} rows)")
            continue
        want = [row for row in csv.reader(open(csv_path))]
        print(f"{label}: {len(rows):,} rows in {dt*1000:.0f} ms")
        rc |= compare(rows, want, label, key)

    # Did-you-mean: substring search must find the entity by a name fragment.
    # Ground truth: nl_totals display names + synonym labels containing it,
    # recomputed in DuckDB; compared as a set of (plant URI, lower-case label).
    fragment = "quercus robur"
    slice_keys = set(pick_slice(con, SLICE))  # only exported entities answer
    q = open(os.path.join(QUERIES, "didyoumean.rq")).read().replace("{{NAME}}", fragment)
    sols = list(st.query(q))
    got = {(term_of(s["plant"]), term_of(s["name"]).lower()) for s in sols}
    want = set()
    for key, label in con.execute("""
            SELECT k.key, k.name FROM nl_totals k WHERE lower(k.name) LIKE ?
            UNION
            SELECT k.key, trim(coalesce(s.name,'') || ' ' || coalesce(s.author,''))
            FROM nl_totals k
            JOIN plant_index s ON s.status = 'Synonym' AND s.accepted_wfo_id = k.wfo_id
            WHERE lower(trim(coalesce(s.name,'') || ' ' || coalesce(s.author,''))) LIKE ?
        """, [f"%{fragment}%", f"%{fragment}%"]).fetchall():
        base = ("https://worldfloraonline.org/taxon/" if key.startswith("wfo-")
                else "https://plantdb.example.org/taxon/")
        if key in slice_keys:
            want.add((base + key, label.lower()))
    ok = got == want
    print(f"didyoumean '{fragment}': {'PASS' if ok else 'FAIL'} "
          f"{len(got)} suggestions (duckdb {len(want)})")
    if not ok:
        print(f"    only in sparql: {sorted(got - want)[:3]}")
        print(f"    only in duckdb: {sorted(want - got)[:3]}")
    rc |= 0 if ok else 1

    # Query A: spot-checked facts (not a full CSV - it is the widest query)
    if args.only and args.only != "A_crassula":
        sys.exit(rc)
    wfo, facts = duck_a_facts(con, "Crassula helmsii")
    rows, dt = run_query(st, "A.rq", "Crassula helmsii")
    got = {p.split("/")[-1]: v for p, _lbl, v in rows}
    print(f"A_crassula: {len(rows):,} facts in {dt*1000:.0f} ms")
    checks = [
        ("normalizedName", facts["normalizedName"], got.get("normalizedName")),
        ("bhlTitleCount", facts["bhlTitleCount"], got.get("bhlTitleCount")),
        ("nlAreaCount", facts["nlAreaCount"], got.get("nlAreaCount")),
        ("nlStatus label", facts.get("nlStatusLabel"),
         got.get("nlStatus")),   # A.rq resolves the DefinedTerm to its label
    ]
    n_alt = sum(1 for p, _l, _v in rows if p.endswith("altLabel"))
    checks.append(("altLabel count", facts["synonymLabels"], str(n_alt)))
    for what, want, got_v in checks:
        ok = want is not None and str(want) == str(got_v)
        print(f"  {'PASS' if ok else 'FAIL'} {what}: duckdb={want} sparql={got_v}")
        rc |= 0 if ok else 1
    sys.exit(rc)


if __name__ == "__main__":
    main()

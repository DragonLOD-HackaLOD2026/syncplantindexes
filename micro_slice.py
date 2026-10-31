#!/usr/bin/env python3
"""Micro slice: 10 plants, three readable queries, one fast test.

Builds plant_data/rdf/micro10.nt from plant_data/plant_index.duckdb: the 5
hand-picked edge-case plants plus the 5 most-observed Dutch plants. Deliberately
NOT build_rdf.py's export - this slice is the readable shape we want:

  * native regions as their actual names (TDWG regions, e.g. 'Caucasus'),
    never just counts - no nativeRegionCount / introducedRegionCount here;
  * plantdb:firstObservationYear and plantdb:lastObservationYear are xsd:gYear;
  * no plantdb:occurrencesOutsideNl and no plantdb:occurrencesUnplaced -
    not relevant to this app;
  * vocabulary labels cover only the predicates this slice actually uses.

Then it writes the three example queries as plain .rq files (one plant name
per file, written straight into the pattern - no VALUES, no property paths,
no ?predicate pivot, every column named after a defined predicate), runs them
against the slice with Oxigraph, and writes the real answers to
prototype/queries/micro/MICRO.md.

    python3 micro_slice.py
"""

import os
import time

import duckdb
import pyoxigraph

from build_rdf import (
    DB, DEFINITIONS, DWC, EDGE_CASES, Exporter, I, L, OWL, PRED, RDF_TYPE,
    RDFS, SCHEMA, SKOS, STATUS_TERMS, XSD, check_definitions, display_case,
    plant_uri, status_uri)

N_PLANTS = 10
NT = "plant_data/rdf/micro10.nt"
QDIR = "prototype/queries/micro"
MD = os.path.join(QDIR, "MICRO.md")

PLACEHOLDERS = lambda n: ", ".join(["?"] * n)  # noqa: E731


def gyear(year):
    """Year literal typed xsd:gYear. build_rdf.L() types any int as
    xsd:integer before it looks at dt, so this slice types years itself."""
    return None if year is None else f'"{int(year)}"^^<{XSD}gYear>'


# ---------------------------------------------------------------- pick 10
def pick_plants(con):
    """Edge cases first, then most-observed, until N_PLANTS entities."""
    edge_keys = [r[0] for r in con.execute(
        "SELECT key FROM nl_totals WHERE lower(name) IN ("
        + PLACEHOLDERS(len(EDGE_CASES)) + ")",
        [e.lower() for e in EDGE_CASES]).fetchall()]
    top_keys = [r[0] for r in con.execute(
        "SELECT key FROM nl_totals ORDER BY occurrences_nl DESC NULLS LAST "
        "LIMIT 30").fetchall()]

    # resolve observation keys to entity keys exactly like build_rdf does
    candidates = edge_keys + top_keys
    rows = con.execute(f"""
        SELECT k.key,
               coalesce(p.wfo_id, k.key) AS entity_key,
               coalesce(p.name, k.gbif_name, k.name) AS name,
               coalesce(p.author, k.author) AS author,
               coalesce(p.rank, k.rank) AS rank,
               coalesce(p.status, 'Unlinked') AS status,
               coalesce(p.family, k.family) AS family,
               p.ipni_id, p.wcvp_id, p.powo_id, p.gbif_id, p.ncbi_taxid,
               k.gbif_name
        FROM (SELECT key, gbif_name, name, author, rank, family
              FROM nl_totals WHERE key IN ({PLACEHOLDERS(len(candidates))})) k
        LEFT JOIN plant_index p
               ON (p.wfo_id = k.key
                   OR (k.key NOT LIKE 'wfo-%' AND lower(p.name) = lower(k.key)
                       AND p.status <> 'Synonym'))
    """, candidates).fetchall()

    entity_of_obskey, info_by_entity, seen = {}, {}, []
    for (okey, ekey, name, author, rank, status, family,
         ipni, wcvp_id, powo, gbif_id, ncbi, gbif_name) in rows:
        entity_of_obskey[okey] = ekey
        if ekey in seen:
            continue
        seen.append(ekey)
        info_by_entity[ekey] = dict(
            name=name, author=author, rank=rank, status=status, family=family,
            ipni=ipni, wcvp=wcvp_id, powo=powo, gbif=gbif_id, ncbi=ncbi,
            gbif_name=gbif_name)

    entity_keys = []
    for okey in edge_keys + top_keys:
        ekey = entity_of_obskey.get(okey)
        if ekey and ekey not in entity_keys:
            entity_keys.append(ekey)
        if len(entity_keys) == N_PLANTS:
            break
    obs_keys = [k for k, e in entity_of_obskey.items() if e in entity_keys]
    return entity_keys, obs_keys, entity_of_obskey, info_by_entity


# ---------------------------------------------------------------- export
def export_micro(con, entity_keys, obs_keys, entity_of_obskey, info_by_entity):
    with open(NT, "w", encoding="utf-8") as fh:
        ex = Exporter(fh)
        e_wfo = [k for k in entity_keys if k.startswith("wfo-")]

        # ---- identity + identifiers
        for ekey in entity_keys:
            info = info_by_entity[ekey]
            p = I(plant_uri(ekey))
            ex.tri(p, I(RDF_TYPE), I(SCHEMA + "BioChemEntity"))
            ex.tri(p, I(RDF_TYPE), I(DWC + "Taxon"))
            display = (info["name"] or info["gbif_name"]) if ekey.startswith(
                "wfo-") else (info["gbif_name"] or info["name"])
            display = display_case(display)
            ex.fact(p, DWC + "scientificName", display)
            ex.fact(p, PRED + "normalizedName", display.lower())
            ex.fact(p, DWC + "scientificNameAuthorship", info["author"])
            ex.fact(p, DWC + "taxonRank", info["rank"])
            ex.fact(p, DWC + "family", info["family"])
            if ekey.startswith("wfo-"):
                ex.tri(p, I(OWL + "sameAs"), I("https://worldfloraonline.org/taxon/" + ekey))
                ex.fact(p, SCHEMA + "identifier", ekey)
            for idval, uri in ((info["gbif"], "https://www.gbif.org/species/"),
                               (info["ncbi"],
                                "https://www.ncbi.nlm.nih.gov/Taxonomy/Browser/wwwtax.cgi?id="),
                               (info["ipni"], "https://ipni.org/n/"),
                               (info["powo"], "https://powo.science.kew.org/taxon/")):
                if idval:
                    ex.fact(p, SCHEMA + "identifier", idval)
                    ex.tri(p, I(OWL + "sameAs"), I(uri + idval))

        # ---- synonyms as skos:altLabel on the accepted plant
        if e_wfo:
            for akey, sname, sauthor in con.execute(f"""
                SELECT accepted_wfo_id, name, author FROM plant_index
                WHERE status = 'Synonym'
                  AND accepted_wfo_id IN ({PLACEHOLDERS(len(e_wfo))})
            """, e_wfo).fetchall():
                p = I(plant_uri(akey))
                ex.fact(p, SKOS + "altLabel",
                        f"{sname} {sauthor}".strip() if sauthor else sname)

        # ---- origin: the actual regions, by name - not counts
        for wfo, regions, groups in con.execute(f"""
            SELECT wfo_id, native_regions, native_groups FROM taxon_ranges
            WHERE wfo_id IN ({PLACEHOLDERS(len(e_wfo))})
        """, e_wfo).fetchall():
            p = I(plant_uri(wfo))
            if regions:
                for r in regions.split(", "):
                    ex.fact(p, PRED + "nativeRegion", r, lang="en")
            if groups:
                for g in groups.split(", "):
                    ex.fact(p, PRED + "nativeGroup", g, lang="en")

        # ---- NL status
        used_status = set()
        for wfo, status, inv, ayear in con.execute(f"""
            SELECT wfo_id, status, invasive, arrival_year FROM nl_status
            WHERE wfo_id IN ({PLACEHOLDERS(len(e_wfo))})
        """, e_wfo).fetchall():
            p = I(plant_uri(wfo))
            ex.tri(p, I(PRED + "nlStatus"), I(status_uri(status)))
            used_status.add(status)
            ex.tri(p, I(PRED + "invasive"), L(bool(inv)))
            ex.fact(p, PRED + "arrivalYear", ayear)

        # ---- NL observation totals (no outside-NL / unplaced counts)
        # years as xsd:gYear
        for okey, n_areas, occ_nl, fy, ly in con.execute(f"""
            SELECT key, coalesce(n_areas_nl, 0), coalesce(occurrences_nl, 0),
                   first_year, last_year
            FROM nl_totals WHERE key IN ({PLACEHOLDERS(len(obs_keys))})
        """, obs_keys).fetchall():
            p = I(plant_uri(entity_of_obskey[okey]))
            ex.tri(p, I(PRED + "nlAreaCount"), L(int(n_areas)))
            ex.tri(p, I(PRED + "nlOccurrenceCount"), L(int(occ_nl)))
            if (fy_lit := gyear(fy)) is not None:
                ex.tri(p, I(PRED + "firstObservationYear"), fy_lit)
            if (ly_lit := gyear(ly)) is not None:
                ex.tri(p, I(PRED + "lastObservationYear"), ly_lit)

        # ---- BHL book counts, same rollup as the big export
        for ekey, titles, pages in con.execute(f"""
            WITH keyed AS (
                SELECT coalesce(nullif(p.accepted_wfo_id, ''), b.wfo_id) AS ekey,
                       b.title_id, sum(b.pages) AS pages
                FROM plant_books b JOIN plant_index p USING (wfo_id)
                WHERE coalesce(nullif(p.accepted_wfo_id, ''), b.wfo_id)
                      IN ({PLACEHOLDERS(len(entity_keys))})
                GROUP BY 1, 2)
            SELECT ekey, count(*), sum(pages) FROM keyed GROUP BY ekey
        """, entity_keys).fetchall():
            p = I(plant_uri(ekey))
            ex.tri(p, I(PRED + "bhlTitleCount"), L(int(titles)))
            ex.tri(p, I(PRED + "bhlPageMentions"), L(int(pages)))

        # ---- vocabulary: only the predicates and status terms in this slice
        for pred, (label, definition, label_nl) in DEFINITIONS.items():
            if I(PRED + pred) in ex.used_predicates:
                u = I(PRED + pred)
                ex.tri(u, I(RDFS + "label"), L(label, lang="en"))
                ex.tri(u, I(RDFS + "label"), L(label_nl, lang="nl"))
                ex.tri(u, I(SKOS + "definition"), L(definition, lang="en"))
        vocab_set = I("https://plantdb.example.org/vocabulary/nl-status")
        ex.tri(vocab_set, I(RDF_TYPE), I(SCHEMA + "DefinedTermSet"))
        ex.tri(vocab_set, I(SCHEMA + "name"),
               L("Dutch plant status vocabulary", lang="en"))
        for value, (label, definition) in STATUS_TERMS.items():
            if value in used_status:
                u = I(status_uri(value))
                ex.tri(u, I(RDF_TYPE), I(SCHEMA + "DefinedTerm"))
                ex.tri(u, I(RDFS + "label"), L(label, lang="en"))
                ex.tri(u, I(SKOS + "definition"), L(definition, lang="en"))
                ex.tri(vocab_set, I(SCHEMA + "hasPart"), u)

        check_definitions([ex])
        return ex.n


# ---------------------------------------------------------------- queries
# Plain SPARQL 1.1. The plant name sits in the pattern as the lower-cased
# normalizedName (that is what plantdb:normalizedName is by definition), so
# every query is a flat star join - no VALUES, no property paths, no
# ?predicate/?value pivot. Column names match the predicates they come from.
QUERIES = [
    ("plants.rq", """\
# All plants in this slice, one row per plant.
# Run it as-is: the slice contains exactly these 10 plants.
PREFIX dwc:     <http://rs.tdwg.org/dwc/terms/>
PREFIX rdfs:    <http://www.w3.org/2000/01/rdf-schema#>
PREFIX plantdb: <https://plantdb.example.org/def/>
SELECT ?scientificName ?family ?nlStatus ?areasSeenInNl ?observationsInNl
       ?firstObservationYear ?lastObservationYear
WHERE {
  ?plant a dwc:Taxon ;
         dwc:scientificName ?scientificName .
  OPTIONAL { ?plant dwc:family ?family }
  OPTIONAL { ?plant plantdb:nlStatus ?statusUri .
             ?statusUri rdfs:label ?nlStatus }
  OPTIONAL { ?plant plantdb:nlAreaCount ?areasSeenInNl }
  OPTIONAL { ?plant plantdb:nlOccurrenceCount ?observationsInNl }
  OPTIONAL { ?plant plantdb:firstObservationYear ?firstObservationYear }
  OPTIONAL { ?plant plantdb:lastObservationYear ?lastObservationYear }
}
ORDER BY DESC(?observationsInNl) ?scientificName""",
     "scientificName", "family", "nlStatus", "areasSeenInNl",
     "observationsInNl", "firstObservationYear", "lastObservationYear"),

    ("plant.rq", """\
# Everything we know about one plant, one row, named columns.
# plantdb:normalizedName is the scientificName in lower case - that is the
# lookup key. Change the name on the next line to look up another plant.
PREFIX dwc:     <http://rs.tdwg.org/dwc/terms/>
PREFIX rdfs:    <http://www.w3.org/2000/01/rdf-schema#>
PREFIX plantdb: <https://plantdb.example.org/def/>
SELECT ?scientificName ?author ?family ?rank ?nlStatus ?arrivalYear
       ?areasSeenInNl ?observationsInNl
       ?firstObservationYear ?lastObservationYear ?booksMentioning
WHERE {
  ?plant plantdb:normalizedName "crassula helmsii" ;
         dwc:scientificName ?scientificName .
  OPTIONAL { ?plant dwc:scientificNameAuthorship ?author }
  OPTIONAL { ?plant dwc:family ?family }
  OPTIONAL { ?plant dwc:taxonRank ?rank }
  OPTIONAL { ?plant plantdb:nlStatus ?statusUri .
             ?statusUri rdfs:label ?nlStatus }
  OPTIONAL { ?plant plantdb:arrivalYear ?arrivalYear }
  OPTIONAL { ?plant plantdb:nlAreaCount ?areasSeenInNl }
  OPTIONAL { ?plant plantdb:nlOccurrenceCount ?observationsInNl }
  OPTIONAL { ?plant plantdb:firstObservationYear ?firstObservationYear }
  OPTIONAL { ?plant plantdb:lastObservationYear ?lastObservationYear }
  OPTIONAL { ?plant plantdb:bhlTitleCount ?booksMentioning }
}""",
     "scientificName", "author", "family", "rank", "nlStatus", "arrivalYear",
     "areasSeenInNl", "observationsInNl", "firstObservationYear",
     "lastObservationYear", "booksMentioning"),

    ("regions.rq", """\
# Where one plant is native: the actual geographic regions, by name, one row
# each. A narrow native range is exported as region names (plantdb:nativeRegion,
# e.g. 'Transcaucasus'); a wide one as region groups (plantdb:nativeGroup,
# e.g. 'Caucasus'). Exactly one of the two per plant, so the UNION covers all.
PREFIX dwc:     <http://rs.tdwg.org/dwc/terms/>
PREFIX plantdb: <https://plantdb.example.org/def/>
SELECT ?scientificName ?nativeRegion
WHERE {
  ?plant plantdb:normalizedName "quercus robur" ;
         dwc:scientificName ?scientificName .
  { ?plant plantdb:nativeRegion ?nativeRegion }
  UNION
  { ?plant plantdb:nativeGroup  ?nativeRegion }
}
ORDER BY ?nativeRegion""",
     "scientificName", "nativeRegion"),
]


# ---------------------------------------------------------------- run
def bind(sol, var):
    """Binding of ?var, or None when an OPTIONAL left it unbound."""
    try:
        return sol[var]
    except KeyError:
        return None


def cell(term):
    """One SPARQL result term -> plain markdown cell text."""
    if term is None:
        return ""
    if isinstance(term, pyoxigraph.Literal):
        v = term.value
    else:
        v = str(term)
    return v.replace("|", "\\|").replace("\n", " ")


def main():
    con = duckdb.connect(DB, read_only=True)
    entity_keys, obs_keys, entity_of_obskey, info = pick_plants(con)
    assert len(entity_keys) == N_PLANTS, f"picked {len(entity_keys)} entities"
    names = []
    for k in entity_keys:
        first, second = ((info[k]["name"], info[k]["gbif_name"])
                         if k.startswith("wfo-")
                         else (info[k]["gbif_name"], info[k]["name"]))
        names.append(display_case(first or second))
    print(f"micro slice of {N_PLANTS} plants:")
    for n in names:
        print(f"  {n}")

    n_tri = export_micro(con, entity_keys, obs_keys, entity_of_obskey, info)
    print(f"wrote {NT}: {n_tri:,} triples")

    os.makedirs(QDIR, exist_ok=True)
    for fname, sparql, *_ in QUERIES:
        with open(os.path.join(QDIR, fname), "w") as fh:
            fh.write(sparql + "\n")
        print(f"wrote {os.path.join(QDIR, fname)}")

    store = pyoxigraph.Store()
    with open(NT, "rb") as fh:
        store.load(fh, format=pyoxigraph.RdfFormat.N_TRIPLES)

    lines = [
        "# Micro slice: 10 plants, readable queries",
        "",
        "Generated by `python3 micro_slice.py`. The slice is",
        f"`plant_data/rdf/micro10.nt` ({len(store):,} triples loaded,",
        f"{n_tri:,} emitted): the 5 edge-case plants plus the 5 most-observed",
        "Dutch plants. The shape differs from the big export on purpose:",
        "",
        "- native regions carry their **names**, never just counts:",
        "  `plantdb:nativeRegion` for a narrow range (TDWG names like",
        "  'Transcaucasus'), `plantdb:nativeGroup` for a wide one (like",
        "  'Caucasus') - exactly one of the two per plant;",
        "- `plantdb:firstObservationYear` / `plantdb:lastObservationYear` are",
        "  **xsd:gYear**;",
        "- `plantdb:occurrencesOutsideNl` and `plantdb:occurrencesUnplaced` are",
        "  not exported here (not relevant to this app);",
        "- vocabulary labels cover only the predicates the slice actually uses.",
        "",
        "Every query is a flat star join: the plant name is the lower-cased",
        "`plantdb:normalizedName` written straight into the pattern, and every",
        "column is named after the defined predicate it comes from. The .rq",
        "files run as-is against the slice.",
        "",
    ]
    for fname, sparql, *cols in QUERIES:
        t0 = time.time()
        rows = [[bind(sol, c) for c in cols] for sol in store.query(sparql)]
        ms = (time.time() - t0) * 1000
        print(f"  {fname}: {len(rows)} rows in {ms:.1f} ms")
        lines += [
            f"## `{fname}`",
            "",
            "```sparql", sparql, "```",
            "",
            f"**{len(rows)} rows** in {ms:.1f} ms (Oxigraph, in-memory)",
            "",
            "| " + " | ".join(cols) + " |",
            "|" + "---|" * len(cols),
        ]
        for r in rows:
            lines.append("| " + " | ".join(cell(t) for t in r) + " |")
        lines.append("")

    with open(MD, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    print(f"wrote {MD}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

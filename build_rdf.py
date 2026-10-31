#!/usr/bin/env python3
"""Export the plant index as N-Triples for SPARQL serving (issues #11, #15).

Reads plant_data/plant_index.duckdb, writes N-Triples under plant_data/rdf/.

`--slice 200` restricts the export to the 200 most-observed Dutch plants (plus
hand-picked edge cases), their gemeenten facts and their books. Without --slice
the same code exports everything (issue #15).

Locked unit-of-analysis decisions (2026-10-31, measured against the live DB):
  1. A plant entity is a WFO record that is NOT a synonym, or an observation
     name-key (no WFO record). Synonyms hang on the accepted plant as
     skos:altLabel - they are never entities.
  2. Book mentions roll up to the accepted plant (synonym wfo_ids re-keyed via
     accepted_wfo_id), deduped per (plant, title) with pages summed.
  3. plant_books rows with no wfo_id are not exported (nothing to attach to).
  4. One node per BHL title with the citation; the mention edge carries one
     plantdb:pages integer. Author stays a plain string (no Person nodes).
  5. Only the 355 Dutch areas become schema:Place nodes; foreign areas exist
     only as counts on the plant.
  6. Every observation fact carries a pre-computed plantdb:prevalenceScore
     (plant occurrences / all plant occurrences in that area); zero rows are
     included so one query returns a complete municipality map.

Every plantdb: predicate and every status term carries rdfs:label +
skos:definition in the output, so the endpoint can explain itself.
No Wikidata anything (parked, issue #2).
"""

import argparse
import os
import sys
from datetime import date, timezone, datetime
from urllib.parse import quote

import duckdb

EXPORT_DATE = datetime.now(timezone.utc).strftime("%Y-%m-%d")  # xsd:date form

DB = "plant_data/plant_index.duckdb"
OUT_DIR = "plant_data/rdf"

# ---------------------------------------------------------------- namespaces
WFO = "https://worldfloraonline.org/taxon/"
BASE = "https://plantdb.example.org/"
PRED = BASE + "def/"          # plantdb: predicates and vocabulary terms
TAXON = BASE + "taxon/"       # name-key plant entities (no WFO record)
AREA = BASE + "area/"         # Dutch GADM areas (gemeenten)
OBS = BASE + "obs/"           # plant x area observation facts
MENT = BASE + "mention/"      # plant x book mention edges (reified)
STATUS = BASE + "status/"     # NL status DefinedTerms
VOCAB = BASE + "vocabulary/"
BHL = "https://www.biodiversitylibrary.org/bibliography/"
GBIF = "https://www.gbif.org/species/"
NCBI = "https://www.ncbi.nlm.nih.gov/Taxonomy/Browser/wwwtax.cgi?id="
IPNI = "https://ipni.org/n/"
POWO = "https://powo.science.kew.org/taxon/"

RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
RDFS = "http://www.w3.org/2000/01/rdf-schema#"
SKOS = "http://www.w3.org/2004/02/skos/core#"
XSD = "http://www.w3.org/2001/XMLSchema#"
DWC = "http://rs.tdwg.org/dwc/terms/"
SCHEMA = "https://schema.org/"
QB = "http://purl.org/linked-data/cube#"
OWL = "http://www.w3.org/2002/07/owl#"

# plantdb: vocabulary - label + plain-language definition travel with the data.
# One entry per predicate used anywhere in the export; the emitter asserts this.
DEFINITIONS = {
    # plant facts
    "normalizedName": ("Normalised name", "The plant name in lower case, for case- and accent-insensitive lookup.", "De plantnaam in kleine letters, voor opzoeken zonder hoofdlettergevoeligheid"),
    "bhlTitleCount": ("Books mentioning this plant", "The number of books in the Biodiversity Heritage Library that mention this plant by name.", "Boeken die deze plant noemen"),
    "bhlPageMentions": ("Page mentions in books", "Total number of book pages on which this plant is mentioned, summed over all its names.", "Totaal aantal boekpagina's waarop deze plant voorkomt"),
    "bhlFirstYear": ("First book mention year", "Year of the oldest book that mentions this plant.", "Jaar van het oudste boek dat deze plant noemt"),
    "bhlLastYear": ("Last book mention year", "Year of the newest book that mentions this plant.", "Jaar van het nieuwste boek dat deze plant noemt"),
    "dutchTitleCount": ("Dutch books mentioning this plant", "Number of mentioning books that are either in Dutch or were published in the Netherlands.", "Aantal noemende boeken in het Nederlands of in Nederland uitgegeven"),
    "dutchLanguageTitleCount": ("Dutch-language books", "Number of mentioning books written in Dutch.", "Aantal noemende boeken in het Nederlands"),
    "dutchPublishedTitleCount": ("Books published in NL", "Number of mentioning books published in the Netherlands.", "Aantal noemende boeken in Nederland uitgegeven"),
    "nativeGroup": ("Native group", "Named region group (for example 'Western Europe') where this plant is native.", "Genoemde regio-groep waar deze plant van nature voorkomt"),
    "nativeRegion": ("Native region", "Named region where this plant is native.", "Genoemde regio waar deze plant van nature voorkomt"),
    "nativeRegionCount": ("Native region count", "Number of regions where this plant is native.", "Aantal regio's waar deze plant van nature voorkomt"),
    "introducedRegionCount": ("Introduced region count", "Number of regions where this plant is introduced.", "Aantal regio's waar deze plant ingevoerd is"),
    "regionTotalCount": ("Total region count", "Number of regions this plant occurs in, native plus introduced.", "Aantal regio's waar deze plant voorkomt"),
    "nlStatus": ("Status in the Netherlands", "Whether this plant is native, introduced, invasive or naturalized in the Netherlands, as resolved from four sources.", "Of deze plant in Nederland inheems, ingevoerd, invasief of genaturaliseerd is"),
    "statusConflict": ("Status sources disagree", "True when the Dutch status sources disagree about this plant.", "Waar de Nederlandse bronnen het oneens zijn over deze plant"),
    "invasive": ("Invasive in NL", "True when this plant is classified invasive in the Netherlands.", "Invasief in Nederland"),
    "extinct": ("Extinct in NL", "True when this plant no longer occurs in the Netherlands.", "Uitgestorven in Nederland"),
    "arrivalYear": ("Arrival year in NL", "First known year of arrival in the Netherlands, from the Sinas database of alien species.", "Eerst bekende jaar van aankomst in Nederland"),
    "statusSourceCount": ("Status source count", "How many Dutch sources report a status for this plant.", "Hoeveel Nederlandse bronnen een status voor deze plant melden"),
    "statusWcvp": ("WCVP verdict", "Status for the Netherlands according to the World Checklist of Vascular Plants.", "Status volgens de World Checklist of Vascular Plants"),
    "statusGlonaf": ("GLONAF verdict", "Status according to the Dutch database of alien plants (Glonaf).", "Status volgens Glonaf"),
    "statusSinas": ("SInAS verdict", "Status according to the Sinas database of alien species.", "Status volgens SInAS"),
    "statusGriis": ("GRIIS verdict", "Status according to the Dutch National Register of Alien Species (GRIIS).", "Status volgens GRIIS"),
    "nlAreaCount": ("Areas seen in NL", "Number of Dutch areas (of 355) where this plant has been observed.", "Aantal Nederlandse gebieden (van 355) waar deze plant is waargenomen"),
    "nlOccurrenceCount": ("Observations in NL", "Total observation records for this plant in the Netherlands.", "Totaal aantal waarnemingen van deze plant in Nederland"),
    "occurrencesOutsideNl": ("Observations outside NL", "Observation records for this plant outside the Netherlands.", "Waarnemingen van deze plant buiten Nederland"),
    "occurrencesUnplaced": ("Observations without area", "Observation records without usable coordinates.", "Waarnemingen zonder bruikbare coordinaten"),
    "firstObservationYear": ("First NL observation", "Year of the oldest observation record in the Netherlands.", "Jaar van de oudste waarneming in Nederland"),
    "lastObservationYear": ("Last NL observation", "Year of the newest observation record in the Netherlands.", "Jaar van de nieuwste waarneming in Nederland"),
    "linkSource": ("Observation link source", "How the observation name was linked to a taxonomy record.", "Hoe de naam van de waarneming aan een taxonomische record is gekoppeld"),
    "gbifLinkSource": ("GBIF link source", "How the GBIF identifier was found for this plant.", "Hoe het GBIF-identificatienummer is gevonden"),
    "ncbiLinkSource": ("NCBI link source", "How the NCBI taxonomy identifier was found for this plant.", "Hoe het NCBI-identificatienummer is gevonden"),
    "gbifDatasetKey": ("GBIF dataset code", "Code of the GBIF dataset the Dutch observation records for this name came from.", "Code van de GBIF-dataset waar de Nederlandse waarnemingen van deze naam vandaan komen"),
    # areas
    "hasCode": ("Area code", "Netherlands geographic statistical code (HAS) for this area.", "HAS-gebiedscode voor dit gebied"),
    "areaTotalOccurrences": ("All plant observations here", "Total plant observation records in this area, for every plant together - the denominator of the prevalence score.", "Totaal aantal plantwaarnemingen in dit gebied - de noemer van de prevalentie"),
    "plantSpeciesCount": ("Plant species here", "Number of distinct plant species observed in this area.", "Aantal verschillende plantesoorten in dit gebied"),
    "bboxNorth": ("Area top latitude", "Northern edge of the area's bounding box.", "Noordelijke grens van dit gebied"),
    "bboxSouth": ("Area bottom latitude", "Southern edge of the area's bounding box.", "Zuidelijke grens van dit gebied"),
    "bboxEast": ("Area right longitude", "Eastern edge of the area's bounding box.", "Oostelijke grens van dit gebied"),
    "bboxWest": ("Area left longitude", "Western edge of the area's bounding box.", "Westelijke grens van dit gebied"),
    # observation facts
    "aboutPlant": ("About plant", "The plant this fact is about.", "De plant waarover dit feit gaat"),
    "observedIn": ("Observed in", "The area where these observations were recorded.", "Het gebied waar deze waarnemingen zijn gedaan"),
    "occurrences": ("Observations of this plant here", "Observation records of this plant in this area.", "Waarnemingen van deze plant in dit gebied"),
    "prevalenceScore": ("Prevalence score", "Share of all plant observations in this area that are this plant, from 0 (never seen) to 1 (every record). Lets areas be compared by how dominant one plant is.", "Deel van alle plantwaarnemingen in dit gebied dat deze plant is, van 0 tot 1"),
    "firstRecordYear": ("First observation here", "Year of the oldest observation of this plant in this area.", "Jaar van de oudste waarneming van deze plant in dit gebied"),
    "lastRecordYear": ("Last observation here", "Year of the newest observation of this plant in this area.", "Jaar van de nieuwste waarneming van deze plant in dit gebied"),
    # books
    "authorOfBook": ("Book author", "Author of the book, as recorded by the Biodiversity Heritage Library, as plain text.", "Auteur van het boek zoals BHL die noteert, als tekst"),
    "coverageStartYear": ("Book series first year", "First year covered by this book series.", "Eerste jaar van deze boekserie"),
    "coverageEndYear": ("Book series last year", "Last year covered by this book series.", "Laatste jaar van deze boekserie"),
    "publishedPlace": ("Published place", "Place of publication, recorded for Dutch-relevant titles.", "Plaats van uitgave, genoteerd voor Nederlandse titels"),
    "languageCode": ("Book language code", "BHL language code of the book.", "BHL-taalcode van het boek"),
    "dutchLanguage": ("In Dutch", "True when the book is written in Dutch.", "Boek is in het Nederlands"),
    "dutchPublished": ("Published in NL", "True when the book was published in the Netherlands.", "Boek is in Nederland uitgegeven"),
    "dutchRelevance": ("Dutch-relevant book", "True when the book is in Dutch or was published in the Netherlands - the usual filter for this app.", "Boek is Nederlands of in Nederland uitgegeven"),
    "pages": ("Pages mentioning the plant", "Number of pages of this book on which this plant is mentioned, summed over all names of the plant.", "Aantal pagina's van dit boek waarop deze plant voorkomt"),
    "mentionsBook": ("Mentions book", "The book in which the plant is mentioned.", "Het boek waarin de plant wordt genoemd"),
    "TaxonMention": ("Book mention", "A book mentioning a plant, with the page count as its own node.", "Een boek dat een plant noemt"),
    "StatusVocabulary": ("Dutch status vocabulary", "The possible statuses of a plant in the Netherlands.", "Mogelijke statussen van een plant in Nederland"),
}

STATUS_TERMS = {
    "native": ("Native in NL", "The plant occurs in the Netherlands naturally, without people bringing it."),
    "introduced": ("Introduced in NL", "The plant was brought to the Netherlands by people, deliberately or by accident."),
    "invasive": ("Invasive in NL", "Introduced, and spreading at the cost of nature or society."),
    "naturalized": ("Naturalized in NL", "Introduced, and now sustaining wild populations."),
    "cryptogenic|uncertain": ("Origin uncertain", "Whether the plant is native or introduced is unknown."),
    "introduced; uncertain": ("Introduced (uncertain)", "Reported introduced, but not certain."),
    "native; introduced": ("Native and introduced", "Sources disagree: native in some readings, introduced in others."),
    "introduced; native": ("Introduced and native", "Sources disagree: introduced in some readings, native in others."),
}

# Plants always in the slice, whatever their observation count.
EDGE_CASES = [
    "Quercus robur",            # GBIF name collapses onto accepted WFO record
    "Crassula helmsii",         # sources link a synonym; invasive
    "Amelanchier lamarckii",    # no WFO record - entity keyed by name
    "Verbena × hybrida",        # hybrid name with the multiplication sign
    "Heracleum mantegazzianum", # full stack: invasive, arrival 1900, books
]

EDGE_WFO_NAMES = set(EDGE_CASES)


# ---------------------------------------------------------------- n-triples
def esc(s: str) -> str:
    return (s.replace("\\", "\\\\")
             .replace('"', '\\"')
             .replace("\n", "\\n")
             .replace("\r", "\\r")
             .replace("\t", "\\t"))


def I(iri: str) -> str:  # noqa: E743 - Iri
    return f"<{iri}>"


def L(value, lang=None, dt=None) -> str:
    if isinstance(value, bool):
        return f'"{"true" if value else "false"}"^^<{XSD}boolean>'
    if isinstance(value, float):
        # 9 decimals: the SHACL check recomputes occurrences / areaTotal and
        # compares within 1e-9, so rounding must stay below that
        return f'"{value:.9f}"^^<{XSD}decimal>'
    if isinstance(value, int):
        return f'"{value}"^^<{XSD}integer>'
    if lang:
        return f'"{esc(str(value))}"@{lang}'
    if dt:
        return f'"{esc(str(value))}"^^<{dt}>'
    return f'"{esc(str(value))}"'


class Exporter:
    def __init__(self, fh):
        self.fh = fh
        self.n = 0
        self.used_predicates = set()

    def tri(self, s, p, o):
        self.used_predicates.add(p)
        self.fh.write(f"{s} {p} {o} .\n")
        self.n += 1

    def lit_o(self, value, lang=None, dt=None):
        return L(value, lang, dt)

    def fact(self, s, p, value, lang=None, dt=None):
        if value is None or value == "" or (isinstance(value, float) and value != value):
            return
        self.tri(s, I(p), self.lit_o(value, lang, dt))


def plant_uri(key: str) -> str:
    if key.startswith("wfo-"):
        return WFO + key
    return TAXON + quote(key.lower())


def display_case(name: str) -> str:
    """Proper-case a binomial stored lowercase by the matching pipeline:
    capitalise the first name, or the word after a leading hybrid x sign.
    Formatting only - no other characters are touched."""
    if not name:
        return name
    if name.startswith(("× ", "x ")):
        return name[:2] + display_case(name[2:])
    return name[0].upper() + name[1:]


def area_uri(aid: str) -> str:
    return AREA + quote(aid)


def obs_uri(key: str, aid: str) -> str:
    return OBS + quote(key.lower()) + "/" + quote(aid)


def mention_uri(key: str, title_id: str) -> str:
    return MENT + quote(key.lower()) + "/" + title_id


def status_uri(value: str) -> str:
    return STATUS + quote(value)


# ---------------------------------------------------------------- the export
def pick_slice(con, n: int):
    """Most-observed Dutch plants + edge cases; returns list of keys."""
    rows = con.execute(
        "SELECT key FROM nl_totals ORDER BY occurrences_nl DESC NULLS LAST LIMIT ?",
        [n],
    ).fetchall()
    keys = [r[0] for r in rows]
    forced = con.execute(
        "SELECT key FROM nl_totals WHERE lower(name) IN ("
        + ",".join("?" * len(EDGE_CASES)) + ")",
        [e.lower() for e in EDGE_CASES],
    ).fetchall()
    for r in forced:
        if r[0] not in keys:
            keys.append(r[0])
    return keys


def export(con, fh, slice_keys=None):
    ex = Exporter(fh)
    counts = {}

    if slice_keys is not None:
        key_list = ", ".join(f"'{k.replace(chr(39), chr(39)*2)}'" for k in slice_keys)
        plant_filter = f"key IN ({key_list})"
        wfo_filter = f"wfo_id IN ({key_list})"
    else:
        plant_filter = wfo_filter = "TRUE"

    # ---- plant entities: observation keys (12k incl. name-only), plus WFO
    # records that are not synonyms. Non-synonym WFO records that were never
    # observed carry identity + origin + books; observed ones join onto WFO.
    plants = con.execute(f"""
        SELECT k.key,
               coalesce(p.wfo_id, k.key) AS entity_key,
               coalesce(p.name, k.gbif_name, k.name) AS name,
               coalesce(p.author, k.author) AS author,
               coalesce(p.rank, k.rank) AS rank,
               coalesce(p.status, 'Unlinked') AS status,
               coalesce(p.family, k.family) AS family,
               p.ipni_id, p.wcvp_id, p.powo_id, p.gbif_id, p.ncbi_taxid,
               p.gbif_src, p.ncbi_src, k.link_src, k.gbif_name
        FROM (SELECT key, gbif_name, name, author, rank, family, link_src
              FROM nl_totals WHERE {plant_filter}) k
        LEFT JOIN plant_index p
               ON (p.wfo_id = k.key
                   OR (k.key NOT LIKE 'wfo-%' AND lower(p.name) = lower(k.key)
                       AND p.status <> 'Synonym'))
    """).fetchall()
    # plant entities keyed by their observation key; entity_key drives URIs
    entity_keys = []           # ordered, deduplicated entity keys
    entity_of_obskey = {}      # observation key -> entity key
    info_by_entity = {}
    seen = set()
    for (okey, ekey, name, author, rank, status, family,
         ipni, wcvp_id, powo, gbif_id, ncbi, gbif_src, ncbi_src,
         link_src, gbif_name) in plants:
        if ekey in seen:
            entity_of_obskey[okey] = ekey
            continue
        seen.add(ekey)
        entity_keys.append(ekey)
        entity_of_obskey[okey] = ekey
        info_by_entity[ekey] = dict(
            name=name, author=author, rank=rank, status=status, family=family,
            ipni=ipni, wcvp=wcvp_id, powo=powo, gbif=gbif_id, ncbi=ncbi,
            gbif_src=gbif_src, ncbi_src=ncbi_src, link_src=link_src,
            gbif_name=gbif_name)
    counts["plants"] = len(entity_keys)

    # entities that are WFO records themselves also answer by their wfo_id
    entity_by_wfo = {k: k for k in entity_keys if k.startswith("wfo-")}

    # ---- Dutch areas as schema:Place (355 gemeenten)
    areas = con.execute(
        "SELECT id, name, province, hasc, lat, lng, xmin, xmax, ymin, ymax,"
        " n_names, occurrences FROM obs_areas WHERE in_nl ORDER BY id"
    ).fetchall()
    for (aid, aname, prov, hasc, lat, lng, xmin, xmax, ymin, ymax,
         n_names, occ) in areas:
        a = I(area_uri(aid))
        ex.tri(a, I(RDF_TYPE), I(SCHEMA + "Place"))
        ex.fact(a, SCHEMA + "name", aname, lang="nl")
        ex.fact(a, SCHEMA + "administrativeArea", prov, lang="nl")
        ex.fact(a, PRED + "hasCode", hasc)
        ex.tri(a, I(SCHEMA + "geo"), f"_:geo{aid}")
        ex.tri(f"_:geo{aid}", I(RDF_TYPE), I(SCHEMA + "GeoCoordinates"))
        ex.tri(f"_:geo{aid}", I(SCHEMA + "latitude"), L(float(lat)))
        ex.tri(f"_:geo{aid}", I(SCHEMA + "longitude"), L(float(lng)))
        ex.fact(a, PRED + "bboxNorth", ymax)
        ex.fact(a, PRED + "bboxSouth", ymin)
        ex.fact(a, PRED + "bboxEast", xmax)
        ex.fact(a, PRED + "bboxWest", xmin)
        ex.tri(a, I(PRED + "areaTotalOccurrences"), L(int(occ)))
        ex.tri(a, I(PRED + "plantSpeciesCount"), L(int(n_names)))
    counts["areas"] = len(areas)
    area_totals = {r[0]: r[1] for r in
                   con.execute("SELECT id, occurrences FROM obs_areas WHERE in_nl").fetchall()}

    # ---- plant facts: identity + identifiers + provenance
    for ekey in entity_keys:
        info = info_by_entity[ekey]
        p = I(plant_uri(ekey))
        ex.tri(p, I(RDF_TYPE), I(SCHEMA + "BioChemEntity"))
        ex.tri(p, I(RDF_TYPE), I(DWC + "Taxon"))
        # proper-case display name: from WFO for WFO entities, from the GBIF
        # name for name-key entities (nl_totals.name is lower-cased for those)
        if ekey.startswith("wfo-"):
            display = info["name"] or info["gbif_name"]
        else:
            display = info["gbif_name"] or info["name"]
        display = display_case(display) if display else display
        ex.fact(p, DWC + "scientificName", display)
        if display:
            ex.fact(p, PRED + "normalizedName", display.lower())
        ex.fact(p, DWC + "scientificNameAuthorship", info["author"])
        ex.fact(p, DWC + "taxonRank", info["rank"])
        if info["status"] != "Unlinked":
            ex.fact(p, DWC + "taxonomicStatus", info["status"])
        ex.fact(p, DWC + "family", info["family"])
        if ekey.startswith("wfo-"):
            ex.tri(p, I(OWL + "sameAs"), I(WFO + ekey))
            ex.fact(p, SCHEMA + "identifier", ekey)
        for idval, uri, label in (
                (info["gbif"], GBIF, "gbif"),
                (info["ncbi"], NCBI, "ncbi"),
                (info["ipni"], IPNI, "ipni"),
                (info["powo"], POWO, "powo")):
            if idval:
                ex.fact(p, SCHEMA + "identifier", idval)
                ex.tri(p, I(OWL + "sameAs"), I(uri + idval))
        ex.fact(p, PRED + "gbifLinkSource", info["gbif_src"])
        ex.fact(p, PRED + "ncbiLinkSource", info["ncbi_src"])
        ex.fact(p, PRED + "linkSource", info["link_src"])
    counts["identity_edges"] = ex.n

    # ---- synonyms as skos:altLabel on the accepted plant (never entities)
    syn = con.execute(f"""
        SELECT s.accepted_wfo_id, s.name, s.author
        FROM plant_index s
        WHERE s.status = 'Synonym' AND s.accepted_wfo_id IN (
            SELECT p.wfo_id FROM plant_index p
            WHERE p.wfo_id IN ({",".join("?" * len(entity_by_wfo))}))
    """, list(entity_by_wfo)).fetchall() if entity_by_wfo else []
    n_syn = 0
    for akey, sname, sauthor in syn:
        p = I(plant_uri(entity_by_wfo[akey]))
        label = f"{sname} {sauthor}".strip() if sauthor else sname
        ex.fact(p, SKOS + "altLabel", label)
        n_syn += 1
    counts["synonym_labels"] = n_syn

    # ---- name-only plants: keep the GBIF dataset code as a plain fact.
    # gbif_key here is a download/dataset code (e.g. '92SRN'), NOT a species
    # key - we hold no resolvable GBIF id for these names, so no owl:sameAs.
    for okey, ekey in entity_of_obskey.items():
        if ekey.startswith("wfo-"):
            continue
        row = con.execute(
            "SELECT gbif_key FROM observations_nl WHERE key = ? AND gbif_key IS NOT NULL LIMIT 1",
            [okey]).fetchone()
        if row:
            ex.fact(I(plant_uri(ekey)), PRED + "gbifDatasetKey", row[0])

    # ---- origin (WCVP distributions via taxon_ranges)
    rng = con.execute(f"""
        SELECT wfo_id, n_native_regions, native_regions, native_groups,
               n_introduced_regions, n_regions_total
        FROM taxon_ranges WHERE {wfo_filter}""").fetchall()
    for wfo, n_nat, regions, groups, n_intro, n_tot in rng:
        p = I(plant_uri(wfo))
        ex.tri(p, I(PRED + "nativeRegionCount"), L(int(n_nat)))
        if regions:
            for r in regions.split(", "):
                ex.fact(p, PRED + "nativeRegion", r, lang="en")
        if groups:
            for g in groups.split(", "):
                ex.fact(p, PRED + "nativeGroup", g, lang="en")
        ex.tri(p, I(PRED + "introducedRegionCount"), L(int(n_intro)))
        ex.tri(p, I(PRED + "regionTotalCount"), L(int(n_tot)))
    counts["origin_rows"] = len(rng)

    # ---- NL status: resolved value as a DefinedTerm + source verdicts
    st = con.execute(f"""
        SELECT wfo_id, status, conflict, invasive, extinct, arrival_year,
               n_sources, wcvp_status, glonaf_status, sinas_status, griis_status
        FROM nl_status WHERE {wfo_filter}""").fetchall()
    for (wfo, status, conflict, inv, ext, ayear, nsrc,
         wcvp_s, glonaf_s, sinas_s, griis_s) in st:
        p = I(plant_uri(wfo))
        ex.tri(p, I(PRED + "nlStatus"), I(status_uri(status)))
        ex.tri(p, I(PRED + "statusConflict"), L(bool(conflict)))
        ex.tri(p, I(PRED + "invasive"), L(bool(inv)))
        ex.tri(p, I(PRED + "extinct"), L(bool(ext)))
        ex.fact(p, PRED + "arrivalYear", ayear)
        ex.tri(p, I(PRED + "statusSourceCount"), L(int(nsrc)))
        ex.fact(p, PRED + "statusWcvp", wcvp_s)
        ex.fact(p, PRED + "statusGlonaf", glonaf_s)
        ex.fact(p, PRED + "statusSinas", sinas_s)
        ex.fact(p, PRED + "statusGriis", griis_s)
    counts["status_rows"] = len(st)

    # ---- NL observation totals per plant (foreign areas stay counts only)
    tot = con.execute(f"""
        SELECT key, coalesce(n_areas_nl, 0), coalesce(occurrences_nl, 0),
               coalesce(occurrences_all, 0), coalesce(occurrences_no_area, 0),
               coalesce(occurrences_outside_nl, 0), first_year, last_year
        FROM nl_totals WHERE {plant_filter}""").fetchall()
    for (okey, n_areas, occ_nl, occ_all, occ_none, occ_out, fy, ly) in tot:
        ekey = entity_of_obskey[okey]
        p = I(plant_uri(ekey))
        ex.tri(p, I(PRED + "nlAreaCount"), L(int(n_areas)))
        ex.tri(p, I(PRED + "nlOccurrenceCount"), L(int(occ_nl)))
        ex.tri(p, I(PRED + "occurrencesOutsideNl"), L(int(occ_out)))
        ex.tri(p, I(PRED + "occurrencesUnplaced"), L(int(occ_none)))
        ex.fact(p, PRED + "firstObservationYear", fy)
        ex.fact(p, PRED + "lastObservationYear", ly)
    counts["obs_totals_rows"] = len(tot)

    # ---- books: one CreativeWork per title mentioned by the exported plants,
    # mention edges reified with a single pages integer (rollup decision 2)
    ment = con.execute(f"""
        WITH keyed AS (
            SELECT coalesce(nullif(p.accepted_wfo_id, ''), b.wfo_id) AS ekey,
                   b.title_id,
                   sum(b.pages) AS pages,
                   bool_or(b.dutch_language) AS d_lang,
                   bool_or(b.dutch_published) AS d_pub,
                   bool_or(b.dutch <> '') AS d_any,
                   max(b.published_place) AS place
            FROM plant_books b
            JOIN plant_index p USING (wfo_id)
            WHERE coalesce(nullif(p.accepted_wfo_id, ''), b.wfo_id)
                  IN ({",".join("?" * len(entity_keys))})
            GROUP BY 1, 2)
        SELECT m.ekey, m.title_id, m.pages, m.d_lang, m.d_pub, m.d_any, m.place,
               t."FullTitle", t."TL2Author", t."LanguageCode",
               t."StartYear", t."EndYear", t."TitleURL"
        FROM keyed m
        LEFT JOIN bhl_titles t ON t."TitleID" = m.title_id
    """, entity_keys).fetchall()
    books_seen = {}
    for (ekey, tid, pages, d_lang, d_pub, d_any, place,
         full_title, author_of_book, lang_code, start_y, end_y, title_url) in ment:
        bu = BHL + tid
        if tid not in books_seen:
            b = I(bu)
            ex.tri(b, I(RDF_TYPE), I(SCHEMA + "CreativeWork"))
            # NDE SCHEMA-AP requires exactly one sdDatePublished per CreativeWork:
            # the date this record was last (re)generated.
            ex.tri(b, I(SCHEMA + "sdDatePublished"), f'"{EXPORT_DATE}"^^<{XSD}date>')
            if full_title:
                ex.fact(b, SCHEMA + "name", full_title, lang="mul")
            ex.fact(b, PRED + "authorOfBook", author_of_book)
            sy = int(start_y) if (start_y or "").isdigit() else None
            ey = int(end_y) if (end_y or "").isdigit() else None
            ex.fact(b, PRED + "coverageStartYear", sy)
            ex.fact(b, PRED + "coverageEndYear", ey)
            ex.fact(b, PRED + "publishedPlace", place)
            ex.fact(b, PRED + "languageCode", lang_code)
            iso = {"ENG": "en", "GER": "de", "FRE": "fr", "LAT": "la",
                   "SPA": "es", "ITA": "it", "DUT": "nl", "DUTCH": "nl",
                   "RUS": "ru", "SCR": "sr", "POL": "pl", "SWE": "sv",
                   "DAN": "da", "POR": "pt", "FIN": "fi", "GRE": "el",
                   "HUN": "hu", "CZE": "cs", "UKR": "uk"}.get(lang_code)
            if iso:
                ex.fact(b, SCHEMA + "inLanguage", iso)
            ex.tri(b, I(PRED + "dutchLanguage"), L(bool(d_lang)))
            ex.tri(b, I(PRED + "dutchPublished"), L(bool(d_pub)))
            ex.tri(b, I(PRED + "dutchRelevance"), L(bool(d_any)))
            if title_url:
                # explicit BHL link, so Query C can SELECT it as part of the
                # citation; our book URI is the https form of the same page
                ex.tri(b, I(SCHEMA + "url"), I(title_url))
                ex.tri(b, I(OWL + "sameAs"), I(title_url))
            books_seen[tid] = True
        p = I(plant_uri(ekey))
        mu = I(mention_uri(ekey, tid))
        ex.tri(mu, I(RDF_TYPE), I(PRED + "TaxonMention"))
        ex.tri(mu, I(PRED + "aboutPlant"), p)
        ex.tri(mu, I(PRED + "mentionsBook"), I(bu))
        ex.tri(mu, I(PRED + "pages"), L(int(pages)))
    counts["books"] = len(books_seen)
    counts["mentions"] = len(ment)

    # ---- per-plant BHL counts, computed from the rolled-up mentions so
    # Query A's numbers always match Query C's answer (single source of truth)
    agg = con.execute(f"""
        SELECT ekey, count(*) AS titles, sum(pages) AS pages FROM (
            WITH keyed AS (
                SELECT coalesce(nullif(p.accepted_wfo_id, ''), b.wfo_id) AS ekey,
                       b.title_id, sum(b.pages) AS pages
                FROM plant_books b JOIN plant_index p USING (wfo_id)
                WHERE coalesce(nullif(p.accepted_wfo_id, ''), b.wfo_id)
                      IN ({",".join("?" * len(entity_keys))})
                GROUP BY 1, 2)
            SELECT ekey, title_id, pages FROM keyed)
        GROUP BY ekey""", entity_keys).fetchall()
    dutch_agg = con.execute(f"""
        WITH keyed AS (
            SELECT coalesce(nullif(p.accepted_wfo_id, ''), b.wfo_id) AS ekey,
                   b.title_id,
                   bool_or(b.dutch_language) AS d_lang,
                   bool_or(b.dutch_published) AS d_pub,
                   bool_or(b.dutch <> '') AS d_any
            FROM plant_books b JOIN plant_index p USING (wfo_id)
            WHERE coalesce(nullif(p.accepted_wfo_id, ''), b.wfo_id)
                  IN ({",".join("?" * len(entity_keys))})
            GROUP BY 1, 2)
        SELECT ekey, count(*) FILTER (d_any), count(*) FILTER (d_lang),
               count(*) FILTER (d_pub)
        FROM keyed GROUP BY ekey""", entity_keys).fetchall()
    year_agg = {}
    if ment:
        for ekey, fy, ly in con.execute(f"""
            SELECT ekey, min(first_year), max(last_year) FROM (
                WITH keyed AS (
                    SELECT coalesce(nullif(p.accepted_wfo_id, ''), b.wfo_id) AS ekey,
                           b.title_id, min(b.first_year) AS first_year,
                           max(b.last_year) AS last_year
                    FROM plant_books b JOIN plant_index p USING (wfo_id)
                    WHERE coalesce(nullif(p.accepted_wfo_id, ''), b.wfo_id)
                          IN ({",".join("?" * len(entity_keys))})
                    GROUP BY 1, 2)
                SELECT ekey, first_year, last_year FROM keyed)
            GROUP BY ekey""", entity_keys).fetchall():
            year_agg[ekey] = (fy, ly)
    agg_map = {r[0]: (r[1], r[2]) for r in agg}
    dutch_map = {r[0]: (r[1], r[2], r[3]) for r in dutch_agg}
    for ekey in entity_keys:
        p = I(plant_uri(ekey))
        if ekey in agg_map:
            titles, pages = agg_map[ekey]
            ex.tri(p, I(PRED + "bhlTitleCount"), L(int(titles)))
            ex.tri(p, I(PRED + "bhlPageMentions"), L(int(pages)))
            fy, ly = year_agg.get(ekey, (None, None))
            ex.fact(p, PRED + "bhlFirstYear", fy)
            ex.fact(p, PRED + "bhlLastYear", ly)
        if ekey in dutch_map:
            d_any, d_lang, d_pub = dutch_map[ekey]
            ex.tri(p, I(PRED + "dutchTitleCount"), L(int(d_any)))
            ex.tri(p, I(PRED + "dutchLanguageTitleCount"), L(int(d_lang)))
            ex.tri(p, I(PRED + "dutchPublishedTitleCount"), L(int(d_pub)))

    # ---- observation facts: plant x NL area, prevalence pre-computed,
    # zero rows included so Query B returns one complete map
    obs = con.execute(f"""
        SELECT key, area_id, occurrences, first_year, last_year
        FROM observations_nl
        WHERE in_nl AND {plant_filter}""").fetchall()
    present = set()
    n_obs = 0
    for (okey, aid, occ, fy, ly) in obs:
        ekey = entity_of_obskey[okey]
        o = I(obs_uri(ekey, aid))
        ex.tri(o, I(RDF_TYPE), I(QB + "Observation"))
        ex.tri(o, I(PRED + "aboutPlant"), I(plant_uri(ekey)))
        ex.tri(o, I(PRED + "observedIn"), I(area_uri(aid)))
        total = area_totals[aid]
        ex.tri(o, I(PRED + "occurrences"), L(int(occ)))
        ex.tri(o, I(PRED + "areaTotalOccurrences"), L(int(total)))
        ex.tri(o, I(PRED + "prevalenceScore"), L(round(occ / total, 6)))
        ex.fact(o, PRED + "firstRecordYear", fy)
        ex.fact(o, PRED + "lastRecordYear", ly)
        present.add((ekey, aid))
        n_obs += 1
    # zero rows: every exported plant x every Dutch area not already present
    for ekey in entity_keys:
        for aid, total in area_totals.items():
            if (ekey, aid) in present:
                continue
            o = I(obs_uri(ekey, aid))
            ex.tri(o, I(RDF_TYPE), I(QB + "Observation"))
            ex.tri(o, I(PRED + "aboutPlant"), I(plant_uri(ekey)))
            ex.tri(o, I(PRED + "observedIn"), I(area_uri(aid)))
            ex.tri(o, I(PRED + "occurrences"), L(0))
            ex.tri(o, I(PRED + "areaTotalOccurrences"), L(int(total)))
            ex.tri(o, I(PRED + "prevalenceScore"), L(0.0))
            n_obs += 1
    counts["observations"] = n_obs

    # ---- vocabulary: every plantdb: predicate and status term explains itself
    for pred, (label, definition, label_nl) in DEFINITIONS.items():
        u = I(PRED + pred)
        ex.tri(u, I(RDFS + "label"), L(label, lang="en"))
        ex.tri(u, I(RDFS + "label"), L(label_nl, lang="nl"))
        ex.tri(u, I(SKOS + "definition"), L(definition, lang="en"))
    vocab_set = I(VOCAB + "nl-status")
    ex.tri(vocab_set, I(RDF_TYPE), I(SCHEMA + "DefinedTermSet"))
    ex.tri(vocab_set, I(SCHEMA + "name"), L("Dutch plant status vocabulary", lang="en"))
    for value, (label, definition) in STATUS_TERMS.items():
        u = I(status_uri(value))
        ex.tri(u, I(RDF_TYPE), I(SCHEMA + "DefinedTerm"))
        ex.tri(u, I(RDFS + "label"), L(label, lang="en"))
        ex.tri(u, I(SKOS + "definition"), L(definition, lang="en"))
        ex.tri(vocab_set, I(SCHEMA + "hasPart"), u)

    # ---- self-check: every plantdb: predicate used must have a definition
    used = {u[len(PRED):] for u in
            [p for p in ex.used_predicates if p.startswith(I(PRED))]}
    missing = used - set(DEFINITIONS)
    if missing:
        print(f"FATAL: plantdb: predicates used but not defined: {missing}",
              file=sys.stderr)
        sys.exit(1)

    return counts, ex.n


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--slice", type=int, metavar="N", default=None,
                    help="export only the N most-observed plants + edge cases")
    ap.add_argument("--force", action="store_true",
                    help="regenerate even if the output already exists")
    ap.add_argument("--out", default=os.path.join(OUT_DIR, "slice.nt"),
                    help="output file (default plant_data/rdf/slice.nt)")
    args = ap.parse_args()

    if not os.path.exists(DB):
        sys.exit(f"no database at {DB}")
    if os.path.exists(args.out) and not args.force:
        print(f"{args.out} exists (use --force to regenerate)")
        return

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    con = duckdb.connect(DB, read_only=True)
    slice_keys = pick_slice(con, args.slice) if args.slice else None
    part = args.out + ".part"
    with open(part, "w", encoding="utf-8") as fh:
        counts, triples = export(con, fh, slice_keys)
    os.replace(part, args.out)

    print(f"wrote {args.out}: {triples:,} triples")
    for k, v in counts.items():
        print(f"  {k:20} {v:,}")


if __name__ == "__main__":
    main()

"""
Ask the plant index questions from Python.

    from plant_query import books, plants, origin, nl_status

    books("Typha latifolia")                     every BHL title mentioning it
    books("Typha latifolia", dutch=True)         only the Dutch ones
    books("Quercus robur", dutch="published")    printed in the Netherlands
    plants("Typha")                              what the index knows, by name or id
    origin("Quercus robur")                      where it is from, and where it got to
    nl_status("Heracleum mantegazzianum")        native / alien / invasive in the Netherlands
    nl_status(status="invasive")                 every invasive alien species in the NL

Everything is read-only: the database is opened with read_only=True, so this never
fights a running build (and works while one runs, as long as no other writer holds it).

Name resolution goes through plant_index: exact normalised name (hybrid x, spacing and
case insensitive), then WFO / WCVP / POWO / IPNI / GBIF id. Homonyms come back as several
rows rather than a guess. Wikidata is parked (issue #2), so no common-name lookup here.

Issue #9 folds this module into a `plantdb/` package; the functions stay as they are.
"""
import re
from pathlib import Path

import duckdb

DATA = Path(__file__).resolve().parent / "plant_data"
DB = DATA / "plant_index.duckdb"

# same normalisation every build script uses
NORM = "lower(trim(regexp_replace(replace({c}, '\u00d7', 'x '), '\\s+', ' ', 'g')))"

ID_COLUMNS = ("wfo_id", "wcvp_id", "powo_id", "ipni_id", "gbif_id", "ncbi_taxid")


def norm(text):
    """Canonical form of a name as a *value*: lower case, single spaces, hybrid x plain."""
    return re.sub(r"\s+", " ", str(text).replace("\u00d7", "x ").strip()).lower()


def sql_norm(column):
    """The same normalisation as a SQL expression over a column."""
    return NORM.format(c=column)


def connect():
    """Read-only connection, so queries never block or corrupt a build."""
    if not DB.exists():
        raise SystemExit(f"No database at {DB} - run build_index.py first.")
    try:
        con = duckdb.connect(str(DB), read_only=True)
    except duckdb.IOException as e:
        raise SystemExit(f"Cannot open {DB} read-only (is a build running?):\n{e}")
    con.execute("SET preserve_insertion_order = false")
    return con


def _table(con, name):
    """Table or view name if it exists, else '' - so a missing derived table can be
    reported as 'run build_x.py' instead of a SQL error."""
    return name if con.execute("SELECT count(*) FROM information_schema.tables "
                               "WHERE table_name = ?", [name]).fetchone()[0] else ""


def _where_name(con, needle, table="plant_index"):
    """SQL condition matching one name or id, plus the resolved normalised name."""
    n = str(needle).strip()
    if not n:
        raise ValueError("empty name")
    if n.lower().startswith("wfo-") or n.isdigit():
        cols = " OR ".join(f"cast({c} as VARCHAR) = ?" for c in ID_COLUMNS)
        params = [n] * len(ID_COLUMNS)
        rows = con.execute(f"SELECT DISTINCT {NORM.format(c='name')} FROM {table} "
                           f"WHERE {cols}", params).fetchall()
        if not rows:
            raise LookupError(f"no plant in {table} with id {n!r}")
        return [r[0] for r in rows]
    exact = con.execute(f"SELECT DISTINCT {NORM.format(c='name')} FROM {table} "
                        f"WHERE {NORM.format(c='name')} = ?", [norm(n)]).fetchall()
    if exact:
        return [r[0] for r in exact]
    near = con.execute(f"SELECT name, {NORM.format(c='name')} n FROM {table} "
                       f"GROUP BY 1 ORDER BY levenshtein(n, ?) LIMIT 6", [norm(n)]).fetchall()
    hint = ", ".join(r[0] for r in near) or "nothing close"
    raise LookupError(f"no plant named {n!r} in {table}; closest: {hint}")


def plants(needle, con=None):
    """Every plant_index row matching this name or id (several rows = homonyms)."""
    own = con is None
    con = con or connect()
    try:
        names = _where_name(con, needle)
        marks = ", ".join("?" for _ in names)
        return con.execute(f"""
            SELECT name, author, rank, status, family, wfo_id, accepted_wfo_id,
                   wcvp_id, powo_id, ipni_id, gbif_id, gbif_src, ncbi_taxid, in_megatree
            FROM plant_index WHERE {NORM.format(c='name')} IN ({marks})
            ORDER BY name, author""", names).fetchdf()
    finally:
        if own:
            con.close()


def _match(con, needle, id_column="wfo_id"):
    """
    SQL condition + params selecting one plant's rows in a derived table.

    Matches both spellings of an identity: the normalised name (so a plant the range
    sources never linked to a WFO id is still reachable) and every WFO id that name has
    in plant_index (so a homonym does not drag in a different plant with the same name).
    """
    names = _where_name(con, needle)
    conds = [f"{NORM.format(c='name')} IN ({', '.join('?' for _ in names)})"]
    params = list(names)
    # the derived tables key on the *accepted* WFO record (build_status canonicalises
    # synonym ids), so ask with accepted ids: an id needle that happens to be a synonym
    # still finds its plant
    ids = [r[0] for r in con.execute(
        f"SELECT DISTINCT coalesce(accepted_wfo_id, wfo_id) FROM plant_index "
        f"WHERE {NORM.format(c='name')} IN ({', '.join('?' for _ in names)})", names
    ).fetchall() if r[0]]
    if ids:
        conds.append(f"{id_column} IN ({', '.join('?' for _ in ids)})")
        params += ids
    return "(" + " OR ".join(conds) + ")", params


def origin(needle, con=None):
    """
    Where a plant is from: its native range, and how far it has travelled.

    Returns one row per plant_index match (homonyms give several): wfo_id, name, author,
    rank, family, n_native_regions, native_regions, native_groups, n_introduced_regions,
    n_regions_total, sources.

    `native_regions` is the TDWG region names when the range is small enough to read
    (up to 12); past that the same information is summarised as `native_groups`, its
    TDWG level-2 regions. Counts are TDWG regions from WCVP, the only source with global
    coverage. Needs build_status.py.
    """
    own = con is None
    con = con or connect()
    try:
        table = _table(con, "taxon_ranges")
        if not table:
            raise LookupError("no taxon_ranges table - run build_status.py first")
        cond, params = _match(con, needle)
        return con.execute(f"""
            SELECT wfo_id, name, author, rank, family, n_native_regions, native_regions,
                   native_groups, n_introduced_regions, n_regions_total, sources
            FROM {table} WHERE {cond}
            ORDER BY name, author""", params).fetchdf()
    finally:
        if own:
            con.close()


def nl_status(needle=None, status=None, limit=None, con=None):
    """
    Is it native here, or an alien that arrived - and is it invasive?

    needle=None       every taxon any source records for the Netherlands (4,806 of them)
    status='native' / 'alien' / 'invasive' / 'naturalized' / 'introduced'
                      'alien' means anything not called native: the answer to "is this
                      species alien in the Netherlands?" for a whole list at once.

    Returns wfo_id, name, author, rank, status, status_source, conflict, one column per
    source (wcvp_status, glonaf_status, sinas_status, griis_status), n_sources,
    n_sources_native, n_sources_alien, invasive, extinct, arrival_year, arrival_source.

    `status` is the resolved answer (GRIIS > SInAS > GloNAF > WCVP, and any source that
    says invasive wins); the four source columns stay so the disagreement is visible,
    and `conflict` marks the 88 taxa where sources differ. `arrival_year` comes from
    SInAS - GRIIS-NL has almost no dates. An empty frame means no source records this
    plant in the Netherlands, which for a garden escape is itself the answer. Needs
    build_status.py.
    """
    own = con is None
    con = con or connect()
    try:
        table = _table(con, "nl_status")
        if not table:
            raise LookupError("no nl_status table - run build_status.py first")
        sql, params = f"SELECT * FROM {table}", []
        clauses = []
        if needle is not None:
            cond, params = _match(con, needle)
            clauses.append(cond)
        if status:
            if status == "alien":
                clauses.append("status <> 'native'")
            elif status in ("native", "invasive", "naturalized", "introduced"):
                clauses.append("status = ?")
                params.append(status)
            else:
                raise ValueError("status must be native, alien, invasive, naturalized "
                                 "or introduced")
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY name, author"
        if limit:
            sql += " LIMIT ?"
            params.append(int(limit))
        return con.execute(sql, params).fetchdf()
    finally:
        if own:
            con.close()


def books(needle, dutch=None, limit=100, con=None):
    """
    BHL titles mentioning a plant, most-mentioned first.

    dutch=None        every book that mentions it
    dutch=True        any Dutch connection (language or imprint)
    dutch='language'  Dutch-language (LanguageCode DUT/NLA)
    dutch='published' printed in the Netherlands (Amstelodami, Lugduni Batavorum, ...)
    dutch='both'      Dutch-language AND Dutch-published

    Returns: nname, wfo_id, name, author, rank, full_title, short_title, author_of_book,
    start_year, end_year, language_code, publication_details, published_place, dutch,
    pages, copies, first_year, last_year, call_number, institution, volume, item_url,
    pdf_url, title_url.

    `pages` counts page mentions summed over digitised copies; `dutch` says why a row was
    matched. Needs build_books.py: without it there is no plant_books table.
    """
    own = con is None
    con = con or connect()
    try:
        view = _table(con, "plant_books_full")
        if not view:
            raise LookupError("no plant_books table - run build_books.py first")
        names = _where_name(con, needle)
        marks = ", ".join("?" for _ in names)
        sql = f"SELECT * FROM {view} WHERE nname IN ({marks})"
        params = list(names)
        if dutch is True:
            sql += " AND dutch <> ''"
        elif isinstance(dutch, str):
            if dutch not in ("language", "published", "both"):
                raise ValueError("dutch must be None, True, 'language', 'published' or 'both'")
            sql += " AND dutch = ?"
            params.append(dutch)
        elif dutch is not None:
            raise ValueError("dutch must be None, True, 'language', 'published' or 'both'")
        sql += " ORDER BY pages DESC, last_year NULLS LAST"
        if limit:
            sql += " LIMIT ?"
            params.append(int(limit))
        return con.execute(sql, params).fetchdf()
    finally:
        if own:
            con.close()


if __name__ == "__main__":
    import sys
    arg = sys.argv[1] if len(sys.argv) > 1 else "Typha latifolia"
    print(plants(arg).to_string(index=False))
    print()
    print(origin(arg).to_string(index=False))
    print()
    print(nl_status(arg).to_string(index=False))
    print()
    print(books(arg, limit=10)[["full_title", "language_code", "published_place", "dutch",
                                "pages", "first_year", "last_year"]].to_string(index=False))

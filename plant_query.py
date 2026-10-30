"""
Ask the plant index questions from Python.

    from plant_query import books, plants

    books("Typha latifolia")                     every BHL title mentioning it
    books("Typha latifolia", dutch=True)         only the Dutch ones
    books("Quercus robur", dutch="published")    printed in the Netherlands
    plants("Typha")                              what the index knows, by name or id

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
    print(books(arg, limit=10)[["full_title", "language_code", "published_place", "dutch",
                                "pages", "first_year", "last_year"]].to_string(index=False))

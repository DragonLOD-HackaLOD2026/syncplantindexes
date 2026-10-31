"""
GBIF: plant species per admin region in the Netherlands, all years, with record
counts and the first and most recent year seen. One small table, not all records.

    python gbif_nl.py              # GADM level 2 = the 352 municipalities
    python gbif_nl.py --admin 3    # GADM level 3 = the ~3,100 wards (bigger)
    python gbif_nl.py --admin 0    # no area: one row per species, all of NL

Uses GBIF's SQL download API, which does the GROUP BY on GBIF's side:
https://techdocs.gbif.org/en/data-use/api-sql-downloads
Output: plant_data/gbif_nl/nl_plants_admin<LEVEL>_all.zip (TSV inside), plus a
.doi.txt with the download's DOI and query (cite the DOI if you publish).

Rerunnable: skips if the output exists; a pending request is remembered in a
.key file, so a rerun waits for it (no login needed) instead of resubmitting.
Nothing is read from or written to the environment; the password is not stored.
"""
import argparse
import getpass
import time
from pathlib import Path

import requests

DATA = Path(__file__).resolve().parent / "plant_data"
OUT_DIR = DATA / "gbif_nl"
COUNTRY = "NL"
API = "https://api.gbif.org/v1/occurrence/download"


def sql(level):
    """One row per species x area. level=0 leaves the area out: one row per species."""
    area = f"level{level}gid, level{level}name, " if level else ""
    return f"""
SELECT specieskey, species, family, {area}
       COUNT(*) AS occurrences, MIN("year") AS first_year, MAX("year") AS last_year
FROM occurrence
WHERE countrycode = '{COUNTRY}' AND kingdom = 'Plantae' AND specieskey IS NOT NULL
GROUP BY specieskey, species, family, {area}
"""


def log(*a):
    print(*a, flush=True)


def submit(query):
    user = input("GBIF username (not email): ").strip()
    pwd = getpass.getpass("GBIF password: ")
    r = requests.post(f"{API}/request", auth=(user, pwd), timeout=60,
                      json={"format": "SQL_TSV_ZIP", "sql": query})
    if r.status_code == 401:
        raise SystemExit("GBIF: wrong username or password.")
    if r.status_code != 201:
        raise SystemExit(f"GBIF refused the request ({r.status_code}): {r.text}")
    return r.text.strip()


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--admin", type=int, choices=(0, 1, 2, 3), default=2,
                    help="GADM level: 0 = no area, 1 = province, 2 = municipality "
                         "(default), 3 = ward/sub-division")
    level = ap.parse_args().admin
    query = sql(level)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"nl_plants_admin{level}_all.zip"
    keyfile = out.with_suffix(".key")
    if out.exists():
        log("skip", out.name)
        return

    if keyfile.exists():
        key = keyfile.read_text().strip()
        log("resuming GBIF download", key)
    else:
        key = submit(query)
        keyfile.write_text(key)
        log("submitted GBIF download", key)

    while True:
        meta = requests.get(f"{API}/{key}", timeout=60).json()
        status = meta.get("status")
        if status == "SUCCEEDED":
            break
        if status in ("FAILED", "KILLED", "CANCELLED", "FILE_ERASED"):
            keyfile.unlink(missing_ok=True)
            raise SystemExit(f"GBIF download {key} ended with {status}; rerun to resubmit.")
        log(f"  {status} ... checking again in 60 s (Ctrl+C is safe; rerun resumes)")
        time.sleep(60)

    log("downloading", out.name, "| DOI:", meta.get("doi"))
    tmp = out.with_suffix(".zip.part")
    with requests.get(f"{API}/request/{key}.zip", stream=True, timeout=(30, 600)) as r:
        r.raise_for_status()
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
    tmp.rename(out)
    keyfile.unlink(missing_ok=True)
    out.with_suffix(".doi.txt").write_text(f"{meta.get('doi')}\n{query}")
    log("done ->", out)


if __name__ == "__main__":
    main()
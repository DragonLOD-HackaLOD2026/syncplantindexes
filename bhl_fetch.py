"""
BHL fetch: download + extract only. No database work.

    python bhl_fetch.py

Downloads BHL's monthly TSV export (data.zip, 2.4+ GB) to plant_data/bhl/ and
extracts the four tables needed: pagename, page, item, title.
Rerunnable: existing files are skipped. Next step: python bhl_build.py
Schema: https://www.biodiversitylibrary.org/data/TSV/BHLExportSchema.pdf
"""
import zipfile
from pathlib import Path

import requests

DATA = Path(__file__).resolve().parent / "plant_data"
BHL = DATA / "bhl"
BHL_ZIP_URL = "https://www.biodiversitylibrary.org/data/TSV/data.zip"
NEEDED = ("pagename", "page", "item", "title")


def log(*a):
    print(*a, flush=True)


def download():
    BHL.mkdir(parents=True, exist_ok=True)
    dest = BHL / "data.zip"
    if dest.exists():
        log("skip", dest.name)
        return
    log("downloading BHL data.zip (2.4+ GB)")
    tmp = dest.with_suffix(".zip.part")
    with requests.get(BHL_ZIP_URL, stream=True, timeout=(30, 600)) as r:
        r.raise_for_status()
        with open(tmp, "wb") as out:
            for chunk in r.iter_content(1 << 22):
                out.write(chunk)
    tmp.rename(dest)


def extract():
    with zipfile.ZipFile(BHL / "data.zip") as zf:
        for m in zf.namelist():
            dest = BHL / Path(m).name
            if Path(m).stem.lower() not in NEEDED:
                continue
            if dest.exists():
                log("skip", dest.name)
                continue
            log("extracting", m)
            tmp = dest.with_suffix(dest.suffix + ".part")
            with zf.open(m) as src, open(tmp, "wb") as dst:
                while chunk := src.read(1 << 22):
                    dst.write(chunk)
            tmp.rename(dest)
    missing = [n for n in NEEDED if not any(p.stem.lower() == n for p in BHL.glob("*.txt"))]
    if missing:
        log("!! not found in data.zip:", missing, "- contents:", zf.namelist())


if __name__ == "__main__":
    download()
    extract()
    log("done ->", BHL)
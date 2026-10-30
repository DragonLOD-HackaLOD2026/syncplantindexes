"""
Setup for offline plant data. Safe to rerun: anything already installed or
downloaded is skipped.

Requirements: Python 3.9+, R installed (for rpy2), ~20 GB free disk.

GBIF species-list download needs a free GBIF account:
    export GBIF_USER=... GBIF_PWD=... GBIF_EMAIL=...
"""
import importlib.util
import os
import subprocess
import sys
import time
from pathlib import Path

# Everything goes in plant_data/ next to this script, wherever you run it from.
# Existing files are never overwritten: every step skips files that exist.
DATA = Path(os.environ.get("PLANT_SOURCES_DIR",
                           Path(__file__).resolve().parent / "plant_data")).resolve()
DATA.mkdir(exist_ok=True)
print("data folder:", DATA)


# 0. Python packages (installed only if missing) ----------------------------
def setup_python():
    pkgs = {"pygbif": "pygbif", "ete4": "ete4", "rpy2": "rpy2",
            "pandas": "pandas", "duckdb": "duckdb", "requests": "requests"}
    missing = [pip for mod, pip in pkgs.items() if importlib.util.find_spec(mod) is None]
    if missing:
        subprocess.check_call([sys.executable, "-m", "pip", "install", *missing])
    else:
        print("skip python packages")


def download(url, dest):
    """Stream a file to disk, skipping if it already exists."""
    import requests
    dest = DATA / dest
    if dest.exists():
        print(f"skip {dest}")
        return dest
    print(f"downloading {url}")
    tmp = dest.with_suffix(dest.suffix + ".part")  # no half-files left behind
    with requests.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
    tmp.rename(dest)
    return dest


# 1. R packages + WFO backbone + megatree via rpy2 --------------------------
def setup_r():
    import rpy2.robjects as ro

    d = str(DATA.resolve()).replace("\\", "/")
    ro.r(f"""
    # Precompiled Ubuntu 24.04 binaries (no compiling, no linuxbrew lib clashes)
    options(repos = c(CRAN = "https://packagemanager.posit.co/cran/__linux__/noble/latest"),
            HTTPUserAgent = sprintf("R/%s R (%s)", getRversion(),
                paste(getRversion(), R.version$platform, R.version$arch, R.version$os)),
            timeout = 3600)
    # Installed = present in the library; never re-installs or upgrades anything.
    need <- function(p) !nzchar(system.file(package = p))

    for (p in c("remotes", "WorldFlora", "ape", "httr", "rvest", "xml2", "curl", "openssl"))
        if (need(p)) install.packages(p) else message("skip ", p)
    # upgrade = "never": otherwise remotes upgrades (reinstalls/compiles) every
    # outdated dependency, because rpy2 runs R non-interactively.
    gh <- function(p, repo)
        if (need(p)) remotes::install_github(repo, upgrade = "never") else message("skip ", p)
    gh("rWCVP", "matildabrown/rWCVP")            # no longer on CRAN for this R version
    gh("rWCVPdata", "matildabrown/rWCVPdata")
    gh("V.PhyloMaker2", "jinyizju/V.PhyloMaker2")

    # WFO backbone (local CSV). WorldFlora's built-in URL is dead, so get it
    # from Zenodo (https://zenodo.org/records/7460932/latest): download the
    # DwC backbone zip and put classification.csv in plant_data/.
    # Never calls WFO.download: it would overwrite an existing zip.
    wfo <- file.path("{d}", "classification.csv")
    zips <- list.files("{d}", pattern = "^(WFO|_?DwC).*\\\\.zip$",  # not gbif_backbone.zip
                       ignore.case = TRUE, full.names = TRUE)
    if (file.exists(wfo)) {{
        message("skip ", wfo)
    }} else if (length(zips) > 0) {{
        message("unzipping existing ", zips[1])          # extract only, never re-download
        utils::unzip(zips[1], files = "classification.csv", exdir = "{d}", overwrite = FALSE)
    }} else {{
        message("MISSING ", wfo, " - download it manually from Zenodo (see comment)")
    }}

    # Megatree -> Newick for ETE/dendropy
    tre <- file.path("{d}", "megatree_GBOTB.tre")
    if (!file.exists(tre)) {{
        suppressPackageStartupMessages(library(V.PhyloMaker2))
        ape::write.tree(GBOTB.extended.TPL, file = tre)
    }} else message("skip ", tre)
    """)


# 2. NCBI taxonomy -> local SQLite (ETE only downloads if the DB is missing) -
def setup_ncbi():
    from ete4 import NCBITaxa
    db = DATA / "taxa.sqlite"
    if db.exists():
        print(f"skip {db}")
        return
    # ETE saves taxdump.tar.gz in the current directory, so run it inside DATA.
    cwd = os.getcwd()
    os.chdir(DATA)
    try:
        NCBITaxa(dbfile=str(db))  # downloads taxdump + builds the DB here
    finally:
        os.chdir(cwd)
    # Later use: NCBITaxa(dbfile="plant_data/taxa.sqlite", update=False)


# 3. GBIF taxonomy archive ---------------------------------------------------
def setup_gbif_taxonomy():
    # Check https://www.gbif.org/developer/species for the current archive
    # (GBIF moved its taxonomy to Catalogue of Life XR in 2026).
    download("https://hosted-datasets.gbif.org/datasets/backbone/current/backbone.zip",
             "gbif_backbone.zip")


# 4. GBIF species list for a country (a few MB) -----------------------------
def gbif_species_list(country="NL"):
    marker = DATA / f"gbif_species_{country}.zip"
    if marker.exists():
        print(f"skip {marker}")
        return
    user, pwd, email = (os.environ.get(k) for k in ("GBIF_USER", "GBIF_PWD", "GBIF_EMAIL"))
    if not user:
        print("skip GBIF species list (set GBIF_USER/GBIF_PWD/GBIF_EMAIL)")
        return
    from pygbif import occurrences as occ
    key, _ = occ.download([f"country = {country}", "taxonKey = 6"],  # 6 = Plantae
                          format="SPECIES_LIST", user=user, pwd=pwd, email=email)
    while (status := occ.download_meta(key)["status"]) not in ("SUCCEEDED", "FAILED", "KILLED"):
        time.sleep(60)
    if status != "SUCCEEDED":
        print(f"GBIF download {key} ended with {status}")
        return
    res = occ.download_get(key, path=str(DATA))
    Path(res["path"]).rename(marker)


if __name__ == "__main__":
    setup_python()
    setup_r()
    setup_ncbi()
    setup_gbif_taxonomy()
    gbif_species_list("NL")
    # Manual (no stable direct URLs; small files):
    #   GloNAF            - iDiv data portal / Zenodo
    #   GRIIS checklists  - gbif.org, search datasets for "GRIIS"
    #   Alien first records (Seebens et al.) - Zenodo
    print("done ->", DATA.resolve())
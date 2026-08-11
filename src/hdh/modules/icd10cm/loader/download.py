"""Fetch and cache the official CMS ICD-10-CM release files.

`hdh icd load --download` pulls the two public-domain zips for a fiscal
year, extracts the order file and tabular XML into ``~/.hdh/icd10cm/<fy>/``
with normalized names, and returns that directory for the acquire stage.
Files already cached are not re-downloaded. Override the URL templates with
``HDH_ICD_URL_ORDER`` / ``HDH_ICD_URL_TABLES`` if CMS moves them.
"""

from __future__ import annotations

import os
import urllib.request
import zipfile
from pathlib import Path

from hdh.modules.icd10cm.loader import LoadError

ORDER_URL = "https://www.cms.gov/files/zip/{fy}-code-descriptions-tabular-order.zip"
TABLES_URL = "https://www.cms.gov/files/zip/{fy}-code-tables-tabular-and-index.zip"

# zip member (with {fy}) → normalized cache name the acquire stage globs
_WANTED = {
    "order": ("icd10cm_order_{fy}.txt", "icd10cm-order-{fy}.txt"),
    "tables": ("Table and Index/icd10cm_tabular_{fy}.xml", "icd10cm-tabular-{fy}.xml"),
}


def cache_dir(fiscal_year: int) -> Path:
    return Path.home() / ".hdh" / "icd10cm" / str(fiscal_year)


def _fetch(url: str, dest: Path) -> None:
    try:
        with urllib.request.urlopen(url, timeout=120) as response:  # noqa: S310 — https CMS URL
            dest.write_bytes(response.read())
    except OSError as err:
        raise LoadError(f"download failed: {url} ({err})") from None


def download_release(fiscal_year: int) -> Path:
    """Ensure the FY's release files are cached locally; return the directory."""
    target = cache_dir(fiscal_year)
    target.mkdir(parents=True, exist_ok=True)
    urls = {
        "order": os.environ.get("HDH_ICD_URL_ORDER", ORDER_URL),
        "tables": os.environ.get("HDH_ICD_URL_TABLES", TABLES_URL),
    }
    for kind, (member_tpl, normalized_tpl) in _WANTED.items():
        member = member_tpl.format(fy=fiscal_year)
        normalized = target / normalized_tpl.format(fy=fiscal_year)
        if normalized.exists():
            continue
        archive = target / f"{kind}.zip"
        if not archive.exists():
            url = urls[kind].format(fy=fiscal_year)
            print(f"   ⬇ {url}")
            _fetch(url, archive)
        try:
            with zipfile.ZipFile(archive) as zf:
                normalized.write_bytes(zf.read(member))
        except (KeyError, zipfile.BadZipFile) as err:
            raise LoadError(f"{archive.name}: cannot extract '{member}' ({err})") from None
    return target

"""Fetch and cache a SNOMED CT US Edition release via NLM's UTS (design §2).

`hdh snomed load --download` asks the UTS releases index for the wanted
edition, downloads the RF2 zip through the authenticated download
endpoint, and extracts the four Snapshot files into
``~/.hdh/snomed/<release>/``. The zip is fetched with the USER'S OWN
UMLS credential and cached per-user — hdh never ships or redistributes
SNOMED CT content.

The API key is read from ``UMLS_API_KEY`` (or ``HDH_UMLS_API_KEY``),
falling back to a plain ``.env`` in the working directory for shells
that don't load it (only ``just`` recipes do). The key is sent only to
``uts-ws.nlm.nih.gov`` and never printed. Endpoint URLs are overridable
(``HDH_UTS_RELEASES_URL`` / ``HDH_UTS_DOWNLOAD_URL``) in case NLM moves
them; ``HDH_SNOMED_ZIP_URL`` skips the index entirely.
"""

from __future__ import annotations

import json
import os
import re
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

from hdh.modules.snomed.loader import LoadError

RELEASES_URL = "https://uts-ws.nlm.nih.gov/releases?releaseType=snomed-ct-us-edition"
DOWNLOAD_URL = "https://uts-ws.nlm.nih.gov/download"

# zip member basename prefixes → the acquire stage's expected files
_WANTED_PREFIXES = (
    "sct2_Concept_Snapshot",
    "sct2_Description_Snapshot",
    "sct2_Relationship_Snapshot",
    "der2_cRefset_LanguageSnapshot",
)


def cache_dir(release: int) -> Path:
    return Path.home() / ".hdh" / "snomed" / str(release)


def api_key() -> str:
    """The user's UTS key from the environment or a local .env (never printed)."""
    for name in ("UMLS_API_KEY", "HDH_UMLS_API_KEY"):
        value = os.environ.get(name)
        if value:
            return value
    env_file = Path(".env")
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            match = re.match(r"\s*(?:HDH_)?UMLS_API_KEY\s*=\s*(\S+)", line)
            if match:
                return match.group(1).strip("'\"")
    raise LoadError(
        "no UMLS API key found — set UMLS_API_KEY in .env (create one at "
        "https://uts.nlm.nih.gov under My Profile) or export it in the shell"
    )


def _http_json(url: str) -> list[dict]:
    try:
        with urllib.request.urlopen(url, timeout=60) as response:  # noqa: S310 — https NLM URL
            return json.loads(response.read().decode("utf-8"))
    except OSError as err:
        raise LoadError(f"UTS releases index failed: {url} ({err})") from None


def pick_release(release: int | None) -> tuple[int, str]:
    """Resolve (release YYYYMM, zip url) from the UTS index — or from
    HDH_SNOMED_ZIP_URL, which then requires an explicit --release."""
    override = os.environ.get("HDH_SNOMED_ZIP_URL")
    if override:
        if release is None:
            raise LoadError("HDH_SNOMED_ZIP_URL is set — pass --release YYYYMM alongside it")
        return release, override
    entries = _http_json(os.environ.get("HDH_UTS_RELEASES_URL", RELEASES_URL))
    dated: list[tuple[int, str]] = []
    for entry in entries:
        url = entry.get("downloadUrl", "")
        stamp = re.search(r"(\d{8})", url) or re.search(
            r"(\d{4})-?(\d{2})", str(entry.get("releaseDate", ""))
        )
        if not url or stamp is None:
            continue
        yyyymm = int(stamp.group(1)[:6].replace("-", ""))
        dated.append((yyyymm, url))
    if not dated:
        raise LoadError("UTS releases index returned no SNOMED CT US Edition entries")
    dated.sort()
    if release is None:
        return dated[-1]
    for yyyymm, url in dated:
        if yyyymm == release:
            return yyyymm, url
    raise LoadError(f"release {release} not in the UTS index (available: {[r for r, _ in dated]})")


def _fetch_zip(zip_url: str, dest: Path) -> None:
    """Stream the release zip through the authenticated download endpoint."""
    endpoint = os.environ.get("HDH_UTS_DOWNLOAD_URL", DOWNLOAD_URL)
    query = urllib.parse.urlencode({"url": zip_url, "apiKey": api_key()})
    partial = dest.with_suffix(".part")
    try:
        with urllib.request.urlopen(f"{endpoint}?{query}", timeout=600) as response:  # noqa: S310
            with partial.open("wb") as fh:
                while chunk := response.read(1 << 20):
                    fh.write(chunk)
    except OSError as err:
        partial.unlink(missing_ok=True)
        raise LoadError(
            f"authenticated download failed ({err}) — check the UMLS API key and "
            "your UTS license status at https://uts.nlm.nih.gov"
        ) from None
    partial.replace(dest)


def download_release(release: int | None = None) -> tuple[int, Path]:
    """Ensure a US Edition release is cached locally; return (release, dir).

    Snapshot-only extraction: Full/Delta members are ignored (design §5)."""
    resolved, zip_url = pick_release(release)
    target = cache_dir(resolved)
    target.mkdir(parents=True, exist_ok=True)
    if all(list(target.glob(prefix + "*.txt")) for prefix in _WANTED_PREFIXES):
        return resolved, target  # fully cached
    archive = target / "release.zip"
    if not archive.exists():
        print(f"   ⬇ SNOMED CT US Edition {resolved} via UTS (licensed — cached for this user only)")
        _fetch_zip(zip_url, archive)
    try:
        with zipfile.ZipFile(archive) as zf:
            for member in zf.namelist():
                base = Path(member).name
                if "/Snapshot/" in member and any(base.startswith(p) for p in _WANTED_PREFIXES):
                    (target / base).write_bytes(zf.read(member))
    except zipfile.BadZipFile as err:
        raise LoadError(f"{archive.name}: not a valid zip ({err}) — delete it and retry") from None
    missing = [p for p in _WANTED_PREFIXES if not list(target.glob(p + "*.txt"))]
    if missing:
        raise LoadError(f"release zip lacked expected Snapshot files: {missing}")
    return resolved, target

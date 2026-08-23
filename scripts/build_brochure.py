"""Build the agent-first-EHR brochure: standalone HTML, and a PDF.

    uv run --with pillow python scripts/make_social_card.py   # the card first
    uv run python scripts/build_brochure.py                   # then this

The source keeps a ``__CARD__`` placeholder rather than a megabyte of base64,
so the file stays reviewable in a diff. This inlines the card as a data URI —
required, not stylistic: the artifact host's CSP blocks every external
request, so a page that links its image renders empty.

The PDF is printed by headless Chrome, which is the only renderer that
honours the same CSS the page was designed against. No extra dependency: if
Chrome is not installed the HTML is still written and the PDF is skipped.
"""

from __future__ import annotations

import base64
import pathlib
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / "docs" / "brochure" / "agent-first-ehr.src.html"
HTML = ROOT / "docs" / "brochure" / "agent-first-ehr.html"
PDF = ROOT / "docs" / "brochure" / "agent-first-ehr.pdf"
CARD = ROOT / "docs" / "assets" / "hdh-card-wide@2x.png"

#: Where Chrome usually is. Also honours a CHROME env var.
CANDIDATES = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "google-chrome",
    "chromium",
)


def find_chrome() -> str | None:
    import os

    override = os.environ.get("CHROME")
    if override and pathlib.Path(override).exists():
        return override
    for candidate in CANDIDATES:
        if pathlib.Path(candidate).exists():
            return candidate
        found = shutil.which(candidate)
        if found:
            return found
    return None


def build_html() -> str:
    if not CARD.exists():
        sys.exit(f"missing {CARD.relative_to(ROOT)} — run scripts/make_social_card.py first")
    source = SRC.read_text(encoding="utf-8")
    if "__CARD__" not in source:
        sys.exit(f"{SRC.name} has no __CARD__ placeholder — did it get built in place?")
    uri = "data:image/png;base64," + base64.b64encode(CARD.read_bytes()).decode()
    page = source.replace("__CARD__", uri)
    HTML.write_text(page, encoding="utf-8")
    print(f"html  {HTML.relative_to(ROOT)}  ({len(page) / 1024:.0f} KB)")
    return page


def build_pdf() -> None:
    """Render the PDF, and refuse to pretend when it did not render.

    Chrome exits 0 even when it could not write the file — and on Windows it
    cannot, if a PDF viewer has the old one open. The stale PDF then stays on
    disk looking freshly built, which is how a layout fix reached the HTML
    and never reached the PDF. So: render beside the target, check it, and
    only then move it into place.
    """
    import os
    import tempfile

    chrome = find_chrome()
    if chrome is None:
        print("pdf   skipped — no Chrome found (set CHROME=/path/to/chrome)")
        return

    with tempfile.TemporaryDirectory() as scratch:
        staged = pathlib.Path(scratch) / "brochure.pdf"
        profile = pathlib.Path(scratch) / "profile"  # never reuse a cached render
        subprocess.run(
            [
                chrome,
                "--headless",
                "--disable-gpu",
                "--no-pdf-header-footer",
                f"--user-data-dir={profile}",
                f"--print-to-pdf={staged}",
                HTML.as_uri(),
            ],
            check=True,
            capture_output=True,
        )
        if not staged.exists() or staged.stat().st_size < 1024:
            sys.exit("pdf   FAILED — Chrome wrote nothing (it still exits 0 when it doesn't)")
        try:
            os.replace(staged, PDF)
        except PermissionError:
            sys.exit(
                f"pdf   FAILED — {PDF.relative_to(ROOT)} is open in another program.\n"
                "      Close it and run again; the PDF on disk is STALE until you do."
            )

    print(f"pdf   {PDF.relative_to(ROOT)}  ({PDF.stat().st_size / 1024:.0f} KB)")


if __name__ == "__main__":
    build_html()
    build_pdf()

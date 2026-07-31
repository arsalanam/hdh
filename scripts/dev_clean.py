#!/usr/bin/env python
"""Remove caches, coverage output, and build artifacts (cross-platform)."""

import shutil
from pathlib import Path

TARGETS = ("htmlcov", ".pytest_cache", ".mypy_cache", ".ruff_cache", "dist", "build")


def main() -> None:
    """Delete tool caches, coverage files, and __pycache__ directories."""
    for target in TARGETS:
        shutil.rmtree(target, ignore_errors=True)
    Path(".coverage").unlink(missing_ok=True)
    for pycache in [*Path("src").rglob("__pycache__"), *Path("tests").rglob("__pycache__")]:
        shutil.rmtree(pycache, ignore_errors=True)
    print("🧹 cleaned")


if __name__ == "__main__":
    main()

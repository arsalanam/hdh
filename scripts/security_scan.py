#!/usr/bin/env python
"""Security scan stage of the build pipeline (cross-platform).

Audits the locked dependency set for known CVEs with pip-audit (fetched on
demand via uvx), then runs trivy if it is installed. Findings are advisory
for now — set BLOCKING = True to make them fail the build.

Extension points for OWASP tooling:
  - OWASP Dependency-Check:  dependency-check --scan . --format HTML
  - OWASP ZAP (against `just docker-serve`):  zap-baseline.py -t http://localhost:8000
  - Trivy image scan (post-build):  trivy image hdh:latest
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

BLOCKING = False  # flip to make dependency CVEs fail `just build`


def audit_dependencies() -> int:
    """Export uv.lock to requirements and run pip-audit over it."""
    print("── dependency CVE audit (pip-audit over uv.lock) ──────────")
    with tempfile.TemporaryDirectory() as tmp:
        req = Path(tmp) / "requirements-audit.txt"
        subprocess.run(
            [
                "uv",
                "export",
                "--format",
                "requirements-txt",
                "--no-emit-project",
                "--all-extras",
                "--quiet",
                "-o",
                str(req),
            ],
            check=True,
        )
        result = subprocess.run(["uvx", "pip-audit", "-r", str(req), "--disable-pip"], check=False)
    if result.returncode != 0:
        print("⚠  pip-audit reported findings (advisory — see scripts/security_scan.py)")
    return result.returncode


def run_trivy() -> int:
    """Run a trivy filesystem scan when trivy is installed."""
    if not shutil.which("trivy"):
        print("(trivy not installed — add OWASP Dependency-Check / ZAP / trivy here;")
        print(" see the extension points in scripts/security_scan.py)")
        return 0
    print("── trivy filesystem scan ──────────────────────────────────")
    return subprocess.run(
        ["trivy", "fs", "--scanners", "vuln", "--exit-code", "0", "."], check=False
    ).returncode


def main() -> int:
    """Run all scanners; exit non-zero only when BLOCKING is enabled."""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    audit_rc = audit_dependencies()
    trivy_rc = run_trivy()
    if BLOCKING and (audit_rc or trivy_rc):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

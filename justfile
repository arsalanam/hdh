# hdh build pipeline — run `just` to list recipes.
#
# Quality gates (test → coverage → lint → format → types → security) all run
# before a Docker image is produced: `just build`.

set windows-shell := ["bash", "-uc"]

python := if os_family() == "windows" { ".venv/Scripts/python.exe" } else { ".venv/bin/python" }
image := "hdh"
tag := "latest"

# List available recipes
default:
    @just --list

# Install the project with all extras + dev tools into .venv
setup:
    {{python}} -m pip install -e ".[all]"

# ── Quality gates ────────────────────────────────────────────────────────────

# Run unit tests
test:
    {{python}} -m pytest tests/ -q

# Run unit tests and print a coverage report (HTML report in htmlcov/)
coverage:
    {{python}} -m pytest tests/ -q --cov --cov-report=term --cov-report=html

# Lint with ruff
lint:
    {{python}} -m ruff check src tests scripts

# Check formatting with ruff (no changes)
format-check:
    {{python}} -m ruff format --check src tests scripts

# Auto-fix lint findings and reformat the codebase
format:
    {{python}} -m ruff check src tests scripts --fix
    {{python}} -m ruff format src tests scripts

# Static type checks with mypy
typecheck:
    {{python}} -m mypy

# Design-principle checks: contracts, no god classes, pluggable interfaces,
# dependency injection, immutability, injection safety, data abstraction.
quality:
    {{python}} scripts/quality_gate.py

# Security scanning. Currently best-effort; wire your OWASP tooling in here.
#   - OWASP Dependency-Check:  dependency-check --scan . --format HTML
#   - OWASP ZAP (against `just docker-serve`):  zap-baseline.py -t http://localhost:8000
#   - Trivy (image scan, post-build):  trivy image {{image}}:{{tag}}
#   - pip-audit (dependency CVEs):  pip install pip-audit
security:
    #!/usr/bin/env bash
    echo "── security scan ──────────────────────────────────────────"
    if command -v trivy >/dev/null 2>&1; then
        trivy fs --scanners vuln --exit-code 0 .
    elif {{python}} -m pip_audit --version >/dev/null 2>&1; then
        {{python}} -m pip_audit --skip-editable
    else
        echo "no scanner installed — add OWASP Dependency-Check / ZAP / trivy here"
        echo "(see comments above this recipe in the justfile)"
    fi

# All quality gates, in order
qa: test coverage lint format-check typecheck quality security
    @echo "✅ all quality gates passed"

# ── Docker ───────────────────────────────────────────────────────────────────

# Build the Docker image — only after every quality gate passes
build: qa
    docker build -t {{image}}:{{tag}} .
    @echo "✅ built {{image}}:{{tag}}"

# Build without the quality gates (local iteration only)
build-only:
    docker build -t {{image}}:{{tag}} .

# Show the image's CLI help (smoke test)
docker-smoke:
    docker run --rm {{image}}:{{tag}} --help

# Generate a small dataset inside a container, persisted to ./data
docker-generate n="1000":
    docker run --rm -v "{{justfile_directory()}}/data:/data" {{image}}:{{tag}} generate --patients {{n}} --years 2

# Serve the FHIR API from the container against ./data
docker-serve:
    docker run --rm -p 8000:8000 -v "{{justfile_directory()}}/data:/data" {{image}}:{{tag}} serve --host 0.0.0.0 --port 8000

# ── Housekeeping ─────────────────────────────────────────────────────────────

# Remove caches, coverage output, and build artifacts
clean:
    #!/usr/bin/env bash
    rm -rf htmlcov .coverage .pytest_cache .mypy_cache .ruff_cache dist build
    find src tests -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true

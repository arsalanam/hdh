# hdh build pipeline — run `just` to list recipes.
#
# Quality gates (test → coverage → lint → format → types → security) all run
# before a Docker image is produced: `just build`.

# cmd.exe is always present and resolves .exe names natively — no dependence
# on which bash (Git Bash vs WSL) or cygpath happens to be on PATH. Recipes
# are single commands; anything needing logic lives in scripts/*.py.
set windows-shell := ["cmd.exe", "/c"]

# uv manages the venv and lockfile; `uv run` resolves the right interpreter
# on every platform (no .venv/Scripts vs .venv/bin juggling).
run := "uv run"
image := "hdh"
tag := "latest"

# List available recipes
default:
    @just --list

# Create/update .venv from uv.lock with all extras + dev tools
setup:
    uv sync --all-extras

# Upgrade locked dependency versions within pyproject constraints
lock-upgrade:
    uv lock --upgrade

# ── Quality gates ────────────────────────────────────────────────────────────

# Run unit tests
test:
    {{run}} pytest tests/ -q

# Run unit tests and print a coverage report (HTML report in htmlcov/)
coverage:
    {{run}} pytest tests/ -q --cov --cov-report=term --cov-report=html

# Lint with ruff
lint:
    {{run}} ruff check src tests scripts

# Check formatting with ruff (no changes)
format-check:
    {{run}} ruff format --check src tests scripts

# Auto-fix lint findings and reformat the codebase
format:
    {{run}} ruff check src tests scripts --fix
    {{run}} ruff format src tests scripts

# Static type checks with mypy
typecheck:
    {{run}} mypy

# Design-principle checks: contracts, no god classes, pluggable interfaces,
# dependency injection, immutability, injection safety, data abstraction.
quality:
    {{run}} python scripts/quality_gate.py

# Security scanning: pip-audit over uv.lock (advisory), trivy if installed.
# OWASP Dependency-Check / ZAP extension points live in the script.
security:
    {{run}} python scripts/security_scan.py

# All quality gates, in order
qa: test coverage lint format-check typecheck quality security
    @echo ✅ all quality gates passed

# ── Docker ───────────────────────────────────────────────────────────────────

# Build the Docker image — only after every quality gate passes
build: qa
    docker build -t {{image}}:{{tag}} .
    @echo ✅ built {{image}}:{{tag}}

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
    {{run}} python scripts/dev_clean.py

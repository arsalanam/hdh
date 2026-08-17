# hdh build pipeline — run `just` to list recipes.
#
# Quality gates (test → coverage → lint → format → types → security) all run
# before a Docker image is produced: `just build`.

# cmd.exe is always present and resolves .exe names natively — no dependence
# on which bash (Git Bash vs WSL) or cygpath happens to be on PATH. Recipes
# are single commands; anything needing logic lives in scripts/*.py.
set windows-shell := ["cmd.exe", "/c"]

# Load .env (gitignored; see .env.example) into every recipe's environment —
# this is where ANTHROPIC_API_KEY lives for local development.
set dotenv-load := true

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

# Check whether the agent's API key is configured (never prints the key)
check-env:
    {{run}} python scripts/check_env.py

# ── Dependency containers (PostgreSQL + Redis) ───────────────────────────────

# Start PostgreSQL + Redis containers and wait until healthy
deps:
    docker compose -f docker-compose.deps.yml up -d --wait

# Stop the dependency containers (data volume preserved)
deps-down:
    docker compose -f docker-compose.deps.yml down

# Stop the dependency containers and DELETE the data volume
deps-nuke:
    docker compose -f docker-compose.deps.yml down -v

# Run the PostgreSQL integration tests against the `just deps` containers
test-pg:
    {{run}} python scripts/test_pg.py

# Comprehension eval against generator ground truth — BILLABLE, on demand.
# Never part of `just qa`: the suite blocks live API calls (tests/conftest.py).
# ~$0.01 per note; N=25 is the baseline size recorded in the design doc §12.
eval n="25":
    {{run}} hdh comprehend --eval {{n}}

# ── Schema migrations (Alembic over registry-merged metadata) ────────────────

# Autogenerate a migration after editing model code or module schema JSON
db-revision message:
    {{run}} alembic revision --autogenerate -m "{{message}}"

# Apply pending migrations (HDH_DB_URL when set, else the SQLite file)
db-upgrade:
    {{run}} alembic upgrade head

# Mark an existing database (built by create_all) as current — one-time
db-stamp:
    {{run}} alembic stamp head

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

# ── Releasing ────────────────────────────────────────────────────────────────

# Gate a release-asset candidate: FAILS if it contains licensed SNOMED CT
# content (issue #31). Target: a .db file, a .zip holding one, or a DB URL.
release-check target:
    {{run}} python scripts/release_check.py "{{target}}"

# Contributing to hdh

Thanks for your interest in contributing!

## Setup

```bash
git clone <repo-url> && cd hdh
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[all]"
just qa        # tests + coverage + ruff lint/format + mypy + security scan
```

The [`just`](https://github.com/casey/just) recipes are the source of truth
for quality gates; `just build` runs them all and then builds the Docker
image. Run `just format` to auto-fix style before committing.

Generate a small working database for local development:

```bash
hdh generate --patients 500 --years 2
```

## Architecture rules

1. **`hdh.core` is self-contained.** It must never import from `hdh.modules`.
   If a change to core is needed for a module, keep it generic.
2. **Modules depend only on core.** Modules must not import from each other's
   internals. Shared logic belongs in core or a new shared utility.
3. **Heavy dependencies stay optional.** A module's `cli.py` must import only
   the standard library at module level and defer third-party imports into its
   command handlers, so `hdh --help` always works with a core-only install.
   Declare new dependencies as extras in `pyproject.toml`.
4. **No real patient data.** Ever. All data in this repo must be synthetic.
   Do not commit `.db` files, exports, or trained model artifacts.

## Adding a new feature module

1. Create `src/hdh/modules/<name>/` with an `__init__.py` and (optionally) `cli.py`.
2. In `cli.py`, expose `register_cli(subparsers)` that adds argparse subcommands
   and sets `parser.set_defaults(func=handler)` where `handler(session, args)`
   receives an open SQLAlchemy session.
3. Add the module to `CLI_MODULES` in `src/hdh/modules/__init__.py`.
4. Add tests under `tests/` and a short README in the module directory.

## Adding a new condition to the disease engine

Add a `ConditionProfile` entry to `CONDITIONS` in `src/hdh/core/disease_engine.py`
with ICD-10 code, vitals deltas, lab specs, formulary options, follow-up interval,
and (if applicable) seasonal weights. Then wire it into the appropriate age groups
in `pick_condition`.

## Pull requests

- Keep PRs focused; one feature or fix per PR.
- Include tests for new behavior.
- Run `pytest` before submitting.

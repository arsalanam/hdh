"""The same seed must rebuild the same chart — in a *different process*.

The eval cohort is not committed. `cohort.json` says patients are
"regenerated from the seed rather than committed, because generation is
deterministic under one", so every measurement the harness makes rests on
that sentence being true.

It was not. A set of strings iterates in hash order, Python randomises
string hashing per process, and two sites iterated one while drawing from
the RNG — the onset date of each seeded condition, and a child's family
history. Same seed, same machine, two runs: 8 of 205 conditions carried
different onset dates and 28 of 273 notes read differently.

An in-process test cannot see this, because one process has one hash seed.
So this spawns two, with hash seeds chosen to disagree.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap

import pytest

# Small enough to run twice in a test, large enough to include a household
# with children — the child path is where family history is built.
PROGRAM = textwrap.dedent(
    """
    import hashlib, sys
    from sqlalchemy import inspect, select
    from hdh.core.generators import build_dataset
    from hdh.core.models import Base, get_engine, get_session
    from hdh.core.schema_registry import bootstrap_schema

    # Wall-clock, not chart content.
    SKIP = {"created_at", "updated_at"}

    bootstrap_schema()
    engine = get_engine(sys.argv[1])
    Base.metadata.create_all(engine)
    session = get_session(engine)
    build_dataset(session, n_patients=12, years_of_history=3, verbose=False, seed=4242)

    inspector = inspect(engine)
    digest = hashlib.sha256()
    for table in sorted(set(inspector.get_table_names()) & set(Base.metadata.tables)):
        columns = [
            c["name"] for c in inspector.get_columns(table) if c["name"] not in SKIP
        ]
        table_obj = Base.metadata.tables[table]
        if "id" not in table_obj.c or not columns:
            continue
        rows = session.execute(
            select(*[table_obj.c[c] for c in columns]).order_by(table_obj.c.id)
        ).all()
        digest.update(table.encode())
        digest.update(repr([tuple(map(str, r)) for r in rows]).encode())
    print(digest.hexdigest())
    """
)


def _generate_with_hash_seed(tmp_path, seed: str) -> str:
    """Generate a dataset in a fresh interpreter pinned to one hash seed."""
    script = tmp_path / f"gen_{seed}.py"
    script.write_text(PROGRAM, encoding="utf-8")
    env = dict(os.environ, PYTHONHASHSEED=seed)
    result = subprocess.run(
        [sys.executable, str(script), str(tmp_path / f"chart_{seed}.db")],
        capture_output=True,
        text=True,
        env=env,
        timeout=900,
    )
    if result.returncode != 0:
        pytest.fail(f"generation under PYTHONHASHSEED={seed} failed:\n{result.stderr[-2000:]}")
    return result.stdout.strip().splitlines()[-1]


def test_the_same_seed_rebuilds_the_same_chart_under_any_hash_seed(tmp_path):
    """Two interpreters, two hash seeds, one seed — one chart.

    This is the property `cohort.json` promises. If it fails, every baseline
    the eval harness holds is a comparison against a chart that no longer
    exists, and a score moved because the patient did.
    """
    first = _generate_with_hash_seed(tmp_path, "1")
    second = _generate_with_hash_seed(tmp_path, "2")
    assert first == second, (
        "seed 4242 produced two different charts under two hash seeds — "
        "something iterates a set where the order reaches the RNG or the output"
    )


# ── the second wall clock, found by CI crossing midnight ─────────────────

#: The same generation, run as though the machine's calendar said two
#: different days. With ``as_of`` given, both must produce the same chart.
CLOCK_PROGRAM = textwrap.dedent(
    """
    import hashlib, sys
    from datetime import date
    from sqlalchemy import inspect, select

    import hdh.core.generators as gen
    from hdh.core.models import Base, get_engine, get_session
    from hdh.core.schema_registry import bootstrap_schema

    pretend = date.fromisoformat(sys.argv[2])

    class Frozen(date):
        @classmethod
        def today(cls):
            return pretend

    # Both spellings of "now" a generator could reach for.
    gen.date = Frozen
    gen.TODAY = pretend

    SKIP = {"created_at", "updated_at"}
    bootstrap_schema()
    engine = get_engine(sys.argv[1])
    Base.metadata.create_all(engine)
    session = get_session(engine)
    gen.build_dataset(
        session, n_patients=12, years_of_history=3, verbose=False,
        seed=4242, as_of=date(2026, 8, 14),
    )

    inspector = inspect(engine)
    digest = hashlib.sha256()
    for table in sorted(set(inspector.get_table_names()) & set(Base.metadata.tables)):
        columns = [c["name"] for c in inspector.get_columns(table) if c["name"] not in SKIP]
        table_obj = Base.metadata.tables[table]
        if "id" not in table_obj.c or not columns:
            continue
        rows = session.execute(
            select(*[table_obj.c[c] for c in columns]).order_by(table_obj.c.id)
        ).all()
        digest.update(table.encode())
        digest.update(repr([tuple(map(str, r)) for r in rows]).encode())
    print(digest.hexdigest())
    """
)


def _generate_on_day(tmp_path, day: str) -> str:
    script = tmp_path / f"clock_{day}.py"
    script.write_text(CLOCK_PROGRAM, encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(script), str(tmp_path / f"clock_{day}.db"), day],
        capture_output=True,
        text=True,
        env=dict(os.environ),
        timeout=900,
    )
    if result.returncode != 0:
        pytest.fail(f"generation pretending it is {day} failed:\n{result.stderr[-2000:]}")
    return result.stdout.strip().splitlines()[-1]


def test_a_pinned_generation_date_ignores_the_machine_calendar(tmp_path):
    """``as_of`` must be the only clock generation reads.

    Found the expensive way: CI went red on a PR that changed one JSON file,
    because the merge before it landed at 22:25 and this one at 00:01. Four
    sites read ``date.today()``, so a chart generated on Tuesday differed
    from the same seed on Wednesday — and not by a uniform offset. In a
    500-patient run the CKD-and-hypertension overlap moved from 40 charts to
    29, because the history window slid across staging boundaries.

    The determinism test above could not see this: two processes on one day
    share a calendar. This one does not.
    """
    tuesday = _generate_on_day(tmp_path, "2026-08-28")
    wednesday = _generate_on_day(tmp_path, "2026-08-29")
    assert tuesday == wednesday, (
        "the same seed and the same as_of produced two different charts on "
        "two different days — something in generation still reads the clock"
    )

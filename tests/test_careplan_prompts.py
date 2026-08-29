"""S5a: the prompts are data, and the version is the point.

The rubrics were always data; the text producing what they grade was string
literals. This moves it — and the move is only worth anything because of the
stamp, which is what stops a prompt change reading as a better plan.
"""

from __future__ import annotations

import json

import pytest

from hdh.modules.careplan import prompts
from hdh.modules.careplan.prompts import (
    REQUIRED,
    PromptError,
    load_prompt_set,
    parse_prompt_set,
    prompt_set,
)


@pytest.fixture(autouse=True)
def _fresh():
    prompts.reset()
    yield
    prompts.reset()


def _raw(**overrides) -> dict:
    base = {
        "prompt_set_id": "trial",
        "version": 3,
        "prompts": {key: "text " + " ".join("{" + p + "}" for p in req) for key, req in REQUIRED.items()},
    }
    base.update(overrides)
    return base


# ── the shipped set is complete and loadable ─────────────────────────────


def test_the_default_set_loads_and_holds_every_prompt():
    active = prompt_set()
    assert active.prompt_set_id == "default"
    assert set(active.texts) >= set(REQUIRED)


def test_the_stamp_is_what_gets_recorded():
    assert prompt_set().stamp == "default@1"


def test_every_prompt_the_code_asks_for_exists():
    """A prompt requested but not on disk would be a run that cannot start —
    better here than at the first API call."""
    active = prompt_set()
    for key in REQUIRED:
        assert active.text(key)


# ── placeholders are checked at load, not at send ────────────────────────


def test_a_prompt_that_lost_its_placeholder_is_refused():
    """The failure this prevents: a feedback preamble with no {feedback}
    still formats and still sends. The revise loop keeps running and
    silently stops carrying the reviewer's words."""
    raw = _raw()
    raw["prompts"]["feedback_preamble"] = "A previous attempt was reviewed."
    with pytest.raises(PromptError, match="lost the placeholder"):
        parse_prompt_set(raw)


def test_the_refusal_says_what_would_have_happened():
    raw = _raw()
    raw["prompts"]["selection_envelope"] = "{instruction} {situation}"
    with pytest.raises(PromptError, match="still format and still send"):
        parse_prompt_set(raw)


def test_a_missing_prompt_is_named():
    raw = _raw()
    del raw["prompts"]["goals"]
    with pytest.raises(PromptError, match="missing: goals"):
        parse_prompt_set(raw)


def test_an_unknown_prompt_key_lists_what_exists():
    active = parse_prompt_set(_raw())
    with pytest.raises(PromptError, match="has: "):
        active.text("nonexistent")


def test_a_missing_prompt_never_falls_back_to_a_default():
    """A silent fallback is a run that scores differently from the set it
    claims to be using."""
    active = parse_prompt_set(_raw())
    with pytest.raises(PromptError):
        active.text("concerns_v2")


# ── a set cannot be renamed in one place only ────────────────────────────


def test_the_filename_carries_the_id(tmp_path):
    (tmp_path / "renamed.json").write_text(json.dumps(_raw()), encoding="utf-8")
    with pytest.raises(PromptError, match="declares id"):
        load_prompt_set("renamed", root=tmp_path)


def test_an_absent_set_lists_what_is_on_disk(tmp_path):
    (tmp_path / "one.json").write_text(json.dumps(_raw(prompt_set_id="one")), encoding="utf-8")
    with pytest.raises(PromptError, match="on disk: one"):
        load_prompt_set("two", root=tmp_path)


def test_a_set_can_be_chosen_by_environment(tmp_path, monkeypatch):
    """An experiment should be an env var, not a branch."""
    (tmp_path / "trial.json").write_text(json.dumps(_raw()), encoding="utf-8")
    monkeypatch.setattr(prompts, "HERE", tmp_path)
    monkeypatch.setenv(prompts.ENV_VAR, "trial")
    prompts.reset()
    assert prompt_set().stamp == "trial@3"


# ── the stamp reaches everything the prompts produced ────────────────────


def test_the_harness_records_which_prompts_measured_it():
    from hdh.modules.careplan import evalset

    report = evalset.Report(cohort="default", version=2, prompts="default@1")
    assert report.as_dict()["prompts"] == "default@1"


def test_compare_refuses_across_prompt_versions():
    """The one comparison nothing else in the harness can catch: same
    cohort, same charts, same MRNs, and only the wording changed."""
    from hdh.modules.careplan import evalset

    report = evalset.Report(cohort="default", version=2, prompts="default@2")
    lines = evalset.compare(
        report,
        {"cohort": "default", "version": 2, "prompts": "default@1", "mean": 3.7, "cases": []},
    )
    assert any("not a comparison" in line for line in lines)
    assert any("only the wording changed" in line for line in lines)


def test_matching_prompt_versions_still_compare():
    """The guard must not refuse the comparison the harness exists for."""
    from hdh.modules.careplan import evalset

    report = evalset.Report(cohort="default", version=2, prompts="default@1")
    report.measurements.append(
        evalset.Measurement(mrn="MRN1", stratum="single", rubric="default", runs=[{"mean": 4.0}])
    )
    lines = evalset.compare(
        report,
        {
            "cohort": "default",
            "version": 2,
            "prompts": "default@1",
            "mean": 3.9,
            "cases": [{"mrn": "MRN1", "mean": 3.9}],
        },
    )
    assert not any("not a comparison" in line for line in lines)


def test_a_plan_records_the_prompt_set_that_produced_it():
    """Two plans for the same patient from the same chart can differ
    entirely because the wording changed between them."""
    import json as _json
    import pathlib

    schema = _json.loads(
        pathlib.Path("src/hdh/modules/careplan/schema/entities/care_plan_record.json").read_text(
            encoding="utf-8"
        )
    )
    columns = {c["name"]: c for c in schema["columns"]}
    assert "prompt_set" in columns
    # Nullable: plans written before prompts were versioned cannot say which
    # wording produced them, and inventing one would be worse than a NULL.
    assert columns["prompt_set"].get("nullable") is not False

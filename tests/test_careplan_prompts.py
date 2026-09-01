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


# ── the candidate set for the goal_quality experiment ────────────────────


def test_the_candidate_set_changes_exactly_one_prompt():
    """#128 found goal_quality pinned at 3 because goals_with_target was 0
    and the model was never asked for a target. This set asks.

    Everything else must be byte-identical to default@1 — otherwise a score
    that moves cannot be attributed to the sentences under test, which is
    the entire reason prompt sets carry versions.
    """
    base = load_prompt_set("default")
    trial = load_prompt_set("measurable-goals")
    differing = [key for key in base.texts if base.texts[key] != trial.texts.get(key)]
    assert differing == ["goals"]


def test_the_candidate_asks_for_a_number_and_refuses_invention():
    """Pushing for measurable targets is exactly the pressure that produces
    invented clinical figures. The instruction has to make an empty target
    the correct answer when the evidence does not supply one."""
    goals = load_prompt_set("measurable-goals").text("goals")
    assert "target_value" in goals
    assert "leave target_value empty" in goals
    assert "invented target is worse than an absent one" in goals


def test_the_candidate_carries_a_different_stamp():
    """So `compare` refuses, and a run under it cannot be read as a delta
    against the default baseline."""
    assert load_prompt_set("measurable-goals").stamp != load_prompt_set("default").stamp


# ── the shape is part of what the model is asked ─────────────────────────


def test_the_default_set_does_not_require_a_goal_target():
    """default is what every baseline was measured under and must not move."""
    assert load_prompt_set("default").requires_goal_target is False


def test_the_candidate_requires_one():
    assert load_prompt_set("measurable-goals").requires_goal_target is True


def test_the_goal_schema_follows_the_active_set():
    """Design §10: asking in words moved nothing, because a model omits an
    optional field rather than filling it. The requirement has to be part of
    the set — applied globally it could not be attributed to the set it was
    meant to test."""
    from hdh.modules.careplan.generate import goal_schema

    def required(name):
        with prompts.using(name):
            return goal_schema()["properties"]["selections"]["items"]["required"]

    assert "target_value" not in required("default")
    assert "target_value" in required("measurable-goals")


def test_target_value_is_offered_by_both_sets():
    """Required-ness is the only difference. A set that did not offer the
    field at all would be a different experiment."""
    from hdh.modules.careplan.generate import goal_schema

    for name in ("default", "measurable-goals"):
        with prompts.using(name):
            properties = goal_schema()["properties"]["selections"]["items"]["properties"]
            assert "target_value" in properties


def test_requiring_a_target_does_not_require_a_non_empty_one():
    """ "You must decide", not "you must invent". An empty string stays a
    valid answer, because a fabricated target scores well while being worse
    than no target at all."""
    from hdh.modules.careplan.generate import goal_schema

    with prompts.using("measurable-goals"):
        properties = goal_schema()["properties"]["selections"]["items"]["properties"]
    assert properties["target_value"] == {"type": "string"}
    assert "minLength" not in properties["target_value"]


# ── the third thing that moves scores while everything else holds ────────


def test_the_baseline_records_which_retriever_fetched_the_evidence():
    from hdh.modules.careplan import evalset

    report = evalset.Report(cohort="default", version=2, retriever="vector+rerank")
    assert report.as_dict()["retriever"] == "vector+rerank"


def test_compare_refuses_across_retrievers():
    """Cohort, charts, MRNs and wording all identical — only the evidence
    differs, which reads exactly like the model reasoning better."""
    from hdh.modules.careplan import evalset

    report = evalset.Report(cohort="default", version=2, prompts="default@1", retriever="vector+rerank")
    lines = evalset.compare(
        report,
        {
            "cohort": "default",
            "version": 2,
            "prompts": "default@1",
            "retriever": "lexical",
            "mean": 3.7,
            "cases": [],
        },
    )
    assert any("not a comparison" in line for line in lines)
    assert any("different evidence" in line for line in lines)


def test_the_same_retriever_still_compares():
    """The guard must not refuse the comparison the harness exists for."""
    from hdh.modules.careplan import evalset

    report = evalset.Report(cohort="default", version=2, prompts="default@1", retriever="lexical")
    report.measurements.append(
        evalset.Measurement(mrn="MRN1", stratum="single", rubric="default", runs=[{"mean": 4.0}])
    )
    lines = evalset.compare(
        report,
        {
            "cohort": "default",
            "version": 2,
            "prompts": "default@1",
            "retriever": "lexical",
            "mean": 3.9,
            "cases": [{"mrn": "MRN1", "mean": 3.9}],
        },
    )
    assert not any("not a comparison" in line for line in lines)

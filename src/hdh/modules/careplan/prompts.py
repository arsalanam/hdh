"""S5a of `interactive-care-planning.md`: the prompts are data, and versioned.

Rubrics have always been data. The text that produces what the rubrics grade
was a set of string literals inside `generate.py` and `evaluate.py`, so
"adjust the rubric and the prompts" meant one config edit and one code edit.
That is the wrong asymmetry for the half of the system that is meant to be
tuned.

**Why the version is not optional, and is the point of this module.**

We learned this the expensive way twice in one week. A generator change
moved what seed 4242 produces while the cohort name and half the MRNs
stayed put, and `compare` printed a delta across charts that were not the
same charts. The answer was a cohort version and a refusal.

Prompt tuning is the identical failure with a different noun. The cohort
will be unchanged, the MRNs identical, the charts byte-for-byte the same —
and the scores will move because the *prompt* moved. Without a stamp that is
indistinguishable from a real improvement, and it is the improvement
everyone will want to believe. So a prompt set carries a version, the
version is stamped on what it produced, and `compare` refuses across
versions exactly as it refuses across cohort versions.

**Placeholders are checked at load.** A prompt that lost its `{feedback}`
slot would still format, still send, and silently drop the reviewer's words
— the revise loop would go on running and stop working. Validating at load
turns that into a startup error instead of a quiet regression.
"""

from __future__ import annotations

import json
import os
import pathlib
import string
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache
from types import MappingProxyType

#: Where prompt sets live, beside the rubrics they are graded by.
HERE = pathlib.Path(__file__).parent / "prompts"

#: Which set to use. Same shape as the retriever and checkpointer registries,
#: so an experiment is an env var rather than a branch.
ENV_VAR = "HDH_CAREPLAN_PROMPTS"
DEFAULT = "default"

#: Every prompt the module sends to a model, and the placeholders each must
#: keep. A prompt is only listed here if changing it changes what a model
#: produces — that is the boundary the version is claiming to cover.
REQUIRED: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "concerns": frozenset(),
        "goals": frozenset(),
        "interventions": frozenset(),
        "feedback_preamble": frozenset({"feedback"}),
        "selection_envelope": frozenset({"instruction", "situation", "menu"}),
        "grading_instruction": frozenset({"title", "question", "anchors", "situation", "plan", "facts"}),
    }
)


class PromptError(RuntimeError):
    """A prompt set is missing, malformed, or has lost a placeholder."""


def _placeholders(text: str) -> set[str]:
    return {name for _lit, name, _spec, _conv in string.Formatter().parse(text) if name}


@dataclass(frozen=True)
class PromptSet:
    """The model-facing text of one care-plan configuration."""

    prompt_set_id: str
    version: int
    texts: Mapping[str, str]
    #: Must a goal answer with a ``target_value`` key at all?
    #:
    #: Part of the set because **what the model is asked includes the shape
    #: it must answer in**. Measured (design §10): asking for a target in
    #: words moved nothing, because the field was optional and a model omits
    #: an optional field rather than filling it — 0 goals carried a target
    #: before and after. A schema change applied globally could not be
    #: attributed to the set it was meant to test, which is the same problem
    #: versions exist to solve.
    #:
    #: Required-key with an empty string still permitted: "you must decide",
    #: not "you must invent". The instruction says to leave it empty when
    #: nothing supports a number, and a fabricated target scores well while
    #: being worse than none.
    requires_goal_target: bool = False

    def text(self, key: str) -> str:
        """One prompt, or a refusal naming what exists.

        A missing prompt must not fall back to a default. A silent default
        is a run that scores differently from the set it claims to be using.
        """
        try:
            return self.texts[key]
        except KeyError:
            have = ", ".join(sorted(self.texts))
            raise PromptError(f"no prompt {key!r} in {self.prompt_set_id!r} — has: {have}") from None

    @property
    def stamp(self) -> str:
        """How this set is recorded on what it produced."""
        return f"{self.prompt_set_id}@{self.version}"


def parse_prompt_set(raw: Mapping) -> PromptSet:
    """Validate a prompt set, including every placeholder it must keep."""
    for key in ("prompt_set_id", "version", "prompts"):
        if key not in raw:
            raise PromptError(f"prompt set is missing {key!r}")

    texts = dict(raw["prompts"])
    missing = sorted(set(REQUIRED) - set(texts))
    if missing:
        raise PromptError(f"prompt set {raw['prompt_set_id']!r} is missing: {', '.join(missing)}")

    for key, required in REQUIRED.items():
        found = _placeholders(texts[key])
        lost = sorted(required - found)
        if lost:
            # The failure this prevents: a feedback preamble with no
            # {feedback} still formats and still sends, and the revise loop
            # goes on running while silently dropping the reviewer's words.
            raise PromptError(
                f"prompt {key!r} lost the placeholder(s) {', '.join(lost)} — "
                f"it would still format and still send, saying nothing"
            )

    return PromptSet(
        prompt_set_id=str(raw["prompt_set_id"]),
        version=int(raw["version"]),
        texts=MappingProxyType(texts),
        requires_goal_target=bool(raw.get("requires_goal_target", False)),
    )


def load_prompt_set(name: str | None = None, root: pathlib.Path | None = None) -> PromptSet:
    """Load one prompt set from disk, validated."""
    chosen = name or os.environ.get(ENV_VAR) or DEFAULT
    path = (root or HERE) / f"{chosen}.json"
    if not path.exists():
        available = ", ".join(sorted(p.stem for p in (root or HERE).glob("*.json"))) or "none"
        raise PromptError(f"no prompt set {chosen!r} — on disk: {available}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as err:
        raise PromptError(f"{path.name} is not valid JSON: {err}") from None
    parsed = parse_prompt_set(raw)
    if parsed.prompt_set_id != chosen:
        raise PromptError(
            f"{path.name} declares id {parsed.prompt_set_id!r} — the filename carries the id, "
            f"so a set cannot be renamed in one place and referenced by its old name in another"
        )
    return parsed


@lru_cache(maxsize=4)
def _cached(name: str | None) -> PromptSet:
    return load_prompt_set(name)


#: Set by :func:`using` for the duration of a block. Not a configuration
#: knob — the one caller is the tuning loop, which has to run the same code
#: twice under two different sets.
_override: PromptSet | None = None


def prompt_set(name: str | None = None) -> PromptSet:
    """The active prompt set, read once per process.

    Cached because it is read on every node call and every graded dimension,
    and re-reading a file per API call would be a lot of syscalls to learn
    the same thing. :func:`reset` exists for tests that write their own.
    """
    if name is None and _override is not None:
        return _override
    return _cached(name or os.environ.get(ENV_VAR) or DEFAULT)


@contextmanager
def using(name: str):
    """Run a block with one prompt set active, whatever the environment says.

    The tuning loop needs the *whole* run — generation and grading both — on
    one set, because the grading instruction is part of the set too: an edit
    that changed how plans are graded would otherwise be invisible. Passing a
    name down every call would have meant threading it through nodes that
    have no business knowing about prompt sets.

    Restores on the way out, including on an exception, so a failed tuning
    run cannot leave the process quietly generating under the wrong wording.
    """
    global _override
    previous = _override
    _override = prompt_set(name)
    try:
        yield _override
    finally:
        _override = previous


def reset() -> None:
    """Forget the cached set and any override. For tests and reloads."""
    global _override
    _override = None
    _cached.cache_clear()

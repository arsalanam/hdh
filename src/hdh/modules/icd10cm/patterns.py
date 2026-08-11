"""Graph patterns: a closed JSON query language, compiled — never trusted.

The design §7.3 rule: an LLM (or a human) may propose a pattern, but the
pattern must survive strict validation and is then compiled to
parameterized SQL by this module. Free text reaches exactly two places —
the FTS/LIKE anchor term and axis *values* — both as bound parameters.
An invalid pattern raises ``PatternError`` with a reason suitable for the
agent's retry-with-feedback loop.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import or_, select

from hdh.modules.icd10cm.service import AXIS_VALUES

EDGE_TYPES = frozenset(
    {
        "parent_of",
        "contralateral",
        "axis_variant",
        "episode_variant",
        "excludes1",
        "excludes2",
        "code_first",
        "use_additional",
        "includes",
        "maps_to",
    }
)
_TOP_KEYS = {"anchor", "axes", "traverse", "constraints", "limit"}


class PatternError(Exception):
    """A pattern failed validation — the message is the retry feedback."""


@dataclass(frozen=True)
class PatternHit:
    """One concept a pattern matched."""

    code: str
    display: str
    is_billable: bool


def validate_pattern(pattern: dict) -> None:
    """Reject anything outside the closed schema, with actionable reasons."""
    if not isinstance(pattern, dict):
        raise PatternError("pattern must be a JSON object")
    unknown = set(pattern) - _TOP_KEYS
    if unknown:
        raise PatternError(f"unknown pattern keys: {sorted(unknown)} (allowed: {sorted(_TOP_KEYS)})")
    anchor = pattern.get("anchor", {})
    if anchor and (not isinstance(anchor, dict) or set(anchor) - {"terms", "code"}):
        raise PatternError("anchor supports only 'terms' and/or 'code'")
    for axis, value in pattern.get("axes", {}).items():
        if axis not in AXIS_VALUES:
            raise PatternError(f"unknown axis '{axis}' (allowed: {sorted(AXIS_VALUES)})")
        if value not in AXIS_VALUES[axis]:
            raise PatternError(f"axis {axis}: unknown value '{value}' (allowed: {AXIS_VALUES[axis]})")
    for step in pattern.get("traverse", []):
        if set(step) - {"edge", "dir", "depth"}:
            raise PatternError("traverse steps support only edge/dir/depth")
        if step.get("edge") not in EDGE_TYPES:
            raise PatternError(f"unknown edge type '{step.get('edge')}' (allowed: {sorted(EDGE_TYPES)})")
        if step.get("edge") == "parent_of" and step.get("depth", "*") not in ("*", 1):
            raise PatternError("parent_of supports depth '*' (descendants) or 1 (children)")
        if step.get("edge") != "parent_of" and step.get("depth", 1) != 1:
            raise PatternError("typed edges traverse depth 1 only")
    constraints = pattern.get("constraints", {})
    if set(constraints) - {"billable", "kind"}:
        raise PatternError("constraints support only 'billable' and 'kind'")


def run_pattern(session, pattern: dict, limit: int = 20) -> list[PatternHit]:
    """Validate, compile, and execute a pattern; return matching concepts."""
    from hdh.core.models import Base
    from hdh.modules.icd10cm.cli import search_concepts

    validate_pattern(pattern)
    concepts_t = Base.metadata.tables["ontology_concepts"]
    edges_t = Base.metadata.tables["ontology_edges"]

    ids = _anchor_ids(session, pattern.get("anchor", {}), concepts_t, search_concepts)
    for step in pattern.get("traverse", []):
        if step["edge"] == "parent_of" and step.get("depth", "*") == "*":
            ids = _descend(session, ids, concepts_t)
        else:
            ids = [
                row[0]
                for row in session.execute(
                    select(edges_t.c.target_id).where(
                        edges_t.c.source_id.in_(ids), edges_t.c.edge_type == step["edge"]
                    )
                )
            ]
        if not ids:
            return []

    query = select(concepts_t.c.code, concepts_t.c.display, concepts_t.c.is_billable).where(
        concepts_t.c.id.in_(ids)
    )
    for axis, value in pattern.get("axes", {}).items():
        query = query.where(concepts_t.c.properties["axes"][axis].as_string() == value)
    constraints = pattern.get("constraints", {})
    if constraints.get("billable"):
        query = query.where(concepts_t.c.is_billable)
    if constraints.get("kind"):
        query = query.where(concepts_t.c.kind == str(constraints["kind"]))
    rows = session.execute(
        query.order_by(concepts_t.c.hierarchy_depth.desc(), concepts_t.c.code).limit(
            min(int(pattern.get("limit", limit)), 200)
        )
    ).all()
    return [PatternHit(*row) for row in rows]


def _anchor_ids(session, anchor: dict, concepts_t, search_concepts) -> list[str]:
    if anchor.get("code"):
        rows = session.execute(
            select(concepts_t.c.id).where(
                concepts_t.c.ontology == "icd10cm",
                concepts_t.c.code == str(anchor["code"]).upper(),
            )
        ).all()
    elif anchor.get("terms"):
        hits = search_concepts(session, str(anchor["terms"]), 25)
        codes = [h.code for h in hits]
        rows = session.execute(
            select(concepts_t.c.id).where(concepts_t.c.ontology == "icd10cm", concepts_t.c.code.in_(codes))
        ).all()
    else:
        raise PatternError("anchor must provide 'terms' or 'code'")
    return [row[0] for row in rows]


def _descend(session, ids: list[str], concepts_t) -> list[str]:
    paths = [row[0] for row in session.execute(select(concepts_t.c.path).where(concepts_t.c.id.in_(ids)))]
    if not paths:
        return []
    prefix_filters = [or_(concepts_t.c.path == p, concepts_t.c.path.like(p + ".%")) for p in paths]
    return [
        row[0] for row in session.execute(select(concepts_t.c.id).where(or_(*prefix_filters)).limit(5000))
    ]

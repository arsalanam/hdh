"""`hdh careplan` — the module's command surface.

Milestone 1b is the knowledge layer, so the commands are the ones that put
knowledge in and get it out: ``ingest``, ``corpora``, ``search``. Plan
generation arrives with the subagent.

``search`` exists for a reason beyond convenience: retrieval is the part of
a generated plan a reader most needs to inspect, and being able to ask what
the model *would* have been shown — without generating anything — is how
you tell a bad plan from a bad corpus.
"""

from __future__ import annotations


def register_cli(subparsers) -> None:
    """Discovery hook consumed by hdh.cli (see hdh.modules.CLI_MODULES)."""
    parser = subparsers.add_parser("careplan", help="Care plans: knowledge corpora and retrieval")
    sub = parser.add_subparsers(dest="careplan_cmd", required=True)

    ingest = sub.add_parser("ingest", help="Load a knowledge corpus into the store")
    ingest.add_argument("--corpus", help="Corpus name (default: every bundled corpus)")
    ingest.add_argument("--root", help="Directory holding corpora (default: the bundled ones)")

    sub.add_parser("corpora", help="What is ingested, and how much")

    search = sub.add_parser("search", help="Retrieve chunks, exactly as the subagent would")
    search.add_argument("query", nargs="+", help="The clinical situation to retrieve for")
    search.add_argument("--corpus", required=True)
    search.add_argument("-k", type=int, default=5)

    stratify_p = sub.add_parser(
        "stratify", help="The deterministic flags for one patient, before any generation"
    )
    stratify_p.add_argument("--mrn", required=True)

    gen = sub.add_parser("generate", help="Generate a plan from the chart and the corpora")
    gen.add_argument("--mrn", required=True)
    gen.add_argument("--dry-run", action="store_true", help="Propose it, write nothing")
    gen.add_argument("--model", help="Model override (default: HDH_AGENT_MODEL)")
    gen.add_argument("--evaluate", action="store_true", help="Grade the written plan against its rubric (§9)")

    grade = sub.add_parser("evaluate", help="Grade an existing plan against its rubric")
    grade.add_argument("--id", type=int, required=True, help="Care plan id")
    grade.add_argument("--model", help="Model override (default: HDH_AGENT_MODEL)")
    grade.add_argument("--dry-run", action="store_true", help="Score it, record nothing")

    show = sub.add_parser("show", help="One plan, with what each element traces to")
    show.add_argument("--id", type=int, required=True)

    sub.add_parser("rubrics", help="The evaluation rubrics on disk, and what each dimension asks")

    facts_p = sub.add_parser(
        "facts", help="What the grader would be TOLD about a plan, before anything is scored"
    )
    facts_p.add_argument("--id", type=int, required=True, help="Care plan id")

    parser.set_defaults(func=run)


def run(session, args) -> None:
    """Dispatch a `hdh careplan` subcommand."""
    {
        "ingest": lambda: _cmd_ingest(session, args),
        "corpora": lambda: _cmd_corpora(session),
        "search": lambda: _cmd_search(session, args),
        "stratify": lambda: _cmd_stratify(session, args),
        "generate": lambda: _cmd_generate(session, args),
        "show": lambda: _cmd_show(session, args),
        "rubrics": lambda: _cmd_rubrics(),
        "facts": lambda: _cmd_facts(session, args),
        "evaluate": lambda: _cmd_evaluate(session, args),
    }[args.careplan_cmd]()


def _print_evaluation(rubric, evaluation) -> None:
    """One evaluation, printed so a disputed score can be argued with.

    Every graded dimension prints the anchor it was scored against, not
    just the number — a 3 means nothing on its own, and the whole point of
    an anchored scale is that the level has a description a reviewer can
    disagree with.
    """
    print()
    print(f"  rubric: {rubric.rubric_id}@{rubric.version} ({rubric.title})")
    print()
    for score in evaluation.scores:
        dimension = rubric.dimension(score.dimension_id)
        title = dimension.title if dimension is not None else score.dimension_id
        if score.score is None:
            print(f"  ??  {title}")
            print(f"      ungraded: {score.ungraded_reason}")
            print()
            continue
        anchor = (dimension.anchors.get(score.score, "") if dimension is not None else "").strip()
        print(f"  {score.score}/{rubric.scale_max}  {title}")
        if anchor:
            print(f"      anchor: {anchor}")
        if score.justification:
            print(f"      because: {score.justification}")
        print()

    verdict = evaluation.verdict(rubric)
    mark = {"pass": "✅", "revise": "⚠", "fail": "⛔"}.get(verdict, "·")
    print(f"  {mark} {evaluation.narrative(rubric)}")
    # §9 draws this line, and the mirror matters as much as the rule: a
    # `fail` that binned a plan would take away the decision reserved for
    # a person, exactly as an auto-approval would.
    print("     advisory only — grading neither approves nor rejects a plan")


def _cmd_evaluate(session, args) -> None:
    """Grade a written plan against the rubric its archetype selects."""
    from sqlalchemy import select

    from hdh.core.models import Base, Patient
    from hdh.modules.careplan.context import build_context
    from hdh.modules.careplan.evaluate import (
        EvaluationError,
        evaluate,
        llm_grader,
        record_evaluation,
    )
    from hdh.modules.careplan.facts import gather
    from hdh.modules.careplan.rubric import RubricError
    from hdh.modules.careplan.stratify import stratify

    plans = Base.metadata.tables["care_plan_records"]
    plan = session.execute(select(plans).where(plans.c.id == args.id)).first()
    if plan is None:
        raise SystemExit(f"no care plan #{args.id}")
    patient = session.query(Patient).filter(Patient.id == plan.patient_id).first()
    if patient is None:
        raise SystemExit(f"care plan #{args.id} has no patient")

    try:
        grader = llm_grader(model=args.model)
    except ImportError:
        raise SystemExit("careplan evaluate needs the agent extra: pip install 'hdh[agent]'") from None

    context = build_context(session, patient)
    evidence = gather(session, args.id, context, stratify(context))
    try:
        rubric, evaluation = evaluate(evidence, grader)
    except RubricError as err:
        raise SystemExit(f"hdh careplan evaluate: {err}") from None

    # One environmental fault — no API key, an exhausted rate limit — hits
    # every dimension identically. Printing six ungraded dimensions would
    # bury the diagnosis in a scorecard that says nothing.
    shared = evaluation.common_failure
    if shared is not None:
        raise SystemExit(f"hdh careplan evaluate: no dimension could be graded — {shared}")

    print()
    print(f"#{plan.id}  {plan.title}")
    _print_evaluation(rubric, evaluation)

    if args.dry_run:
        print("     dry run — nothing recorded")
        return
    try:
        evaluation_id = record_evaluation(session, args.id, rubric, evaluation)
    except EvaluationError as err:
        raise SystemExit(f"hdh careplan evaluate: {err}") from None
    session.commit()
    print(f"     recorded as evaluation #{evaluation_id}; plan status unchanged")


def _cmd_rubrics() -> None:
    """Every rubric on disk, validated by the act of listing them."""
    from hdh.modules.careplan.rubric import RubricError, load_rubrics

    try:
        rubrics = load_rubrics()
    except RubricError as err:
        raise SystemExit(f"hdh careplan rubrics: {err}") from None

    print()
    for rubric in rubrics:
        match = ", ".join(f"{k}={v}" for k, v in rubric.match.items()) or "everyone (fallback)"
        print(f"  {rubric.rubric_id}@{rubric.version}  {rubric.title}")
        print(f"      applies to: {match}")
        print(
            f"      scale {rubric.scale_min}-{rubric.scale_max}"
            f"  ·  revise below {rubric.revise_below}  ·  fail below {rubric.fail_below}"
        )
        for dimension in rubric.dimensions:
            print(f"      · {dimension.id:<22} {dimension.question}")
            print(f"        {'':<22} facts: {', '.join(dimension.facts) or 'none'}")
        print()


def _cmd_facts(session, args) -> None:
    """The deterministic facts for a written plan, and the rubric selected.

    Worth its own command for the reason `search` and `stratify` are: these
    facts are what the grader will be handed, and a wrong score is far more
    often a wrong fact than a wrong judgement.
    """
    from sqlalchemy import select

    from hdh.core.models import Base, Patient
    from hdh.modules.careplan.context import build_context
    from hdh.modules.careplan.evaluate import render_plan
    from hdh.modules.careplan.facts import gather
    from hdh.modules.careplan.rubric import RubricError, select_rubric
    from hdh.modules.careplan.stratify import stratify

    plans = Base.metadata.tables["care_plan_records"]
    plan = session.execute(select(plans).where(plans.c.id == args.id)).first()
    if plan is None:
        raise SystemExit(f"no care plan #{args.id}")
    patient = session.query(Patient).filter(Patient.id == plan.patient_id).first()
    if patient is None:
        raise SystemExit(f"care plan #{args.id} has no patient")

    context = build_context(session, patient)
    flags = stratify(context)
    evidence = gather(session, args.id, context, flags)
    try:
        rubric = select_rubric(context)
    except RubricError as err:
        raise SystemExit(f"hdh careplan facts: {err}") from None

    print()
    print(f"#{plan.id}  {plan.title}")
    print(f"  rubric: {rubric.rubric_id}@{rubric.version} ({rubric.title})")
    print()
    print(render_plan(evidence))
    print()
    for dimension in rubric.dimensions:
        print(f"  {dimension.title}")
        for line in evidence_lines(evidence, dimension):
            print(f"      {line}")
        print()


def evidence_lines(evidence, dimension) -> list[str]:
    """The fact lines one dimension would be given."""
    from hdh.modules.careplan.facts import compute_facts

    return compute_facts(evidence, dimension.facts).as_lines(dimension.facts) or ["(no facts declared)"]


def _cmd_stratify(session, args) -> None:
    """Nodes 1-2 for one patient, with nothing generated.

    Worth its own command for the same reason `search` is: these flags are
    what the generating nodes will be handed, and seeing them without a plan
    attached is how you tell a wrong plan from a wrong chart.
    """
    from hdh.core.models import Patient
    from hdh.modules.careplan.context import build_context
    from hdh.modules.careplan.stratify import stratify

    patient = session.query(Patient).filter(Patient.mrn == args.mrn).first()
    if patient is None:
        raise SystemExit(f"no patient {args.mrn}")

    context = build_context(session, patient)
    social = context.social
    print()
    print(f"{context.mrn} · {context.age}{context.sex[:1].lower()} · {len(context.problems)} chronic")
    drugs = ", ".join(f"{m.name} [{m.drug_class}]" for m in context.medications)
    print(f"  medications: {drugs or 'none recorded'}")
    if social is not None:
        alone = {True: "yes", False: "no", None: "unknown"}[social.lives_alone]
        print(f"  lives alone: {alone}  ({social.lives_alone_basis})")
    if context.risk_score is not None:
        print(f"  risk score:  {context.risk_score:.3f}")

    flags = stratify(context)
    print()
    if not flags:
        print("  no flags fired — nothing for a plan to open with")
        return
    print(f"{len(flags)} flag(s):")
    print()
    for flag in flags:
        print(f"  [{flag.kind}] {flag.statement}")
        print(f"      because: {flag.basis}")
        print(f"      see:     {flag.cites}")
        print()


def _cmd_ingest(session, args) -> None:
    import pathlib

    from hdh.core.dialect import DatabaseFeatureError, require_postgresql
    from hdh.modules.careplan.ingest import CorpusError, available, ingest_corpus

    try:
        require_postgresql(session, "Care-plan knowledge ingestion")
    except DatabaseFeatureError as err:
        raise SystemExit(f"hdh careplan ingest: {err}") from None

    root = pathlib.Path(args.root) if args.root else None
    names = [args.corpus] if args.corpus else available(root)
    if not names:
        raise SystemExit("no corpora found — nothing to ingest")

    total = 0
    for name in names:
        try:
            written = ingest_corpus(session, name, root)
        except CorpusError as err:
            raise SystemExit(f"hdh careplan ingest: {name}: {err}") from None
        print(f"  {name:<24} {written:>4} chunks")
        total += written
    session.commit()
    print(f"\n📚 ingested {total} chunks across {len(names)} corpus/corpora")


def _cmd_corpora(session) -> None:
    from hdh.modules.careplan.knowledge import corpora

    rows = corpora(session)
    if not rows:
        raise SystemExit("nothing ingested yet — run `hdh careplan ingest`")
    print()
    for name, count in rows:
        print(f"  {name:<24} {count:>4} chunks")


def _cmd_search(session, args) -> None:
    from hdh.core.dialect import DatabaseFeatureError
    from hdh.modules.careplan.knowledge import PgStore

    query = " ".join(args.query)
    try:
        hits = PgStore(session).search(query, args.corpus, k=args.k)
    except DatabaseFeatureError as err:
        raise SystemExit(f"hdh careplan search: {err}") from None

    if not hits:
        # An empty result is an answer, not a failure: a plan element with
        # no retrieved evidence should not be generated at all.
        print(f"\nno chunks in {args.corpus!r} match {query!r} — nothing to cite")
        return
    print(f"\n{len(hits)} chunk(s) for {query!r}\n")
    for hit in hits:
        first_line = hit.chunk.strip().splitlines()[0]
        print(f"  {hit.score:.3f}  {hit.citation()}")
        print(f"         {first_line[:96]}")
        print(f"         source: {hit.source}  ·  {hit.license}")
        print()


def _cmd_generate(session, args) -> None:
    """Nodes 1-5 and 7, then structural validation.

    Prints what was DROPPED as prominently as what was kept: a selection
    that cited something never offered is the model doing the one thing
    this design forbids, and hiding it would defeat the check.
    """
    from hdh.core.dialect import DatabaseFeatureError
    from hdh.core.models import Patient
    from hdh.modules.careplan.evaluate import llm_grader
    from hdh.modules.careplan.generate import llm_selector
    from hdh.modules.careplan.plan import generate_plan

    patient = session.query(Patient).filter(Patient.mrn == args.mrn).first()
    if patient is None:
        raise SystemExit(f"no patient {args.mrn}")

    try:
        selector = llm_selector(model=args.model)
    except ImportError:
        raise SystemExit("careplan generate needs the agent extra: pip install 'hdh[agent]'") from None

    try:
        grader = llm_grader(model=args.model) if args.evaluate else None
        result = generate_plan(session, patient, selector=selector, grader=grader, dry_run=args.dry_run)
    except DatabaseFeatureError as err:
        raise SystemExit(f"hdh careplan generate: {err}") from None

    print()
    print(f"{result.context.mrn} · {len(result.flags)} flag(s) · {len(result.draft.concerns)} concern(s)")
    for concern in result.draft.concerns:
        print(f"  concern      {concern.statement}")
        print(f"               cites {', '.join(concern.evidence_refs)}")
    for goal in result.draft.goals:
        print(f"  goal         {goal.statement}")
    for intervention in result.draft.interventions:
        owner = f" [{intervention.owner_role}]" if intervention.owner_role else ""
        print(f"  intervention {intervention.statement}{owner}")

    reconciliation = result.reconciliation
    if reconciliation is not None and (reconciliation.merged or reconciliation.vetoed):
        print()
        print(f"  reconciled: {len(reconciliation.merged)} merged, {len(reconciliation.vetoed)} vetoed")
        for item in reconciliation.vetoed:
            print(f"    ⛔ {item}")
        for item in reconciliation.merged:
            print(f"    ↳ {item}")
    if reconciliation is not None and reconciliation.burden_flagged:
        print(f"    ⚠ burden {reconciliation.burden} — review before approval")
    for index in reconciliation.bare_goals if reconciliation else []:
        print(f"    ⚠ goal {index + 1} has no intervention of its own after merging")

    if result.draft.deferred:
        # Printed as prominently as what the plan DID address. A deferral
        # a reader never sees is indistinguishable from an omission.
        print()
        print(f"  {len(result.draft.deferred)} problem(s) deferred, recorded on the plan:")
        for item in result.draft.deferred:
            print(f"    — {item}")

    if result.draft.dropped:
        print()
        print(f"  {len(result.draft.dropped)} dropped for lack of evidence:")
        for item in result.draft.dropped:
            print(f"    ✗ {item}")

    print()
    if result.refused:
        for error in result.report.errors:
            print(f"  refused: {error}")
        return
    print(f"  ✅ plan #{result.plan_id} written as ai_generated — not approved")
    if result.evaluation is not None and result.rubric is not None:
        _print_evaluation(result.rubric, result.evaluation)
        print(f"     recorded as evaluation #{result.evaluation_id}")
    for check in result.report.checked:
        print(f"     checked: {check}")


def _cmd_show(session, args) -> None:
    """One plan, printed so every element shows what it traces to."""
    from sqlalchemy import select

    from hdh.core.models import Base

    tables = Base.metadata.tables
    plans = tables["care_plan_records"]
    plan = session.execute(select(plans).where(plans.c.id == args.id)).first()
    if plan is None:
        raise SystemExit(f"no care plan #{args.id}")

    print()
    print(f"#{plan.id}  {plan.title}")
    print(f"  status: {plan.status}")
    for item in (getattr(plan, "deferred", None) or {}).get("problems") or []:
        print(f"  deferred: {item}")

    concerns = session.execute(
        select(tables["health_concerns"]).where(tables["health_concerns"].c.care_plan_id == plan.id)
    ).all()
    for concern in concerns:
        refs = ", ".join((concern.evidence_refs or {}).get("chunks", [])) or "—"
        print()
        print(f"  [{concern.concern_type}] {concern.statement}   ({concern.source}, cites {refs})")
        goals = session.execute(
            select(tables["plan_goals"]).where(tables["plan_goals"].c.concern_id == concern.id)
        ).all()
        for goal in goals:
            target = f" → {goal.target_value}" if goal.target_value else ""
            print(f"      goal: {goal.statement}{target}")
            interventions = session.execute(
                select(tables["plan_interventions"]).where(tables["plan_interventions"].c.goal_id == goal.id)
            ).all()
            for item in interventions:
                owner = f" [{item.owner_role}]" if item.owner_role else ""
                print(f"          {item.intervention_type}: {item.statement}{owner}")

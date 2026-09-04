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
    gen.add_argument(
        "--revise",
        action="store_true",
        help="Grade, send low scores back to the node that caused them, and keep the best "
        "attempt (implies --evaluate; costs a full regeneration per round)",
    )

    grade = sub.add_parser("evaluate", help="Grade an existing plan against its rubric")
    grade.add_argument("--id", type=int, required=True, help="Care plan id")
    grade.add_argument("--model", help="Model override (default: HDH_AGENT_MODEL)")
    grade.add_argument("--dry-run", action="store_true", help="Score it, record nothing")

    show = sub.add_parser("show", help="One plan, with what each element traces to")
    # Either identifier. A clinician has an MRN in front of them, not a plan
    # id, and requiring the id meant the answer to "show me this patient's
    # care plan" was a SQL query first.
    show.add_argument("--id", type=int, help="Care plan id")
    show.add_argument("--mrn", help="Patient MRN; shows their current plan")

    plans = sub.add_parser("plans", help="Every saved plan for a patient, and which one is current")
    plans.add_argument("--mrn", required=True)

    sub.add_parser("rubrics", help="The evaluation rubrics on disk, and what each dimension asks")

    sub.add_parser("retrievers", help="Which retrieval strategies exist, and which one is configured")

    tune_p = sub.add_parser("tune", help="One patient, two prompt sets — see what a wording change did")
    tune_p.add_argument("--mrn", required=True)
    tune_p.add_argument("--before", default="default", help="Prompt set to compare from")
    tune_p.add_argument("--after", required=True, help="Prompt set to compare to")
    tune_p.add_argument("--html", help="Directory to write both plans as pages")
    tune_p.add_argument("--model", help="Model override (default: HDH_AGENT_MODEL)")

    ev = sub.add_parser("eval", help="The fixed cohort: build it, check it, measure against it")
    ev_sub = ev.add_subparsers(dest="eval_cmd", required=True)
    ev_build = ev_sub.add_parser("build", help="Regenerate the cohort's patients from its pinned seed")
    ev_build.add_argument("--cohort", default="default")
    ev_cases = ev_sub.add_parser("cases", help="The selected cases and their shape (no generation)")
    ev_cases.add_argument("--cohort", default="default")
    ev_check = ev_sub.add_parser(
        "check", help="Deterministic checks over every case — no LLM, and these are assertions"
    )
    ev_check.add_argument("--cohort", default="default")
    ev_run = ev_sub.add_parser("run", help="Generate and grade every case; compare to the baseline")
    ev_run.add_argument("--cohort", default="default")
    ev_run.add_argument("--repeat", type=int, default=1, help="Runs per case — 2+ measures the noise floor")
    ev_run.add_argument("--revise", action="store_true", help="Use the revise loop for each plan")
    ev_run.add_argument("--limit", type=int, help="Only the first N cases (cost control)")
    ev_run.add_argument("--model", help="Model override (default: HDH_AGENT_MODEL)")
    ev_run.add_argument("--save", action="store_true", help="Write this run as the new baseline")

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
        "plans": lambda: _cmd_plans(session, args),
        "rubrics": lambda: _cmd_rubrics(),
        "retrievers": lambda: _cmd_retrievers(),
        "facts": lambda: _cmd_facts(session, args),
        "evaluate": lambda: _cmd_evaluate(session, args),
        "tune": lambda: _cmd_tune(session, args),
        "eval": lambda: _cmd_eval(session, args),
    }[args.careplan_cmd]()


def _baseline_path(cohort: str):
    from hdh.modules.careplan import evalset

    return evalset.HERE / (f"baseline-{cohort}.json" if cohort != "default" else "baseline.json")


def _cmd_tune(session, args) -> None:
    """`hdh careplan tune` — the fast loop, with the slow one named at the end.

    Prints the comparison and then refuses to draw a conclusion from it. The
    refusal is not decoration: the person reading this just made the change
    and wants it to have worked, which is exactly when a single-case delta
    is most likely to be believed.
    """
    from hdh.modules.careplan import tune as tuning
    from hdh.modules.careplan.evaluate import llm_grader
    from hdh.modules.careplan.generate import llm_selector
    from hdh.modules.careplan.graph import PlanServices

    try:
        services = PlanServices(selector=llm_selector(model=args.model), grader=llm_grader(model=args.model))
    except ImportError:
        raise SystemExit("careplan tune needs the agent extra: pip install 'hdh[agent]'") from None

    try:
        result = tuning.tune(
            session, args.mrn, args.before, args.after, services, noise=tuning.cohort_noise()
        )
    except ValueError as err:
        # A wrong MRN or a wrong database is a user error, not a crash. A
        # traceback here says "hdh is broken" when the answer is one env var.
        raise SystemExit(f"hdh careplan tune: {err}") from None
    print()
    for line in tuning.summarise(result):
        print(line)
    if args.html:
        print()
        for path in tuning.written_pages(result, args.html):
            print(f"  wrote {path}")


def _cmd_eval(session, args) -> None:
    """`hdh careplan eval` — build, inspect, check, or measure."""
    from hdh.modules.careplan import evalset

    try:
        cohort = evalset.load_cohort(args.cohort)
    except evalset.EvalError as err:
        raise SystemExit(f"hdh careplan eval: {err}") from None

    if args.eval_cmd == "build":
        written = evalset.build_cohort(session, cohort)
        print(f"\n  generated {written} patients from seed {cohort.seed}")
        print("  (deterministic — the same seed rebuilds the same charts)")
        return

    cases = evalset.select_cases(session, cohort)
    if not cases:
        raise SystemExit(
            f"hdh careplan eval: no cases — run `hdh careplan eval build` first, "
            f"or point HDH_DB_URL at the database holding cohort {cohort.name!r}"
        )

    if args.eval_cmd == "cases":
        print(
            f"\n  cohort {cohort.name}@{cohort.version}, seed {cohort.seed}, "
            f"{len(cases)}/{cohort.case_count} cases selected\n"
        )
        print(
            f"  {'stratum':<10}{'mrn':<14}{'age':>4}{'probs':>7}{'meds':>6}"
            f"{'flags':>7}{'topics':>8}{'defer':>7}  rubric"
        )
        for case in cases:
            print(
                f"  {case.stratum:<10}{case.mrn:<14}{case.age:>4}{case.problems:>7}"
                f"{case.medications:>6}{case.flags:>7}{case.topics:>8}{case.deferred:>7}"
                f"  {case.rubric}"
            )
        return

    if args.eval_cmd == "check":
        failures = 0
        print()
        for case in cases:
            result = evalset.check_case(session, case.mrn)
            mark = "ok" if result.ok else "FAIL"
            print(f"  [{mark:>4}] {case.mrn}  ({case.stratum}, {case.topics} topics)")
            for line in result.failures:
                print(f"         {line}")
            failures += 0 if result.ok else 1
        print()
        if failures:
            raise SystemExit(f"{failures}/{len(cases)} case(s) failed the deterministic checks")
        print(f"  all {len(cases)} cases pass the deterministic checks")
        return

    _cmd_eval_run(session, args, cohort, cases)


def _cmd_eval_run(session, args, cohort, cases) -> None:
    """Generate and grade every case, then say whether anything moved."""
    from hdh.modules.careplan import evalset
    from hdh.modules.careplan.evaluate import llm_grader
    from hdh.modules.careplan.generate import llm_selector
    from hdh.modules.careplan.plan import PlanServices

    if args.limit:
        cases = cases[: args.limit]
    try:
        services = PlanServices(selector=llm_selector(model=args.model), grader=llm_grader(model=args.model))
    except ImportError:
        raise SystemExit("careplan eval needs the agent extra: pip install 'hdh[agent]'") from None

    total = len(cases) * args.repeat
    print(f"\n  {len(cases)} case(s) x {args.repeat} run(s) = {total} plans, each generated and graded")
    if args.repeat < 2:
        print("  (one run per case measures no noise floor — use --repeat 2 to get one)")
    print()

    def announce(measurement):
        spread = f" sd {measurement.deviation}" if measurement.deviation else ""
        print(f"  {measurement.mrn:<14} {measurement.stratum:<10} mean {measurement.mean}{spread}")

    report = evalset.run(
        session,
        cases,
        services,
        evalset.RunSettings(
            repeat=args.repeat, revise=args.revise, cohort=cohort.name, version=cohort.version
        ),
        on_case=announce,
    )

    print()
    print(
        f"  cohort mean {report.mean} across {len(report.measurements)} case(s), "
        f"noise {report.noise} (pooled sd; widest observed range {report.widest})"
    )

    if report.usage.get("calls"):
        from hdh.modules.careplan.usage import Ledger
        from hdh.modules.careplan.usage import summarise as usage_lines

        spent = Ledger(
            calls=report.usage["calls"],
            input_tokens=report.usage["input_tokens"],
            output_tokens=report.usage["output_tokens"],
            by_stage={stage: Ledger(**counts) for stage, counts in report.usage["by_stage"].items()},
        )
        print()
        for line in usage_lines(spent, "  cost: "):
            print(line)

    path = _baseline_path(cohort.name)
    if path.is_file():
        print()
        print("  against the baseline:")
        for line in evalset.compare(report, evalset.load_baseline(path)):
            print(line)
    else:
        print(f"  no baseline yet at {path.name}")

    if args.save:
        evalset.save_baseline(path, report)
        print()
        print(f"  written as the baseline: {path}")


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


def _cmd_retrievers() -> None:
    """The retrieval menu, and what this environment asks for.

    Worth a command because the choice is now configuration (§15.1), and a
    setting nobody can inspect is a setting nobody trusts.
    """
    from hdh.modules.careplan.retriever import DEFAULT, ENV_VAR, catalogue, configured

    chosen = configured()
    print()
    for name, (built, description) in catalogue().items():
        mark = "->" if name == chosen else "  "
        state = "" if built else "   (not implemented yet)"
        print(f"  {mark} {name:<16} {description}{state}")
    print()
    print(f"  configured by {ENV_VAR} (default: {DEFAULT})")


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
    from hdh.modules.careplan.retriever import RetrieverError, build_store

    query = " ".join(args.query)
    try:
        hits = build_store(session).search(query, args.corpus, k=args.k)
    except (DatabaseFeatureError, RetrieverError) as err:
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
    from hdh.modules.careplan.plan import PlanServices, generate_plan

    patient = session.query(Patient).filter(Patient.mrn == args.mrn).first()
    if patient is None:
        raise SystemExit(f"no patient {args.mrn}")

    try:
        selector = llm_selector(model=args.model)
    except ImportError:
        raise SystemExit("careplan generate needs the agent extra: pip install 'hdh[agent]'") from None

    try:
        grader = llm_grader(model=args.model) if (args.evaluate or args.revise) else None
        result = generate_plan(
            session,
            patient,
            services=PlanServices(selector=selector, grader=grader),
            revise=args.revise,
            dry_run=args.dry_run,
        )
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
    if result.revision is not None and result.rubric is not None:
        print()
        print(f"  {len(result.revision.rounds)} attempt(s):")
        for line in result.revision.as_lines(result.rubric):
            print(line)
    if result.evaluation is not None and result.rubric is not None:
        _print_evaluation(result.rubric, result.evaluation)
        print(f"     recorded as evaluation #{result.evaluation_id}")
    for check in result.report.checked:
        print(f"     checked: {check}")


def _cmd_show(session, args) -> None:
    """One plan, printed so every element shows what it traces to."""
    from hdh.modules.careplan.persist import current_plan_id, load_plan, render_record

    plan_id = args.id
    if not plan_id:
        if not args.mrn:
            raise SystemExit("give --id or --mrn")
        patient = _patient(session, args.mrn)
        plan_id = current_plan_id(session, patient.id)
        if not plan_id:
            raise SystemExit(f"{args.mrn} has no saved care plan")

    plan = load_plan(session, plan_id)
    if plan is None:
        raise SystemExit(f"no care plan #{plan_id}")
    print()
    print(render_record(plan))


def _patient(session, mrn: str):
    from hdh.core.models import Patient

    patient = session.query(Patient).filter(Patient.mrn == mrn).first()
    if patient is None:
        raise SystemExit(f"no patient {mrn}")
    return patient


def _cmd_plans(session, args) -> None:
    """Every saved plan for one patient, newest first."""
    from hdh.modules.careplan.persist import _standing, plans_for

    patient = _patient(session, args.mrn)
    plans = plans_for(session, patient.id)
    if not plans:
        raise SystemExit(f"{args.mrn} has no saved care plan")
    print()
    print(f"care plans for {args.mrn}")
    for plan in plans:
        standing = _standing(plan["superseded_by"], plan["current"])
        supersedes = f"  supersedes #{plan['supersedes']}" if plan["supersedes"] else ""
        when = plan["created_at"].strftime("%Y-%m-%d") if plan["created_at"] else "—"
        print(f"  #{plan['id']:<5}{when}  {plan['status']:<13}{standing}{supersedes}")
        print(f"         {plan['title']}")

"""`hdh comprehend` — the comprehension module's CLI (milestones A–B).

Default: run stages 1–5 on one note and print the comprehended record
(codes, final assertions with evidence, confidence). ``--store`` writes
NoteRecord/NoteMention rows for a stored visit note; ``--eval N`` runs
the §8 harness over N stored notes and prints honest numbers. The LLM
extractor needs the ``[agent]`` extra and an API key.
"""

from __future__ import annotations

REVIEW_CONFIDENCE = 0.6  # mentions below this are what the queue flags


def register_cli(subparsers) -> None:
    """Register the `hdh comprehend` subcommand."""
    p = subparsers.add_parser("comprehend", help="Doctor-note comprehension: note → structured record")
    p.add_argument("--file", help="Path to a note text file")
    p.add_argument("--mrn", help="Comprehend a stored visit note (with --visit-date)")
    p.add_argument("--visit-date", help="Visit date YYYY-MM-DD (with --mrn)")
    p.add_argument(
        "--store", action="store_true", help="Write NoteRecord/NoteMention rows (stored notes only)"
    )
    p.add_argument(
        "--eval",
        type=int,
        default=None,
        metavar="N",
        help="Evaluate the pipeline over N stored notes against generator ground truth",
    )
    p.add_argument(
        "--apply",
        action="store_true",
        help="Apply the comprehended note to the chart (reconciled; needs --mrn)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="With --apply: show every reconciliation verdict, write NOTHING",
    )
    p.add_argument(
        "--fhir",
        metavar="PATH",
        default=None,
        help="Write the FHIR document Bundle (export artifact) to PATH",
    )
    p.add_argument("--review", action="store_true", help="List needs_review records (the review queue)")
    p.add_argument(
        "--resolve", type=int, default=None, metavar="ID", help="With --review: resolve one record"
    )
    p.add_argument(
        "--decision",
        choices=("accept", "reject"),
        default="accept",
        help="With --resolve: accept (complete) or reject (failed)",
    )
    p.add_argument(
        "--icd10",
        default=None,
        metavar="CODE",
        help="With --resolve --decision accept: chart the flagged problem with this billing code",
    )
    p.add_argument(
        "--mention",
        default=None,
        help="With --icd10: which flagged mention to chart, when a record has several",
    )
    p.add_argument("--model", default=None, help="Model override (default: HDH_AGENT_MODEL)")
    p.set_defaults(func=run)


def run(session, args) -> None:
    """Dispatch: review queue, evaluation harness, or single-note runs."""
    if args.review:
        run_review(session, args)
        return
    try:
        from hdh.modules.comprehension.extract import llm_extractor

        extractor = llm_extractor(model=args.model)
    except ImportError:
        raise SystemExit(
            "comprehend needs the [agent] extra (pip install hdh[agent]) and an API key"
        ) from None

    if args.eval is not None:
        from hdh.modules.comprehension.evaluate import evaluate_corpus

        print(f"evaluating stages 1–5 over {args.eval} stored notes (LLM extraction per note)…")
        print(evaluate_corpus(session, extractor, limit=args.eval).report())
        return

    from hdh.modules.comprehension.comprehend import ComprehensionError, comprehend_text
    from hdh.modules.comprehension.pipeline import comprehend_note, store_record

    note_text, visit_note_id = _load_note(session, args)
    try:
        extraction = comprehend_text(note_text, extractor)
    except ComprehensionError as err:
        raise SystemExit(f"comprehension failed: {err}") from None
    comprehended = comprehend_note(session, extraction)
    print(render_comprehended(comprehended))
    if args.store:
        if visit_note_id is None:
            raise SystemExit("--store needs a stored note (--mrn/--visit-date), not --file")
        record_id = store_record(session, visit_note_id, comprehended)
        status = "needs_review" if comprehended.needs_review else "complete"
        print(f"stored as NoteRecord #{record_id} ({status})")
    if args.apply:
        _apply(session, args, comprehended, visit_note_id)
    if args.fhir:
        _export_fhir(session, args.fhir, comprehended)


def _apply(session, args, comprehended, visit_note_id) -> None:
    """Chart application with reconciliation verdicts (design §10.3)."""
    from hdh.core.models import Patient, VisitNote
    from hdh.modules.comprehension.applier import apply_to_chart

    if not args.mrn:
        raise SystemExit("--apply needs --mrn (the chart to update)")
    patient = session.query(Patient).filter(Patient.mrn == args.mrn).first()
    if patient is None:
        raise SystemExit(f"no patient {args.mrn}")
    visit = session.get(VisitNote, visit_note_id).visit if visit_note_id else None
    from hdh.modules.comprehension.applier import VisitTarget

    result = apply_to_chart(
        session, patient, comprehended, target=VisitTarget(visit=visit), dry_run=args.dry_run
    )
    origin = "existing visit" if not result.created_visit else f"NEW visit #{result.visit_id}"
    mode = "DRY RUN — nothing written; would apply" if args.dry_run else "applied"
    print(f"\n{mode} to {origin}:")
    for verdict in result.verdicts:
        print(f"  {verdict.action:<10} {verdict.kind:<10} {verdict.detail}")
    if result.needs_review:
        print("  ⚠ review items were NOT written — resolve and re-run")


def _export_fhir(session, path: str, comprehended) -> None:
    import json
    from pathlib import Path

    from hdh.modules.comprehension.assemble import assemble_bundle

    bundle = assemble_bundle(session, comprehended)
    Path(path).write_text(json.dumps(bundle, indent=1), encoding="utf-8")
    print(f"FHIR document bundle ({len(bundle['entry'])} entries) → {path}")


def render_comprehended(note) -> str:
    """The stages-1–5 report: type, span, code, assertion, confidence."""
    lines = [f"sections: {len(note.extraction.sections)} · mentions: {len(note.mentions)}"]
    for item in note.mentions:
        mention = item.mention
        code = f"{item.code.system}:{item.code.code} {item.code.display!r}" if item.code else "— unlinked"
        attrs = ", ".join(f"{a.kind.value}={a.text!r}" for a in mention.attributes)
        lines.append(
            f"  [{mention.id}] {mention.mention_type.value:<10} {mention.text!r} → {code}"
            + (f"  {{{attrs}}}" if attrs else "")
        )
        lines.append(
            f"       assertion: {item.assertion.assertion.value} ({item.assertion.evidence}) · "
            f"confidence {item.confidence}"
        )
    for relation in note.extraction.relations:
        source = note.extraction.mentions[relation.source_id]
        target = note.extraction.mentions[relation.target_id]
        flag = " (inferred)" if relation.inferred else ""
        lines.append(f"  relation: {source.text!r} —{relation.kind.value}→ {target.text!r}{flag}")
    if note.needs_review:
        lines.append("  ⚠ low-confidence mentions present — record would be flagged needs_review")
    return "\n".join(lines)


def _load_note(session, args) -> tuple[str, int | None]:
    """The note text (+ VisitNote id when stored) from --file or --mrn."""
    from pathlib import Path

    if args.file:
        return Path(args.file).read_text(encoding="utf-8"), None
    if args.mrn and args.visit_date:
        from datetime import date

        from hdh.core.models import Patient, Visit, VisitNote

        row = (
            session.query(VisitNote)
            .join(Visit, VisitNote.visit_id == Visit.id)
            .join(Patient, Visit.patient_id == Patient.id)
            .filter(Patient.mrn == args.mrn, Visit.visit_date == date.fromisoformat(args.visit_date))
            .first()
        )
        if row is None:
            raise SystemExit(f"no stored note for {args.mrn} on {args.visit_date}")
        return row.text, row.id
    raise SystemExit("provide --file PATH, or --mrn and --visit-date for a stored note")


def run_review(session, args) -> None:
    """The review loop's CLI basics (master design §14 Q5): list every
    needs_review record with its flagged mentions; --resolve marks one
    complete (accept) or failed (reject) — a human decision, recorded."""
    from sqlalchemy import select, update

    from hdh.core.models import Base

    tables = Base.metadata.tables
    records_t, mentions_t = tables["note_records"], tables["note_mentions"]

    if args.resolve is not None:
        decision = "complete" if args.decision == "accept" else "failed"
        charted = ""
        if getattr(args, "icd10", None):
            if args.decision != "accept":
                raise SystemExit("--icd10 only applies to --decision accept")
            charted = _chart_review_item(session, args)
        updated = session.execute(
            update(records_t)
            .where(records_t.c.id == args.resolve, records_t.c.status == "needs_review")
            .values(status=decision)
        )
        session.commit()
        if updated.rowcount == 0:
            raise SystemExit(f"no needs_review record #{args.resolve}")
        print(f"record #{args.resolve} → {decision}{charted}")
        return

    rows = session.execute(
        select(records_t).where(records_t.c.status == "needs_review").order_by(records_t.c.id)
    ).all()
    if not rows:
        print("review queue is empty")
        return
    for record in rows:
        print(f"record #{record.id} (visit_note {record.visit_note_id}, v{record.pipeline_version}):")
        flagged = session.execute(
            select(mentions_t)
            .where(mentions_t.c.record_id == record.id, mentions_t.c.confidence < REVIEW_CONFIDENCE)
            .order_by(mentions_t.c.start)
        ).all()
        for mention in flagged:
            code = mention.concept_id or (mention.properties or {}).get("code") or "unlinked"
            print(f"  {str(mention.mention_type):<12} {mention.text!r} → {code} (conf {mention.confidence})")
    print("\nresolve with: hdh comprehend --review --resolve <id> --decision accept|reject")


def _flagged_mentions(session, record_id: int, wanted: str | None) -> list:
    """The record's low-confidence problem mentions, narrowed by --mention."""
    from sqlalchemy import select

    from hdh.core.models import Base

    mentions_t = Base.metadata.tables["note_mentions"]
    rows = session.execute(
        select(mentions_t)
        .where(mentions_t.c.record_id == record_id, mentions_t.c.confidence < REVIEW_CONFIDENCE)
        .order_by(mentions_t.c.start)
    ).all()
    problems = [row for row in rows if str(row.mention_type).endswith("problem")]
    if wanted:
        problems = [row for row in problems if wanted.lower() in row.text.lower()]
    return problems


def _chart_review_item(session, args) -> str:
    """Accepting a review item WRITES it (design chart-maintenance.md §4).

    This is the transition the review queue always implied and never had:
    the item a human just approved becomes a real Condition, created
    through the same audited path as every other chart change — so the
    trail records that a person, not the pipeline, made the call."""
    from sqlalchemy import select

    from hdh.core.chartedit import Actor, record_creation
    from hdh.core.chartedit.cli import _actor
    from hdh.core.models import Base, Condition, ConditionStatus, EditSource, Visit, VisitNote

    records_t = Base.metadata.tables["note_records"]
    record = session.execute(select(records_t).where(records_t.c.id == args.resolve)).first()
    if record is None:
        raise SystemExit(f"no record #{args.resolve}")
    note = session.get(VisitNote, record.visit_note_id)
    visit = session.get(Visit, note.visit_id) if note else None
    if visit is None:
        raise SystemExit(f"record #{args.resolve} has no visit to chart against")

    flagged = _flagged_mentions(session, args.resolve, args.mention)
    if not flagged:
        raise SystemExit(
            f"record #{args.resolve} has no flagged problem mention"
            + (f" matching {args.mention!r}" if args.mention else " to chart")
        )
    if len(flagged) > 1:
        names = ", ".join(repr(row.text) for row in flagged)
        raise SystemExit(
            f"record #{args.resolve} has several flagged mentions ({names}) — pick one with --mention"
        )

    mention = flagged[0]
    snomed = (mention.concept_id or "").split(":", 1)[-1] if mention.concept_id else None
    row = Condition(
        patient_id=visit.patient_id,
        visit_id=visit.id,
        icd10_code=args.icd10,
        description=mention.text,
        chronic=False,
        status=ConditionStatus.ACTIVE,
        onset_date=visit.visit_date,
    )
    if snomed and hasattr(row, "snomed_code") and hasattr(row, "snomed_display"):
        row.snomed_code = snomed
        row.snomed_display = (mention.properties or {}).get("display") or mention.text
    session.add(row)
    session.flush()
    actor = _actor()
    record_creation(
        session,
        Actor(name=actor.name, source=EditSource.CLI, provider_id=None),
        "Condition",
        row,
        reason=f"review resolution: record #{args.resolve} accepted with {args.icd10}",
    )
    return f" · charted {mention.text!r} as {args.icd10} (Condition #{row.id})"

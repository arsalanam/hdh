"""`hdh chart` — the human surface for chart maintenance (design §3.4).

A thin client of :func:`hdh.core.chartedit.apply_edits`: it parses,
prints, and does nothing else. Every mutating command supports
``--dry-run`` and prints exactly the outcome lines the agent reports.
"""

from __future__ import annotations


def register(subparsers) -> None:
    """Register `hdh chart` and its operations."""
    parser = subparsers.add_parser("chart", help="Amend, void, and audit chart entries")
    chart_sub = parser.add_subparsers(dest="chart_cmd", required=True)

    hist = chart_sub.add_parser("history", help="The audit trail for one patient, newest first")
    hist.add_argument("--mrn", required=True)
    hist.add_argument("--limit", type=int, default=50)

    amend = chart_sub.add_parser("amend", help="Change fields on one chart row")
    amend.add_argument(
        "--entity", required=True, help="Condition | Prescription | Vital | LabResult | Allergy | Visit"
    )
    amend.add_argument("--id", type=int, required=True, dest="row_id")
    amend.add_argument("--set", action="append", default=[], metavar="FIELD=VALUE", dest="assignments")
    amend.add_argument("--reason", default="", help="Required for clinical entities")
    amend.add_argument("--dry-run", action="store_true")

    void = chart_sub.add_parser("void", help="Mark one row (or a whole visit) entered-in-error")
    void.add_argument("--entity", default="Visit")
    void.add_argument("--id", type=int, dest="row_id", help="Row id (with --entity)")
    void.add_argument("--visit", type=int, default=None, help="Shorthand for --entity Visit --id N")
    void.add_argument("--reason", default="")
    void.add_argument("--dry-run", action="store_true")

    purge = chart_sub.add_parser(
        "purge-visit", help="ADMIN: really delete a visit and everything it owns (not clinical)"
    )
    purge.add_argument("--id", type=int, required=True, dest="row_id")
    purge.add_argument("--yes", action="store_true", help="Required: this cannot be undone")


def run(session, args) -> None:
    """Dispatch one `hdh chart` operation."""
    if args.chart_cmd == "history":
        _history(session, args)
    elif args.chart_cmd == "purge-visit":
        _purge(session, args)
    else:
        _edit(session, args)


def _actor(session):
    """The signed-in identity, or the OS user when not logged in (AU2)."""
    from hdh.core.identity import cli_actor

    return cli_actor(session)


def _patient(session, mrn: str):
    from hdh.core.models import Patient

    patient = session.query(Patient).filter(Patient.mrn == mrn).first()
    if patient is None:
        raise SystemExit(f"no patient {mrn}")
    return patient


def _history(session, args) -> None:
    from hdh.core.chartedit import history

    patient = _patient(session, args.mrn)
    events = history(session, patient.id, limit=args.limit)
    if not events:
        print(f"no recorded changes for {args.mrn}")
        return
    print(f"chart history for {args.mrn} ({len(events)} most recent):\n")
    for event in events:
        stamp = event.occurred_at.strftime("%Y-%m-%d %H:%M")
        source = event.actor_source.value if hasattr(event.actor_source, "value") else event.actor_source
        action = event.action.value if hasattr(event.action, "value") else event.action
        print(f"  {stamp}  {action:<7} {event.entity}#{event.row_id:<6} by {event.actor_name} ({source})")
        if event.reason:
            print(f"           reason: {event.reason}")
        if event.before or event.after:
            print(f"           {event.before or {}} → {event.after or {}}")


def _edit(session, args) -> None:
    from hdh.core.chartedit import ChartEdit, EditAction, apply_edits

    if args.chart_cmd == "amend":
        changes = {}
        for assignment in args.assignments:
            if "=" not in assignment:
                raise SystemExit(f"--set expects FIELD=VALUE, got {assignment!r}")
            field, value = assignment.split("=", 1)
            changes[field.strip()] = value.strip()
        edit = ChartEdit(args.entity, args.row_id, EditAction.AMEND, changes, args.reason)
    else:
        row_id = args.visit if args.visit is not None else args.row_id
        if row_id is None:
            raise SystemExit("void needs --id (with --entity) or --visit")
        entity = "Visit" if args.visit is not None else args.entity
        edit = ChartEdit(entity, row_id, EditAction.VOID, {}, args.reason)

    # A chart edit requires login and the matching permission (AU3): amending
    # a row is `chart:edit`, voiding one is `chart:void`.
    from hdh.core.identity import authorize_cli, resolve_actor
    from hdh.core.models import EditSource

    permission = "chart:void" if edit.action is EditAction.VOID else "chart:edit"
    identity = authorize_cli(session, permission)
    actor = resolve_actor(session, identity, EditSource.CLI)

    outcomes = apply_edits(session, actor, [edit], dry_run=args.dry_run)
    for outcome in outcomes:
        print(("✅ " if outcome.applied else "⚠️  ") + outcome.detail)
    if not all(o.applied for o in outcomes):
        raise SystemExit(1)


def _purge(session, args) -> None:
    from hdh.core.chartedit import purge_visit

    if not args.yes:
        raise SystemExit("purge-visit really deletes rows — pass --yes to confirm")
    try:
        counts = purge_visit(session, args.row_id)
    except ValueError as err:
        raise SystemExit(str(err)) from None
    deleted = ", ".join(f"{name} {count}" for name, count in counts.items() if count)
    print(f"🗑  purged visit #{args.row_id}: {deleted or 'nothing'}")

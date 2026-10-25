"""Scheduling: provider calendars, appointments, reminders (design appointments).

The appointment *schema* is core (``hdh.core.models.Appointment`` — the chart's
encounters link to it); this module owns the *behavior*: the pure store + conflict
checks (:mod:`.appointments`), the agent's find/book/reschedule/cancel tools
(:mod:`.agent_tools`), and — in a later phase — SES email reminders and the
patient-facing cancel/change-by-email path.

Everything is driven through the agent (web ``/ask`` or the CLI agent); there is
no dedicated scheduling UI and no appointment-CRUD CLI by design.
"""

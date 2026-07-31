# Billing module (scaffold)

Simulates professional billing for visits: E/M CPT assignment from visit type
and patient age, work RVUs, and a rough charge estimate.

Current state: `cpt_for_visit()` and `estimate_claim()`.

## Planned extensions

- Lab and procedure CPT codes (from LOINC-coded lab orders).
- Full RVU components (work + practice expense + malpractice) and GPCI.
- Insurance-claim lifecycle simulation (submitted → adjudicated → paid/denied)
  keyed to each patient's insurer.
- An `hdh billing` CLI subcommand and a claims exporter (X12 837P-like JSON).

See CONTRIBUTING.md for how modules hook into the core.

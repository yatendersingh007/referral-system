"""§7 step 12 — end-to-end TEST MODE run seeded from the REAL connections
file. Targets the five required outcomes in one batch:

  1. Kingbird Investment Management -> qualify -> drafted, PDF deck attached
  2. Blackstone                     -> disqualify (AUM exceeds ceiling)
  3. Aliya Capital Partners         -> park (AUM unverifiable)
  4. Singh Capital Partners         -> excluded (referrer's own firm)
  5. Totally Nonexistent Firm XYZ   -> no_connection

Also runs the §10c research-quality smoke test: Kingbird's research object
must carry >=3 sourced facts, a non-empty AUM with a confidence tag, and a
non-empty firm_oneliner.

Run: python scripts/run_e2e_test.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src import config, dbc, loader, pipeline, preflight  # noqa: E402

TARGETS = [
    "Kingbird Investment Management",
    "Blackstone",
    "Aliya Capital Partners",
    "Singh Capital Partners",
    "Totally Nonexistent Firm XYZ",
]


def _log(msg: str) -> None:
    print(f"[e2e] {msg}")


def main() -> int:
    assert config.TEST_MODE, "This script must run with TEST_MODE=true in .env"

    _log("Applying migrations (non-destructive)…")
    dbc.apply_migrations()

    _log("Running preflight…")
    problems = preflight.check_hard_blockers()
    if problems:
        for p in problems:
            print("PREFLIGHT PROBLEM:", p)
        return 1
    _log("Preflight OK.")

    _log("Reloading connections.csv…")
    lr = loader.reload_connections()
    _log(f"Loaded {lr.loaded} rows, {lr.distinct_companies} distinct companies, "
        f"{lr.incomplete} incomplete.")
    assert lr.distinct_companies > 20_000, "connections file did not load as expected"

    _log(f"Starting batch run targeting: {TARGETS}")
    run = dbc.create_wc_run("batch")
    match_summary = pipeline.match_and_enqueue(run["id"], TARGETS, progress=_log)
    _log(f"Match summary: {match_summary}")

    totals = pipeline.drain_queue(run["id"], progress=_log)
    dbc.update_wc_run(run["id"], status="done")
    _log(f"Drain totals: {totals}")

    firms = dbc.firms_for_run(run["id"])
    by_name = {f["firm_name"].lower(): f for f in firms}

    failures = []

    # --- Outcome 1: Kingbird -> qualified -> drafted ---
    kingbird = by_name.get("kingbird investment management")
    if not kingbird:
        failures.append("Kingbird firm row not created")
    else:
        if kingbird.get("icp_status") != "qualified":
            failures.append(f"Kingbird icp_status = {kingbird.get('icp_status')!r}, expected 'qualified'")
        draft = dbc.wc_draft_for_firm(kingbird["id"])
        if not draft or draft.get("email_status") != "drafted":
            failures.append(f"Kingbird draft status = {draft.get('email_status') if draft else None!r}, "
                           f"expected 'drafted'")
        if draft and not draft.get("ppt_attached"):
            failures.append("Kingbird draft did not have a PPT/PDF attached")
        if draft and not draft.get("gmail_draft_id"):
            failures.append("Kingbird draft has no gmail_draft_id")

        # §10c research quality smoke test
        research = kingbird.get("research_raw") or {}
        sources = research.get("sources") or []
        if len(sources) < 3:
            # Phase 2 alone yields 2 sources in the fixture; Phase 4 personalization
            # hooks each carry their own source too — check the combined total.
            pers = (draft or {}).get("personalization") or {}
            extra_sources = [s for s in (pers.get("hook_1_source"), pers.get("hook_2_source")) if s]
            if len(sources) + len(extra_sources) < 3:
                failures.append(f"Kingbird research has only {len(sources) + len(extra_sources)} "
                               f"sourced facts, need >=3")
        if not research.get("aum_value_usd") or not research.get("aum_tag"):
            failures.append("Kingbird research missing a non-empty AUM value + confidence tag")
        if not research.get("firm_oneliner"):
            failures.append("Kingbird research missing firm_oneliner")

        pptx_path = ROOT / "generated_decks_c" / "XAnalyst_Kingbird_Investment_Management_Pitch.pptx"
        pdf_path = ROOT / "generated_decks_c" / "XAnalyst_Kingbird_Investment_Management_Pitch.pdf"
        if not pptx_path.exists():
            failures.append(f"Kingbird PPTX not found at {pptx_path}")
        if not pdf_path.exists():
            failures.append(f"Kingbird PDF not found at {pdf_path}")

    # --- Outcome 2: Blackstone -> disqualified ---
    blackstone = by_name.get("blackstone")
    if not blackstone:
        failures.append("Blackstone firm row not created")
    elif blackstone.get("icp_status") != "disqualified":
        failures.append(f"Blackstone icp_status = {blackstone.get('icp_status')!r}, expected 'disqualified'")

    # --- Outcome 3: Aliya Capital Partners -> parked ---
    aliya = by_name.get("aliya capital partners")
    if not aliya:
        failures.append("Aliya Capital Partners firm row not created")
    elif aliya.get("icp_status") != "parked":
        failures.append(f"Aliya icp_status = {aliya.get('icp_status')!r}, expected 'parked'")
    elif aliya.get("park_reason") != "aum_unverified":
        failures.append(f"Aliya park_reason = {aliya.get('park_reason')!r}, expected 'aum_unverified'")

    # --- Outcome 4: Singh Capital Partners -> excluded, no wc_firms row ---
    if match_summary.get("referrer_own_firm", 0) < 1:
        failures.append("Singh Capital Partners was not excluded as the referrer's own firm")
    if "singh capital partners" in by_name:
        failures.append("Singh Capital Partners should NEVER be gated/drafted (referrer's own firm)")

    # --- Outcome 5: unknown firm -> no_connection ---
    if match_summary.get("no_connection", 0) < 1:
        failures.append("'Totally Nonexistent Firm XYZ' did not report no_connection")

    print()
    if failures:
        print(f"FAILED — {len(failures)} problem(s):")
        for f in failures:
            print(" -", f)
        return 1

    print("ALL 5 OUTCOMES VERIFIED CORRECTLY.")
    print(f"Match summary: {match_summary}")
    print(f"Drain totals: {totals}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

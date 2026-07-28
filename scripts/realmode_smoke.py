"""QA Part 8 — real-mode dry-run harness. SCAFFOLD ONLY: this script is not
executed as part of the automated QA pass (TEST_MODE fixtures cannot prove
research accuracy, email quality, or deck factual correctness — those need
real API calls and human judgment, per the QA brief's Layer 1 / Layer 2
split). Run this yourself, deliberately, when you want to judge one firm by
hand.

Runs the FULL live pipeline (real Tavily/Exa/LLM/Apollo/Gmail-draft calls,
no mocks) for exactly ONE firm name and writes a human-readable report to
qa_out/realmode_<firm>.md: the raw research object (AUM value + tag + every
source URL), the matched connection, the Apollo result, the generated email
body, and the path to the generated PDF deck.

Refuses to run with TEST_MODE=true (that would silently produce a fake
"success" using fixtures, defeating the entire point of this script) and
refuses more than one firm name (this is a single-firm judgment tool, not a
batch cost-control bypass — batch runs go through the real /referral batch
command with its own budget guards).

Usage:
    TEST_MODE=false python scripts/realmode_smoke.py "Kingbird Investment Management"

NEVER call this from an automated test. NEVER run it without the user's
explicit go-ahead — it spends real Tavily/Exa/LLM/Apollo credits and creates
a REAL Gmail draft (draft only, per HARD RULE 1 — it will not send).
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src import (apollo_enrich, config, dbc, deck_filler, email_drafter,  # noqa: E402
                 gmail_client, icp_gate, personalization, poc_ranking, preflight)
from src.matching import firm_matcher  # noqa: E402


def _fail(msg: str) -> None:
    print(f"REFUSING TO RUN: {msg}", file=sys.stderr)
    sys.exit(1)


def _first_name(poc_name: str) -> str:
    parts = (poc_name or "").strip().split()
    return parts[0] if parts else "there"


def main() -> int:
    if config.TEST_MODE:
        _fail("TEST_MODE=true — this script is meaningless with fixtures. "
             "Set TEST_MODE=false in .env (or override in the environment) and re-run.")

    if len(sys.argv) != 2:
        _fail(f'usage: python {Path(__file__).name} "<Exact Firm Name>" (exactly ONE firm — '
             f"this is a single-firm judgment tool, not a batch cost-control bypass)")

    firm_query = sys.argv[1].strip()
    if not firm_query:
        _fail("empty firm name")

    print("=" * 70)
    print("⚠️  SPENDING REAL API CREDITS — Tavily, Exa, LLM, Apollo, and a real")
    print("    Gmail DRAFT (never sent, per HARD RULE 1) will all be touched.")
    print(f"    Target firm: {firm_query!r}")
    print("=" * 70)

    print("Running preflight (hard blockers + transient reachability)...")
    try:
        preflight.run_preflight(progress=print)
    except preflight.PreflightFailed as e:
        _fail(f"preflight failed: {e.problems}")

    report_lines: list[str] = [
        f"# Real-mode smoke report — {firm_query}",
        f"Generated: {datetime.now(timezone.utc).isoformat()}",
        "",
        "**This report is for human judgment of research accuracy, email quality, "
        "and deck factual correctness — the parts no automated TEST_MODE test can "
        "score.** Read it skeptically: verify the AUM figure and every source URL "
        "actually say what the research object claims.",
        "",
    ]

    # --- Phase 1: match against the real connections corpus ---
    company_names = dbc.distinct_company_names()
    if len(company_names) < 1000:
        _fail("connections table looks empty/tiny — run /referral reload or "
             "src.loader.reload_connections() first")
    match_results = firm_matcher.match_firms([firm_query], company_names)
    match = match_results[0]
    report_lines += [
        "## Phase 1 — Firm match",
        f"- Outcome: `{match.outcome}`",
        f"- Method: `{match.match_method}`",
        f"- Matched company: `{match.matched_company}`",
        f"- Confidence: `{match.confidence}`",
        "",
    ]
    if match.outcome != "matched":
        report_lines.append(f"**Stopped here — no usable match ({match.outcome}).**")
        _write_report(firm_query, report_lines)
        print(f"No match ({match.outcome}). Report written; nothing further to run.")
        return 0

    matched_company = match.matched_company
    connections_for_firm = dbc.connections_for_company(matched_company)
    top, alternates = poc_ranking.rank_pocs(connections_for_firm)
    report_lines += [
        "## Matched connection (top POC)",
        f"- Name: {top['poc_name'] if top else '(none eligible)'}",
        f"- Designation: {top.get('designation') if top else ''}",
        f"- LinkedIn URL: {top.get('linkedin_url') if top else ''}",
        f"- Seed email (from export): {top.get('poc_email_seed') if top else ''}",
        f"- Alternate POCs on file: {len(alternates)}",
        "",
    ]
    if not top:
        report_lines.append("**Stopped here — no higher-level-management POC among matched connections.**")
        _write_report(firm_query, report_lines)
        print("No eligible POC. Report written; nothing further to run.")
        return 0

    # --- Phase 2: ICP gate (real research + LLM extraction + deterministic apply_gate) ---
    print("Researching the firm (Tavily) and gating (LLM extraction + apply_gate)...")
    gate = icp_gate.research_and_gate(matched_company, progress=print)
    research = gate.research
    report_lines += [
        "## Phase 2 — ICP gate",
        f"- Result: `{gate.icp_result}`",
        f"- Reason: {gate.icp_reason}",
        f"- AUM tag: `{gate.aum_tag}`",
        f"- Proxy type: `{research.get('proxy_type')}`",
        f"- AUM value (raw): `{research.get('aum_value_usd')}`",
        f"- AUM display: {research.get('aum_display')}",
        f"- Confidence: {gate.confidence}",
        f"- Search calls: {gate.search_calls} | Tokens: {gate.tokens_used} | Cost: ${gate.research_cost_usd:.4f}",
        f"- Firm oneliner: {research.get('firm_oneliner')}",
        f"- Geography: {research.get('geography')}",
        f"- Capital stack: {research.get('capital_stack')}",
        f"- Founded year: {research.get('founded_year')}",
        "- Sources:",
    ] + [f"  - {u}" for u in (research.get("sources") or [])] + [""]

    if gate.icp_result != "qualified":
        report_lines.append(f"**Stopped here — gate result is `{gate.icp_result}`, not qualified.**")
        _write_report(firm_query, report_lines)
        print(f"Gate result: {gate.icp_result}. Report written; nothing further to run.")
        return 0

    # --- Phase 3: Apollo email discovery ---
    print("Resolving email (Apollo, or using the export's seed email)...")
    email, confidence = apollo_enrich.find_email(
        top["poc_name"], matched_company, top.get("linkedin_url"), research.get("domain"),
        seed_email=top.get("poc_email_seed"), progress=print,
    )
    report_lines += [
        "## Phase 3 — Apollo email discovery",
        f"- Email: {email}",
        f"- Confidence/source: {confidence}",
        "",
    ]
    if not email:
        report_lines.append("**Stopped here — no email found for the top POC.**")
        _write_report(firm_query, report_lines)
        print("No email found. Report written; nothing further to run.")
        return 0

    # --- Phase 4: personalization research ---
    print("Researching public personalization hooks (Tavily/Exa + LLM extraction)...")
    pers = personalization.research_personalization(matched_company, top["poc_name"], research, progress=print)
    report_lines += [
        "## Phase 4 — Personalization research",
        f"- thin_personalization: {pers.get('thin_personalization')}",
        f"- Hook 1: {pers.get('hook_1')} (source: {pers.get('hook_1_source')})",
        f"- Hook 2: {pers.get('hook_2')} (source: {pers.get('hook_2_source')})",
        f"- Pain point angle: {pers.get('pain_point_angle')} (source: {pers.get('pain_point_angle_source')})",
        f"- Service fit: {pers.get('service_fit')}",
        "",
    ]

    # --- Phase 5a: email draft ---
    print("Generating the email (LLM, referral_email.md prompt)...")
    email_draft = email_drafter.draft_email(
        matched_company, _first_name(top["poc_name"]), email, research, pers, progress=print,
    )
    report_lines += [
        "## Phase 5a — Email draft",
        f"- Subject: {email_draft['subject']}",
        f"- Word count: {email_draft['word_count']}",
        f"- thin_personalization fallback used: {email_draft['thin_personalization']}",
        "",
        "```",
        email_draft["body"],
        "```",
        "",
    ]

    # --- Phase 5b: PPT fill ---
    pdf_path = None
    if config.PITCH_MASTER_PPTX.exists():
        print("Filling the pitch deck (LLM extraction for bullets/fit paragraphs) and converting to PDF...")
        deck_content = deck_filler.get_deck_content(matched_company, research, pers)
        deck_result = deck_filler.build_and_convert(matched_company, research, deck_content, progress=print, personalization=pers)
        pdf_path = deck_result["pdf_path"]
        report_lines += [
            "## Phase 5b — Pitch deck",
            f"- PPTX: {deck_result['pptx_path']}",
            f"- PDF: {pdf_path}",
            f"- Fill data: {deck_result['fill_data']}",
            "",
        ]
    else:
        report_lines += ["## Phase 5b — Pitch deck", "Master PPTX not found; skipped (email-only).", ""]

    # --- Gmail draft (compose-only, HARD RULE 1 — never sends) ---
    print("Creating the Gmail DRAFT (compose scope only — never sends)...")
    draft_result = gmail_client.create_draft(email_draft["to"], email_draft["subject"],
                                             email_draft["body"], pdf_path, progress=print)
    report_lines += [
        "## Gmail draft",
        f"- Draft ID: {draft_result['draft_id']}",
        f"- Attached: {draft_result['attached']}",
        "",
        "**Review this draft by hand in Gmail before ever considering sending it "
        "(the system itself never sends — that's still your call, manually).**",
    ]

    _write_report(firm_query, report_lines)
    print(f"\nDone. Report: {_report_path(firm_query)}")
    return 0


def _report_path(firm_query: str) -> Path:
    import re

    slug = re.sub(r"[^A-Za-z0-9]+", "_", firm_query).strip("_") or "firm"
    out_dir = ROOT / "qa_out"
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir / f"realmode_{slug}.md"


def _write_report(firm_query: str, lines: list[str]) -> None:
    _report_path(firm_query).write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())

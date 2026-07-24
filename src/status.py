"""/status c dashboard + drill-down (§6). Token/search counts are logged to
Postgres for internal tracking but never shown in Telegram (§8 definition of
done) — spend stays in wc_firms for operator/DB inspection only.
"""
from __future__ import annotations

from . import dbc


def dashboard_text() -> str:
    run = dbc.latest_wc_run()
    firms = dbc.all_wc_firms()
    drafts = dbc.all_wc_drafts()

    qualified = [f for f in firms if f.get("icp_status") == "qualified"]
    disqualified = [f for f in firms if f.get("icp_status") == "disqualified"]
    parked = [f for f in firms if f.get("icp_status") == "parked"]
    drafted = [d for d in drafts if d.get("email_status") == "drafted"]
    thin = [d for d in drafted if d.get("thin_personalization")]
    no_email = [d for d in drafts if d.get("email_status") == "no_email_found"]
    dup = [d for d in drafts if d.get("email_status") == "skipped_duplicate"]

    lines = ["📊 Workflow C dashboard"]
    if run:
        lines.append(f"Last run: #{run['id']} ({run['mode']}) — status {run['status']}")
    lines.append(f"Firms seen: {len(firms)} | Qualified: {len(qualified)} | "
                f"Disqualified: {len(disqualified)} | Parked: {len(parked)}")
    lines.append(f"Drafts: {len(drafted)} ({len(thin)} thin-personalization) | "
                f"No email found: {len(no_email)} | Skipped duplicates: {len(dup)}")
    if parked:
        lines.append("")
        lines.append("Parked firms:")
        for f in parked:
            lines.append(f"  - {f['firm_name']}: {f.get('icp_reason') or f.get('park_reason')}")
    return "\n".join(lines)


def detail_text(firm_name: str) -> str:
    firm = dbc.wc_firm_by_name(firm_name)
    if not firm:
        return f'No record for "{firm_name}". Try /status c with no argument for the dashboard.'
    draft = dbc.wc_draft_for_firm(firm["id"])
    lines = [f"📋 {firm['firm_name']}"]
    lines.append(f"Match: {firm.get('match_method')} (confidence {firm.get('match_confidence')})")
    lines.append(f"ICP: {firm.get('icp_status')} — {firm.get('icp_reason')}")
    if firm.get("aum_tag"):
        lines.append(f"AUM tag: {firm['aum_tag']}")
    if draft:
        lines.append(f"POC email status: {draft.get('email_status')}")
        if draft.get("poc_email"):
            lines.append(f"Email: {draft['poc_email']}")
        if draft.get("gmail_draft_id"):
            lines.append(f"Gmail draft id: {draft['gmail_draft_id']}")
        lines.append(f"PPT attached: {draft.get('ppt_attached')}")
        lines.append(f"Thin personalization: {draft.get('thin_personalization')}")
        pers = draft.get("personalization") or {}
        if pers.get("hook_1"):
            lines.append(f"Hook 1: {pers['hook_1']} (source: {pers.get('hook_1_source', '')})")
        if pers.get("hook_2"):
            lines.append(f"Hook 2: {pers['hook_2']} (source: {pers.get('hook_2_source', '')})")
    return "\n".join(lines)


_AUM_VERIFIED_LABEL = {
    "AUM_CONFIRMED": "confirmed", "AUM_ESTIMATED": "estimated", "AUM_UNVERIFIED": "unverified",
}


def prospect_brief_text(firm_id: int) -> str:
    """Feature 1 — single-firm-mode-only 'prospect brief', reading like the
    old outreach-system agent explaining a prospect. Never called for batch
    mode (see src/pipeline.py's `chunk_style` split and telegram_bot.py's
    call sites — batch keeps grouped chunk output only)."""
    firm = dbc.get_wc_firm(firm_id)
    if not firm:
        return "No prospect brief available (firm record not found)."
    draft = dbc.wc_draft_for_firm(firm_id)
    research = firm.get("research_raw") or {}

    lines = [f"📋 PROSPECT BRIEF — {firm['firm_name']}", ""]

    lines.append("🏢 Firm")
    oneliner = research.get("firm_oneliner")
    if oneliner:
        lines.append(f"   {oneliner}")
    aum_tag = firm.get("aum_tag")
    verified = _AUM_VERIFIED_LABEL.get(aum_tag, aum_tag or "unknown")
    aum_display = research.get("aum_display") or "unknown"
    aum_line = f"   AUM: {aum_display} ({verified}"
    reason = firm.get("icp_reason") or ""
    if verified == "estimated" and " — " in reason:
        # icp_reason carries the proxy basis, e.g. "QUALIFIED (est. $800M — invested capital proxy)"
        basis = reason.split(" — ", 1)[1].rstrip(")")
        aum_line += f", {basis}"
    aum_line += ")"
    lines.append(aum_line)
    if research.get("geography"):
        lines.append(f"   Geography: {research['geography']}")
    if research.get("capital_stack"):
        lines.append(f"   Capital stack: {research['capital_stack']}")
    if research.get("founded_year"):
        lines.append(f"   Founded: {research['founded_year']}")
    lines.append("")

    lines.append("🎯 Why this prospect")
    lines.append(f"   {firm.get('icp_reason') or '(no reason on record)'}")
    lines.append("")

    lines.append("👤 Who we're reaching")
    top = dbc.connection_by_id(firm.get("top_poc_connection_id"))
    if top:
        title = top.get("designation") or "(no title on file)"
        lines.append(f"   {top['poc_name']} — {title}")
        lines.append(f"   Picked as top POC: highest-ranked accepted management title on file "
                    f"({title})")
    else:
        lines.append("   (no POC on file)")
    alt = firm.get("alternate_pocs") or []
    lines.append(f"   {len(alt)} alternate POC(s) on file for this firm")
    lines.append("")

    lines.append("💡 Personalization")
    pers = (draft or {}).get("personalization") or {}
    if not draft:
        lines.append("   (no draft on record)")
    elif pers.get("thin_personalization"):
        lines.append("   thin_personalization fallback used — research was too thin for "
                    "credible firm-specific hooks; the email uses the firm oneliner and Harp's "
                    "generic pain paragraph instead.")
    else:
        if pers.get("hook_1"):
            lines.append(f"   Hook 1: {pers['hook_1']}")
            lines.append(f"      source: {pers.get('hook_1_source', '(none)')}")
        if pers.get("hook_2"):
            lines.append(f"   Hook 2: {pers['hook_2']}")
            lines.append(f"      source: {pers.get('hook_2_source', '(none)')}")
        if not pers.get("hook_1") and not pers.get("hook_2"):
            lines.append("   (no hooks on record)")
    lines.append("")

    lines.append("✉️ Draft")
    if draft and draft.get("gmail_draft_id"):
        deck_note = "with PDF deck" if draft.get("ppt_attached") else "email only, no deck"
        lines.append(f"   Gmail draft {draft['gmail_draft_id']} ({deck_note})")
    else:
        lines.append("   (no draft created)")

    return "\n".join(lines)

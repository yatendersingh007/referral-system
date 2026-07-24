"""Gmail drafter (Workflow A finale).

For each qualified lead with an enriched email: draft with PREMIUM_MODEL, run
the style guard, and create a Gmail DRAFT (scope gmail.compose only, never send)
with assets/case_study.pdf attached. Mark lead 'drafted', log the draft id.

TEST MODE: owner's own Gmail. PRODUCTION: re-run OAuth with Harpreet's account
and replace the cached token; no code change.
"""
from __future__ import annotations

import base64
import json
import os
from datetime import datetime, timezone
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Callable, Optional

from .. import config, db, models
from . import style_guard

Progress = Callable[[str], None]


def _noop(_: str) -> None:
    pass


# ---------------------------------------------------------------------------
# Gmail service (OAuth installed-app flow, token cached to disk)
# ---------------------------------------------------------------------------
def get_service(interactive: bool = True):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    creds = None
    token_path = config.GMAIL_TOKEN_PATH
    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, config.GMAIL_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        elif interactive:
            if not os.path.exists(config.GMAIL_CREDENTIALS_PATH):
                raise RuntimeError(
                    f"Gmail credentials not found at {config.GMAIL_CREDENTIALS_PATH}."
                )
            flow = InstalledAppFlow.from_client_secrets_file(
                config.GMAIL_CREDENTIALS_PATH, config.GMAIL_SCOPES
            )
            creds = flow.run_local_server(port=0)
        else:
            raise RuntimeError(
                "Gmail not authorized. Run `python -m src.drafting.gmail_drafter auth` "
                "once on a machine with a browser to cache the token."
            )
        with open(token_path, "w", encoding="utf-8") as f:
            f.write(creds.to_json())

    return build("gmail", "v1", credentials=creds)


# ---------------------------------------------------------------------------
# Draft text generation (PREMIUM_MODEL + style guard)
# ---------------------------------------------------------------------------
def _format_examples(templates: list[dict]) -> str:
    if not templates:
        return "(no past examples provided yet)"
    parts = []
    for t in templates[:5]:
        subj = f"Subject: {t['subject']}\n" if t.get("subject") else ""
        parts.append(f"---\n{subj}{t['body']}")
    return "\n".join(parts)


def _first_name(lead: dict) -> str:
    fn = (lead.get("first_name") or "").strip()
    if fn:
        return fn
    full = (lead.get("full_name") or "").strip()
    return full.split()[0] if full else "there"


def _service_focus_for(company: dict) -> str:
    """Which X Analyst service to lead with, from the client profile (default
    'property accounting'). Resolved via the company's client_name."""
    client = (company or {}).get("client_name")
    if client:
        prof = db.get_client_profile(client)
        if prof and prof.get("pitch_service_focus"):
            return prof["pitch_service_focus"]
    return "property accounting"


def _signal_text(s) -> str:
    """A trigger_event is either a {"text","url"} dict (new rows) or a bare
    string (old rows). Return just the human text for the draft prompt."""
    if isinstance(s, dict):
        return (s.get("text") or s.get("event") or "").strip()
    return str(s or "").strip()


def _generate_email(lead: dict, company: dict, templates: list[dict],
                    max_attempts: int = 4) -> tuple[str, str, models.Usage]:
    tpl = config.load_prompt("gmail_draft.md")
    pain = company.get("pain_points") or []
    # trigger_events may be [{"text","url"}] (new) or bare strings (old rows).
    signals = [_signal_text(s) for s in (company.get("trigger_events") or [])]
    signals = [s for s in signals if s]
    prompt = tpl.format(
        first_name=_first_name(lead),
        full_name=lead.get("full_name", ""),
        designation=lead.get("designation", "") or "finance leader",
        company=lead.get("company", ""),
        company_summary=company.get("summary", "") or "",
        pain_points="; ".join(pain) if isinstance(pain, list) else str(pain),
        recent_signals="; ".join(signals) if signals else "",
        examples=_format_examples(templates),
        service_focus=_service_focus_for(company),
    )

    total = models.Usage()
    last_subject = last_body = ""
    for _ in range(max_attempts):
        parsed, u = models.complete_json(prompt, model=config.PREMIUM_MODEL,
                                         temperature=0.6, max_tokens=1200)
        total.add(u)
        subject = (parsed.get("subject") or "").strip()
        body = (parsed.get("body") or "").strip().replace("\r\n", "\n")
        last_subject, last_body = subject, body
        # Both guards must pass: style (subject+body) and body structure.
        if style_guard.is_clean(f"{subject}\n{body}") and style_guard.is_well_formed(body):
            return subject, body, total

    # Final safety net: dash sanitation can fix style; structure cannot be faked.
    subject = style_guard.sanitize(last_subject)
    body = style_guard.sanitize(last_body)
    style_v = style_guard.violations(f"{subject}\n{body}")
    fmt_v = style_guard.format_violations(body)
    if style_v or fmt_v:
        raise ValueError(
            f"draft failed guards after {max_attempts} attempts: "
            f"style={style_v} format={fmt_v}"
        )
    return subject, body, total


# ---------------------------------------------------------------------------
# MIME + draft creation
# ---------------------------------------------------------------------------
_SUBTYPES = {
    ".pdf": "pdf",
    ".pptx": "vnd.openxmlformats-officedocument.presentationml.presentation",
}


def _build_raw(to: str, subject: str, body: str, attach_path: Optional[str]) -> str:
    msg = MIMEMultipart()
    msg["To"] = to
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))
    if attach_path and os.path.exists(attach_path):
        ext = os.path.splitext(attach_path)[1].lower()
        subtype = _SUBTYPES.get(ext, "octet-stream")
        with open(attach_path, "rb") as f:
            part = MIMEApplication(f.read(), _subtype=subtype)
        part.add_header("Content-Disposition", "attachment",
                        filename=os.path.basename(attach_path))
        msg.attach(part)
    return base64.urlsafe_b64encode(msg.as_bytes()).decode()


def draft_for_lead(lead: dict, company: dict, templates: list[dict],
                   service=None, progress: Progress = _noop) -> dict:
    """Generate + create a Gmail draft for one lead. Returns a status dict."""
    if not lead.get("email"):
        return {"status": "skipped", "reason": "no email", "lead_id": lead["id"]}

    subject, body, usage = _generate_email(lead, company, templates)

    # Attachment: firm-specific pitch deck (PPTX), with fallback to the static PDF.
    attach, attach_kind = _resolve_attachment(company, progress)

    if service is None:
        service = get_service(interactive=False)

    raw = _build_raw(lead["email"], subject, body, attach)
    created = service.users().drafts().create(
        userId="me", body={"message": {"raw": raw}}
    ).execute()
    draft_id = created.get("id")

    db.transition_lead(
        lead["id"], "drafted", "drafter",
        detail={"draft_id": draft_id, "subject": subject, "model": config.PREMIUM_MODEL,
                "attachment": os.path.basename(attach) if attach else None,
                "attachment_kind": attach_kind, "tokens": usage.total_tokens},
        draft_channel="gmail_draft", draft_subject=subject, draft_message=body,
        gmail_draft_id=draft_id, drafted_at=datetime.now(timezone.utc),
        drafted_model=config.PREMIUM_MODEL,
    )
    return {"status": "drafted", "lead_id": lead["id"], "draft_id": draft_id,
            "subject": subject, "body": body,
            "attached": bool(attach), "attachment_kind": attach_kind,
            "usage": usage}


def _resolve_attachment(company: dict, progress: Progress) -> tuple[Optional[str], str]:
    """Firm-specific deck first; fall back to the static case-study PDF; never
    block the draft. Returns (path_or_None, kind)."""
    try:
        from . import pitch_deck
        rr = company.get("research_raw")
        rr = rr if isinstance(rr, str) else json.dumps(rr or {}, ensure_ascii=False, default=str)
        path = pitch_deck.generate_deck(company, rr)
        progress(f"Attached firm-specific deck: {os.path.basename(path)}")
        return path, "deck"
    except Exception as e:  # noqa: BLE001 - deck failure must never block a draft
        progress(f"Deck generation failed ({str(e)[:140]}); falling back to static PDF.")
    pdf = config.resolve_case_study()
    if pdf:
        progress(f"Using static case study PDF: {pdf.name}")
        return str(pdf), "static_pdf"
    progress("WARNING: no deck and no static PDF; drafting without attachment.")
    return None, "none"


if __name__ == "__main__":
    import sys

    if any(a in ("auth", "--auth") for a in sys.argv[1:]):
        # One-time interactive OAuth (run LOCALLY with a browser). Writes
        # token.json next to credentials.json, then exits. SCP both to the VPS.
        get_service(interactive=True)
        print(f"Gmail authorized. Token cached to {config.GMAIL_TOKEN_PATH}")
    else:
        print("usage: python -m src.drafting.gmail_drafter --auth")

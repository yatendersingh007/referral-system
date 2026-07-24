"""Gmail draft creation (§4 Phase 5, HARD RULE 1). Scope is gmail.compose
ONLY. This module contains exactly one call into the Gmail API surface —
`service.users().drafts().create(...)` — and no other. There is no function
in this file, in this package, or anywhere in Workflow C that can reach
`messages().send(...)`; grep the source tree for `.send(` against a Gmail
service object to confirm.

TEST_MODE routes to testmode.FakeGmailService, which doesn't even implement
a `.send()` method — so there is no code path, mocked or real, that could
send an email from this system. Drafts only. Ever.
"""
from __future__ import annotations

import base64
import os
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Callable, Optional

from . import config, testmode

Progress = Callable[[str], None]


def _noop(_: str) -> None:
    pass


_service_singleton = None


def get_service():
    """Returns a Gmail API service object scoped to gmail.compose only, or
    (in TEST_MODE) a FakeGmailService that records calls and never touches
    the network. Cached for the process lifetime."""
    global _service_singleton
    if _service_singleton is not None:
        return _service_singleton

    if config.TEST_MODE:
        _service_singleton = testmode.FakeGmailService()
        return _service_singleton

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
        else:
            if not os.path.exists(config.GMAIL_CREDENTIALS_PATH):
                raise RuntimeError(f"Gmail credentials not found at {config.GMAIL_CREDENTIALS_PATH}.")
            flow = InstalledAppFlow.from_client_secrets_file(
                config.GMAIL_CREDENTIALS_PATH, config.GMAIL_SCOPES
            )
            creds = flow.run_local_server(port=0)
        with open(token_path, "w", encoding="utf-8") as f:
            f.write(creds.to_json())

    _service_singleton = build("gmail", "v1", credentials=creds)
    return _service_singleton


_SUBTYPES = {".pdf": "pdf", ".pptx": "vnd.openxmlformats-officedocument.presentationml.presentation"}


def _build_raw(to: str, subject: str, body: str, attach_path: Optional[Path]) -> str:
    msg = MIMEMultipart()
    msg["To"] = to
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))
    if attach_path and Path(attach_path).exists():
        ext = Path(attach_path).suffix.lower()
        subtype = _SUBTYPES.get(ext, "octet-stream")
        with open(attach_path, "rb") as f:
            part = MIMEApplication(f.read(), _subtype=subtype)
        part.add_header("Content-Disposition", "attachment", filename=Path(attach_path).name)
        msg.attach(part)
    return base64.urlsafe_b64encode(msg.as_bytes()).decode()


def create_draft(to: str, subject: str, body: str, attach_path: Optional[Path] = None,
                 progress: Progress = _noop) -> dict:
    """The ONLY Gmail write in this system: drafts().create(). Returns
    {"draft_id", "attached"}."""
    service = get_service()
    raw = _build_raw(to, subject, body, attach_path)
    created = service.users().drafts().create(userId="me", body={"message": {"raw": raw}}).execute()
    draft_id = created.get("id")
    progress(f"Gmail draft created: {draft_id}" + (f" (attached {Path(attach_path).name})" if attach_path else ""))
    return {"draft_id": draft_id, "attached": bool(attach_path)}

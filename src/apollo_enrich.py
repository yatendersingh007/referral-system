"""Phase 3b — Apollo email discovery (§4 Phase 3).

HARD RULE 13: if the connections export already has an email for this POC,
use it and skip Apollo entirely. Otherwise, ONE sequential `people/match`
call (never `bulk_match` — proven 403 on the current plan; see
lib_snapshot/enrichment/apollo.py), keyed primarily on the LinkedIn URL, with
name + company + resolved domain riding along in the same payload as
fallback context (the domain hint is the single biggest lever on hit rate).
Transient errors (429/5xx/timeout) retry inside `_post_match`; a 403 plan
error re-raises to the caller, which parks the POC-level outcome rather than
guessing (fail cheap — HARD RULE 5).
"""
from __future__ import annotations

from typing import Callable, Optional

import requests

from . import config, testmode
from lib_snapshot.enrichment.apollo import ApolloUnavailable, _post_match, _valid_email

Progress = Callable[[str], None]


def _noop(_: str) -> None:
    pass


def _split_name(poc_name: str) -> tuple[str, str]:
    parts = (poc_name or "").strip().split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def find_email(poc_name: str, company_name: str, linkedin_url: Optional[str] = None,
               domain: Optional[str] = None, seed_email: Optional[str] = None,
               progress: Progress = _noop) -> tuple[Optional[str], Optional[str]]:
    """Returns (email, confidence) or (None, None). HARD RULE 13: seed_email
    (from the connections export) short-circuits everything below it."""
    if seed_email:
        return seed_email, "from_export"

    if config.TEST_MODE:
        hit = testmode.apollo_email_fixture(poc_name, company_name)
        return hit if hit else (None, None)

    if not config.APOLLO_API_KEY:
        raise ApolloUnavailable(
            "APOLLO_API_KEY missing. POC created without an email; manual "
            "lookup required to draft this firm."
        )

    first, last = _split_name(poc_name)
    detail: dict = {}
    if linkedin_url:
        detail["linkedin_url"] = linkedin_url  # primary match key
    if first:
        detail["first_name"] = first
    if last:
        detail["last_name"] = last
    if company_name:
        detail["organization_name"] = company_name
    if domain:
        detail["domain"] = domain  # biggest lever on hit rate (proven, Workflow A)

    try:
        person = _post_match(detail)
    except requests.RequestException as e:
        progress(f"Apollo lookup failed for {poc_name}: {e}")
        return None, None

    if isinstance(person, dict):
        email = person.get("email")
        if _valid_email(email):
            confidence = person.get("email_status") or person.get("confidence") or "apollo"
            return email, str(confidence)
    return None, None

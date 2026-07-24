"""Apollo email enrichment (POST /api/v1/people/match — SINGLE, sequential).

We use the single-person `people/match` endpoint, ONE request per lead, NOT
`people/bulk_match`: bulk_match returns 403 on our Apollo plan (proven). Each
request carries the person's name + organization AND the company DOMAIN hint
whenever the lead has one (the domain is what materially lifts Apollo's match
hit rate). Matched emails + confidence are written back to leads.

If APOLLO_API_KEY is missing, raise ApolloUnavailable so the caller can fall
back to a manual email hunt — the contacts still exist, they just need emails.
"""
from __future__ import annotations

import sys
import time
from typing import Callable, Optional

import requests

from .. import config, db

MATCH_URL = "https://api.apollo.io/api/v1/people/match"
HEALTH_URL = "https://api.apollo.io/v1/auth/health"   # cheapest key-verifying call
_TIMEOUT = 40
_MAX_RETRIES = 2          # transient errors only (429 / 5xx / timeout)
_SLEEP_BETWEEN = 0.5      # gentle on the rate limit between sequential lookups

Progress = Callable[[str], None]


def _noop(_: str) -> None:
    pass


class ApolloUnavailable(RuntimeError):
    pass


def healthcheck(timeout: int = 10) -> bool:
    """Cheapest auth-verifying call: confirm the key is accepted. Returns True or
    raises (ApolloUnavailable if no key, RequestException/HTTPError otherwise)."""
    if not config.APOLLO_API_KEY:
        raise ApolloUnavailable("APOLLO_API_KEY missing")
    r = requests.get(HEALTH_URL,
                     headers={"X-Api-Key": config.APOLLO_API_KEY, "Cache-Control": "no-cache"},
                     timeout=timeout)
    r.raise_for_status()
    return True


def _detail_for(lead: dict) -> dict:
    """Build the Apollo people/match payload for one lead.

    ALWAYS includes the company `domain` hint when the lead carries one (joined
    from companies.domain by competitor_engine._load_leads) — that hint is the
    single biggest lever on Apollo's match hit rate.
    """
    d: dict = {}
    if lead.get("first_name"):
        d["first_name"] = lead["first_name"]
    if lead.get("last_name"):
        d["last_name"] = lead["last_name"]
    if not d and lead.get("full_name"):
        d["name"] = lead["full_name"]
    if lead.get("company"):
        d["organization_name"] = lead["company"]
    if lead.get("domain"):
        d["domain"] = lead["domain"]
    return d


def _debug_log(lead: dict, detail: dict) -> None:
    """Debug line (server logs, NOT Telegram): who we looked up, at which
    company, and whether the domain hint was included in the payload."""
    print(
        "[apollo] people/match lookup: "
        f"name={lead.get('full_name') or detail.get('name') or '?'!r} "
        f"company={lead.get('company') or '?'!r} "
        f"domain_included={'yes' if detail.get('domain') else 'no'}"
        + (f" domain={detail['domain']!r}" if detail.get("domain") else ""),
        file=sys.stderr, flush=True,
    )


def _post_match(detail: dict) -> Optional[dict]:
    """One people/match call. Returns the matched person dict, or None on a
    clean no-match. Retries transient failures (429 / 5xx / timeout); re-raises
    other RequestExceptions (e.g. a 403 plan error) to the caller."""
    headers = {
        "Content-Type": "application/json",
        "Cache-Control": "no-cache",
        "X-Api-Key": config.APOLLO_API_KEY,
    }
    payload = {**detail, "reveal_personal_emails": True}
    last_exc: Optional[Exception] = None
    for attempt in range(_MAX_RETRIES + 1):
        try:
            r = requests.post(MATCH_URL, json=payload, headers=headers, timeout=_TIMEOUT)
            if r.status_code in (429, 500, 502, 503, 504):
                last_exc = requests.HTTPError(f"HTTP {r.status_code} (transient)")
                time.sleep(1.5 * (attempt + 1))
                continue
            r.raise_for_status()
            data = r.json()
            return data.get("person") or None
        except (requests.Timeout, requests.ConnectionError) as e:
            last_exc = e
            time.sleep(1.5 * (attempt + 1))
    if last_exc:
        raise last_exc
    return None


def _valid_email(email: Optional[str]) -> bool:
    if not email:
        return False
    e = email.lower()
    if "email_not_unlocked" in e or "not_unlocked" in e:
        return False
    return "@" in e


def enrich_leads(leads: list[dict], progress: Progress = _noop) -> dict:
    """Enrich lead rows (id / first_name / last_name / company / domain) via
    Apollo people/match, ONE request per lead (sequential). Writes email +
    email_confidence back via db. Returns {attempted, enriched}. Raises
    ApolloUnavailable if the API key is missing (caller decides the fallback)."""
    targets = [l for l in leads if not l.get("email")]
    if not targets:
        return {"attempted": 0, "enriched": 0}

    if not config.APOLLO_API_KEY:
        raise ApolloUnavailable(
            "APOLLO_API_KEY missing. Contacts were created without emails. "
            "Manual fallback: export the contacts, look up emails, and load them "
            "back into the leads table (leads.email), then run the drafter."
        )

    enriched = 0
    for lead in targets:
        detail = _detail_for(lead)
        _debug_log(lead, detail)
        try:
            person = _post_match(detail)
        except requests.RequestException as e:
            progress(f"Apollo lookup failed for {lead.get('full_name', '?')}: {e}")
            continue
        if isinstance(person, dict):
            email = person.get("email")
            if _valid_email(email):
                confidence = (person.get("email_status")
                              or person.get("confidence") or "apollo")
                db.set_lead_email(lead["id"], email, str(confidence))
                enriched += 1
        time.sleep(_SLEEP_BETWEEN)

    progress(f"Apollo: {enriched}/{len(targets)} emails found.")
    return {"attempted": len(targets), "enriched": enriched}

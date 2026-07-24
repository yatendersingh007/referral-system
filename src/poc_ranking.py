"""Phase 3a — POC selection (§4 Phase 3). Ranks a firm's matched connections
by seniority and picks the top 1 to draft to; the rest are stored as
alternate_pocs for future use.

QA finding (real-mode Atlas Real Estate run): the original approach rejected
anyone whose designation didn't match a small, exact-phrase whitelight
(`lib_snapshot.config.MGMT_TITLE_HINTS` — finance/operations titles only).
A perfectly matched firm dead-ended because its only two contacts, "VP of
Capital Markets" and "Investor Relations Specialist", both fell outside that
narrow whitelist. Eligibility is now decided by SENIORITY signal, not job
function: any senior title in ANY department is accepted; only a small set
of clearly-junior individual-contributor titles is rejected. A title that
matches neither list is ambiguous -- surfaced (accepted, flagged, ranked
last) rather than silently dropped, since a matched firm with a borderline
contact is worth a human's ten seconds more than a dead end.
"""
from __future__ import annotations

import re
from typing import Optional

# Reject only clearly-junior individual-contributor titles. "assistant" is
# excluded from this when it's actually "Assistant Vice President"/"Assistant
# VP" -- a real VP-level title, not a junior one -- via the negative
# lookahead below.
_JUNIOR_RE = re.compile(
    r'\b(analyst|associate|specialist|coordinator|'
    r'assistant(?!\s+(?:vice\s*president|vp)\b)|'
    r'intern|trainee|clerk|representative)\b',
    re.IGNORECASE,
)

# Most-senior-first. A title's rank is the index of the first tier it
# matches; ties within a tier keep input order (stable sort). Deliberately
# patterns, not exact phrases -- "VP of Capital Markets", "Director of Asset
# Management", "Head of Acquisitions" all match on department-agnostic
# seniority language, not a specific business function.
_SENIOR_TIERS: list[re.Pattern] = [
    re.compile(r'\b(chief\s+\w+\s+officer|ceo|cfo|coo|cio|cto|cmo|chro)\b', re.IGNORECASE),
    re.compile(r'\b(founder|owner|president)\b', re.IGNORECASE),
    re.compile(r'\bmanaging\s+(director|partner)\b', re.IGNORECASE),
    re.compile(r'\b(partner|principal)\b', re.IGNORECASE),
    re.compile(r'\b(director|head\s+of)\b', re.IGNORECASE),
    re.compile(r'\b(svp|evp|avp|vp)\b|\bvice\s*president\b', re.IGNORECASE),
]

# Ambiguous titles (match neither list) are accepted but rank below every
# confirmed-senior tier -- never preferred over a confirmed match, but never
# silently dropped either.
AMBIGUOUS_RANK = len(_SENIOR_TIERS)


def _classify(designation: str) -> Optional[int]:
    """Returns a rank (lower = more senior) if the title is eligible, or
    None if it's a clear junior/individual-contributor title (reject).
    AMBIGUOUS_RANK means neither list matched -- eligible, but flagged."""
    title = designation or ""
    if _JUNIOR_RE.search(title):
        return None
    for i, tier in enumerate(_SENIOR_TIERS):
        if tier.search(title):
            return i
    return AMBIGUOUS_RANK


def rank_pocs(connections: list[dict]) -> tuple[Optional[dict], list[dict]]:
    """Returns (top_poc, alternate_pocs) — both are connection rows (dicts),
    sorted most-senior-first. (None, []) if nobody on the list is eligible.
    A connection whose title matched neither the junior denylist nor a
    senior tier is included with an added "eligibility_flag": "ambiguous"
    key (absent on confidently-classified rows) so a caller can surface it
    for human review."""
    scored = []
    for c in connections:
        rank = _classify(c.get("designation"))
        if rank is None:
            continue
        if rank == AMBIGUOUS_RANK:
            c = {**c, "eligibility_flag": "ambiguous"}
        scored.append((rank, c))
    scored.sort(key=lambda pair: pair[0])
    if not scored:
        return None, []
    ranked = [c for _, c in scored]
    return ranked[0], ranked[1:]

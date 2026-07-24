"""Numeric fact-grounding check shared by email_drafter.py (Block 2/4) and
deck_filler.py (profile bullets / fit paragraphs).

QA finding (real-mode Kingbird run): Phase 2's research dict is the single
source of truth for a firm's numbers, but Phase 4 (personalization.py) and
Phase 5b (deck_filler.get_deck_content) each make their OWN separate LLM
call, given the research dict only as context -- nothing mechanically stops
that second call from restating a number differently (rounding drift,
misreading a units digit) or inventing one outright. Every dollar figure or
"N <unit>" count a generation step writes must be traceable to a number
that already appears somewhere in the research/personalization data it was
generated from; if it isn't, the text is unverifiable and must be dropped
or regenerated, never partially hand-edited (same "drop the whole item,
never patch it" philosophy as HARD RULE 14's money-leak guard in
deck_filler.py -- a fixed-up sentence reads as obviously mutilated).

Deliberately narrow: this only recognizes dollar figures and explicit
counts next to a small set of unit words (units, states, properties,
entities, funds, deals, sq ft). It cannot verify a qualitative claim, only
that a NUMBER cited in generated text is at least traceable to the source
data.

QA finding (this module's own verification run): referral_email.md's Block
4 "Good example" text contains a specific count ("40+ entities") as a STYLE
sample. A real LLM call echoed that exact number into a live Kingbird draft
where it did not belong -- "entities" was not yet in the recognized
unit-word list below, so the check missed it. Few-shot examples with
concrete numbers are a known way for a model to copy a number instead of
inventing a fact from the actual research object; expand the unit-word list
whenever a new one of these slips through rather than assuming this list is
complete.
"""
from __future__ import annotations

import json
import re

_DOLLAR_RE = re.compile(
    r'\$\s?([\d][\d,]*(?:\.\d+)?)\s*(billion|million|bn|mm|b|m)?\b',
    re.IGNORECASE,
)
_COUNT_RE = re.compile(
    r'\b([\d][\d,]*)\s*\+?\s*(?:\w+\s+){0,2}?'
    r'(?:units?|states?|propert(?:y|ies)|entit(?:y|ies)|funds?|deals?|'
    r'sq\.?\s*ft\.?|square\s*feet|square\s*footage)\b',
    re.IGNORECASE,
)


def _scale_suffix(raw: str | None) -> str:
    s = (raw or "").lower()
    if s in ("billion", "bn", "b"):
        return "B"
    if s in ("million", "mm", "m"):
        return "M"
    return ""


def extract_numeric_claims(text: str) -> set[str]:
    """Every dollar figure or explicit count-with-unit in `text`, normalized
    to a bare digit string (commas stripped) plus a B/M scale suffix where
    a dollar figure carries one."""
    claims: set[str] = set()
    for number, scale in _DOLLAR_RE.findall(text or ""):
        claims.add(number.replace(",", "") + _scale_suffix(scale))
    for number in _COUNT_RE.findall(text or ""):
        claims.add(number.replace(",", ""))
    return claims


def grounding_corpus(*sources: dict) -> str:
    """Flattens one or more research/personalization dicts into one text
    blob to check numeric claims against. Every field goes in, not just
    ones with an obviously numeric name -- a real fact (a unit count, a
    state count) often only appears inside a free-form hook/oneliner
    string, not a dedicated structured field."""
    return " ".join(json.dumps(s, default=str) for s in sources if s)


def unverifiable_claims(text: str, corpus_text: str) -> set[str]:
    """Numeric claims in `text` that do NOT appear anywhere in
    `corpus_text` -- unverifiable against the source data (extraction
    drift, rounding, or an outright hallucination) and must not ship."""
    claims = extract_numeric_claims(text)
    if not claims:
        return set()
    return claims - extract_numeric_claims(corpus_text)

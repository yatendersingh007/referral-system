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
counts next to a set of unit words (see _UNIT_WORD_ALTERNATION below). It
cannot verify a qualitative claim, only that a NUMBER cited in generated
text is at least traceable to the source data.

QA finding (this module's own verification run): referral_email.md's Block
4 "Good example" text contains a specific count ("40+ entities") as a STYLE
sample. A real LLM call echoed that exact number into a live Kingbird draft
where it did not belong -- "entities" was not yet in the recognized
unit-word list below, so the check missed it. Few-shot examples with
concrete numbers are a known way for a model to copy a number instead of
inventing a fact from the actual research object; expand the unit-word list
whenever a new one of these slips through rather than assuming this list is
complete.

FIX 35 (real-mode American Real Estate Partners run): FIX 9 (drift), FIX 17
(calendar years), and FIX 33 (dollar-adjacent counts) were three symptoms of
the SAME defect class -- a digit run's meaning was decided by which WORD
happened to sit near it, not by anything about the number itself. Retired
that whole class with a single-pass TYPED extractor: every numeric span is
classified into exactly one type (CURRENCY / RANGE / YEAR / PERCENTAGE /
COUNT) by its OWN attachments -- a leading "$", a range partner, a bare
1900-2099 shape, a trailing "%" -- checked BEFORE any unit-word proximity
test, so a dollar figure can never again double as a count of whatever unit
word happens to follow it. Also closed two confirmed blind spots: bare "SF"
(no period, no "sq"/"square") and numeric RANGES ("12-15 projects"), neither
of which the old _COUNT_RE could see at all -- verified against the real
American Real Estate Partners Block 2, which carried five numbers and used
to yield only {'309M'} (dropping the fund-size range and the acquisition's
square footage entirely).

Grounding is now ASYMMETRIC (unverifiable_claims), not a symmetric set
difference: a CURRENCY claim is grounded only by a matching dollar figure in
the corpus (same digits, same scale meaning); a COUNT claim (including range
endpoints) is grounded by its digits appearing ANYWHERE in the corpus as a
standalone number token, whether or not the corpus happens to phrase it next
to the same unit word. This retires FIX 33's own residual dependency on
corpus PHRASING -- its fix (span ownership) stopped the block's own text
from misfiring, but grounding still required the corpus to independently
re-derive the identical typed claim, which a JSON field like
'"unit_count": "21,794"' (a number with no adjacent unit word at all) could
never do. Content, not phrasing.
"""
from __future__ import annotations

import json
import re

# ---------------------------------------------------------------------------
# Shared vocabulary and low-level helpers
# ---------------------------------------------------------------------------
_SCALE_WORDS = r"billion|million|bn|mm|b|m"

# FIX 35: expanded from the original units?|states?|propert(?:y|ies)|
# entit(?:y|ies)|funds?|deals?|sq\.?\s*ft\.?|square\s*feet|square\s*footage
# with bare "sf" (no period, no "sq") and "s.f." (confirmed blind spot --
# the American run's own "1,015,740 SF" acquisition was invisible before
# this), plus projects/buildings/markets/acquisitions/assets/investments/
# loans/transactions/tenants/leases/offices/floors/beds/keys/doors (the
# same "confirmed blind spot in the deployed regex" the American run's
# "12-15 ... projects" exposed). Per the module docstring's own standing
# instruction: expand this list whenever a new one slips through, don't
# assume it's complete.
_UNIT_WORD_ALTERNATION = (
    r"units?|states?|propert(?:y|ies)|entit(?:y|ies)|funds?|deals?|"
    r"sq\.?\s*ft\.?|square\s*feet|square\s*footage|sf|s\.?f\.?|"
    r"projects?|buildings?|markets?|acquisitions?|assets?|investments?|"
    r"loans?|transactions?|tenants?|leases?|offices?|floors?|beds?|keys?|doors?"
)

# The exact pre-FIX-35 dollar pattern, plus an optional "US" per FIX 35's
# CURRENCY type spec -- reused both inside the single-pass claim extractor
# (below) and standalone for asymmetric currency grounding against a
# corpus (_currency_tokens).
_DOLLAR_RE = re.compile(
    r"\$\s?(?:US\s?)?([\d][\d,]*(?:\.\d+)?)\s*(" + _SCALE_WORDS + r")?\b",
    re.IGNORECASE,
)


def _scale_suffix(raw: str | None) -> str:
    s = (raw or "").lower()
    if s in ("billion", "bn", "b"):
        return "B"
    if s in ("million", "mm", "m"):
        return "M"
    return ""


# FIX 17 (real-mode Kairos Real Estate Partners run): a plain count regex
# requires no particular RELATIONSHIP between a number and a nearby unit
# word beyond proximity -- it has no concept of a calendar YEAR at all.
# "...deployed roughly $254M since 2013 across commercial properties" trips
# it: "2013" sits within a couple words of "properties" purely incidentally
# (from an unrelated "since <year>" clause), so it gets misread as a claimed
# COUNT of properties, not recognized as the year it actually is. Confirmed
# via direct printed evidence (not assumed): founded_year IS present in
# grounding_corpus()'s JSON dump as a raw substring, but the OLD extractor
# never recognized it as a "claim" at all (nothing in the JSON followed it
# with a recognized unit word) -- so the mismatch wasn't a missing fact, it
# was proximity-based misclassification of an incidental year mention as a
# fabricated count. A plain (no comma/decimal) 4-digit number in the
# 1900-2099 range is a far more likely calendar year than a genuine unit/
# state/property/etc. count in this domain (a real count that large is
# conventionally written with a comma, e.g. "2,013 units") -- excluded from
# count-claim extraction entirely (FIX 35: now checked as part of TYPING a
# candidate span, before it can ever become a COUNT claim), so it's never
# flagged as either a claim to prove OR a claim to disprove; it just isn't a
# "count claim" in the first place. Kept verbatim by FIX 35 -- this
# function and its reasoning are unchanged.
def _looks_like_bare_calendar_year(raw_number: str) -> bool:
    if "," in raw_number or "." in raw_number:
        return False
    if len(raw_number) != 4 or not raw_number.isdigit():
        return False
    return 1900 <= int(raw_number) <= 2099


_URL_RE = re.compile(r"https?://\S*")


def _is_inside_url(text: str, start: int, end: int) -> bool:
    """True if the span [start, end) falls entirely inside a URL substring
    of `text` -- corpus JSON routinely carries source URLs whose paths and
    query strings contain digits (a listing ID, a date segment) that must
    never be mistaken for a grounded fact (FIX 35's anti-weakening guard)."""
    return any(start >= m.start() and end <= m.end() for m in _URL_RE.finditer(text))


# ---------------------------------------------------------------------------
# FIX 35 -- single-pass TYPED claim extraction, replacing the old
# _DOLLAR_RE/_COUNT_RE dual scan (FIX 33's span-ownership suppression is
# superseded entirely: typed classification makes it structurally
# impossible for a dollar figure's own digits to ALSO become a count, no
# suppression step needed after the fact).
#
# One combined regex, alternation ordered by classification priority --
# a number is classified by its OWN attachments (a leading "$", a range
# partner, a bare year shape, a trailing "%"), never by unit-word
# proximity, which is checked LAST and only for what's left over:
#   1. CURRENCY  -- "$" (+ optional "US") prefix, optional B/M-family scale.
#   2. RANGE     -- two numbers joined by "-"/en dash/"to", sharing ONE
#                   following unit word (a wider 0-3 filler-word window
#                   than plain COUNT below -- real prose routinely inserts
#                   a short descriptive phrase between a range and its
#                   unit, e.g. "12-15 PowerHouse data center projects").
#                   Each endpoint becomes its own COUNT-typed claim.
#   3. PERCENTAGE -- number followed by "%" or "percent". Typed and
#                    DISCARDED (emits no claim) -- grounding a percentage
#                    is out of this module's deliberately narrow scope, but
#                    typing it here stops it from ever being misread as an
#                    untyped bare count by whatever accidentally follows it.
#   4. COUNT     -- number followed, within the ORIGINAL 2-filler-word
#                   window (unchanged from pre-FIX-35), by a unit word.
#   5. (implicit) UNTYPED -- a number matching none of the above emits no
#      claim, same "deliberately narrow" philosophy as before.
# A bare calendar year (see _looks_like_bare_calendar_year) is checked
# before COUNT/RANGE claims are emitted, exactly as FIX 17 established.
# ---------------------------------------------------------------------------
_CURRENCY_FRAG = (
    r"\$\s?(?:US\s?)?(?P<cur_num>[\d][\d,]*(?:\.\d+)?)\s*(?P<cur_scale>" + _SCALE_WORDS + r")?\b"
)
_RANGE_FRAG = (
    r"\b(?P<range_a>[\d][\d,]*)\s*(?:-|–|to)\s*(?P<range_b>[\d][\d,]*)"
    r"\s*\+?\s*(?:\w+\s+){0,3}?(?:" + _UNIT_WORD_ALTERNATION + r")\b"
)
_PERCENT_FRAG = r"\b(?P<pct_num>[\d][\d,]*(?:\.\d+)?)\s*(?:%|percent)\b"
_COUNT_FRAG = (
    r"\b(?P<count_num>[\d][\d,]*)\s*\+?\s*(?:\w+\s+){0,2}?(?:" + _UNIT_WORD_ALTERNATION + r")\b"
)

_CLAIM_RE = re.compile(
    "(?:" + _CURRENCY_FRAG + ")|(?:" + _RANGE_FRAG + ")|(?:" + _PERCENT_FRAG + ")|(?:" + _COUNT_FRAG + ")",
    re.IGNORECASE,
)


def _extract_typed_claims(text: str) -> list[tuple[str, str]]:
    """Internal: every numeric claim in `text` as (claim_string, kind) pairs,
    kind one of "currency"/"count". extract_numeric_claims() (the public
    API) derives its flat set[str] from this; unverifiable_claims() uses
    the kind tags for FIX 35's asymmetric grounding -- a currency claim and
    a count claim are grounded against the corpus in different ways (see
    below), and the public set[str] alone can't carry that distinction."""
    text = text or ""
    claims: list[tuple[str, str]] = []
    for m in _CLAIM_RE.finditer(text):
        if m.group("cur_num") is not None:
            claims.append((m.group("cur_num").replace(",", "") + _scale_suffix(m.group("cur_scale")),
                          "currency"))
        elif m.group("range_a") is not None:
            for raw in (m.group("range_a"), m.group("range_b")):
                if not _looks_like_bare_calendar_year(raw):
                    claims.append((raw.replace(",", ""), "count"))
        elif m.group("pct_num") is not None:
            pass  # typed PERCENTAGE, deliberately no claim -- see class docstring above
        elif m.group("count_num") is not None:
            raw = m.group("count_num")
            if not _looks_like_bare_calendar_year(raw):
                claims.append((raw.replace(",", ""), "count"))
    return claims


def extract_numeric_claims(text: str) -> set[str]:
    """Every dollar figure or explicit count-with-unit in `text`, normalized
    to a bare digit string (commas stripped) plus a B/M scale suffix where
    a dollar figure carries one. A bare calendar year adjacent to a unit
    word (see _looks_like_bare_calendar_year) is excluded -- it's a date,
    not a count claim. A numeric range ("12-15 projects") emits BOTH
    endpoints as separate count claims. A percentage is typed but never
    emits a claim (see _extract_typed_claims). Public signature unchanged
    by FIX 35; see the module docstring for what changed underneath it."""
    return {claim for claim, _kind in _extract_typed_claims(text)}


def grounding_corpus(*sources: dict) -> str:
    """Flattens one or more research/personalization dicts into one text
    blob to check numeric claims against. Every field goes in, not just
    ones with an obviously numeric name -- a real fact (a unit count, a
    state count) often only appears inside a free-form hook/oneliner
    string, not a dedicated structured field.

    FIX 37 (real-mode American Real Estate Partners run): `ensure_ascii=
    False` is REQUIRED, not cosmetic. json.dumps()'s default (ensure_ascii=
    True) escapes any non-ASCII character -- e.g. the en dash in a hook
    like "a 12–15 project pipeline" -- into a literal six-character
    "\\u2013" sequence. Diagnosed live, not assumed: the hypothesis going in
    was an en-dash-vs-hyphen asymmetry in claim/corpus extraction, but
    isolated testing showed hyphen and en dash extract and ground
    IDENTICALLY on both sides -- there was no such asymmetry. The actual
    cause: that escape sequence's own trailing hex digits ("2013") sit with
    ZERO separator before the real, immediately-following "15" ("12\\u2013
    15" -> "...2013" + "15" with nothing between them), so
    _ANY_NUMBER_TOKEN_RE's greedy digit-run match swallows both into one
    bogus token ("201315") -- "15" then never appears as a standalone
    number in the corpus at all, and a range endpoint that should ground
    cleanly reports as unverifiable. Confirmed directly: dumping the exact
    live hook text with ensure_ascii=True yields token '201315'; the same
    dump with ensure_ascii=False yields the correct, separate '12'/'15'.
    Fixed at the source (this is the ONLY place corpus text is built from
    JSON) rather than patching around it in the tokenizer -- any other
    non-ASCII character adjacent to a digit run would corrupt tokenization
    the same way, not just an en dash."""
    return " ".join(json.dumps(s, default=str, ensure_ascii=False) for s in sources if s)


def _currency_tokens(corpus_text: str) -> set[str]:
    """Every dollar figure in `corpus_text`, normalized exactly like a
    CURRENCY claim (digits + B/M suffix), excluding any match that falls
    inside a URL. Used for asymmetric grounding: a CURRENCY claim is
    grounded ONLY by an actual matching dollar figure here -- never by a
    bare, dollar-less number of the same digits (FIX 35's explicit
    requirement: '$309M' is not grounded by a corpus '309' with no '$')."""
    corpus_text = corpus_text or ""
    tokens: set[str] = set()
    for m in _DOLLAR_RE.finditer(corpus_text):
        if _is_inside_url(corpus_text, m.start(), m.end()):
            continue
        tokens.add(m.group(1).replace(",", "") + _scale_suffix(m.group(2)))
    return tokens


_ANY_NUMBER_TOKEN_RE = re.compile(r"[\d][\d,]*(?:\.\d+)?")
_PRECEDED_BY_DOLLAR_RE = re.compile(r"\$\s?(?:US\s?)?$", re.IGNORECASE)


def _standalone_number_tokens(corpus_text: str) -> set[str]:
    """Every maximal digit run in `corpus_text` (comma-grouping collapsed),
    EXCLUDING one that falls inside a URL, and EXCLUDING one immediately
    preceded by '$' (that's a dollar figure's own digits, not an
    independent count -- see the module docstring: a dollar figure must
    never ground an unrelated count claim of the same digits).

    FIX 35's core Part B rationale: this is deliberately NOT "re-run the
    same typed count extraction on the corpus" -- the corpus is JSON, and a
    genuine count there routinely has NO adjacent unit word at all (e.g.
    '"unit_count": "21,794"', the key naming it BEFORE the number, in a
    vocabulary no unit-word list could ever anticipate). Requiring the
    corpus to independently re-derive the identical TYPED claim makes
    grounding depend on corpus PHRASING -- FIX 33's own paren-dependence
    bug, generalized. Grounding must depend on corpus CONTENT: does this
    digit string appear anywhere as a standalone number, full stop."""
    corpus_text = corpus_text or ""
    tokens: set[str] = set()
    for m in _ANY_NUMBER_TOKEN_RE.finditer(corpus_text):
        if _is_inside_url(corpus_text, m.start(), m.end()):
            continue
        prefix = corpus_text[max(0, m.start() - 4):m.start()]
        if _PRECEDED_BY_DOLLAR_RE.search(prefix):
            continue
        tokens.add(m.group(0).replace(",", ""))
    return tokens


def unverifiable_claims(text: str, corpus_text: str) -> set[str]:
    """Numeric claims in `text` that do NOT appear anywhere in
    `corpus_text` -- unverifiable against the source data (extraction
    drift, rounding, or an outright hallucination) and must not ship.

    FIX 35: grounding is now ASYMMETRIC, not a symmetric set difference
    (extract_numeric_claims(text) - extract_numeric_claims(corpus)). A
    CURRENCY claim is grounded only by a matching dollar figure in the
    corpus (_currency_tokens); a COUNT claim (including range endpoints) is
    grounded by its digits appearing anywhere in the corpus as a standalone
    number token (_standalone_number_tokens), independent of whether the
    corpus happens to phrase it next to the same unit word. Public
    signature unchanged; see the module docstring for the full rationale."""
    typed_claims = _extract_typed_claims(text)
    if not typed_claims:
        return set()
    corpus_currency = _currency_tokens(corpus_text)
    corpus_counts = _standalone_number_tokens(corpus_text)
    missing: set[str] = set()
    for claim, kind in typed_claims:
        grounded = claim in corpus_currency if kind == "currency" else claim in corpus_counts
        if not grounded:
            missing.add(claim)
    return missing

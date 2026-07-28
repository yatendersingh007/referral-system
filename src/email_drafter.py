"""Phase 5a — email drafting from templates/referral_email.md (§4 Phase 5,
HARD RULE 8, §10/§11 anti-slop rules). The template is an LLM GENERATION
PROMPT, not a slot-fill template: fixed blocks (1/3/5/6/7) are reproduced
VERBATIM; Block 2 (firm hook) and Block 4 (pain point) are LLM-generated from
the Phase 2/4 research object, subject to the anti-AI-writing rules and a
230-word total length guard (referral_email.md's own generation-rules
section states 230; §4's prose elsewhere says "~220" as an approximate
figure — the template file governs the actual LLM call, so its explicit 230
is authoritative here).

This module never calls Gmail directly — see gmail_client.py for draft
creation. It only produces {subject, to, body}.
"""
from __future__ import annotations

import re
from typing import Callable, Optional

from . import config, fact_check, testmode

Progress = Callable[[str], None]


def _noop(_: str) -> None:
    pass


FIXED_SUBJECT = "Intro via Manpreet Singh (Singh Capital)"

_THIN_FALLBACK_BLOCK_4 = (
    "Most mid-market firms are still running this one of two ways: overpaying "
    "a provider whose partners never open their ledger, or burning their "
    "controller's nights on work a dedicated property accountant should own. "
    "Not because either works well, because switching feels harder than "
    "overpaying. That inertia has a price, and you're paying it every close."
)

# ---------------------------------------------------------------------------
# Fixed-block extraction (parsed from the template file, never hand-copied,
# so a template edit is picked up on the next service restart per §11.5
# without a second source of truth living in Python).
# ---------------------------------------------------------------------------
_BLOCK_HEADER_RE = re.compile(
    r"^### BLOCK (\d) - .*?\(FIXED, verbatim\)\s*$", re.MULTILINE
)


def _parse_fixed_blocks(template_text: str) -> dict[int, str]:
    headers = list(_BLOCK_HEADER_RE.finditer(template_text))
    blocks: dict[int, str] = {}
    for i, m in enumerate(headers):
        start = m.end()
        end = headers[i + 1].start() if i + 1 < len(headers) else len(template_text)
        num = int(m.group(1))
        body = template_text[start:end].strip("\n")
        # Trim a trailing "### BLOCK N - ..." non-fixed section header if the
        # next block boundary isn't itself a FIXED header (Blocks 2/4 sit
        # between fixed ones and aren't matched by _BLOCK_HEADER_RE at all).
        body = re.split(r"\n### BLOCK \d", body)[0]
        # Block 7 is the last fixed block; the file continues after it with a
        # "---" divider into the (non-email) "Generation rules" meta-section.
        body = re.split(r"\n---\s*\n", body)[0]
        blocks[num] = body.strip()
    return blocks


_fixed_blocks_cache: Optional[dict[int, str]] = None


def _fixed_blocks() -> dict[int, str]:
    global _fixed_blocks_cache
    if _fixed_blocks_cache is None:
        text = config.REFERRAL_EMAIL_TEMPLATE.read_text(encoding="utf-8")
        _fixed_blocks_cache = _parse_fixed_blocks(text)
    return _fixed_blocks_cache


def reload_template() -> None:
    """Force a re-parse on the next call (mirrors sync_templates_from_folder's
    restart-to-reload convention; exposed for tests)."""
    global _fixed_blocks_cache
    _fixed_blocks_cache = None


# ---------------------------------------------------------------------------
# Anti-AI-writing guard (mechanically checkable rules only — sentence-length
# variety and opener repetition are prompt-guided, not gated here)
# ---------------------------------------------------------------------------
_BANNED_PHRASES = [
    "i noticed", "i came across", "i was impressed", "it's clear that",
    "exciting opportunity", "unique opportunity", "great opportunity",
    "i wanted to reach out", "i'd love to connect", "at the intersection of",
    "when it comes to", "in today's market",
]
_BANNED_WORDS = [
    "leverage", "optimize", "streamline", "synergy", "unlock", "empower",
    "crucial", "pivotal", "vital", "landscape", "robust", "comprehensive",
    "holistic", "innovative", "cutting-edge", "enhance", "foster", "cultivate",
    "showcase", "underscore", "highlight", "delve", "navigate", "spearhead",
    "bolster", "garner", "resonate", "seamless", "transformative",
    "groundbreaking", "strategic", "scalable", "tailored", "bespoke", "key",
]
_WORD_RE = {w: re.compile(rf"\b{re.escape(w)}\b", re.IGNORECASE) for w in _BANNED_WORDS}

# HARD RULE 8 / referral_email.md rule 4 (REFERRAL CLAIM DISCIPLINE): Block 2
# and Block 4 may reference the Manpreet connection as context ("part of
# Manpreet's network", "Singh Capital and {firm} sit in the same corner") but
# must NEVER fabricate something attributed TO him ("Manpreet mentioned your
# Q3 raise", "Manpreet speaks highly of your work"). Until this check was
# added (a QA finding — the rule previously existed only as a prompt
# instruction to the LLM with no mechanical backstop, unlike the banned-words
# list above), a model slip here would have shipped silently. Matches
# "manpreet" followed by a reported-speech verb within a few words.
_MANPREET_ATTRIBUTION_RE = re.compile(
    r"\bmanpreet\w*\b(?:\W+\w+){0,4}?\W+"
    r"(mentioned|told|said|shared|speaks?|spoke|thinks?|believes?|noted|"
    r"indicated|informed|vouches?|vouched|says?|recommends?|recommended|"
    r"describes?|described|tells?)\b",
    re.IGNORECASE,
)


def find_violations(text: str) -> list[str]:
    """Rules mandatory per referral_email.md's ANTI-AI WRITING RULES section
    plus HARD RULE 8. Checked against Block 2 + Block 4 text only (the
    LLM-generated blocks)."""
    v = []
    if "—" in text or "--" in text:
        v.append("em_dash")
    if ";" in text:
        v.append("semicolon")
    if "“" in text or "”" in text or "‘" in text or "’" in text:
        v.append("curly_quotes")
    if "?" in text:
        v.append("question_mark")
    if _MANPREET_ATTRIBUTION_RE.search(text):
        v.append("fabricated_manpreet_attribution")
    low = text.lower()
    for phrase in _BANNED_PHRASES:
        if phrase in low:
            v.append(f"banned_phrase:{phrase}")
    for word, rx in _WORD_RE.items():
        if rx.search(text):
            v.append(f"banned_word:{word}")
    return v


# QA finding (real-mode Kingbird run): research had 17 states / $4.3B; Block
# 2 said "12 states / $4.3B" -- Phase 5a's LLM call is given the research
# object as context but nothing mechanically stopped it from restating a
# figure with drift (or inventing one). Every dollar figure or count Block
# 2/4 cites must trace to the research+personalization data it was
# generated from.
def find_grounding_violations(text: str, corpus_text: str) -> list[str]:
    return [f"ungrounded_claim:{c}" for c in sorted(fact_check.unverifiable_claims(text, corpus_text))]


# QA finding (real-mode Kingbird run): Block 2 and Block 4 both said "roughly
# 21,000 units" -- Block 4 restated a fact Block 2 already used instead of
# adding a new one, reading as padded rather than tightly written.
def find_redundant_claims(block2: str, block4: str) -> list[str]:
    shared = fact_check.extract_numeric_claims(block2) & fact_check.extract_numeric_claims(block4)
    return [f"redundant_claim:{c}" for c in sorted(shared)]


# FIX 10 (real-mode Kairos Real Estate Partners run): a real run produced a
# complete, well-formed, non-violating email with NO dollar figure anywhere,
# despite research.aum_display holding a gate-verified $254M -- the gate
# already did the work of establishing this figure before qualifying the
# firm, so silently dropping it is a real defect, not a stylistic choice the
# model is free to make. find_grounding_violations only catches a WRONG
# number already present in the text; it has nothing to say about a number
# that's simply absent. This checks the one figure the gate itself verified
# is actually present somewhere in the combined Block 2 + Block 4 text.
#
# FIX 22 (real-mode In-Rel Properties run): disclosed_figure -- verbatim,
# no arithmetic -- is now the primary required fact, since it's the ONLY
# AUM-shaped value a prospect is ever meant to read. aum_display (now
# itself just a combination of disclosed_figure/disclosed_label) remains a
# fallback for un-migrated research dicts that predate the split.
def find_missing_required_facts(combined_text: str, research: dict) -> list[str]:
    research = research or {}
    required_source = (str(research.get("disclosed_figure") or "").strip()
                       or str(research.get("aum_display") or "").strip())
    if not required_source:
        return []
    required = fact_check.extract_numeric_claims(required_source)
    if not required:
        return []
    present = fact_check.extract_numeric_claims(combined_text)
    missing = required - present
    return [f"missing_required_aum:{c}" for c in sorted(missing)]


# FIX 16 (real-mode Kairos Real Estate Partners run): an invested/deployed-
# capital proxy is a CUMULATIVE figure summed over the firm's whole
# operating history, not a snapshot of current holdings -- referral_email.md
# now instructs a "since <founded_year>" framing for this specific proxy
# shape ("roughly $254M deployed since 2013", not "$254M across the
# portfolio", which wrongly implies a current asset base). Prompt-level
# guidance alone proved unreliable (a live check found the model wrote "$254M
# in invested capital across..." despite the instruction) -- mechanically
# requires founded_year's own digits appear in the text whenever this proxy
# shape is in play, the same "don't just ask nicely" pattern as
# find_missing_required_facts above. A well-framed "since <year>" sentence
# naturally includes the year; an "across the portfolio"-style sentence has
# no reason to mention a founding year at all, so this reliably distinguishes
# the two without trying to parse for the literal word "since".
def find_missing_temporal_framing(combined_text: str, research: dict) -> list[str]:
    research = research or {}
    # FIX 32A (real-mode Kairos Real Estate Partners run, FIX 31's own
    # VERIFY): a real extraction returned founded_year as a bare JSON int
    # (2013, not "2013") -- the extraction prompt's own schema hint ("e.g.
    # 2008 or empty string") shows a bare-number example, inviting this.
    # str(...) BEFORE the existing "or ''" falsy-guard, applied to every
    # research[...] value below that assumes a string, not just
    # founded_year -- belt and suspenders, same posture as the HARD RULE 2
    # linkedin-source guard elsewhere in this module (don't trust the
    # extraction prompt alone).
    aum_tag = str(research.get("aum_tag") or "").upper()
    proxy_type = str(research.get("proxy_type") or "").strip().lower()
    proxy_basis = str(research.get("proxy_basis") or "").lower()
    founded_year = str(research.get("founded_year") or "").strip()
    is_invested_capital_proxy = (
        "ESTIMATED" in aum_tag and proxy_type != "gav"
        and ("invested capital" in proxy_basis or "deployed" in proxy_basis)
    )
    if not (is_invested_capital_proxy and founded_year):
        return []
    if founded_year in combined_text:
        return []
    return [f"missing_temporal_framing:{founded_year}"]


# FIX 30 (real-mode Kairos Real Estate Partners run, FIX 29's own
# verification): referral_email.md's Block 4 spec requires two elements --
# the "overpaying a provider OR burning your controller's nights" core
# insight, and a closing beat near "that inertia has a price, and you're
# paying it every close." Neither was ever mechanically checked -- every
# check above covers punctuation/style, grounding, redundancy, or figures,
# not argument structure. The one successfully-generated (non-fallback)
# Block 4 from FIX 29's own verification shipped on-topic and PropTech-free
# but had NEITHER element, since today both are only guaranteed when
# generation FAILS (the fixed fallback paragraph below has both; generated
# text had no such guarantee). Checks meaning via keyword co-occurrence,
# not string equality -- referral_email.md itself says "something close
# to", so a good paraphrase in different words must still pass; reuses
# find_violations' word-list/regex convention above rather than inventing a
# new style.
# "outsourc\w*" (not the narrower literal "outsourced"), "high-fee", and
# "admin" were all added after this fix's own real-mode VERIFY, each a
# genuinely well-formed, two-bad-options Block 4 wrongly rejected because
# the word list didn't recognize the specific synonym used: "...pushes you
# into high-fee outsourcers..." (noun form, not the adjective "outsourced");
# "...paying an admin who has never opened your ledgers..." -- the second
# one notable because "fund admin" is the exact vocabulary
# referral_email.md's OWN "Good example" Block 4 text uses, which had never
# actually been run through this check before. Each is a mis-tuned check
# causing an unnecessary fallback, exactly the failure mode this fix's own
# tuning constraint warned against -- not a sign the emails got worse.
_PROVIDER_COST_SIGNAL_RE = re.compile(
    r"\b(overpay(?:ing)?|expensive|retainer|outsourc\w*|provider|firm fees|"
    r"high-fee|admin)\b",
    re.IGNORECASE,
)
_INTERNAL_BURDEN_SIGNAL_RE = re.compile(
    r"\b(controller|in-house|internal|nights?|long hours|month-end crunch|"
    r"overload(?:ing)?)\b",
    re.IGNORECASE,
)
_DISJUNCTION_RE = re.compile(
    r"\b(or|either|versus|vs\.?|trade-off|choice between)\b",
    re.IGNORECASE,
)
# \bcost\w*\b (bare "cost"/"costs"/"costly"/"costing") was added after this
# fix's own real-mode VERIFY: referral_email.md's OWN second Block 4 "Good
# example" ("...Both cost more than they should.") failed this check on its
# closing sentence before this addition -- the check was rejecting the very
# style the prompt itself holds up as correct.
_INACTION_COST_SIGNAL_RE = re.compile(
    r"(\binertia\b|that price|costing you|paying it|every close|status quo|"
    r"\bcost\w*\b)",
    re.IGNORECASE,
)


def find_missing_block4_argument(block4: str) -> list[str]:
    """Block 4's own two required elements, checked semantically (keyword
    co-occurrence), not by exact wording. Returns a distinct violation
    string per missing element so the existing per-check fallback logging
    (FIX 19 / §9p) names which one failed.

    FIX 32B (FIX 31's own VERIFY): the two-bad-options co-occurrence is
    scoped to the FIRST sentence only, not the whole block. _trim_to_budget
    drops trailing sentences first -- correct, per the template's own
    ordering rule -- so a model that places the disjunction in a LATER
    sentence ships an argument-free Block 4 once that sentence is trimmed,
    even though the untrimmed original passed this check. Scoping to
    sentence one is deterministic: a passing Block 4 leads with the
    argument, and trimming preserves sentence one, so a pass is guaranteed
    to survive to what ships. The closing-beat check is UNCHANGED -- it
    still scans the LAST sentences, since referral_email.md's own new rule
    (FIX 32B) makes the closing beat expendable but the core insight not."""
    v = []
    sentences = _sentences(block4)
    first_sentence = sentences[0] if sentences else block4
    has_two_bad_options = bool(
        _PROVIDER_COST_SIGNAL_RE.search(first_sentence)
        and _INTERNAL_BURDEN_SIGNAL_RE.search(first_sentence)
        and _DISJUNCTION_RE.search(first_sentence)
    )
    if not has_two_bad_options:
        v.append("missing_two_bad_options")

    closing = " ".join(sentences[-2:]) if len(sentences) >= 2 else (sentences[-1] if sentences else block4)
    if not _INACTION_COST_SIGNAL_RE.search(closing):
        v.append("missing_inertia_closing")
    return v


# ---------------------------------------------------------------------------
# Block 2 / Block 4 generation
# ---------------------------------------------------------------------------
_GENERATE_PROMPT = """{template}

---
Using the generation rules above, write ONLY Block 2 and Block 4 for this firm.
Research object (every claim in Block 2 must trace to one of these facts):
{research_json}

Return a SINGLE JSON object, no prose, no markdown fence:
{{"block_2": "...", "block_4": "..."}}
"""


def _fallback_blocks(research: dict) -> tuple[str, str]:
    oneliner = str(research.get("firm_oneliner") or "").strip()
    block2 = oneliner or "Your firm came up in Manpreet's network as a fit for what we do."
    return block2, _THIN_FALLBACK_BLOCK_4


# ---------------------------------------------------------------------------
# FIX 34 (real-mode American Real Estate Partners run): a single validator
# false positive on EITHER block discarded BOTH via _fallback_blocks, with
# no retry -- a clean Block 4 paid for a Block 2 grounding false positive
# (FIX 33's own phantom-309 bug) that had nothing to do with it. Violations
# are now attributed per-block; a clean block always ships untouched; a
# violating block gets exactly one retry before falling back, and never
# drags the other, already-clean block down with it.
# ---------------------------------------------------------------------------
_fallback_counts: dict[str, int] = {}


def reset_fallback_counts() -> None:
    """Exposed for a batch-level caller to reset the aggregate at the start
    of a run. NOTE: this fix's scope is email_drafter.py only -- nothing
    outside this module calls this (or print_fallback_summary below) at an
    actual batch-run boundary yet; that wiring belongs in pipeline.py /
    telegram_bot.py, which are out of scope for this fix. The counter and
    printer are self-contained and ready for that wiring to be added."""
    _fallback_counts.clear()


def fallback_counts() -> dict[str, int]:
    """Per-run aggregate of ACTUAL fallback events (a block that still
    violated after its one retry, or that skipped retry on the token
    guard) by violation reason, accumulated across every _generate_blocks()
    call in this process since the last reset() -- not every violation
    merely detected, since a first-pass violation that the retry fixes was
    never actually a fallback."""
    return dict(_fallback_counts)


def print_fallback_summary() -> None:
    if not _fallback_counts:
        return
    print("[EMAIL FALLBACK SUMMARY] " + ", ".join(
        f"{reason}={count}" for reason, count in sorted(_fallback_counts.items())
    ))


def _record_fallback(reasons: list[str]) -> None:
    for reason in reasons:
        _fallback_counts[reason] = _fallback_counts.get(reason, 0) + 1


def _classify_violations(block2: str, block4: str, research: dict, personalization: dict,
                         firm_name: str, stage: str) -> tuple[list[str], list[str]]:
    """Runs every existing check -- same functions, same arguments as
    before FIX 34 -- and attributes each violation to Block 2 or Block 4,
    needed for per-block fallback. redundant_claim is Block 4's to fix
    (referral_email.md states it as a Block 4 rule: "Do NOT repeat a
    number Block 2 already used"); missing_required_fact and
    temporal_framing are Block 2's (the firm-specific-hook block the
    gate's own verified figure has always been expected to state -- see
    FIX 10's own "Good example" precedent, which states the figure IN
    Block 2). Prints the existing [EMAIL FALLBACK] log line for every
    violation, now naming the STAGE too (first-pass / post-retry)."""
    corpus_text = fact_check.grounding_corpus(research, personalization)
    combined_text = f"{block2} {block4}"
    checks = [
        ("style", find_violations(block2), block2, "block2"),
        ("style", find_violations(block4), block4, "block4"),
        ("grounding", find_grounding_violations(block2, corpus_text), block2, "block2"),
        ("grounding", find_grounding_violations(block4, corpus_text), block4, "block4"),
        ("redundancy", find_redundant_claims(block2, block4),
         f"block2={block2!r} block4={block4!r}", "block4"),
        ("missing_required_fact", find_missing_required_facts(combined_text, research),
         combined_text, "block2"),
        ("temporal_framing", find_missing_temporal_framing(combined_text, research),
         combined_text, "block2"),
        ("block4_argument", find_missing_block4_argument(block4), block4, "block4"),
    ]
    block2_violations: list[str] = []
    block4_violations: list[str] = []
    for category, found, offending_text, owner in checks:
        if found:
            (block2_violations if owner == "block2" else block4_violations).extend(found)
            print(f"[EMAIL FALLBACK] {firm_name}: {category} violation "
                 f"({', '.join(found)}) -- offending text: {offending_text!r} [{stage}]")
    return block2_violations, block4_violations


_RETRY_PROMPT = """{template}

---
A previous draft of {retry_target} for {firm_name} was rejected by an
automated check. Rewrite ONLY {retry_target}, fixing the flagged
problem(s) below -- keep the same approach and voice, following every
rule above.
{kept_context}Flagged problem(s):
{violation_summary}

Research object (every claim must trace to one of these facts):
{research_json}

Return a SINGLE JSON object, no prose, no markdown fence, with ONLY:
{{{json_keys}}}
"""


def _build_retry_prompt(template: str, research_json: str, firm_name: str,
                        retry_block2: bool, retry_block4: bool,
                        block2: str, block4: str,
                        block2_violations: list[str], block4_violations: list[str]) -> str:
    """The already-clean block (if any) is handed back as fixed CONTEXT
    only, never asked for again -- the model is told explicitly not to
    rewrite it, and the JSON schema requested doesn't even include its
    key, so a clean block can never be put at risk by the other block's
    retry."""
    targets = [name for name, requested in (("Block 2", retry_block2), ("Block 4", retry_block4)) if requested]
    retry_target = " and ".join(targets)

    kept_lines = []
    if not retry_block2:
        kept_lines.append(f'Block 2 (already correct -- keep as context, do not rewrite it): "{block2}"')
    if not retry_block4:
        kept_lines.append(f'Block 4 (already correct -- keep as context, do not rewrite it): "{block4}"')
    kept_context = ("\n".join(kept_lines) + "\n\n") if kept_lines else ""

    violation_lines = []
    if retry_block2:
        violation_lines.append(f"Block 2 ({', '.join(block2_violations)}): {block2!r}")
    if retry_block4:
        violation_lines.append(f"Block 4 ({', '.join(block4_violations)}): {block4!r}")
    violation_summary = "\n".join(violation_lines)

    json_keys = ", ".join(
        f'"block_{n}": "..."' for n, requested in (("2", retry_block2), ("4", retry_block4)) if requested
    )

    return _RETRY_PROMPT.format(template=template, firm_name=firm_name, retry_target=retry_target,
                                kept_context=kept_context, violation_summary=violation_summary,
                                research_json=research_json, json_keys=json_keys)


def _generate_blocks(firm_name: str, research: dict, personalization: dict,
                     progress: Progress = _noop) -> tuple[str, str, bool]:
    """Returns (block_2, block_4, block4_is_fixed_fallback).

    FIX 34: a clean block always ships untouched; a violating block gets
    exactly one retry (re-prompted with the specific violation names and
    offending text, asking the model to fix ONLY the flagged block(s)) --
    the already-clean block is passed as fixed context and never
    regenerated. If the retry still violates (or the token budget can't
    afford one), that block alone falls back to _fallback_blocks -- never
    the other. The 3rd return value's meaning narrows to "is Block 4 the
    fixed _THIN_FALLBACK_BLOCK_4 text" (what _trim_to_budget's
    block4_is_fixed parameter and draft_email()'s "thin_personalization"
    reporting field actually need) -- Block 2 can independently fall back
    to its own oneliner without that being true."""
    thin = bool(personalization.get("thin_personalization"))

    if config.TEST_MODE:
        fixture = testmode.email_blocks_fixture(firm_name)
        if fixture and not thin:
            return fixture["block_2"], fixture["block_4"], False
        return (*_fallback_blocks(research), True)

    if thin:
        return (*_fallback_blocks(research), True)

    from lib_snapshot import models
    import json as _json

    template = config.REFERRAL_EMAIL_TEMPLATE.read_text(encoding="utf-8")
    combined_research = {**research, **personalization}
    research_json = _json.dumps(combined_research, default=str)

    # FIX 15/16 follow-up (real-mode Kairos run): this call had no
    # reasoning_effort and no visibility into finish_reason/reasoning_tokens/
    # which model answered, same gap deck_filler.py's get_deck_content() and
    # personalization.py's research_personalization() had before being
    # instrumented and measured. A live diagnostic (3 runs against the Kairos
    # research object) confirmed the same reasoning-budget-exhaustion
    # signature FIX 15 fixed for get_deck_content(): 2 of 3 runs escalated to
    # DeepSeek after gpt-5-mini hit the FULL 2048-token reasoning ceiling
    # twice each (finish_reason='length', reasoning_tokens=2048,
    # response_chars=0); the one run that succeeded on attempt 1 still spent
    # 1728 of that same 2048-token budget on reasoning. reasoning_effort=
    # "minimal" applied on that basis. Kept as a warning (not unconditional
    # print) for ongoing visibility, matching the established convention.
    def _capture(model_name: str, text: str, finish_reason: str, reasoning_tokens: int) -> None:
        if finish_reason == "length" or not text.strip():
            print(f"[EMAIL DRAFT WARNING] {firm_name}: {model_name} returned "
                 f"finish_reason={finish_reason!r} reasoning_tokens={reasoning_tokens} "
                 f"response_chars={len(text)} -- possible reasoning-budget exhaustion.")

    prompt = _GENERATE_PROMPT.format(template=template, research_json=research_json)
    parsed, usage = models.complete_json_retry(prompt, temperature=0.6, max_tokens=600,
                                               debug_capture=_capture, reasoning_effort="minimal")
    block2 = (parsed.get("block_2") or "").strip()
    block4 = (parsed.get("block_4") or "").strip()

    block2_v, block4_v = _classify_violations(block2, block4, research, personalization,
                                              firm_name, "first-pass")
    if not block2_v and not block4_v:
        return block2, block4, False

    tokens_so_far = getattr(usage, "total_tokens", 0) or 0
    if tokens_so_far >= config.MAX_LLM_TOKENS_PER_FIRM:
        skip_msg = (f"{firm_name}: skipping retry (token budget already at/over "
                   f"{config.MAX_LLM_TOKENS_PER_FIRM}), falling back per-block "
                   f"({', '.join(block2_v + block4_v)}). [per-block]")
        print(f"[EMAIL FALLBACK] {skip_msg}")
        progress(skip_msg)
        _record_fallback(block2_v)
        _record_fallback(block4_v)
        final_block2 = _fallback_blocks(research)[0] if block2_v else block2
        final_block4 = _fallback_blocks(research)[1] if block4_v else block4
        return final_block2, final_block4, bool(block4_v)

    retry_block2, retry_block4 = bool(block2_v), bool(block4_v)
    retry_prompt = _build_retry_prompt(template, research_json, firm_name,
                                       retry_block2, retry_block4, block2, block4,
                                       block2_v, block4_v)
    retry_parsed, _retry_usage = models.complete_json_retry(
        retry_prompt, temperature=0.6, max_tokens=600,
        debug_capture=_capture, reasoning_effort="minimal")
    new_block2 = (retry_parsed.get("block_2") or "").strip() if retry_block2 else block2
    new_block4 = (retry_parsed.get("block_4") or "").strip() if retry_block4 else block4

    new_block2_v, new_block4_v = _classify_violations(new_block2, new_block4, research, personalization,
                                                       firm_name, "post-retry")

    if new_block2_v:
        fallback_msg = f"{firm_name}: Block 2 fell back after retry ({', '.join(new_block2_v)}). [per-block]"
        print(f"[EMAIL FALLBACK] {fallback_msg}")
        progress(fallback_msg)
        _record_fallback(new_block2_v)
    if new_block4_v:
        fallback_msg = f"{firm_name}: Block 4 fell back after retry ({', '.join(new_block4_v)}). [per-block]"
        print(f"[EMAIL FALLBACK] {fallback_msg}")
        progress(fallback_msg)
        _record_fallback(new_block4_v)

    final_block2 = new_block2 if not new_block2_v else _fallback_blocks(research)[0]
    final_block4 = new_block4 if not new_block4_v else _fallback_blocks(research)[1]
    return final_block2, final_block4, bool(new_block4_v)


_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


def _sentences(text: str) -> list[str]:
    return [s for s in _SENTENCE_SPLIT_RE.split(text.strip()) if s]


def _drop_trailing_sentences(text: str, words_to_cut: int) -> str:
    """Drop whole trailing sentences until under the cut target, or until
    only one sentence remains. NEVER truncates mid-sentence — a dangling
    fragment ("...with LP.") is worse than running a few words over budget."""
    sentences = _sentences(text)
    while len(sentences) > 1 and words_to_cut > 0:
        last_len = len(sentences[-1].split())
        sentences.pop()
        words_to_cut -= last_len
    return " ".join(sentences)


def _trim_to_budget(block2: str, block4: str, fixed_word_count: int,
                    block4_is_fixed: bool = False) -> tuple[str, str]:
    """Length guard: total <= config.EMAIL_MAX_WORDS. Trim Block 4 first, then
    Block 2, per referral_email.md's generation rules — by whole sentences,
    never mid-sentence (a truncated clause reads as broken, not concise).

    `block4_is_fixed`: True when Block 4 is the thin_personalization fallback
    — referral_email.md rule 6 presents it as "Harp's original generic pain
    paragraph" verbatim, a specific fixed string he wrote as a complete unit,
    not LLM output whose length is a free variable. It is exempted from
    trimming for the same reason Blocks 1/3/5/6/7 are: cutting a sentence out
    of someone's deliberately-written fixed text is not a length "guard", it's
    editing their copy. (QA finding: this previously trimmed it identically to
    generated content, silently truncating the fallback paragraph whenever the
    fixed blocks' real 204-word total plus the 67-word fallback — already over
    referral_email.md's own stated 230-word cap before Block 2 is even added —
    pushed the email over budget, which is EVERY thin_personalization draft.)
    """
    def word_count(s: str) -> int:
        return len(s.split())

    if block4_is_fixed:
        total = fixed_word_count + word_count(block2) + word_count(block4)
        if total > config.EMAIL_MAX_WORDS:
            over = total - config.EMAIL_MAX_WORDS
            block2 = _drop_trailing_sentences(block2, over)
        return block2, block4

    total = fixed_word_count + word_count(block2) + word_count(block4)
    if total <= config.EMAIL_MAX_WORDS:
        return block2, block4

    over = total - config.EMAIL_MAX_WORDS
    new_block4 = _drop_trailing_sentences(block4, over)
    over -= word_count(block4) - word_count(new_block4)
    block4 = new_block4

    if over > 0:
        new_block2 = _drop_trailing_sentences(block2, over)
        block2 = new_block2

    return block2, block4


def draft_email(firm_name: str, poc_first_name: str, poc_email: str,
                research: dict, personalization: dict, progress: Progress = _noop) -> dict:
    """Phase 5a entry point. Returns {subject, to, body, thin_personalization, word_count}."""
    fixed = _fixed_blocks()
    block1 = fixed[1].replace("{{poc_first_name}}", poc_first_name or "there")
    block3 = fixed[3]
    block5 = fixed[5]
    block6 = fixed[6]
    block7 = fixed[7]

    block2, block4, used_thin = _generate_blocks(firm_name, research, personalization, progress)

    fixed_word_count = sum(len(b.split()) for b in (block1, block3, block5, block6, block7))
    block2, block4 = _trim_to_budget(block2, block4, fixed_word_count, block4_is_fixed=used_thin)

    body = "\n\n".join([block1, block2, block3, block4, block5, block6, block7])
    word_count = len(body.split())
    if word_count > config.EMAIL_MAX_WORDS:
        progress(f"{firm_name}: email still over {config.EMAIL_MAX_WORDS} words "
                 f"({word_count}) after trim — shipping anyway, flag for review.")

    return {
        "subject": FIXED_SUBJECT,
        "to": poc_email,
        "body": body,
        "thin_personalization": used_thin,
        "word_count": word_count,
        "block_2": block2,
        "block_4": block4,
    }

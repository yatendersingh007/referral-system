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
    oneliner = (research.get("firm_oneliner") or "").strip()
    block2 = oneliner or "Your firm came up in Manpreet's network as a fit for what we do."
    return block2, _THIN_FALLBACK_BLOCK_4


def _generate_blocks(firm_name: str, research: dict, personalization: dict,
                     progress: Progress = _noop) -> tuple[str, str, bool]:
    """Returns (block_2, block_4, used_thin_fallback)."""
    thin = bool(personalization.get("thin_personalization"))

    if config.TEST_MODE:
        fixture = testmode.email_blocks_fixture(firm_name)
        if fixture and not thin:
            return fixture["block_2"], fixture["block_4"], False
        return (*_fallback_blocks(research), True)

    if thin:
        return (*_fallback_blocks(research), True)

    from lib_snapshot import models

    combined_research = {**research, **personalization}
    template = config.REFERRAL_EMAIL_TEMPLATE.read_text(encoding="utf-8")
    import json as _json
    prompt = _GENERATE_PROMPT.format(template=template,
                                     research_json=_json.dumps(combined_research, default=str))
    parsed, _usage = models.complete_json_retry(prompt, temperature=0.6, max_tokens=600)
    block2 = (parsed.get("block_2") or "").strip()
    block4 = (parsed.get("block_4") or "").strip()

    corpus_text = fact_check.grounding_corpus(research, personalization)
    violations = (find_violations(block2) + find_violations(block4)
                 + find_grounding_violations(block2, corpus_text)
                 + find_grounding_violations(block4, corpus_text)
                 + find_redundant_claims(block2, block4))
    if violations:
        progress(f"{firm_name}: Block 2/4 fell back to generic text ({', '.join(violations)}).")
        return (*_fallback_blocks(research), True)
    return block2, block4, False


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

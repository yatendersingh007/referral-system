"""Phase 1 firm matcher (§4). Implements, in order: Guard 0 (degenerate input),
Pass 1 deterministic (1a exact / 1b suffix-stripped equality / 1c subset),
Pass 2 fuzzy triage on suffix-stripped strings, the acronym pass, and Pass 3
batched LLM disambiguation for anything routed to the "LLM band".

Every rule below closes one demonstrated hole from the adversarial review
(see BUILD_SPEC_C.md §4) — do not simplify away the ordering or the token
sanity checks; the trap cases in tests/test_firm_matcher.py rely on it.

This module is DB/LLM-decoupled: match_firms() takes a plain list of company
names and an injectable llm_batch_fn, so the 27-case adversarial suite can run
with a deterministic fake LLM instead of a real network call.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

from rapidfuzz import fuzz, process

from .. import config
from .normalize import (
    CorpusIndex,
    is_degenerate,
    looks_like_acronym,
    acronym_letters,
    meaningful_tokens,
    normalize,
    strip_suffix,
)

LlmBatchFn = Callable[[list[dict]], list[dict]]


@dataclass
class MatchResult:
    target: str
    outcome: str  # matched | no_connection | parked_ambiguous | degenerate_name
    matched_company: Optional[str] = None
    match_method: Optional[str] = None  # exact|stripped|subset|fuzzy|acronym|llm
    confidence: Optional[float] = None
    alternatives: list[dict] = field(default_factory=list)  # [{name, confidence}]
    reason: str = ""


@dataclass
class _LlmTicket:
    target: str
    pinned: list[str]
    fuzzy_top10: list[str]
    reason: str


class _Corpus:
    """Precomputed lookups over the distinct company-name corpus, built once
    per matching run (cheap to rebuild; callers may cache it across calls)."""

    def __init__(self, company_names: list[str]):
        self.index = CorpusIndex(company_names)
        self.names = self.index.company_names
        self.norm_to_names: dict[str, list[str]] = {}
        self.stripped_to_names: dict[str, list[str]] = {}
        self.token_to_names: dict[str, set[str]] = {}
        self.stripped_forms: dict[str, str] = {}
        for name in self.names:
            self.norm_to_names.setdefault(normalize(name), []).append(name)
            stripped = strip_suffix(name)
            self.stripped_forms[name] = stripped
            if stripped:
                self.stripped_to_names.setdefault(stripped, []).append(name)
            for tok in set(meaningful_tokens(name)):
                self.token_to_names.setdefault(tok, set()).add(name)


def _subset_candidates(target: str, corpus: _Corpus) -> tuple[list[str], list[str]]:
    """Pass 1c. Returns (qualifying_auto_match, needs_llm_pin) company lists."""
    target_tokens = set(meaningful_tokens(target))
    if not target_tokens:
        return [], []
    seen: set[str] = set()
    for tok in target_tokens:
        seen |= corpus.token_to_names.get(tok, set())
    qualifying, pins = [], []
    for cand in seen:
        cand_tokens = set(meaningful_tokens(cand))
        if not cand_tokens:
            continue
        if cand_tokens <= target_tokens or target_tokens <= cand_tokens:
            shared = cand_tokens & target_tokens
            smaller = min(len(cand_tokens), len(target_tokens))
            if smaller >= 2:
                qualifying.append(cand)
            elif smaller == 1:
                tok = next(iter(shared))
                if corpus.index.is_rare(tok) and target_tokens == {tok}:
                    qualifying.append(cand)
                else:
                    pins.append(cand)
    return qualifying, pins


def _fuzzy_top10(target: str, corpus: _Corpus) -> list[tuple[str, float]]:
    stripped_target = strip_suffix(target) or normalize(target)
    choices = corpus.stripped_forms
    if not choices:
        return []
    results = process.extract(
        stripped_target, choices, scorer=fuzz.token_sort_ratio, limit=10,
    )
    # rapidfuzz.process.extract on a dict returns (choice_value, score, key)
    return [(key, score) for (_value, score, key) in results]


def _shared_meaningful(a: str, b: str) -> int:
    return len(set(meaningful_tokens(a)) & set(meaningful_tokens(b)))


def evaluate_deterministic(target: str, corpus: _Corpus) -> tuple[Optional[MatchResult], Optional[_LlmTicket]]:
    """Guard 0 + Pass 1 + Pass 2 + acronym pass. Returns (final_result, ticket).
    Exactly one of the two is non-None."""
    if is_degenerate(target):
        return MatchResult(target=target, outcome="degenerate_name",
                          reason="target has no meaningful (non-suffix) tokens"), None

    norm_target = normalize(target)

    # Pass 1a — exact
    if norm_target in corpus.norm_to_names:
        return MatchResult(target=target, outcome="matched",
                          matched_company=corpus.norm_to_names[norm_target][0],
                          match_method="exact", confidence=1.0), None

    # Pass 1b — suffix-stripped equality
    stripped_target = strip_suffix(target)
    if stripped_target and stripped_target in corpus.stripped_to_names:
        cands = corpus.stripped_to_names[stripped_target]
        single_token = len(stripped_target.split()) == 1
        escalate = len(cands) > 1  # multiple DISTINCT firms share this stripped
                                    # form — always ambiguous, never auto-merge
        if single_token and corpus.index.token_freq(stripped_target) > config.RARE_TOKEN_CORPUS_FREQ:
            escalate = True
        if escalate:
            return None, _LlmTicket(target=target, pinned=cands, fuzzy_top10=[],
                                    reason="stripped-equality on a common token "
                                    "or multiple distinct firms share it")
        return MatchResult(target=target, outcome="matched", matched_company=cands[0],
                          match_method="stripped", confidence=1.0), None

    # Pass 1c — subset / containment
    qualifying, pins = _subset_candidates(target, corpus)
    if len(qualifying) == 1 and not pins:
        return MatchResult(target=target, outcome="matched", matched_company=qualifying[0],
                          match_method="subset", confidence=1.0), None
    if qualifying or pins:
        return None, _LlmTicket(target=target, pinned=qualifying + pins, fuzzy_top10=[],
                                reason="subset match ambiguous or needs token-sanity review")

    # Pass 2 — fuzzy triage on suffix-stripped strings
    top10 = _fuzzy_top10(target, corpus)
    acronym_hit_names: list[str] = []
    if looks_like_acronym(target):
        # Look up the target's own letters against the corpus initials index
        # (which already carries both full-initials and leading-bigram KEYS
        # for each company — see CorpusIndex). Do NOT additionally compute a
        # leading-bigram variant of the target itself: for a single-token
        # acronym like "SCP", bigram-of-the-target degenerates to the first
        # two characters ("sc"), which can spuriously collide with an
        # unrelated company's real initials (e.g. "Singh Capital" = "sc").
        letters = acronym_letters(target)
        acronym_hit_names = list(dict.fromkeys(corpus.index.initials_hits(letters)))

    if top10:
        top_name, top_score = top10[0]
        if top_score >= config.FUZZY_AUTOMATCH_SCORE:
            if _shared_meaningful(target, top_name) >= 2:
                return MatchResult(target=target, outcome="matched", matched_company=top_name,
                                  match_method="fuzzy", confidence=top_score / 100), None
            return None, _LlmTicket(target=target, pinned=[top_name],
                                    fuzzy_top10=[n for n, _ in top10],
                                    reason="high fuzzy score without token sanity")
        if top_score >= config.FUZZY_LLM_BAND_FLOOR:
            pinned = acronym_hit_names[:]  # acronym injection, if any, rides along
            return None, _LlmTicket(target=target, pinned=pinned,
                                    fuzzy_top10=[n for n, _ in top10],
                                    reason="mid-band fuzzy score")

    # top_score < 55 (or no corpus at all): acronym pass is the only rescue
    if acronym_hit_names:
        return None, _LlmTicket(target=target, pinned=acronym_hit_names,
                                fuzzy_top10=[n for n, _ in top10],
                                reason="acronym initials match")

    return MatchResult(target=target, outcome="no_connection",
                      reason="no deterministic or acronym match"), None


def _default_llm_batch(batch: list[dict]) -> list[dict]:
    """Real Pass-3 LLM call (batched, 15-20 firms/call). Not used by tests —
    they inject a deterministic fake. Never called in TEST_MODE (callers must
    inject a fixture-backed llm_batch_fn instead; see src/testmode.py)."""
    from lib_snapshot import models

    prompt = (
        "You are matching target firm names against candidate companies from a "
        "LinkedIn connections export. For EACH target, decide which candidate (if "
        "any) is the SAME real-world firm.\n\n"
        "CRITICAL: Two firms with similar names but different businesses are "
        "DIFFERENT firms — never merge. If ambiguous, return both with confidence < 0.8.\n\n"
        "Return a JSON object: {\"results\": [{\"target\":.., \"matched_to\": "
        "<candidate name or null>, \"confidence\": 0.0-1.0}, ...]}. A target may "
        "appear more than once in results if more than one candidate is plausible.\n\n"
        f"Batch:\n{batch}"
    )
    parsed, _usage = models.complete_json_retry(prompt, temperature=0.1)
    return parsed.get("results", []) if isinstance(parsed, dict) else []


def _resolve_llm_results(target: str, rows: list[dict]) -> MatchResult:
    rows = [r for r in rows if (r.get("target") or "").strip().lower() == target.strip().lower()]
    strong = [r for r in rows if (r.get("confidence") or 0) >= config.LLM_MATCH_CONFIDENCE_MATCH
             and r.get("matched_to")]
    if strong:
        best = max(strong, key=lambda r: r["confidence"])
        return MatchResult(target=target, outcome="matched", matched_company=best["matched_to"],
                          match_method="llm", confidence=best["confidence"])
    parked = [r for r in rows if r.get("matched_to")
             and (r.get("confidence") or 0) >= config.LLM_MATCH_CONFIDENCE_PARK]
    if parked:
        alts = [{"name": r["matched_to"], "confidence": r["confidence"]} for r in parked]
        return MatchResult(target=target, outcome="parked_ambiguous", alternatives=alts,
                          match_method="llm", reason="firm_match_ambiguous")
    return MatchResult(target=target, outcome="no_connection", match_method="llm",
                      reason="LLM confidence below 0.5")


def match_firms(targets: list[str], company_names: list[str],
               llm_batch_fn: Optional[LlmBatchFn] = None) -> list[MatchResult]:
    """Full Phase 1 pipeline over a target-firm list against the connections
    company corpus. `llm_batch_fn` is REQUIRED to be supplied by the caller in
    TEST_MODE (a fixture-backed fake); defaults to the real batched LLM call
    otherwise."""
    llm_batch_fn = llm_batch_fn or _default_llm_batch
    corpus = _Corpus(company_names)

    results: dict[str, MatchResult] = {}
    tickets: list[_LlmTicket] = []
    for t in targets:
        result, ticket = evaluate_deterministic(t, corpus)
        if result is not None:
            results[t] = result
        else:
            tickets.append(ticket)

    # Pass 3: batch the LLM band, LLM_MATCH_BATCH_SIZE tickets per call.
    size = config.LLM_MATCH_BATCH_SIZE
    for i in range(0, len(tickets), size):
        chunk = tickets[i:i + size]
        batch_payload = [
            {"target": tk.target,
             "candidates": list(dict.fromkeys(tk.pinned + tk.fuzzy_top10))[:10]}
            for tk in chunk
        ]
        rows = llm_batch_fn(batch_payload) or []
        for tk in chunk:
            results[tk.target] = _resolve_llm_results(tk.target, rows)

    return [results[t] for t in targets]

"""Company-name normalization + corpus indices for the Phase 1 firm matcher.

Pure functions, no I/O, no DB, no LLM — this module is the deterministic
foundation the matcher's Pass 1/Pass 2/acronym-pass and Guard 0 are built on.
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Iterable

from ..config import SUFFIX_TOKENS, RARE_TOKEN_CORPUS_FREQ

# Periods and apostrophes COLLAPSE (removed with no space): they appear inside
# a single logical word/initialism ("J.P. Morgan", "U.S. Bancorp", "O'Brien",
# "Ferré Rangel's"), and the source text already carries the real word
# boundary as a space where one exists. Splitting on them instead (the old
# behavior) tokenized "J.P. Morgan" as ["j","p","morgan"] vs "JP Morgan" as
# ["jp","morgan"] — two extremely common spellings of the same real firm
# landing on incompatible token sets, so even 1a exact and 1b stripped-equality
# never fired for garden-variety punctuation variants (found via a "JP Morgan"
# vs "J.P. Morgan" adversarial QA case against the real connections corpus).
_COLLAPSE_RE = re.compile(r"[.'’]+")
# Comma/ampersand/slash/parens/hyphen genuinely separate distinct segments
# ("Smith-Jones", "Holdings, LLC", "Property/Asset Management") and become a
# space.
_PUNCT_RE = re.compile(r"[,&/\\\"()\-]+")
_WS_RE = re.compile(r"\s+")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9 ]+")


def normalize(name: str) -> str:
    """Lowercase, drop punctuation, collapse whitespace. Deterministic and
    idempotent — normalize(normalize(x)) == normalize(x)."""
    s = (name or "").strip().lower()
    s = _COLLAPSE_RE.sub("", s)
    s = s.replace("&", " and ")
    s = _PUNCT_RE.sub(" ", s)
    s = _NON_ALNUM_RE.sub(" ", s)
    s = _WS_RE.sub(" ", s).strip()
    return s


def tokenize(name: str) -> list[str]:
    return normalize(name).split()


def meaningful_tokens(name: str) -> list[str]:
    """Tokens with suffix/generic words (Capital, Partners, LLC, ...) removed."""
    return [t for t in tokenize(name) if t not in SUFFIX_TOKENS]


def strip_suffix(name: str) -> str:
    """Normalized name with trailing/embedded suffix tokens removed, joined
    back into a string (used for stripped-equality and fuzzy-on-stripped)."""
    toks = meaningful_tokens(name)
    return " ".join(toks)


def is_degenerate(target: str) -> bool:
    """Guard 0: empty/whitespace, OR every meaningful token is itself a suffix
    word (so nothing distinctive remains — 'Capital Partners', 'The Group',
    'Real Estate', 'LLC' alone)."""
    if not (target or "").strip():
        return True
    toks = tokenize(target)
    if not toks:
        return True
    return len(meaningful_tokens(target)) == 0


def looks_like_acronym(target: str) -> bool:
    """All-caps <=6 chars (letters only), or dotted initials like 'S.C.P.'"""
    t = (target or "").strip()
    if not t:
        return False
    if re.fullmatch(r"[A-Z]{2,6}", t):
        return True
    if re.fullmatch(r"(?:[A-Za-z]\.){2,6}", t):
        return True
    return False


def acronym_letters(target: str) -> str:
    t = (target or "").strip()
    t = t.replace(".", "")
    return t.lower()


def initials_of(name: str) -> str:
    """Full initials of a company's meaningful tokens: 'Singh Capital Partners' -> 'scp'
    (all tokens, not just meaningful ones, since acronyms often include suffix
    words: SCP = Singh Capital Partners uses 'Capital' and 'Partners' too)."""
    toks = tokenize(name)
    return "".join(t[0] for t in toks if t)


def leading_bigram_variants(name: str) -> set[str]:
    """Leading-bigram variant of initials: for firms where an acronym is formed
    from the first TWO letters of a token (rare but real, e.g. 'ReTa Capital'
    -> 'RETA'). Kept intentionally small/cheap."""
    toks = tokenize(name)
    if not toks:
        return set()
    return {"".join(t[:2] for t in toks if t)}


class CorpusIndex:
    """Built once per matching run over the full connections corpus (distinct
    company names). Powers: meaningful-token frequency (rarity checks in
    Pass 1b/1c/Pass 2) and the initials index (acronym pass)."""

    def __init__(self, company_names: Iterable[str]):
        names = list(dict.fromkeys(n for n in company_names if (n or "").strip()))
        self.company_names: list[str] = names
        self._token_freq: Counter = Counter()
        self._initials_index: dict[str, list[str]] = {}
        for name in names:
            for tok in set(meaningful_tokens(name)):
                self._token_freq[tok] += 1
            for key in {initials_of(name), *leading_bigram_variants(name)}:
                if key:
                    self._initials_index.setdefault(key, []).append(name)

    def token_freq(self, token: str) -> int:
        return self._token_freq.get(token, 0)

    def is_rare(self, token: str) -> bool:
        return self.token_freq(token) <= RARE_TOKEN_CORPUS_FREQ

    def initials_hits(self, letters: str) -> list[str]:
        return self._initials_index.get(letters, [])

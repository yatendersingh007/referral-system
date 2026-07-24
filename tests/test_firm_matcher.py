"""Adversarial regression suite for the Phase 1 firm matcher (BUILD_SPEC_C.md
§4). 27 cases exercising Guard 0, Pass 1 (1a/1b/1c), Pass 2 fuzzy triage, the
acronym pass, and Pass 3 LLM-band disambiguation, run against a frozen sample
of real connection company names plus a small set of synthetic "trap" firms
constructed to exercise rules the real sample doesn't naturally contain (e.g.
three same-token "Apex ..." firms to push corpus frequency over the rare-token
threshold). The real-sample file (tests/fixtures/firm_sample_real.txt) is a
frozen random sample of 165 distinct company names drawn from
data/connections.csv, seeded for reproducibility.

Any matcher change must keep 25/27+ passing with ZERO false-positive
auto-matches (two different real firms merged together) — that second
guarantee is checked explicitly in test_pass_rate_and_zero_false_positives.

Pass-3 LLM disambiguation is stubbed with a deterministic fake (FAKE_LLM_VERDICTS)
so this suite never makes a real network call; the fake also asserts the
matcher actually injected the candidate it was supposed to (proving Pass 1/2/
acronym routing worked), not just that *some* verdict came back.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.matching.firm_matcher import match_firms

FIXTURES_DIR = Path(__file__).parent / "fixtures"

_REAL_SAMPLE = [
    line.strip()
    for line in (FIXTURES_DIR / "firm_sample_real.txt").read_text(encoding="utf-8").splitlines()
    if line.strip()
]

# Synthetic trap firms: real company names alone don't reliably exercise every
# rule (e.g. we need >2 distinct "Apex ..." firms to push corpus frequency of
# the single token "apex" over the rare threshold). Each is commented with the
# specific rule/hole it exists to exercise.
_SYNTHETIC = [
    "Riverside Family Partners",             # 1c positive subset auto-match (2 shared tokens)
    "Westfield Bay Partners",                # 1c positive weak-rare single-token auto-match
    "Monarch Realty Partners",               # 1b ambiguity: 2 distinct "Monarch" firms share stripped form
    "Monarch Capital Advisors",              # ^ same
    "Alternative Investments",               # 1c anti-merge trap vs "Monarch Alternative Capital"
    "Apex Group Ltd",                        # 1b: pushes corpus freq("apex") > 2 -> LLM band
    "Apex Realty Advisors",                  # ^ same
    "Apex Industrial Holdings",              # ^ same (3rd distinct Apex firm)
    "Sterling Ridge Partners",               # Pass 2: high fuzzy score, only 1 shared token
    "Meridian Ridge Partners",               # Pass 2: mid-band fuzzy score
    "Brightwater Summit Advisory Partners",  # Pass 2: positive fuzzy auto-match (1-word typo)
]

CORPUS_NAMES = _REAL_SAMPLE + _SYNTHETIC

# ---------------------------------------------------------------------------
# Deterministic fake for Pass 3 (batched LLM disambiguation). Keyed by target;
# each value is a list of (matched_to, confidence) the fake "LLM" returns.
# verify=True entries assert the candidate was actually present in what the
# matcher pinned/injected — proving the routing worked, not just the verdict.
# ---------------------------------------------------------------------------
FAKE_LLM_VERDICTS: dict[str, list[tuple[str, float]]] = {
    "Apex Capital Partners": [("Apex Group Ltd", 0.85), ("Apex Realty Advisors", 0.2)],
    "Kingbird Realty Brokers": [("Kingbird Investment Management", 0.15)],
    "Kingbird Investment Management Texas Division": [("Kingbird Investment Management", 0.9)],
    "Monarch": [("Monarch Realty Partners", 0.6), ("Monarch Capital Advisors", 0.6)],
    "Monarch Alternative Capital": [
        ("Monarch Capital Advisors", 0.1),
        ("Monarch Realty Partners", 0.1),
        ("Alternative Investments", 0.1),
    ],
    "Sterling Bridge Capital": [("Sterling Ridge Partners", 0.2)],
    "Meridian Point Capital": [("Meridian Ridge Partners", 0.25)],
    "SCP": [("Singh Capital Partners", 0.95)],
    "S.C.P.": [("Singh Capital Partners", 0.95)],
    "KBIM": [
        ("CBM Partners", 0.1), ("CIM Group", 0.1),
        ("Kingbird Investment Management", 0.2), ("FILM CLUB GROUP", 0.05),
        ("Blitz53 Capital", 0.05),
    ],
}


def fake_llm_batch(batch: list[dict]) -> list[dict]:
    """Test double for Pass 3. Asserts every verdict's candidate was actually
    offered to it (pinned or fuzzy-injected) by the deterministic passes."""
    by_target = {item["target"]: item["candidates"] for item in batch}
    rows = []
    for target, candidates in by_target.items():
        verdicts = FAKE_LLM_VERDICTS.get(target, [])
        for matched_to, confidence in verdicts:
            assert matched_to in candidates, (
                f"fake LLM asked to verdict on {matched_to!r} for target {target!r}, "
                f"but the matcher never offered it as a candidate: {candidates!r}"
            )
            rows.append({"target": target, "matched_to": matched_to, "confidence": confidence})
    return rows


# ---------------------------------------------------------------------------
# The 27 cases. Each: (id, target, expected_outcome, expected_method, expected_company)
# expected_method/company are None when not asserted (degenerate/no_connection/ambiguous).
# ---------------------------------------------------------------------------
CASES = [
    # --- Guard 0: degenerate input (5) ---
    ("guard_empty", "", "degenerate_name", None, None),
    ("guard_whitespace", "   ", "degenerate_name", None, None),
    ("guard_all_suffix_capital_partners", "Capital Partners", "degenerate_name", None, None),
    ("guard_all_suffix_the_group", "The Group", "degenerate_name", None, None),
    ("guard_all_suffix_real_estate", "Real Estate", "degenerate_name", None, None),

    # --- Pass 1a: exact (4) ---
    ("exact_kingbird", "Kingbird Investment Management", "matched", "exact",
     "Kingbird Investment Management"),
    ("exact_case_whitespace", "  BLACKSTONE  ", "matched", "exact", "Blackstone"),
    ("exact_aliya", "Aliya Capital Partners", "matched", "exact", "Aliya Capital Partners"),
    ("exact_singh_capital_partners", "Singh Capital Partners", "matched", "exact",
     "Singh Capital Partners"),

    # --- Pass 1b: suffix-stripped equality, clean auto-match (3) ---
    ("stripped_multi_token", "Riverside Family Partners LLC", "matched", "stripped",
     "Riverside Family Partners"),
    ("stripped_single_low_freq", "Blackstone Inc.", "matched", "stripped", "Blackstone"),
    ("stripped_aliya_bare", "Aliya", "matched", "stripped", "Aliya Capital Partners"),

    # --- Pass 1b -> LLM band (common/ambiguous stripped form) (2) ---
    ("stripped_common_token_apex", "Apex Capital Partners", "matched", "llm", "Apex Group Ltd"),
    ("stripped_two_distinct_firms_monarch", "Monarch", "parked_ambiguous", None, None),

    # --- Pass 1c: subset, clean auto-match (2) ---
    ("subset_two_shared_tokens", "Riverside Family Office Holdings", "matched", "subset",
     "Riverside Family Partners"),
    ("subset_weak_rare_single_token", "Westfield", "matched", "subset", "Westfield Bay Partners"),

    # --- Pass 1c: trap, must NOT auto-merge (2) ---
    ("subset_trap_kingbird_realty_brokers", "Kingbird Realty Brokers", "no_connection", None, None),
    ("subset_trap_kingbird_texas_division", "Kingbird Investment Management Texas Division",
     "matched", "llm", "Kingbird Investment Management"),

    # --- Pass 1c: multi-way anti-merge trap (1) ---
    ("subset_trap_monarch_alternative_capital", "Monarch Alternative Capital",
     "no_connection", None, None),

    # --- Pass 2: fuzzy, clean auto-match (1) ---
    ("fuzzy_typo_auto_match", "Brightwater Sumit Advisory Partners", "matched", "fuzzy",
     "Brightwater Summit Advisory Partners"),

    # --- Pass 2: fuzzy, high score without token sanity -> LLM band (1) ---
    ("fuzzy_high_score_no_sanity", "Sterling Bridge Capital", "no_connection", None, None),

    # --- Pass 2: fuzzy, mid-band -> LLM band (1) ---
    ("fuzzy_mid_band", "Meridian Point Capital", "no_connection", None, None),

    # --- Pass 2: fuzzy, clean reject (2) ---
    ("fuzzy_reject_gibberish", "Zzyxquint Nonexistent Holdings", "no_connection", None, None),
    ("fuzzy_reject_unrelated", "Completely Unrelated Startup Inc", "no_connection", None, None),

    # --- Acronym pass: hit (2) ---
    ("acronym_hit_plain", "SCP", "matched", "llm", "Singh Capital Partners"),
    ("acronym_hit_dotted", "S.C.P.", "matched", "llm", "Singh Capital Partners"),

    # --- Acronym pass: documented miss (intra-word acronym) (1) ---
    ("acronym_miss_intraword", "KBIM", "no_connection", None, None),
]

assert len(CASES) == 27, f"expected 27 adversarial cases, found {len(CASES)}"


@pytest.mark.parametrize("case_id,target,expected_outcome,expected_method,expected_company",
                        CASES, ids=[c[0] for c in CASES])
def test_case(case_id, target, expected_outcome, expected_method, expected_company):
    result = match_firms([target], CORPUS_NAMES, llm_batch_fn=fake_llm_batch)[0]
    assert result.outcome == expected_outcome, (
        f"{case_id}: expected outcome {expected_outcome!r}, got {result.outcome!r} "
        f"(method={result.match_method}, company={result.matched_company}, reason={result.reason})"
    )
    if expected_method is not None:
        assert result.match_method == expected_method, (
            f"{case_id}: expected method {expected_method!r}, got {result.match_method!r}"
        )
    if expected_company is not None:
        assert result.matched_company == expected_company, (
            f"{case_id}: expected company {expected_company!r}, got {result.matched_company!r}"
        )


def test_pass_rate_and_zero_false_positives():
    """Spec acceptance bar: 25/27+ correct, and NEVER a false-positive
    auto-match (a 'matched' outcome pointing at the wrong company)."""
    targets = [c[1] for c in CASES]
    results = match_firms(targets, CORPUS_NAMES, llm_batch_fn=fake_llm_batch)
    by_target = {t: r for t, r in zip(targets, results)}

    passed = 0
    false_positives = []
    for case_id, target, expected_outcome, expected_method, expected_company in CASES:
        r = by_target[target]
        ok = r.outcome == expected_outcome
        if ok and expected_method is not None:
            ok = r.match_method == expected_method
        if ok and expected_company is not None:
            ok = r.matched_company == expected_company
        if ok:
            passed += 1
        # A false positive: matcher says "matched" to a DIFFERENT company than
        # the one the case says is correct (or matched when it should not have).
        if r.outcome == "matched":
            if expected_outcome != "matched" or (
                expected_company is not None and r.matched_company != expected_company
            ):
                false_positives.append((case_id, target, r.matched_company))

    assert not false_positives, f"false-positive auto-match(es): {false_positives}"
    assert passed >= 25, f"only {passed}/27 adversarial cases passed (need 25+)"


def test_ambiguous_monarch_lists_both_alternatives():
    """Edge case (§4): one target matching multiple different real firms must
    be flagged ambiguous with BOTH candidates listed, never silently picking one."""
    result = match_firms(["Monarch"], CORPUS_NAMES, llm_batch_fn=fake_llm_batch)[0]
    assert result.outcome == "parked_ambiguous"
    names = {a["name"] for a in result.alternatives}
    assert names == {"Monarch Realty Partners", "Monarch Capital Advisors"}


# ===========================================================================
# QA Part 2 — adversarial cases against the REAL data/connections.csv corpus
# (~23,000 distinct companies). Unlike the 27-case suite above (a frozen,
# hand-built sample), these test real abbreviation/short-name/acronym
# interactions that only exist in the actual file — the whole point per the
# QA brief: "matching correctness IS testable [against real data] here."
#
# NOTE ON NORMALIZATION FIX: exploring these cases surfaced a real bug —
# normalize() converted periods to spaces, so "J.P. Morgan" tokenized as
# ["j","p","morgan"] while "JP Morgan" tokenized as ["jp","morgan"]: two
# extremely common spellings of the same real institution landed on
# incompatible token sets and never matched via 1a/1b. Fixed in
# src/matching/normalize.py (periods/apostrophes now collapse with no space,
# instead of becoming a space) — see the comment on _COLLAPSE_RE there.
# ===========================================================================
import pytest as _pytest  # noqa: E402

from src import dbc as _dbc  # noqa: E402
from src.loader import reload_connections as _reload_connections  # noqa: E402


@_pytest.fixture(scope="module")
def real_corpus_names() -> list[str]:
    names = _dbc.distinct_company_names()
    if len(names) < 1000:  # connections table not loaded yet in this DB
        _reload_connections()
        names = _dbc.distinct_company_names()
    assert len(names) > 20_000, (
        f"expected ~22-23k distinct real companies, found {len(names)} — "
        f"is data/connections.csv the real export?"
    )
    return names


def _spy_llm_batch(calls: list):
    def _fn(batch: list[dict]) -> list[dict]:
        calls.append(batch)
        return []
    return _fn


def test_real_jp_morgan_abbreviation_resolves_deterministically(real_corpus_names):
    """'JP Morgan' (no punctuation) must resolve to the real corpus's bare
    'J.P. Morgan' entry — a pure punctuation/spacing variant, not a real
    ambiguity — WITHOUT ever reaching the LLM band. This is exactly the
    normalize() bug above: before the fix this landed in the LLM band."""
    calls: list = []
    result = match_firms(["JP Morgan"], real_corpus_names, llm_batch_fn=_spy_llm_batch(calls))[0]
    assert result.outcome == "matched"
    assert result.matched_company == "J.P. Morgan"
    assert result.match_method in ("exact", "stripped")
    assert not calls, "a pure punctuation variant should never reach the LLM band"


def test_real_jp_morgan_divisions_never_cross_merge(real_corpus_names):
    """Distinct real J.P. Morgan divisions (Asset Management vs Private Bank)
    must each resolve to THEMSELVES, never to a different division — the
    anti-merge guarantee holding even among near-identical, closely-related
    real corpus entries, not just contrived synthetic traps."""
    targets = ["JP Morgan Asset Management", "JP Morgan Private Bank"]
    results = match_firms(targets, real_corpus_names, llm_batch_fn=_spy_llm_batch([]))
    by_target = dict(zip(targets, results))
    assert by_target["JP Morgan Asset Management"].outcome == "matched"
    assert "Asset Management" in by_target["JP Morgan Asset Management"].matched_company
    assert by_target["JP Morgan Private Bank"].outcome == "matched"
    assert "Private Bank" in by_target["JP Morgan Private Bank"].matched_company
    assert (by_target["JP Morgan Asset Management"].matched_company
           != by_target["JP Morgan Private Bank"].matched_company)


def test_real_madison_cap_abbreviation_escalates_to_llm(real_corpus_names):
    """'Madison Cap' ('Cap' for 'Capital' — a real human abbreviation, not a
    punctuation variant) is genuinely ambiguous to the deterministic passes
    (my normalizer has no synonym table for 'Cap' == 'Capital') and correctly
    escalates to the LLM band with the real candidate pinned, rather than
    either auto-matching on a guess or silently dropping it."""
    def llm(batch):
        for item in batch:
            if item["target"] == "Madison Cap":
                assert "Madison Capital Group Holdings, LLC" in item["candidates"], (
                    "the real candidate must be pinned into the LLM band, or the LLM "
                    "has no way to find it (same failure mode SCP/Singh Capital Partners "
                    "closes for acronyms)"
                )
                return [{"target": "Madison Cap", "matched_to": "Madison Capital Group Holdings, LLC",
                        "confidence": 0.9}]
        return []

    result = match_firms(["Madison Cap"], real_corpus_names, llm_batch_fn=llm)[0]
    assert result.outcome == "matched"
    assert result.matched_company == "Madison Capital Group Holdings, LLC"
    assert result.match_method == "llm"


def test_real_monarch_alt_cap_anti_merge_trap(real_corpus_names):
    """The real corpus does NOT contain 'Monarch Alternative Capital' (the
    actual RE/PE firm) — only four unrelated real 'Monarch'-branded entities
    (Holdings, International, Magazine, Talent Partners). A target abbreviated
    as 'Monarch Alt Cap' must NOT be forced onto any of them; the one
    plausible-looking candidate (Monarch Holdings, sharing just the bare
    token) may be pinned to the LLM band, but a correct LLM (simulated here)
    rejects it and the firm reports no_connection — it must never silently
    become 'Monarch International' or 'Monarch Magazine' either."""
    def llm(batch):
        return [{"target": "Monarch Alt Cap", "matched_to": "Monarch Holdings", "confidence": 0.15}]

    result = match_firms(["Monarch Alt Cap"], real_corpus_names, llm_batch_fn=llm)[0]
    assert result.outcome == "no_connection"
    assert result.matched_company is None


def test_real_kbim_intraword_acronym_documented_miss(real_corpus_names):
    """Intra-word acronym ('KBIM' has no real corpus entry it should honestly
    resolve to) stays a documented miss against the real corpus too, not just
    the synthetic 27-case sample — it must never coincidentally auto-match
    something in the much larger real candidate pool."""
    def llm(batch):
        return [{"target": "KBIM", "matched_to": c, "confidence": 0.1}
               for c in (batch[0]["candidates"] if batch else [])]

    result = match_firms(["KBIM"], real_corpus_names, llm_batch_fn=llm)[0]
    assert result.outcome == "no_connection"


def test_real_free_filter_zero_llm_calls_for_deterministic_firms(real_corpus_names):
    """§4: 'firm-matching against the connections table is a local lookup
    with zero API cost' for anything that resolves deterministically. Exact
    matches and Guard-0 degenerate input must never trigger the LLM stub —
    asserted with a spy, not just inferred from the outcome."""
    calls: list = []
    targets = ["Blackstone", "Kingbird Investment Management", "Capital Partners", ""]
    results = match_firms(targets, real_corpus_names, llm_batch_fn=_spy_llm_batch(calls))
    assert results[0].outcome == "matched" and results[0].matched_company == "Blackstone"
    assert results[1].outcome == "matched" and results[1].matched_company == "Kingbird Investment Management"
    assert results[2].outcome == "degenerate_name"
    assert results[3].outcome == "degenerate_name"
    assert not calls, (
        "deterministic-tier firms (exact matches, degenerate input) must never "
        f"invoke the LLM batch function; it was called {len(calls)} time(s)"
    )


def test_real_corpus_no_connection_can_legitimately_reach_llm_band(real_corpus_names):
    """Documents (does not fight) an accepted spec behavior: against a large
    real corpus, an invented firm name can still coincidentally score >=55 on
    fuzzy similarity against SOME real company and reach the LLM band —
    '"Sunrise Dental Group" may still reach the LLM band via coincidental
    similarity — accepted, it costs 1/20th of a batched call' (§4). The
    guarantee is only that it never auto-matches, not that it never phones
    the LLM."""
    result = match_firms(["Totally Nonexistent Firm XYZ"], real_corpus_names,
                         llm_batch_fn=lambda batch: [])[0]
    assert result.outcome == "no_connection"
    assert result.matched_company is None

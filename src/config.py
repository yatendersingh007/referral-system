"""Workflow C config: extends the shared lib_snapshot.config with C-only settings.

Re-exports everything from lib_snapshot.config (LLM/Tavily/Exa/Apollo/DB/Gmail
settings, PROMPTS_DIR, TEMPLATES_DIR, AUM_MIN_USD/AUM_MAX_USD, load_prompt,
missing_keys) so callers only ever import `src.config`. Never fork a constant
that already lives in lib_snapshot.
"""
from __future__ import annotations


from lib_snapshot.config import *  # noqa: F401,F403 - deliberate re-export
from lib_snapshot.config import _get, ROOT, PROMPTS_DIR, TEMPLATES_DIR  # noqa: F401

# --- Test mode ---------------------------------------------------------------
# HARD constraint: all external calls (Gmail, Apollo, Tavily, Exa, LLM) must be
# mockable via TEST_MODE=true in .env. Every phase module checks this flag
# before making a network call.
TEST_MODE = (_get("TEST_MODE", "false") or "false").strip().lower() in ("1", "true", "yes")

# Diagnosis tooling (FIX 4, real-mode research retrieval review): when set,
# research_and_gate() replays Phase 2 extraction from the cached raw research
# payload at qa_out/debug/research_payload_<Firm>.json instead of making live
# Tavily calls -- lets a human iterate on extraction-prompt/retrieval logic
# for a specific firm without re-spending search budget each time. Has no
# effect in TEST_MODE (which never reaches real research at all).
REPLAY_RESEARCH_FROM_CACHE = (_get("REPLAY_RESEARCH_FROM_CACHE", "false") or "false").strip().lower() in ("1", "true", "yes")

# --- Telegram (Workflow C bot; separate token from Workflow A's bot) --------
TELEGRAM_BOT_TOKEN_C = _get("TELEGRAM_BOT_TOKEN_C")

# Operator allowlist. NEVER hardcode a single user ID in source — always read
# the full comma-separated list from .env. Adding/removing an operator = edit
# .env + restart (§10d).
AUTHORIZED_USER_IDS = [
    x.strip() for x in (_get("AUTHORIZED_USER_IDS", "") or "").split(",") if x.strip()
]

REQUIRED_FOR_BOT_C = ["TELEGRAM_BOT_TOKEN_C", "DATABASE_URL"]

# --- Inputs (§3) --------------------------------------------------------------
CONNECTIONS_CSV = ROOT / "data" / "connections.csv"
REFERRAL_EMAIL_TEMPLATE = TEMPLATES_DIR / "referral_email.md"
PITCH_MASTER_PPTX = TEMPLATES_DIR / "Pitch_Master.pptx"
PITCH_SLOTS_JSON = TEMPLATES_DIR / "pitch_slots.json"
ICP_PROMPT_NAME = "icp_scoring_v2_2.md"

GENERATED_DECKS_DIR = ROOT / "generated_decks_c"
SOFFICE_SCRIPT = ROOT / "scripts" / "office" / "soffice.py"

# --- HARD RULE 11: never target the referrer's own firm ---------------------
REFERRER_FIRMS = {"singh capital partners", "singh capital"}

# FIX 32A Part B: X Analyst is the SENDER of every outreach email -- a
# distinct concept from REFERRER_FIRMS above (Manpreet's firm, whose network
# is being mined). Conflating the two would make the skip message factually
# wrong ("matches referrer's own firm") and make a future referrer change
# ambiguous with a sender change. A real 15-firm batch let "X Analyst"
# through the free filter, ran a full paid research pass, and was only
# disqualified downstream on firm type -- had it qualified, the pipeline
# would have drafted an email pitching X Analyst to X Analyst.
SENDER_FIRMS = {"x analyst", "xanalyst"}

# --- HARD RULE 12 / §4: batch mode chunking ----------------------------------
BATCH_CHUNK_SIZE = 25

# --- HARD RULE 9: per-firm budget guards -------------------------------------
MAX_SEARCH_CALLS_PER_FIRM = 12
MAX_LLM_TOKENS_PER_FIRM = 25_000

# --- HARD RULE 15: circuit breaker -------------------------------------------
CIRCUIT_BREAKER_CONSECUTIVE_FAILURES = 3
QUOTA_RESUME_PROBE_HOURS = 6

# --- Email guard (referral_email.md generation rules) ------------------------
# QA finding (real-mode Kairos Real Estate Partners run -- the first real-mode
# firm in this engagement to ever reach Phase 5a at all): 230 was
# mathematically unreachable. The fixed blocks (1/3/5/6/7) alone total 204
# words once assembled (not the ~160 the template's own generation-rules
# section estimates), and Block 2's 15-30 + Block 4's 30-50 word ranges push
# the floor to 249 before generation even starts -- already documented as a
# known gap in HANDOVER.md before this session. Raised to 260 to match
# reality (HANDOVER.md's own prior recommendation), rather than continuing to
# ship every real email over a cap it structurally cannot meet.
EMAIL_MAX_WORDS = 260

# --- Matching (§4 Phase 1) ----------------------------------------------------
FUZZY_AUTOMATCH_SCORE = 85
FUZZY_LLM_BAND_FLOOR = 55
LLM_MATCH_CONFIDENCE_MATCH = 0.8
LLM_MATCH_CONFIDENCE_PARK = 0.5
RARE_TOKEN_CORPUS_FREQ = 2
LLM_MATCH_BATCH_SIZE = 18   # 15-20 firms per batched call

SUFFIX_TOKENS = {
    "capital", "partners", "group", "llc", "lp", "l.p.", "inc", "incorporated",
    "corp", "corporation", "company", "co", "holdings", "management",
    "investments", "investment", "properties", "property", "realty", "the",
    "ltd", "limited", "advisors", "advisers", "associates", "fund", "funds",
    "ventures", "venture", "real", "estate",
}


def missing_keys_c(required: list[str]) -> list[str]:
    """Like lib_snapshot.config.missing_keys but also checks values defined
    only in this module (e.g. TELEGRAM_BOT_TOKEN_C)."""
    out = []
    for name in required:
        if not globals().get(name):
            out.append(name)
    return out

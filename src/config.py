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

# --- HARD RULE 12 / §4: batch mode chunking ----------------------------------
BATCH_CHUNK_SIZE = 25

# --- HARD RULE 9: per-firm budget guards -------------------------------------
MAX_SEARCH_CALLS_PER_FIRM = 12
MAX_LLM_TOKENS_PER_FIRM = 25_000

# --- HARD RULE 15: circuit breaker -------------------------------------------
CIRCUIT_BREAKER_CONSECUTIVE_FAILURES = 3
QUOTA_RESUME_PROBE_HOURS = 6

# --- Email guard (referral_email.md generation rules) ------------------------
EMAIL_MAX_WORDS = 230

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

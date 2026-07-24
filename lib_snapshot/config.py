"""Central config: env loading, constants, caps, paths.

All secrets come from .env (never committed). Provider switching (DeepSeek
direct <-> Azure AI Foundry <-> OpenRouter) is a single LLM_BASE_URL edit;
no code change anywhere else.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# Project root = parent of src/
ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")


def _get(name: str, default: str | None = None) -> str | None:
    val = os.environ.get(name, default)
    if val is not None:
        val = val.strip()
    return val or default


# --- LLM (OpenAI-compatible) -------------------------------------------------
LLM_BASE_URL = _get("LLM_BASE_URL", "https://api.deepseek.com")
LLM_API_KEY = _get("LLM_API_KEY")
DEFAULT_MODEL = _get("DEFAULT_MODEL", "deepseek-v4-flash")   # research/gate/classify
PREMIUM_MODEL = _get("PREMIUM_MODEL", "deepseek-v4-pro")     # final drafts only

# Azure AI Foundry requires an ?api-version= query param; DeepSeek-direct / Groq
# do not. Env-overridable, and auto-defaults for Azure endpoints so provider
# switching stays a .env-only change.
LLM_API_VERSION = _get("LLM_API_VERSION")
if not LLM_API_VERSION and "azure.com" in (LLM_BASE_URL or ""):
    LLM_API_VERSION = "2024-05-01-preview"

# Optional SECONDARY endpoint: DEFAULT_MODEL and PREMIUM_MODEL may live on
# different Azure resources (e.g. gpt-5-mini on an Azure OpenAI resource, DeepSeek
# on a Foundry model-catalog resource). When SECONDARY_LLM_BASE_URL is set, the
# DEFAULT_MODEL routes there; PREMIUM_MODEL stays on the primary. If unset, both
# use the primary endpoint.
SECONDARY_LLM_BASE_URL = _get("SECONDARY_LLM_BASE_URL")
SECONDARY_LLM_API_KEY = _get("SECONDARY_LLM_API_KEY")
SECONDARY_LLM_API_VERSION = _get("SECONDARY_LLM_API_VERSION")
if (not SECONDARY_LLM_API_VERSION and SECONDARY_LLM_BASE_URL
        and "azure.com" in SECONDARY_LLM_BASE_URL):
    SECONDARY_LLM_API_VERSION = "2024-05-01-preview"

PRIMARY_ENDPOINT = {
    "base_url": LLM_BASE_URL, "api_key": LLM_API_KEY, "api_version": LLM_API_VERSION,
}
SECONDARY_ENDPOINT = (
    {"base_url": SECONDARY_LLM_BASE_URL,
     "api_key": SECONDARY_LLM_API_KEY or LLM_API_KEY,
     "api_version": SECONDARY_LLM_API_VERSION}
    if SECONDARY_LLM_BASE_URL else None
)
# Per-model endpoint routing. Callers pass a model name; models.py resolves the
# endpoint from this map (unknown models fall back to the primary).
MODEL_ENDPOINTS = {
    DEFAULT_MODEL: SECONDARY_ENDPOINT or PRIMARY_ENDPOINT,
    PREMIUM_MODEL: PRIMARY_ENDPOINT,
}

# Rough price table ($ per 1M tokens) for cost visibility only. Approximate;
# used for /status spend reporting, not billing. Unknown models -> 0.
MODEL_PRICES = {
    "deepseek-v4-flash": {"in": 0.14, "out": 0.28},
    "deepseek-v4-pro":   {"in": 0.55, "out": 2.19},
    "deepseek-chat":     {"in": 0.14, "out": 0.28},
    "deepseek-reasoner": {"in": 0.55, "out": 2.19},
    # Groq (testing)
    "llama-3.1-8b-instant":    {"in": 0.05, "out": 0.08},
    "llama-3.3-70b-versatile": {"in": 0.59, "out": 0.79},
    # Azure (current) - approximate, for /status visibility only
    "gpt-5-mini":              {"in": 0.25, "out": 2.00},
    "DeepSeek-V3.2-Speciale":  {"in": 0.55, "out": 2.19},
}

# --- Search / enrichment -----------------------------------------------------
TAVILY_API_KEY = _get("TAVILY_API_KEY")       # per-firm research (company_research)
EXA_API_KEY = _get("EXA_API_KEY")             # competitor discovery (semantic search)
APOLLO_API_KEY = _get("APOLLO_API_KEY")

# Workflow C Feature A: a pool of Tavily keys for long batch runs that outlast
# one free-tier key's monthly quota. TAVILY_API_KEYS is a comma-separated list
# ("key1,key2,key3"); backward compatible -- if unset, the single TAVILY_API_KEY
# above is treated as a one-key pool. Built generically (a plain ordered list)
# so another provider (e.g. Exa) could adopt the same pattern later.
_TAVILY_API_KEYS_RAW = _get("TAVILY_API_KEYS")


def _parse_tavily_keys(raw: str | None, single: str | None) -> list[str]:
    if raw:
        return [k.strip() for k in raw.split(",") if k.strip()]
    return [single] if single else []


TAVILY_API_KEYS = _parse_tavily_keys(_TAVILY_API_KEYS_RAW, TAVILY_API_KEY)


def reload_tavily_keys() -> list[str]:
    """Re-reads .env from DISK (not just the process's cached os.environ) and
    recomputes TAVILY_API_KEY/TAVILY_API_KEYS. Lets `/referral resume` pick up
    keys the operator added to .env while a run was paused for exhaustion,
    without a full bot process restart."""
    global TAVILY_API_KEY, TAVILY_API_KEYS
    load_dotenv(ROOT / ".env", override=True)
    TAVILY_API_KEY = _get("TAVILY_API_KEY")
    TAVILY_API_KEYS = _parse_tavily_keys(_get("TAVILY_API_KEYS"), TAVILY_API_KEY)
    return TAVILY_API_KEYS

# --- Database ----------------------------------------------------------------
DATABASE_URL = _get("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/outreach")

# --- Telegram ----------------------------------------------------------------
TELEGRAM_BOT_TOKEN = _get("TELEGRAM_BOT_TOKEN")
TELEGRAM_USER_ID = _get("TELEGRAM_USER_ID")

# --- Gmail -------------------------------------------------------------------
def _anchor(path: str) -> str:
    """Resolve a path relative to the project ROOT (not the process CWD), so a
    VPS service started from any directory still finds credentials/token."""
    p = Path(path)
    return str(p if p.is_absolute() else (ROOT / p))


GMAIL_CREDENTIALS_PATH = _anchor(_get("GMAIL_CREDENTIALS_PATH", "credentials.json"))
GMAIL_TOKEN_PATH = _anchor(_get("GMAIL_TOKEN_PATH", "token.json"))
# HARD RULE: compose only. Never gmail.send.
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.compose"]

# --- Paths -------------------------------------------------------------------
PROMPTS_DIR = ROOT / "prompts"
TEMPLATES_DIR = ROOT / "templates"
ASSETS_DIR = ROOT / "assets"
CASE_STUDY_PATH = ASSETS_DIR / "case_study.pdf"
SCHEMA_PATH = ROOT / "schema.sql"
MIGRATIONS_DIR = ROOT / "migrations"
DECK_BUILDER_JS = ROOT / "deck_gen" / "build_deck.js"   # node pptxgenjs renderer
DECKS_DIR = ROOT / "generated_decks"                    # per-firm deck cache

# --- ICP firm gate (binary) --------------------------------------------------
AUM_MIN_USD = 200_000_000     # below -> DISQUALIFIED
AUM_MAX_USD = 2_000_000_000   # above -> DISQUALIFIED
NORTH_AMERICA = {"USA", "United States", "Canada", "Mexico", "US", "U.S.", "North America"}

# --- Competitor discovery ----------------------------------------------------
MAX_COMPETITORS = int(_get("MAX_COMPETITORS", "100"))
# Hard safety valve above; soft review cap below. Discovery runs exhaustively
# but presents at most this many candidates for confirmation (keeps the list
# reviewable; the drop is logged, never silent). Raise to see more.
DISCOVERY_REVIEW_MAX = int(_get("DISCOVERY_REVIEW_MAX", "50"))
# Stop when this many consecutive iterations add zero new competitors.
DISCOVERY_DRY_ITERATIONS = 2

# --- Research ----------------------------------------------------------------
RESEARCH_FRESH_DAYS = 30       # reuse company research newer than this
# Worker pool for per-firm processing. Env-driven: Azure Foundry's 20K TPM limit
# needs RESEARCH_WORKERS=1 (sequential); higher tiers can raise it.
RESEARCH_WORKERS = int(_get("RESEARCH_WORKERS", "5"))
TARGET_CONTACTS_PER_FIRM = (2, 4)   # find 2-4 management contacts

# Titles that qualify as "higher-level management" per the ICP prompt.
MGMT_TITLE_HINTS = [
    "chief financial officer", "cfo",
    "vp finance", "vice president of finance", "vice president, finance",
    "finance director", "director of finance", "head of finance",
    "controller", "comptroller",
    "managing director", "managing partner",
    "head of operations", "coo", "chief operating officer",
    "vp of operations",
]

PROJECT_NAME = "X Analyst Outreach"


def load_prompt(name: str) -> str:
    """Read a prompt file from prompts/. Raises FileNotFoundError if missing."""
    return (PROMPTS_DIR / name).read_text(encoding="utf-8")


def resolve_case_study() -> Path | None:
    """Return the case study PDF path, or None if absent.

    Prefers assets/case_study.pdf; otherwise falls back to the single/newest PDF
    in assets/ so an owner-provided file with a different name is still found.
    Absence is handled gracefully by the drafter (attach skipped with a warning).
    """
    if CASE_STUDY_PATH.exists():
        return CASE_STUDY_PATH
    if ASSETS_DIR.exists():
        pdfs = sorted(ASSETS_DIR.glob("*.pdf"), key=lambda p: p.stat().st_mtime, reverse=True)
        if pdfs:
            return pdfs[0]
    return None


def missing_keys(required: list[str]) -> list[str]:
    """Return the names among `required` whose config value is empty."""
    out = []
    for name in required:
        if not globals().get(name):
            out.append(name)
    return out


# Config groups each entrypoint validates before running.
REQUIRED_FOR_PROSPECT = ["LLM_API_KEY", "TAVILY_API_KEY", "DATABASE_URL"]
REQUIRED_FOR_ENRICH = ["APOLLO_API_KEY"]
REQUIRED_FOR_GMAIL = ["GMAIL_CREDENTIALS_PATH"]
REQUIRED_FOR_BOT = ["TELEGRAM_BOT_TOKEN", "TELEGRAM_USER_ID", "DATABASE_URL"]
# Full env surface the bot verifies at startup (missing any -> refuse to start).
REQUIRED_FOR_STARTUP = [
    "DATABASE_URL", "LLM_BASE_URL", "LLM_API_KEY", "DEFAULT_MODEL", "PREMIUM_MODEL",
    "TAVILY_API_KEY", "EXA_API_KEY", "APOLLO_API_KEY", "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_USER_ID", "GMAIL_CREDENTIALS_PATH", "GMAIL_TOKEN_PATH",
]

# --- Reliability limits (Fix 7 / Fix 8) --------------------------------------
FIRM_TIMEOUT_SECONDS = int(_get("FIRM_TIMEOUT_SECONDS", "90"))
RUN_TIMEOUT_MINUTES = int(_get("RUN_TIMEOUT_MINUTES", "30"))
AZURE_CREDITS_END = _get("AZURE_CREDITS_END")   # e.g. 2026-10-31; None disables the warning

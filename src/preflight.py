"""HARD RULE 16 — preflight before every run, two classes:

(a) Hard blockers, abort immediately, no retry: missing env key, missing
    templates/referral_email.md, master PPTX absent/corrupt, any §9
    slot-locator string not found exactly once in its slide XML. Deterministic
    failures don't fix themselves.
(b) Transient checks, retry then decide: LLM ping and search/Apollo
    reachability fire up to 3 attempts, 10s timeout, 5s gap between attempts.
    One healthy pass is enough; 3 failures aborts naming the service.

§10b: the transient search check must verify RESULT CONTENT, not just HTTP
200 — a quota-exhausted Tavily returns 200 with zero results, indistinguishable
from "nothing found" unless the response is inspected.
"""
from __future__ import annotations

import time
from typing import Callable

from . import config

Progress = Callable[[str], None]


def _noop(_: str) -> None:
    pass


class PreflightFailed(RuntimeError):
    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("; ".join(problems))


# ---------------------------------------------------------------------------
# (a) Hard blockers
# ---------------------------------------------------------------------------
def check_hard_blockers() -> list[str]:
    problems: list[str] = []

    required_keys = ["DATABASE_URL"]
    if not config.TEST_MODE:
        required_keys += ["LLM_API_KEY", "TAVILY_API_KEY", "APOLLO_API_KEY"]
    # missing_keys_c checks the src.config module namespace, which already
    # includes every lib_snapshot.config name via `from ... import *`.
    missing = config.missing_keys_c(required_keys)
    if missing:
        problems.append(f"missing env key(s): {missing}")

    if not config.REFERRAL_EMAIL_TEMPLATE.exists():
        problems.append(f"templates/referral_email.md not found at {config.REFERRAL_EMAIL_TEMPLATE} "
                        f"— refusing to start, there is nothing to draft")

    # PPT is optional at the run level (§3 graceful degradation: email-only if
    # missing) — but if it's PRESENT, its slot locators must all resolve, or
    # every draft this run would silently fail Phase 5b later.
    if config.PITCH_MASTER_PPTX.exists():
        from . import deck_filler

        slot_problems = deck_filler.preflight_slots()
        problems.extend(slot_problems)

    if not config.CONNECTIONS_CSV.exists():
        problems.append(f"data/connections.csv not found at {config.CONNECTIONS_CSV}")

    gmail_token_missing = not config.GMAIL_TOKEN_PATH or not __import__("os").path.exists(config.GMAIL_TOKEN_PATH)
    if not config.TEST_MODE and gmail_token_missing:
        problems.append("Gmail token.json missing (hard blocker — class (a); a failed "
                        "refresh at runtime is class (b) instead)")

    return problems


# ---------------------------------------------------------------------------
# (b) Transient checks — retry up to 3x, 10s timeout, 5s gap
# ---------------------------------------------------------------------------
def _retry_probe(name: str, fn: Callable[[], bool], attempts: int = 3,
                 gap_seconds: int = 5) -> tuple[bool, str]:
    last_err = ""
    for i in range(attempts):
        try:
            if fn():
                return True, ""
        except Exception as e:  # noqa: BLE001
            last_err = str(e)
        if i < attempts - 1:
            time.sleep(gap_seconds)
    return False, last_err


def _llm_ping() -> bool:
    from lib_snapshot import models

    return bool(models.ping())


def _search_ping() -> bool:
    """§10b: a known-good query must return >=1 result with non-empty
    content. A 200 with zero results is a FAIL, not a pass."""
    from lib_snapshot.researcher import search_client

    results = search_client.search("Blackstone Group AUM", max_results=3)
    return any((r.get("content") or "").strip() for r in results)


def _apollo_ping() -> bool:
    from lib_snapshot.enrichment import apollo

    return apollo.healthcheck()


def check_transient(progress: Progress = _noop) -> list[str]:
    if config.TEST_MODE:
        progress("TEST_MODE: skipping live LLM/search/Apollo reachability probes.")
        return []
    problems = []
    for name, fn in (("LLM", _llm_ping), ("Tavily", _search_ping), ("Apollo", _apollo_ping)):
        ok, err = _retry_probe(name, fn)
        if not ok:
            problems.append(f"{name} unreachable after 3 attempts: {err}")
        else:
            progress(f"preflight: {name} OK")
    return problems


def run_preflight(progress: Progress = _noop) -> None:
    """Raises PreflightFailed if anything blocks the run. Hard blockers are
    checked first (cheap, deterministic); transient checks only run if those
    pass (no point pinging a dead LLM if the template is missing anyway)."""
    hard = check_hard_blockers()
    if hard:
        raise PreflightFailed(hard)
    transient = check_transient(progress)
    if transient:
        raise PreflightFailed(transient)

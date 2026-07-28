"""FIX 26 — test/production database separation.

The test suite and production runs previously shared ONE Postgres database
(`DATABASE_URL` from .env, the same `outreach` database Workflow A and
Workflow C both write to). `scripts/reset_testmode.py`'s autouse fixture
truncates wc_runs/wc_firms/wc_queue/wc_drafts/contacted_pocs before every
test file — against the SAME tables a real batch run's data lives in. A
full suite run silently wiped a real 13-firm batch's records; sharing
tables across test/production also let one test's leftover firm/run rows
leak into a completely different test's assertions.

This module is the ONE place that decides "what database do tests use,
and is it safe" — imported by tests/conftest.py (to redirect DATABASE_URL
before ANY test module or src.config/lib_snapshot.config is ever imported)
and by scripts/reset_testmode.py (as an independent, standalone-safe
defense: reset_testmode must refuse to run against production even if
invoked directly, `python scripts/reset_testmode.py`, completely outside
pytest and conftest.py's own env-var trick).

Deliberately does NOT touch src/dbc.py or lib_snapshot/db.py — no public
API changes anywhere. The redirection works entirely by setting the
DATABASE_URL environment variable before those modules' own module-level
`config.DATABASE_URL = _get("DATABASE_URL", ...)` assignment runs (same
mechanism tests/conftest.py already uses for TEST_MODE, and the exact
reason this must happen at import time, not inside a fixture).
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import psycopg
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
_ENV_PATH = ROOT / ".env"

_DEFAULT_PROD_URL = "postgresql://postgres:postgres@localhost:5432/outreach"


_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost", ""}


def _normalize_host(host: Optional[str]) -> str:
    """Case-folds and collapses loopback-equivalent spellings (127.0.0.1,
    ::1, an absent host -- libpq's default Unix-socket connection -- and
    the literal "localhost") to one canonical value. Adversarial review
    (FIX 26 follow-up) found the raw string comparison judged
    'localhost:5432/outreach' and '127.0.0.1:5432/outreach' -- the SAME
    physical server reachable by this repo's own .env -- as two different
    database identities, so assert_not_production() would silently pass
    for a TEST_DATABASE_URL that actually points at production, just
    spelled differently. Does NOT resolve arbitrary DNS names to IPs --
    only closes the loopback/case gap that was concretely reproduced
    against this guard, not general DNS-alias equivalence (a real
    non-loopback hostname pointing at the same box some other way is a
    different, unsolved problem this string-level guard was never meant
    to cover)."""
    h = (host or "").strip().lower()
    return "localhost" if h in _LOOPBACK_HOSTS else h


def _db_identity(url: str) -> tuple[str, str, str]:
    """(host, port, dbname) — the three fields that determine whether two
    connection strings point at the same physical database. Deliberately
    ignores user/password (a test DB legitimately might, or might not,
    use different credentials; that's not what makes it "the same
    database")."""
    info = psycopg.conninfo.conninfo_to_dict(url)
    return (_normalize_host(info.get("host")), str(info.get("port") or "5432"), info.get("dbname") or "")


def production_database_url() -> str:
    """The database `.env` designates as production, read directly from
    the FILE on disk (via dotenv_values, never os.environ) — independent
    of whatever this process's environment has already been redirected to.
    This is what makes reset_testmode.py's guard meaningful even when
    invoked standalone, with no conftest.py override in effect at all:
    it's always comparing against .env's own on-disk truth, not against
    a value some other code path may have already overwritten."""
    raw = dotenv_values(_ENV_PATH) if _ENV_PATH.exists() else {}
    return (raw.get("DATABASE_URL") or _DEFAULT_PROD_URL).strip()


def derive_test_database_url(prod_url: Optional[str] = None) -> str:
    """TEST_DATABASE_URL (env var) if explicitly set -- an operator's own
    choice always wins. Otherwise: the production URL with "_test"
    appended to the database name (same host/port/credentials, a
    DIFFERENT database) -- no manual provisioning needed, since
    lib_snapshot.db.init_db()/dbc.apply_migrations() create and migrate
    whatever database DATABASE_URL resolves to on first use."""
    explicit = os.environ.get("TEST_DATABASE_URL")
    if explicit:
        return explicit.strip()
    prod_url = prod_url or production_database_url()
    info = psycopg.conninfo.conninfo_to_dict(prod_url)
    dbname = info.get("dbname") or "outreach"
    if not dbname.endswith("_test"):
        info["dbname"] = f"{dbname}_test"
    return psycopg.conninfo.make_conninfo(**info)


def assert_not_production(candidate_url: str, prod_url: Optional[str] = None) -> None:
    """Refuses (raises AssertionError) if `candidate_url` resolves to the
    SAME database as .env's own production DATABASE_URL. This is the
    "can't silently regress" guard: however the test database URL was
    computed (derived automatically, or an operator-set TEST_DATABASE_URL
    that happens to be misconfigured), if it ever turns out to BE
    production, nothing that calls this ever proceeds."""
    prod_url = prod_url or production_database_url()
    prod_identity = _db_identity(prod_url)
    candidate_identity = _db_identity(candidate_url)
    assert candidate_identity != prod_identity, (
        f"REFUSING TO RUN: the test database ({candidate_identity}) is the SAME "
        f"database .env designates as production ({prod_identity}). This would "
        f"let reset_testmode.py (or any other test-suite table truncation) "
        f"destroy real production data. Set TEST_DATABASE_URL to a genuinely "
        f"separate database, or check DATABASE_URL / TEST_DATABASE_URL."
    )


def active_test_database_url() -> str:
    """Computes the test database URL and asserts it's safe. Does NOT set
    os.environ itself (see apply_test_database_url for that) -- split out
    so reset_testmode.py can call the guard without needing to redirect
    anything (by the time it runs, if it's running under pytest,
    conftest.py's own override has already redirected DATABASE_URL; if
    invoked standalone, this function computes independently what
    "the test database" WOULD be and asserts it's not production, giving
    reset_testmode.py an unconditional, invocation-order-independent
    check)."""
    test_url = derive_test_database_url()
    assert_not_production(test_url)
    return test_url


def apply_test_database_url() -> str:
    """Computes, asserts-safe, and sets os.environ["DATABASE_URL"] to the
    test database URL. MUST be called before src.config / lib_snapshot.
    config is ever imported (see tests/conftest.py) -- a plain module-level
    call, never a fixture (same reasoning as conftest.py's own TEST_MODE
    line: by the time any fixture runs, config.DATABASE_URL has already
    been baked in as a fixed string)."""
    test_url = active_test_database_url()
    os.environ["DATABASE_URL"] = test_url
    return test_url


def assert_active_database_is_not_production() -> None:
    """Defense-in-depth, distinct from apply_test_database_url()'s own
    guard: checks whatever DATABASE_URL lib_snapshot.config has ALREADY
    resolved to and cached (the value dbc.conn()/dbc.cursor() actually use)
    -- not a freshly re-derived theoretical value -- against .env's own
    on-disk production URL. This is what would catch a redirection that
    never actually took effect (e.g. a future change to import order
    breaking the "redirect before config is imported" requirement): since
    lib_snapshot.config.DATABASE_URL is a plain string baked in once at
    import time, re-deriving "what the test URL should be" from scratch
    (as apply_test_database_url()/active_test_database_url() do) would
    still look correct even if the ACTUAL connection pool is pointed at
    production -- only checking the real, already-resolved value catches
    that."""
    from lib_snapshot import config as shared_config
    assert_not_production(shared_config.DATABASE_URL)

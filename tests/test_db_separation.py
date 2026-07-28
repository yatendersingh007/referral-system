"""FIX 26 — tests for scripts/test_db.py's test/production database
separation and guard logic.

These are pure string/URL-derivation tests (no real Postgres connection
needed for any of them) -- they verify the GUARD itself, independent of
whatever database conftest.py has already redirected this pytest process
to. Confirms: the guard actually refuses when a candidate URL resolves to
production; automatic `_test`-suffix derivation behaves correctly across
input shapes; TEST_DATABASE_URL (when set) always wins; user/password
differences are correctly ignored for "same database" comparisons; and
reset_testmode.reset() still refuses under TEST_MODE=false (pre-existing
behavior, unchanged) as well as when pointed at production.
"""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

from scripts import test_db

ROOT = test_db.ROOT


# ---------------------------------------------------------------------------
# _db_identity
# ---------------------------------------------------------------------------

def test_db_identity_ignores_user_and_password():
    a = test_db._db_identity("postgresql://alice:secret1@localhost:5432/outreach")
    b = test_db._db_identity("postgresql://bob:secret2@localhost:5432/outreach")
    assert a == b


def test_db_identity_distinguishes_different_dbnames():
    a = test_db._db_identity("postgresql://postgres:postgres@localhost:5432/outreach")
    b = test_db._db_identity("postgresql://postgres:postgres@localhost:5432/outreach_test")
    assert a != b


def test_db_identity_distinguishes_different_hosts_and_ports():
    base = test_db._db_identity("postgresql://postgres:postgres@localhost:5432/outreach")
    other_host = test_db._db_identity("postgresql://postgres:postgres@db.example.com:5432/outreach")
    other_port = test_db._db_identity("postgresql://postgres:postgres@localhost:5433/outreach")
    assert base != other_host
    assert base != other_port


def test_db_identity_treats_loopback_spellings_as_the_same_host():
    """Adversarial review finding: a raw string comparison judged
    'localhost' and '127.0.0.1' -- the SAME physical server reachable by
    this repo's own .env -- as different identities, so a misconfigured
    TEST_DATABASE_URL using the "other" loopback spelling would silently
    pass the production guard."""
    localhost = test_db._db_identity("postgresql://postgres:postgres@localhost:5432/outreach")
    loopback_v4 = test_db._db_identity("postgresql://postgres:postgres@127.0.0.1:5432/outreach")
    loopback_v6 = test_db._db_identity("postgresql://postgres:postgres@[::1]:5432/outreach")
    no_host = test_db._db_identity("postgresql:///outreach?port=5432")
    assert localhost == loopback_v4 == loopback_v6
    assert localhost[0] == no_host[0] == "localhost"


def test_db_identity_case_folds_the_host():
    lower = test_db._db_identity("postgresql://postgres:postgres@localhost:5432/outreach")
    upper = test_db._db_identity("postgresql://postgres:postgres@LocalHost:5432/outreach")
    assert lower == upper


def test_db_identity_still_distinguishes_a_genuinely_different_non_loopback_host():
    """The loopback normalization must not become so broad it collapses
    two real, different remote hosts together."""
    a = test_db._db_identity("postgresql://postgres:postgres@db-a.example.com:5432/outreach")
    b = test_db._db_identity("postgresql://postgres:postgres@db-b.example.com:5432/outreach")
    assert a != b


# ---------------------------------------------------------------------------
# production_database_url
# ---------------------------------------------------------------------------

def test_production_database_url_reads_env_file_directly(tmp_path, monkeypatch):
    """Reads from the FILE on disk (dotenv_values), not from os.environ --
    proven by setting a DIFFERENT DATABASE_URL in os.environ and confirming
    it's ignored."""
    env_file = tmp_path / ".env"
    env_file.write_text("DATABASE_URL=postgresql://postgres:postgres@localhost:5432/prod_from_file\n")
    monkeypatch.setattr(test_db, "_ENV_PATH", env_file)
    monkeypatch.setenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/from_environ")

    assert test_db.production_database_url() == "postgresql://postgres:postgres@localhost:5432/prod_from_file"


def test_production_database_url_falls_back_to_default_when_env_file_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(test_db, "_ENV_PATH", tmp_path / "does_not_exist.env")
    assert test_db.production_database_url() == test_db._DEFAULT_PROD_URL


def test_production_database_url_falls_back_to_default_when_key_absent(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("SOME_OTHER_KEY=value\n")
    monkeypatch.setattr(test_db, "_ENV_PATH", env_file)
    assert test_db.production_database_url() == test_db._DEFAULT_PROD_URL


# ---------------------------------------------------------------------------
# derive_test_database_url
# ---------------------------------------------------------------------------

def test_derive_test_database_url_appends_test_suffix_by_default(monkeypatch):
    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)
    derived = test_db.derive_test_database_url("postgresql://postgres:postgres@localhost:5432/outreach")
    assert test_db._db_identity(derived) == ("localhost", "5432", "outreach_test")


def test_derive_test_database_url_does_not_double_suffix(monkeypatch):
    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)
    derived = test_db.derive_test_database_url("postgresql://postgres:postgres@localhost:5432/outreach_test")
    assert test_db._db_identity(derived) == ("localhost", "5432", "outreach_test")


def test_derive_test_database_url_prefers_explicit_env_override(monkeypatch):
    monkeypatch.setenv("TEST_DATABASE_URL", "postgresql://someone:pw@otherhost:5433/custom_test_db")
    derived = test_db.derive_test_database_url("postgresql://postgres:postgres@localhost:5432/outreach")
    assert derived == "postgresql://someone:pw@otherhost:5433/custom_test_db"


def test_derive_test_database_url_preserves_host_port_credentials(monkeypatch):
    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)
    derived = test_db.derive_test_database_url("postgresql://myuser:mypw@db.internal:5544/outreach")
    identity = test_db._db_identity(derived)
    assert identity == ("db.internal", "5544", "outreach_test")


# ---------------------------------------------------------------------------
# assert_not_production / active_test_database_url
# ---------------------------------------------------------------------------

def test_assert_not_production_passes_for_genuinely_different_database():
    test_db.assert_not_production(
        "postgresql://postgres:postgres@localhost:5432/outreach_test",
        prod_url="postgresql://postgres:postgres@localhost:5432/outreach",
    )


def test_assert_not_production_refuses_when_candidate_equals_production():
    with pytest.raises(AssertionError, match="REFUSING TO RUN"):
        test_db.assert_not_production(
            "postgresql://postgres:postgres@localhost:5432/outreach",
            prod_url="postgresql://postgres:postgres@localhost:5432/outreach",
        )


def test_assert_not_production_refuses_even_with_different_credentials():
    """A misconfigured TEST_DATABASE_URL pointed at the production dbname
    (just with different creds) must still be caught -- credentials are not
    what makes two URLs "the same database"."""
    with pytest.raises(AssertionError, match="REFUSING TO RUN"):
        test_db.assert_not_production(
            "postgresql://someoneelse:otherpw@localhost:5432/outreach",
            prod_url="postgresql://postgres:postgres@localhost:5432/outreach",
        )


def test_active_test_database_url_refuses_when_misconfigured_to_equal_production(monkeypatch):
    """The exact "can't silently regress" scenario the user asked for: an
    operator sets TEST_DATABASE_URL to production by mistake."""
    monkeypatch.setenv("TEST_DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/outreach")
    monkeypatch.setattr(test_db, "production_database_url",
                        lambda: "postgresql://postgres:postgres@localhost:5432/outreach")
    with pytest.raises(AssertionError, match="REFUSING TO RUN"):
        test_db.active_test_database_url()


def test_active_test_database_url_succeeds_for_a_genuinely_separate_database(monkeypatch):
    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)
    monkeypatch.setattr(test_db, "production_database_url",
                        lambda: "postgresql://postgres:postgres@localhost:5432/outreach")
    result = test_db.active_test_database_url()
    assert test_db._db_identity(result) == ("localhost", "5432", "outreach_test")


# ---------------------------------------------------------------------------
# assert_active_database_is_not_production (checks the REAL, already-
# resolved lib_snapshot.config.DATABASE_URL, not a theoretical re-derivation)
# ---------------------------------------------------------------------------

def test_assert_active_database_is_not_production_passes_for_the_actual_test_db():
    """By the time any test runs, conftest.py has already redirected
    lib_snapshot.config.DATABASE_URL to the test database -- this must not
    raise."""
    test_db.assert_active_database_is_not_production()


def test_assert_active_database_is_not_production_catches_a_broken_redirect(monkeypatch):
    """Simulates the exact regression this function exists to catch: some
    future change breaks the "redirect before config is imported"
    requirement, and lib_snapshot.config.DATABASE_URL ends up equal to
    production despite apply_test_database_url() having been called."""
    from lib_snapshot import config as shared_config

    monkeypatch.setattr(shared_config, "DATABASE_URL", test_db.production_database_url())
    with pytest.raises(AssertionError, match="REFUSING TO RUN"):
        test_db.assert_active_database_is_not_production()


# ---------------------------------------------------------------------------
# reset_testmode.reset() guards
# ---------------------------------------------------------------------------

def test_reset_testmode_refuses_when_test_mode_false(monkeypatch):
    from scripts import reset_testmode
    from src import config

    monkeypatch.setattr(config, "TEST_MODE", False)
    with pytest.raises(AssertionError, match="TEST_MODE"):
        reset_testmode.reset(verbose=False)


def test_reset_testmode_refuses_when_active_database_is_production(monkeypatch):
    from scripts import reset_testmode
    from lib_snapshot import config as shared_config

    monkeypatch.setattr(shared_config, "DATABASE_URL", test_db.production_database_url())
    with pytest.raises(AssertionError, match="REFUSING TO RUN"):
        reset_testmode.reset(verbose=False)


# ---------------------------------------------------------------------------
# Fresh-process (subprocess) proof -- adversarial review finding: every test
# above monkeypatches state WITHIN this already-running, already-redirected
# pytest process, which can never actually exercise "does a brand new process
# refuse", the literal thing tests/conftest.py's module-level redirect and
# scripts/reset_testmode.py's standalone self-redirect both exist to
# guarantee. These two tests spawn a REAL separate `python` process with
# TEST_DATABASE_URL forced to equal production, and confirm it refuses.
# ---------------------------------------------------------------------------

def _env_pointing_test_database_at_production() -> dict:
    env = dict(os.environ)
    env["TEST_DATABASE_URL"] = test_db.production_database_url()
    return env


def test_conftest_import_refuses_in_a_fresh_process_when_pointed_at_production():
    """Simulates a misconfigured TEST_DATABASE_URL in a genuinely separate
    interpreter -- pytest always imports every conftest.py before
    collecting any test, so a fresh process failing to import
    tests/conftest.py IS "the suite refuses to run", not a proxy for it."""
    result = subprocess.run(
        [sys.executable, "-c", "import tests.conftest"],
        cwd=str(ROOT), env=_env_pointing_test_database_at_production(),
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode != 0, (
        f"expected a fresh process to refuse; stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "REFUSING TO RUN" in result.stderr


def test_reset_testmode_standalone_subprocess_refuses_when_pointed_at_production():
    """`python scripts/reset_testmode.py` with no pytest, no conftest.py,
    completely standalone -- the exact invocation the module's own
    docstring calls out as needing its own independent redirect."""
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "reset_testmode.py")],
        cwd=str(ROOT), env=_env_pointing_test_database_at_production(),
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode != 0, (
        f"expected the standalone script to refuse; stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "REFUSING TO RUN" in result.stderr
    assert "[reset] cleared" not in result.stdout, "must never reach the DELETE loop"

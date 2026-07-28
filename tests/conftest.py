"""Session-wide TEST_MODE enforcement, and FIX 26's test/production database
separation.

QA finding: nothing in this codebase ever wrote TEST_MODE into the .env
file — the "something" flipping it back to true across sessions was a
human/agent manually editing .env, treating a deliberately-set
TEST_MODE=false as an anomaly to correct. That's backwards: real-mode work
depends on .env staying whatever a human last set it to. The test suite
must not need .env to say anything in particular at all.

Setting the environment variable here, before any test module (and
therefore before src.config / lib_snapshot.config) is ever imported, is what
makes it take effect: lib_snapshot.config's load_dotenv() call defaults to
override=False, so an env var already set here is never clobbered by
.env's own on-disk value. The .env file's TEST_MODE stays exactly whatever
a human set it to, regardless of what the test suite needs.

This MUST stay a plain module-level statement, not a fixture: pytest
imports every conftest.py in a test directory before importing any test
module in that directory or its subdirectories, which is what guarantees
this line runs before src.config's own module-level TEST_MODE assignment.
A fixture (even autouse, even session-scoped) runs too late — by the time
any fixture executes, every test module has already been collected and
imported, and src.config.TEST_MODE has already been baked in as a fixed
boolean.

FIX 26 (real-mode incident): the test suite and production runs shared ONE
Postgres database (DATABASE_URL, the same `outreach` database Workflow A
and Workflow C both write to) — scripts/reset_testmode.py's autouse
fixture truncates wc_runs/wc_firms/wc_queue/wc_drafts/contacted_pocs
before every test file, which is exactly where a real batch run's data
also lives. A full suite run silently wiped a real 13-firm batch's
records; sharing tables across test/production also let one test's
leftover firm/run rows leak into a different test's assertions. Redirects
DATABASE_URL to a genuinely separate database (scripts/test_db.py) via the
SAME "set the env var before anything imports config" mechanism as
TEST_MODE above, for the same reason (a fixture runs too late). The
derived test database is created and migrated fresh (see the session
fixture below) — never a manual provisioning step."""
import os

os.environ["TEST_MODE"] = "true"

from scripts.test_db import apply_test_database_url  # noqa: E402

apply_test_database_url()

import pytest  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _bootstrap_test_database():
    """Creates the test database if it doesn't exist yet and brings its
    schema up to date -- once per test session, never a manual
    provisioning step. dbc.apply_migrations() (a pre-existing PUBLIC dbc.py
    function, unchanged) calls lib_snapshot.db.migrate(), which itself
    calls _ensure_database() (CREATEs the database if missing) and then
    applies every un-applied migrations/*.sql file -- non-destructive/
    idempotent by its own design. migrations/002_workflow_c.sql alone
    creates every table this repo's own code or tests ever touch
    (connections, wc_runs, wc_queue, wc_firms, wc_drafts, contacted_pocs);
    lib_snapshot.db.init_db()/schema.sql (Workflow A's OWN foundational
    schema, a different codebase entirely) is deliberately NOT called here
    -- schema.sql isn't even present in this repo (this is a snapshot of
    shared code, not the original project), and nothing in src/ or tests/
    references a Workflow-A-only table (companies, leads, prospect_runs,
    ...), so it was never needed. By this point DATABASE_URL has already
    been redirected to the test database (module-level, above) -- this
    call can never reach production.

    FIX 26 follow-up (adversarial review finding): re-asserts
    assert_active_database_is_not_production() here, as the very first
    thing this fixture does. This fixture is the actual FIRST database
    touch of any pytest session and is session-scoped autouse -- runs
    exactly once, before ANY test in ANY file, and a failure here aborts
    collection for the whole session. That makes THIS the literal
    "refuses to run the suite if the target DB is production" gate,
    rather than a per-test-function consequence of whichever test files
    happen to route through scripts/reset_testmode.py's own autouse
    fixture. The review found a real gap this closes: tests/test_ingestion.py
    performs real destructive writes (reload_connections() -> DELETE FROM
    connections) via its own fixture, with no reset_testmode() involved at
    all -- it had no independent production guard on its own path. Now
    every test file's writes are gated on this session-level check having
    already passed, regardless of whether that file calls reset_testmode()
    too."""
    from scripts.test_db import assert_active_database_is_not_production
    from src import dbc

    assert_active_database_is_not_production()
    dbc.apply_migrations()

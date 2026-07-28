"""Reset helper for QA/test runs. Truncates ONLY Workflow C's own tables, in
FK-safe order (children before parents), plus the shared `contacted_pocs`
table (safe to clear in TEST_MODE — it only ever holds TEST_MODE-fixture
draft records from prior QA runs, never real Workflow A data, since this
script only ever runs against the separate test database — see FIX 26
below).

NEVER touches connections (ground-truth mirror of the CSV — reload via
src.loader if you need a fresh copy) or any Workflow A table (companies,
leads, lead_events, prospect_runs, client_profiles, outreach_templates,
system_flags, schema_migrations).

FIX 26 (real-mode incident): a full test-suite run silently wiped a real
13-firm production batch's wc_runs/wc_firms/wc_queue/wc_drafts rows —
tests and production shared ONE Postgres database, and the pre-existing
`assert config.TEST_MODE` guard only checks operator INTENT, never which
DATABASE is actually being written to. Redirects DATABASE_URL to the
separate test database (scripts/test_db.py) BEFORE `src.config`/`dbc` are
ever imported below — this makes `reset()` incapable of reaching
production regardless of how this module is invoked: under pytest,
tests/conftest.py has already redirected DATABASE_URL by the time any test
imports this module (this call just recomputes the identical value, a
harmless no-op); run standalone (`python scripts/reset_testmode.py`,
completely outside pytest, no conftest.py involved at all), THIS is the
only thing that redirects it. `reset()` itself ALSO re-asserts the guard
immediately before deleting anything, as defense-in-depth against a future
refactor changing this import order.

Run: python scripts/reset_testmode.py
Importable: from scripts.reset_testmode import reset; reset()
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.test_db import apply_test_database_url, assert_active_database_is_not_production  # noqa: E402

apply_test_database_url()

from src import config, dbc  # noqa: E402

# Child -> parent order (FK-safe).
_TABLES_FK_ORDER = ["wc_drafts", "wc_queue", "wc_firms", "wc_runs", "contacted_pocs"]


def reset(verbose: bool = True) -> dict:
    assert config.TEST_MODE, (
        "reset_testmode.py refuses to run with TEST_MODE=false — this truncates "
        "wc_drafts/wc_queue/wc_firms/wc_runs/contacted_pocs, which would destroy "
        "real production data."
    )
    # FIX 26: defense-in-depth -- checks the REAL, already-resolved
    # DATABASE_URL dbc.conn() is about to use (not a freshly re-derived
    # theoretical value) immediately before deleting anything.
    assert_active_database_is_not_production()
    counts = {}
    with dbc.conn() as c:
        with c.cursor() as cur:
            for table in _TABLES_FK_ORDER:
                cur.execute(f"SELECT count(*) AS n FROM {table}")
                counts[table] = cur.fetchone()["n"]
                cur.execute(f"DELETE FROM {table}")
        c.commit()
    if verbose:
        for table, n in counts.items():
            print(f"[reset] cleared {table} ({n} row(s))")
    return counts


if __name__ == "__main__":
    reset()

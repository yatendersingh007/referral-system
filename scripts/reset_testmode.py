"""Reset helper for QA/test runs. Truncates ONLY Workflow C's own tables, in
FK-safe order (children before parents), plus the shared `contacted_pocs`
table (safe to clear in TEST_MODE — it only ever holds TEST_MODE-fixture
draft records from prior QA runs, never real Workflow A data, since this
script is never run against a non-TEST_MODE database).

NEVER touches connections (ground-truth mirror of the CSV — reload via
src.loader if you need a fresh copy) or any Workflow A table (companies,
leads, lead_events, prospect_runs, client_profiles, outreach_templates,
system_flags, schema_migrations).

Run: python scripts/reset_testmode.py
Importable: from scripts.reset_testmode import reset; reset()
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src import config, dbc  # noqa: E402

# Child -> parent order (FK-safe).
_TABLES_FK_ORDER = ["wc_drafts", "wc_queue", "wc_firms", "wc_runs", "contacted_pocs"]


def reset(verbose: bool = True) -> dict:
    assert config.TEST_MODE, (
        "reset_testmode.py refuses to run with TEST_MODE=false — this truncates "
        "wc_drafts/wc_queue/wc_firms/wc_runs/contacted_pocs, which would destroy "
        "real production data."
    )
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

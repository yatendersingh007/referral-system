"""Postgres access: connection pool, queue helpers, state transitions.

Postgres is the queue (SELECT ... FOR UPDATE SKIP LOCKED where needed), the
state store, and the audit log. No Redis, no vector DB.

`python -m src.db init`  - create the database if missing and apply schema.sql
                           ONLY on an empty DB. NON-DESTRUCTIVE: if tables
                           already exist it refuses (schema.sql is destructive:
                           it DROPs every table). Safe to re-run.
`python -m src.db reset` - EXPLICITLY drop and recreate everything (data loss).
`python -m src.db migrate` - apply un-applied migrations/*.sql (non-destructive).
"""
from __future__ import annotations

import json
import time
from contextlib import contextmanager
from typing import Any, Iterator, Optional

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from . import config

_pool: Optional[ConnectionPool] = None


def get_pool() -> ConnectionPool:
    """Lazily create the pool so importing this module never needs a live DB."""
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            conninfo=config.DATABASE_URL,
            min_size=1,
            max_size=config.RESEARCH_WORKERS + 3,
            kwargs={"row_factory": dict_row},
            open=True,
            timeout=10,
        )
    return _pool


@contextmanager
def conn() -> Iterator[psycopg.Connection]:
    """Borrow a pooled connection. Commits on success, rolls back on error."""
    pool = get_pool()
    with pool.connection() as c:
        yield c


@contextmanager
def cursor() -> Iterator[psycopg.Cursor]:
    with conn() as c:
        with c.cursor() as cur:
            yield cur


def _jsonb(value: Any) -> Any:
    return psycopg.types.json.Jsonb(value) if value is not None else None


# ---------------------------------------------------------------------------
# Schema bootstrap
# ---------------------------------------------------------------------------
def _ensure_database() -> None:
    """Create the target database if it does not exist."""
    info = psycopg.conninfo.conninfo_to_dict(config.DATABASE_URL)
    dbname = info.get("dbname", "outreach")
    admin_info = dict(info)
    admin_info["dbname"] = "postgres"
    admin_dsn = psycopg.conninfo.make_conninfo(**admin_info)
    with psycopg.connect(admin_dsn, autocommit=True) as c:
        with c.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (dbname,))
            if cur.fetchone() is None:
                cur.execute(f'CREATE DATABASE "{dbname}"')
                print(f"Created database {dbname!r}")
            else:
                print(f"Database {dbname!r} already exists")


def _apply_schema() -> None:
    sql = config.SCHEMA_PATH.read_text(encoding="utf-8")
    with psycopg.connect(config.DATABASE_URL) as c:
        with c.cursor() as cur:
            cur.execute(sql)
        c.commit()
    print("Schema applied.")


def init_db() -> None:
    """Create the DB if absent and apply schema.sql ONLY when the DB is empty.

    NON-DESTRUCTIVE: schema.sql begins with DROP TABLE ... CASCADE, so it would
    wipe data if re-applied. We therefore refuse to re-apply when core tables
    already exist. Use reset_db() to intentionally drop and recreate.
    """
    _ensure_database()
    with psycopg.connect(config.DATABASE_URL) as c:
        with c.cursor() as cur:
            cur.execute("SELECT to_regclass('public.leads')")
            exists = cur.fetchone()[0] is not None
    if exists:
        print("Schema already present. Not re-applying (schema.sql is destructive). "
              "Use `python -m src.db reset` to drop and recreate (DATA LOSS).")
        return
    _apply_schema()


def reset_db() -> None:
    """EXPLICITLY drop and recreate the entire schema. Destroys all data."""
    _ensure_database()
    print("RESET: dropping and recreating all tables (data loss).")
    _apply_schema()


def migrate() -> None:
    """Apply any un-applied migrations/*.sql (tracked in schema_migrations).

    Non-destructive: migration files use CREATE TABLE IF NOT EXISTS and
    idempotent seeds. Never touches schema.sql. Safe to re-run."""
    _ensure_database()
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("CREATE TABLE IF NOT EXISTS schema_migrations "
                        "(filename TEXT PRIMARY KEY, applied_at TIMESTAMPTZ DEFAULT now())")
            cur.execute("SELECT filename FROM schema_migrations")
            applied = {r["filename"] for r in cur.fetchall()}
        c.commit()
    mig_dir = config.MIGRATIONS_DIR
    files = sorted(mig_dir.glob("*.sql")) if mig_dir.exists() else []
    ran = 0
    for path in files:
        if path.name in applied:
            continue
        sql = path.read_text(encoding="utf-8")
        with conn() as c:
            with c.cursor() as cur:
                cur.execute(sql)
                cur.execute("INSERT INTO schema_migrations (filename) VALUES (%s)", (path.name,))
            c.commit()
        print(f"applied migration {path.name}")
        ran += 1
    print("no un-applied migrations" if ran == 0 else f"applied {ran} migration(s)")


# ---------------------------------------------------------------------------
# Client profiles (multi-client)
# ---------------------------------------------------------------------------
def get_client_profile(client_name: str) -> Optional[dict]:
    with cursor() as cur:
        cur.execute("SELECT * FROM client_profiles WHERE lower(client_name)=lower(%s)",
                    (client_name,))
        return cur.fetchone()


def all_client_profiles() -> list[dict]:
    with cursor() as cur:
        cur.execute("SELECT * FROM client_profiles ORDER BY client_name")
        return cur.fetchall()


def upsert_client_profile(client_name: str, **fields) -> dict:
    """Insert or update a client profile by name. Only provided fields change."""
    cols = ["client_name"]
    vals: list[Any] = [client_name]
    for k, v in fields.items():
        cols.append(k)
        vals.append(_jsonb(v) if isinstance(v, (dict, list)) else v)
    placeholders = ", ".join(["%s"] * len(cols))
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols if c != "client_name")
    updates = (updates + ", updated_at = now()") if updates else "updated_at = now()"
    sql = (f"INSERT INTO client_profiles ({', '.join(cols)}) VALUES ({placeholders}) "
           f"ON CONFLICT (client_name) DO UPDATE SET {updates} RETURNING *")
    with conn() as c:
        with c.cursor() as cur:
            cur.execute(sql, vals)
            row = cur.fetchone()
        c.commit()
    return row


# ---------------------------------------------------------------------------
# Events / state transitions
# ---------------------------------------------------------------------------
def log_event(cur, lead_id: int, from_state: Optional[str], to_state: str,
              agent: str, detail: Optional[dict] = None) -> None:
    cur.execute(
        """INSERT INTO lead_events (lead_id, from_state, to_state, agent, detail)
           VALUES (%s, %s, %s, %s, %s)""",
        (lead_id, from_state, to_state, agent, _jsonb(detail)),
    )


def transition_lead(lead_id: int, to_state: str, agent: str,
                    detail: Optional[dict] = None, **fields) -> None:
    """Move a lead to a new state, log the transition, and set extra columns."""
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("SELECT state FROM leads WHERE id = %s", (lead_id,))
            row = cur.fetchone()
            from_state = row["state"] if row else None
            sets = ["state = %s"]
            vals: list[Any] = [to_state]
            for k, v in fields.items():
                sets.append(f"{k} = %s")
                vals.append(_jsonb(v) if isinstance(v, (dict, list)) else v)
            vals.append(lead_id)
            cur.execute(f"UPDATE leads SET {', '.join(sets)} WHERE id = %s", vals)
            log_event(cur, lead_id, from_state, to_state, agent, detail)
        c.commit()


def record_operator_event(to_state: str, detail: Optional[dict] = None,
                          lead_id: Optional[int] = None) -> None:
    """Log a human OPERATOR action (e.g. /qualify, /disqualify) to the events
    table. Company-level actions pass lead_id=None (lead_events.lead_id is
    nullable); agent is always 'operator'. `to_state` must be a valid lead_state
    enum value ('qualified' / 'disqualified' / 'parked')."""
    with conn() as c:
        with c.cursor() as cur:
            log_event(cur, lead_id, None, to_state, "operator", detail)
        c.commit()


# ---------------------------------------------------------------------------
# Companies
# ---------------------------------------------------------------------------
def get_fresh_company(name: str, days: int = config.RESEARCH_FRESH_DAYS) -> Optional[dict]:
    """Return an existing company row if it was researched within `days`."""
    with cursor() as cur:
        cur.execute(
            """SELECT * FROM companies
               WHERE name = %s
                 AND last_researched_at IS NOT NULL
                 AND last_researched_at > now() - (%s || ' days')::interval""",
            (name, str(days)),
        )
        return cur.fetchone()


def get_company_by_name(name: str, ci: bool = False) -> Optional[dict]:
    """Look up a company by name. With ci=True the match is case-insensitive
    (used by the operator /qualify + /disqualify commands, which take a
    human-typed firm name)."""
    with cursor() as cur:
        if ci:
            cur.execute(
                "SELECT * FROM companies WHERE lower(name) = lower(%s) ORDER BY id LIMIT 1",
                (name,),
            )
        else:
            cur.execute("SELECT * FROM companies WHERE name = %s", (name,))
        return cur.fetchone()


def get_company_by_id(company_id: int) -> Optional[dict]:
    with cursor() as cur:
        cur.execute("SELECT * FROM companies WHERE id = %s", (company_id,))
        return cur.fetchone()


def upsert_company(name: str, **fields) -> dict:
    """Insert or update a company by unique name. Returns the full row."""
    cols = ["name"]
    vals: list[Any] = [name]
    for k, v in fields.items():
        cols.append(k)
        vals.append(_jsonb(v) if isinstance(v, (dict, list)) else v)
    placeholders = ", ".join(["%s"] * len(cols))
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols if c != "name")
    sql = f"""INSERT INTO companies ({', '.join(cols)}) VALUES ({placeholders})
              ON CONFLICT (name) DO UPDATE SET {updates}
              RETURNING *"""
    with conn() as c:
        with c.cursor() as cur:
            cur.execute(sql, vals)
            row = cur.fetchone()
        c.commit()
    return row


_AUM_TAGS = {"AUM_CONFIRMED", "AUM_ESTIMATED", "AUM_UNVERIFIED"}


def set_company_icp(company_id: int, icp_result: str, icp_reason: str,
                    aum_tag: Optional[str] = None) -> None:
    """Persist the firm-level gate result. firm_type / aum_value stay in
    research_raw (no dedicated columns in the Workflow-A schema)."""
    if aum_tag not in _AUM_TAGS:
        aum_tag = None  # enum column: only accept valid labels
    with conn() as c:
        with c.cursor() as cur:
            cur.execute(
                "UPDATE companies SET icp_result=%s, icp_reason=%s, aum_tag=%s WHERE id=%s",
                (icp_result, icp_reason, aum_tag, company_id),
            )
        c.commit()


def companies_for_client(client_name: str) -> list[dict]:
    with cursor() as cur:
        cur.execute(
            "SELECT * FROM companies WHERE client_name = %s ORDER BY id",
            (client_name,),
        )
        return cur.fetchall()


# ---------------------------------------------------------------------------
# Leads
# ---------------------------------------------------------------------------
def create_lead(company_id: int, full_name: str, company: str, **fields) -> Optional[dict]:
    """Create a lead. Returns None on duplicate (same full_name at same company,
    enforced by the unique_person constraint). Logs the 'new' transition."""
    cols = ["company_id", "full_name", "company"]
    vals: list[Any] = [company_id, full_name, company]
    for k, v in fields.items():
        cols.append(k)
        vals.append(_jsonb(v) if isinstance(v, (dict, list)) else v)
    placeholders = ", ".join(["%s"] * len(cols))
    with conn() as c:
        with c.cursor() as cur:
            cur.execute(
                f"INSERT INTO leads ({', '.join(cols)}) VALUES ({placeholders}) "
                f"ON CONFLICT (full_name, company) DO NOTHING RETURNING *",
                vals,
            )
            row = cur.fetchone()
            if row:
                log_event(cur, row["id"], None, row["state"], "researcher",
                          {"created": True})
        c.commit()
    return row


def set_lead_email(lead_id: int, email: str, confidence: Optional[str]) -> None:
    with conn() as c:
        with c.cursor() as cur:
            cur.execute(
                "UPDATE leads SET email=%s, email_confidence=%s, enriched_at=now() WHERE id=%s",
                (email, confidence, lead_id),
            )
        c.commit()


def leads_for_company(company_id: int) -> list[dict]:
    with cursor() as cur:
        cur.execute("SELECT * FROM leads WHERE company_id = %s ORDER BY id", (company_id,))
        return cur.fetchall()


def lead_history(lead_id: int) -> list[dict]:
    with cursor() as cur:
        cur.execute(
            "SELECT * FROM lead_events WHERE lead_id = %s ORDER BY id", (lead_id,)
        )
        return cur.fetchall()


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------
def load_templates(channel: str) -> list[dict]:
    with cursor() as cur:
        cur.execute(
            "SELECT * FROM outreach_templates WHERE channel = %s ORDER BY id",
            (channel,),
        )
        return cur.fetchall()


def parse_template(text: str) -> tuple[Optional[str], str]:
    """Split a template file into (subject, body).

    Supported conventions:
      - explicit 'Subject: ...' on the first non-empty line
      - first line = subject, a blank line, then the body
    Otherwise the whole file is the body (subject None).
    """
    lines = text.replace("\r\n", "\n").split("\n")
    # drop leading blank lines
    while lines and not lines[0].strip():
        lines.pop(0)
    if not lines:
        return None, ""
    first = lines[0].strip()
    if first.lower().startswith("subject:"):
        subject = first.split(":", 1)[1].strip()
        body = "\n".join(lines[1:]).strip()
        return (subject or None), body
    # first line followed by a blank line => treat first line as subject
    if len(lines) >= 2 and not lines[1].strip():
        subject = first
        body = "\n".join(lines[2:]).strip()
        return (subject or None), body
    return None, text.strip()


def sync_templates_from_folder() -> int:
    """Load templates/*.txt|*.md into outreach_templates (idempotent).

    Workflow A is Gmail only, so every template is channel 'gmail_draft'. Files
    may carry a subject on the first line (see parse_template). templates/ empty
    is a clean no-op.
    """
    folder = config.TEMPLATES_DIR
    if not folder.exists():
        return 0
    loaded = 0
    for path in sorted(folder.glob("*")):
        if path.suffix.lower() not in (".txt", ".md"):
            continue
        raw = path.read_text(encoding="utf-8", errors="ignore")
        subject, body = parse_template(raw)
        if not body:
            continue
        upsert_template("gmail_draft", path.stem, body, subject=subject,
                        notes=f"loaded from {path.name}")
        loaded += 1
    return loaded


def upsert_template(channel: str, name: str, body: str,
                    subject: Optional[str] = None, notes: Optional[str] = None) -> None:
    """Idempotent by template_name via delete-then-insert (the Workflow-A schema
    has no unique constraint on template_name)."""
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("DELETE FROM outreach_templates WHERE template_name = %s", (name,))
            cur.execute(
                """INSERT INTO outreach_templates (channel, template_name, subject, body, context_notes)
                   VALUES (%s,%s,%s,%s,%s)""",
                (channel, name, subject, body, notes),
            )
        c.commit()


# ---------------------------------------------------------------------------
# System flags
# ---------------------------------------------------------------------------
def get_flag(key: str) -> Optional[str]:
    with cursor() as cur:
        cur.execute("SELECT value FROM system_flags WHERE key = %s", (key,))
        row = cur.fetchone()
        return row["value"] if row else None


def flag_age_seconds(key: str) -> Optional[float]:
    """Seconds since a system_flags row was last updated, or None if absent.
    Used to detect stale/stuck state (e.g. an orphaned active_prospect flag)."""
    with cursor() as cur:
        cur.execute(
            "SELECT extract(epoch FROM (now() - updated_at)) AS age "
            "FROM system_flags WHERE key = %s",
            (key,),
        )
        row = cur.fetchone()
    return float(row["age"]) if row and row["age"] is not None else None


def set_flag(key: str, value: str) -> None:
    with conn() as c:
        with c.cursor() as cur:
            cur.execute(
                """INSERT INTO system_flags (key, value, updated_at)
                   VALUES (%s,%s, now())
                   ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=now()""",
                (key, value),
            )
        c.commit()


# ---------------------------------------------------------------------------
# Single-poller lock (DB heartbeat). Guarantees exactly one Telegram poller.
#
# Telegram's getUpdates 409 goes to the DISPLACED poller, not the newcomer, so a
# 409-at-startup check alone cannot stop a second instance. This lock is the
# authoritative mechanism: a running poller writes {pid, ts} and refreshes it
# each loop; a new instance refuses while the lock is fresh.
# ---------------------------------------------------------------------------
POLLER_KEY = "poller_lock"
POLLER_TTL_SEC = 90        # a lock older than this is considered stale/dead


def acquire_poller_lock(pid: int) -> tuple[bool, Optional[dict]]:
    """Atomically claim the poller lock. Returns (acquired, current_holder).

    Claims if the lock is free, stale (> POLLER_TTL_SEC), or already ours.
    """
    now = time.time()
    val = json.dumps({"pid": pid, "ts": now})
    stale_before = now - POLLER_TTL_SEC
    with conn() as c:
        with c.cursor() as cur:
            cur.execute(
                """INSERT INTO system_flags (key, value, updated_at)
                   VALUES (%s, %s, now())
                   ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=now()
                   WHERE (system_flags.value::json->>'ts')::float < %s
                      OR (system_flags.value::json->>'pid') = %s
                   RETURNING value""",
                (POLLER_KEY, val, stale_before, str(pid)),
            )
            row = cur.fetchone()
        c.commit()
    if row:
        return True, None
    with cursor() as cur:
        cur.execute("SELECT value FROM system_flags WHERE key=%s", (POLLER_KEY,))
        r = cur.fetchone()
    holder = {}
    if r:
        try:
            holder = json.loads(r["value"])
        except (ValueError, TypeError):
            holder = {}
    return False, holder


def heartbeat_poller_lock(pid: int) -> None:
    """Refresh our lock timestamp. Called each poll iteration."""
    set_flag(POLLER_KEY, json.dumps({"pid": pid, "ts": time.time()}))


def release_poller_lock(pid: int) -> None:
    """Release the lock only if we still hold it (best-effort)."""
    with conn() as c:
        with c.cursor() as cur:
            cur.execute(
                "DELETE FROM system_flags WHERE key=%s AND (value::json->>'pid')=%s",
                (POLLER_KEY, str(pid)),
            )
        c.commit()


# ---------------------------------------------------------------------------
# Active-prospect flag. Locks the WHOLE /prospect flow (discovery through
# processing, including the confirmation wait) so a second /prospect "<client>"
# is rejected. Set when discovery starts; cleared only when processing finishes
# (done) or the flow fails. /prospect confirm|trim continue the active run and
# do not touch this flag. /status and /requalify ignore it.
# ---------------------------------------------------------------------------
ACTIVE_PROSPECT_KEY = "active_prospect"
_TERMINAL_STATUSES = ("done", "failed", "cancelled")


def set_active_prospect(client_name: str, run_id: Optional[int] = None) -> None:
    set_flag(ACTIVE_PROSPECT_KEY,
             json.dumps({"client": client_name, "run_id": run_id, "ts": time.time()}))


def get_active_prospect() -> Optional[dict]:
    v = get_flag(ACTIVE_PROSPECT_KEY)
    if not v:
        return None
    try:
        return json.loads(v)
    except (ValueError, TypeError):
        return None


def clear_active_prospect() -> None:
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("DELETE FROM system_flags WHERE key=%s", (ACTIVE_PROSPECT_KEY,))
        c.commit()


def latest_incomplete_run() -> Optional[dict]:
    with cursor() as cur:
        cur.execute(
            "SELECT * FROM prospect_runs WHERE status NOT IN ('done','failed','cancelled') "
            "ORDER BY id DESC LIMIT 1"
        )
        return cur.fetchone()


def cancel_active_runs() -> int:
    """Set every non-terminal run to 'cancelled'. Returns the number changed.
    Used by /cancel and by startup cleanup so a dead run never blocks new work."""
    with conn() as c:
        with c.cursor() as cur:
            cur.execute(
                "UPDATE prospect_runs SET status='cancelled', updated_at=now() "
                "WHERE status NOT IN ('done','failed','cancelled')"
            )
            n = cur.rowcount
        c.commit()
    return n


def startup_cleanup() -> dict:
    """Make persistent state SAFE on boot: cancel any non-terminal runs (their
    worker threads died with the previous process) and clear the active-prospect
    flag. Never raises. Returns {'flag_cleared','runs_cancelled'} for logging.

    NOTE: this deliberately does NOT re-assert the flag for incomplete runs (the
    old reconcile behaviour), which is what left the bot permanently 'in progress'.
    """
    info = {"flag_cleared": False, "runs_cancelled": 0}
    try:
        info["flag_cleared"] = get_active_prospect() is not None
        clear_active_prospect()
    except Exception:  # noqa: BLE001 - startup must never crash on leftover state
        pass
    try:
        info["runs_cancelled"] = cancel_active_runs()
    except Exception:  # noqa: BLE001
        pass
    return info


def reconcile_active_prospect() -> Optional[dict]:
    """On bot startup, make the flag match reality: if a run is still
    incomplete, re-assert the flag for it; otherwise clear a stale flag.

    DEPRECATED for the bot's startup path (it re-asserted the flag and caused the
    permanent 'in progress' lock). Kept for compatibility; use startup_cleanup()."""
    run = latest_incomplete_run()
    if run:
        set_active_prospect(run["client_name"], run["id"])
        return run
    clear_active_prospect()
    return None


# ---------------------------------------------------------------------------
# Prospect runs (Workflow A run tracking + confirmation checkpoint)
# ---------------------------------------------------------------------------
def create_run(client_name: str) -> dict:
    with conn() as c:
        with c.cursor() as cur:
            cur.execute(
                "INSERT INTO prospect_runs (client_name, status) VALUES (%s, 'discovering') RETURNING *",
                (client_name,),
            )
            row = cur.fetchone()
        c.commit()
    return row


def update_run(run_id: int, **fields) -> None:
    if not fields:
        return
    sets = ["updated_at = now()"]
    vals: list[Any] = []
    for k, v in fields.items():
        sets.append(f"{k} = %s")
        vals.append(_jsonb(v) if isinstance(v, (dict, list)) else v)
    vals.append(run_id)
    with conn() as c:
        with c.cursor() as cur:
            cur.execute(f"UPDATE prospect_runs SET {', '.join(sets)} WHERE id = %s", vals)
        c.commit()


def claim_run_for_processing(run_id: int, confirmed_names: list) -> Optional[dict]:
    """Atomically move a run from 'awaiting_confirmation' to 'processing'.

    Returns the updated row if THIS caller won the claim, else None (someone
    already started it). Guarantees a run is processed at most once, even under
    a duplicate confirm or a race.
    """
    with conn() as c:
        with c.cursor() as cur:
            cur.execute(
                """UPDATE prospect_runs
                   SET status='processing', confirmed_names=%s, updated_at=now()
                   WHERE id=%s AND status='awaiting_confirmation'
                   RETURNING *""",
                (_jsonb(confirmed_names), run_id),
            )
            row = cur.fetchone()
        c.commit()
    return row


def get_run(run_id: int) -> Optional[dict]:
    with cursor() as cur:
        cur.execute("SELECT * FROM prospect_runs WHERE id = %s", (run_id,))
        return cur.fetchone()


def latest_run_awaiting_confirmation() -> Optional[dict]:
    with cursor() as cur:
        cur.execute(
            "SELECT * FROM prospect_runs WHERE status = 'awaiting_confirmation' "
            "ORDER BY id DESC LIMIT 1"
        )
        return cur.fetchone()


def latest_run() -> Optional[dict]:
    with cursor() as cur:
        cur.execute("SELECT * FROM prospect_runs ORDER BY id DESC LIMIT 1")
        return cur.fetchone()


def latest_run_for_client(client_name: str) -> Optional[dict]:
    with cursor() as cur:
        cur.execute(
            "SELECT * FROM prospect_runs WHERE lower(client_name) = lower(%s) "
            "ORDER BY id DESC LIMIT 1",
            (client_name,),
        )
        return cur.fetchone()


def latest_completed_run_with_candidates(client_name: str) -> Optional[dict]:
    """Newest finished discovery run for a client that has stored candidates.
    Used to offer reuse instead of re-discovering."""
    with cursor() as cur:
        cur.execute(
            "SELECT * FROM prospect_runs "
            "WHERE lower(client_name) = lower(%s) AND status = 'done' "
            "  AND candidates IS NOT NULL AND jsonb_array_length(candidates) > 0 "
            "ORDER BY id DESC LIMIT 1",
            (client_name,),
        )
        return cur.fetchone()


# ---------------------------------------------------------------------------
# /status aggregates
# ---------------------------------------------------------------------------
def running_totals() -> dict:
    with cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM companies")
        companies = cur.fetchone()["n"]
        cur.execute("SELECT count(*) AS n FROM companies WHERE icp_result = 'QUALIFIED'")
        qualified = cur.fetchone()["n"]
        cur.execute("SELECT count(*) AS n FROM leads WHERE state = 'drafted'")
        drafts = cur.fetchone()["n"]
        cur.execute("SELECT count(*) AS n FROM leads")
        leads = cur.fetchone()["n"]
        cur.execute(
            "SELECT coalesce(avg(research_confidence),0) AS c "
            "FROM companies WHERE icp_result = 'QUALIFIED'"
        )
        avg_conf = float(cur.fetchone()["c"] or 0)
        cur.execute(
            "SELECT coalesce(sum(tokens_used),0) AS t, coalesce(sum(research_cost_usd),0) AS c "
            "FROM companies"
        )
        tc = cur.fetchone()
        return {
            "companies": companies,
            "qualified": qualified,
            "drafts": drafts,
            "leads": leads,
            "avg_confidence_qualified": round(avg_conf, 1),
            "tokens_used": int(tc["t"] or 0),
            "cost_usd": float(tc["c"] or 0),
        }


def parked_companies() -> list[dict]:
    with cursor() as cur:
        cur.execute(
            "SELECT name, icp_reason, client_name FROM companies "
            "WHERE icp_result = 'PARKED' ORDER BY id"
        )
        return cur.fetchall()


def drafted_leads(limit: int = 25) -> list[dict]:
    """Leads that have a Gmail draft, most recent first (for /status)."""
    with cursor() as cur:
        cur.execute(
            "SELECT full_name, company, designation, drafted_at "
            "FROM leads WHERE state = 'drafted' "
            "ORDER BY drafted_at DESC NULLS LAST, id DESC LIMIT %s",
            (limit,),
        )
        return cur.fetchall()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys

    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "init":
        init_db()
    elif cmd == "reset":
        reset_db()
    elif cmd == "migrate":
        migrate()
    else:
        print("usage: python -m src.db init | reset | migrate")

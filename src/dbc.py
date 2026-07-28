"""Workflow C Postgres helpers.

Reuses lib_snapshot.db's connection pool (conn/cursor) — that pool code is
schema-agnostic. Never calls lib_snapshot.db.init_db()/reset_db(): those are
Workflow-A-specific (reset_db DROPs every table) and would be catastrophic
against the shared `outreach` database. Schema application for Workflow C
goes through lib_snapshot.db.migrate(), which only applies un-applied
migrations/*.sql files and is safe to re-run.
"""
from __future__ import annotations

from typing import Any, Optional

from lib_snapshot import db as shared_db

conn = shared_db.conn
cursor = shared_db.cursor


def apply_migrations() -> None:
    shared_db.migrate()


def _jsonb(value: Any) -> Any:
    return shared_db._jsonb(value)


# ---------------------------------------------------------------------------
# connections
# ---------------------------------------------------------------------------
def bulk_insert_connections(rows: list[dict]) -> int:
    """Insert connection rows, upserting by linkedin_url (the export's stable
    identity key). Returns the number of rows written."""
    if not rows:
        return 0
    n = 0
    with conn() as c:
        with c.cursor() as cur:
            for r in rows:
                cur.execute(
                    """INSERT INTO connections
                       (linkedin_url, poc_name, designation, company_name_raw,
                        company_name_norm, poc_email_seed, incomplete_row)
                       VALUES (%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (linkedin_url) DO UPDATE SET
                         poc_name=EXCLUDED.poc_name,
                         designation=EXCLUDED.designation,
                         company_name_raw=EXCLUDED.company_name_raw,
                         company_name_norm=EXCLUDED.company_name_norm,
                         poc_email_seed=EXCLUDED.poc_email_seed,
                         incomplete_row=EXCLUDED.incomplete_row""",
                    (r["linkedin_url"], r["poc_name"], r.get("designation"),
                     r.get("company_name_raw"), r.get("company_name_norm"),
                     r.get("poc_email_seed"), r.get("incomplete_row", False)),
                )
                n += 1
        c.commit()
    return n


def clear_connections() -> None:
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("DELETE FROM connections")
        c.commit()


def all_connections() -> list[dict]:
    with cursor() as cur:
        cur.execute("SELECT * FROM connections WHERE incomplete_row = FALSE")
        return cur.fetchall()


def connections_count() -> int:
    with cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM connections")
        return cur.fetchone()["n"]


def connection_by_url(linkedin_url: str) -> Optional[dict]:
    with cursor() as cur:
        cur.execute("SELECT * FROM connections WHERE linkedin_url = %s", (linkedin_url,))
        return cur.fetchone()


def connection_by_id(connection_id: Optional[int]) -> Optional[dict]:
    if not connection_id:
        return None
    with cursor() as cur:
        cur.execute("SELECT * FROM connections WHERE id = %s", (connection_id,))
        return cur.fetchone()


def connections_for_company(company_name_raw: str) -> list[dict]:
    """Rows whose company_name_raw exactly matches the Phase 1 match result
    (the matcher resolves target-firm-name -> one company_name_raw string)."""
    with cursor() as cur:
        cur.execute(
            "SELECT * FROM connections WHERE company_name_raw = %s AND incomplete_row = FALSE "
            "ORDER BY id",
            (company_name_raw,),
        )
        return cur.fetchall()


def distinct_company_names() -> list[str]:
    with cursor() as cur:
        cur.execute(
            "SELECT DISTINCT company_name_raw FROM connections "
            "WHERE incomplete_row = FALSE AND company_name_raw IS NOT NULL"
        )
        return [r["company_name_raw"] for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# wc_runs / wc_queue
# ---------------------------------------------------------------------------
def create_wc_run(mode: str, query_firm: Optional[str] = None) -> dict:
    with conn() as c:
        with c.cursor() as cur:
            cur.execute(
                "INSERT INTO wc_runs (mode, query_firm) VALUES (%s,%s) RETURNING *",
                (mode, query_firm),
            )
            row = cur.fetchone()
        c.commit()
    return row


def update_wc_run(run_id: int, **fields) -> None:
    if not fields:
        return
    sets = []
    vals: list[Any] = []
    for k, v in fields.items():
        sets.append(f"{k} = %s")
        vals.append(v)
    vals.append(run_id)
    with conn() as c:
        with c.cursor() as cur:
            cur.execute(f"UPDATE wc_runs SET {', '.join(sets)} WHERE id = %s", vals)
        c.commit()


def get_wc_run(run_id: int) -> Optional[dict]:
    with cursor() as cur:
        cur.execute("SELECT * FROM wc_runs WHERE id = %s", (run_id,))
        return cur.fetchone()


def latest_resumable_run() -> Optional[dict]:
    """Runs /referral resume can continue: 'crashed' (circuit-breaker abort,
    HARD RULE 15) or 'cancelled' (Feature B /cancel) — both leave queued work
    pending that resume should pick back up from."""
    with cursor() as cur:
        cur.execute("SELECT * FROM wc_runs WHERE status IN ('crashed', 'cancelled') "
                   "ORDER BY id DESC LIMIT 1")
        return cur.fetchone()


def latest_running_run() -> Optional[dict]:
    """The currently-active run, if any — Feature B's /cancel targets this."""
    with cursor() as cur:
        cur.execute("SELECT * FROM wc_runs WHERE status = 'running' ORDER BY id DESC LIMIT 1")
        return cur.fetchone()


def request_run_cancellation(run_id: int) -> None:
    """Feature B: persists the cancellation flag so it survives the drain
    loop's between-firm checks and a bot restart (an in-memory flag would
    lose either)."""
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("UPDATE wc_runs SET cancel_requested_at = now() WHERE id = %s", (run_id,))
        c.commit()


def latest_wc_run() -> Optional[dict]:
    with cursor() as cur:
        cur.execute("SELECT * FROM wc_runs ORDER BY id DESC LIMIT 1")
        return cur.fetchone()


def wc_firm_by_name(firm_name: str) -> Optional[dict]:
    with cursor() as cur:
        cur.execute(
            "SELECT * FROM wc_firms WHERE lower(firm_name) = lower(%s) ORDER BY id DESC LIMIT 1",
            (firm_name,),
        )
        return cur.fetchone()


def wc_draft_for_firm(firm_id: int) -> Optional[dict]:
    with cursor() as cur:
        cur.execute("SELECT * FROM wc_drafts WHERE firm_id = %s ORDER BY id DESC LIMIT 1", (firm_id,))
        return cur.fetchone()


def all_wc_firms() -> list[dict]:
    with cursor() as cur:
        cur.execute("SELECT * FROM wc_firms ORDER BY id")
        return cur.fetchall()


def all_wc_drafts() -> list[dict]:
    with cursor() as cur:
        cur.execute("SELECT * FROM wc_drafts ORDER BY id")
        return cur.fetchall()


def enqueue_firms(run_id: int, firm_names: list[str]) -> int:
    with conn() as c:
        with c.cursor() as cur:
            for i, name in enumerate(firm_names):
                cur.execute(
                    """INSERT INTO wc_queue (run_id, firm_name_input, position)
                       VALUES (%s,%s,%s)""",
                    (run_id, name, i),
                )
        c.commit()
    return len(firm_names)


def set_queue_status(queue_id: int, status: str) -> None:
    with conn() as c:
        with c.cursor() as cur:
            cur.execute(
                "UPDATE wc_queue SET queue_status=%s, updated_at=now() WHERE id=%s",
                (status, queue_id),
            )
        c.commit()


def set_queue_firm(queue_id: int, firm_id: int) -> None:
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("UPDATE wc_queue SET firm_id=%s WHERE id=%s", (firm_id, queue_id))
        c.commit()


def next_pending_chunk(run_id: int, limit: int) -> list[dict]:
    with cursor() as cur:
        cur.execute(
            """SELECT * FROM wc_queue WHERE run_id=%s AND queue_status='pending'
               ORDER BY position LIMIT %s""",
            (run_id, limit),
        )
        return cur.fetchall()


def infra_paused_firms(run_id: int) -> list[dict]:
    with cursor() as cur:
        cur.execute(
            "SELECT * FROM wc_queue WHERE run_id=%s AND queue_status='infra_paused' ORDER BY position",
            (run_id,),
        )
        return cur.fetchall()


def queue_counts(run_id: int) -> dict:
    with cursor() as cur:
        cur.execute(
            "SELECT queue_status, count(*) AS n FROM wc_queue WHERE run_id=%s GROUP BY queue_status",
            (run_id,),
        )
        return {r["queue_status"]: r["n"] for r in cur.fetchall()}


# ---------------------------------------------------------------------------
# wc_firms
# ---------------------------------------------------------------------------
def create_wc_firm(run_id: int, firm_name: str, **fields) -> dict:
    cols = ["run_id", "firm_name"]
    vals: list[Any] = [run_id, firm_name]
    for k, v in fields.items():
        cols.append(k)
        vals.append(_jsonb(v) if isinstance(v, (dict, list)) else v)
    placeholders = ", ".join(["%s"] * len(cols))
    with conn() as c:
        with c.cursor() as cur:
            cur.execute(
                f"INSERT INTO wc_firms ({', '.join(cols)}) VALUES ({placeholders}) RETURNING *",
                vals,
            )
            row = cur.fetchone()
        c.commit()
    return row


def update_wc_firm(firm_id: int, **fields) -> None:
    if not fields:
        return
    sets = []
    vals: list[Any] = []
    for k, v in fields.items():
        sets.append(f"{k} = %s")
        vals.append(_jsonb(v) if isinstance(v, (dict, list)) else v)
    vals.append(firm_id)
    with conn() as c:
        with c.cursor() as cur:
            cur.execute(f"UPDATE wc_firms SET {', '.join(sets)} WHERE id = %s", vals)
        c.commit()


def get_wc_firm(firm_id: int) -> Optional[dict]:
    with cursor() as cur:
        cur.execute("SELECT * FROM wc_firms WHERE id = %s", (firm_id,))
        return cur.fetchone()


def firms_for_run(run_id: int) -> list[dict]:
    with cursor() as cur:
        cur.execute("SELECT * FROM wc_firms WHERE run_id = %s ORDER BY id", (run_id,))
        return cur.fetchall()


def parked_firms(run_id: Optional[int] = None) -> list[dict]:
    with cursor() as cur:
        if run_id is not None:
            cur.execute(
                "SELECT * FROM wc_firms WHERE run_id=%s AND icp_status='parked' ORDER BY id",
                (run_id,),
            )
        else:
            cur.execute("SELECT * FROM wc_firms WHERE icp_status='parked' ORDER BY id")
        return cur.fetchall()


def parked_firm_by_name(firm_name: str) -> Optional[dict]:
    with cursor() as cur:
        cur.execute(
            "SELECT * FROM wc_firms WHERE lower(firm_name)=lower(%s) AND icp_status='parked' "
            "ORDER BY id DESC LIMIT 1",
            (firm_name,),
        )
        return cur.fetchone()


# ---------------------------------------------------------------------------
# wc_drafts / contacted_pocs (cross-workflow dedup, HARD RULE 7)
# ---------------------------------------------------------------------------
def create_wc_draft(firm_id: int, connection_id: Optional[int], **fields) -> dict:
    cols = ["firm_id", "connection_id"]
    vals: list[Any] = [firm_id, connection_id]
    for k, v in fields.items():
        cols.append(k)
        vals.append(_jsonb(v) if isinstance(v, (dict, list)) else v)
    placeholders = ", ".join(["%s"] * len(cols))
    with conn() as c:
        with c.cursor() as cur:
            cur.execute(
                f"INSERT INTO wc_drafts ({', '.join(cols)}) VALUES ({placeholders}) RETURNING *",
                vals,
            )
            row = cur.fetchone()
        c.commit()
    return row


def draft_exists_for_connection(firm_id: int, connection_id: int) -> bool:
    """Idempotency check for /referral resume: has this POC already been
    drafted for this firm in a prior (possibly crashed) run?"""
    with cursor() as cur:
        cur.execute(
            "SELECT 1 FROM wc_drafts WHERE firm_id=%s AND connection_id=%s "
            "AND email_status='drafted' LIMIT 1",
            (firm_id, connection_id),
        )
        return cur.fetchone() is not None


def is_contacted(linkedin_url: Optional[str], email: Optional[str]) -> bool:
    """HARD RULE 7: cross-workflow dedup against contacted_pocs, matched by
    LinkedIn URL first, falling back to email."""
    return find_prior_contact(linkedin_url, email) is not None


def find_prior_contact(linkedin_url: Optional[str], email: Optional[str]) -> Optional[dict]:
    """Like is_contacted, but returns the MATCHING contacted_pocs row (not
    just a bool) so callers can report WHO/WHAT a duplicate collided with
    (Feature 3 — 'report skips clearly: single-firm names who they collided
    with; batch lists them in the skipped section, not just a bare count').
    Matched by LinkedIn URL first, falling back to email; the earliest
    (`contacted_at`) match wins, since contacted_pocs never gets a second row
    for the same URL (ON CONFLICT DO NOTHING) but CAN carry more than one row
    sharing an email (no unique constraint there).

    QA finding (Part 9 adversarial re-attack): the URL comparison normalizes
    both sides (lowercase, http/https and www. collapsed, trailing slash
    stripped) rather than a raw exact match. A plain exact match let two
    connections rows for the SAME real LinkedIn profile -- e.g. one with and
    one without a trailing slash, a formatting inconsistency a connections
    export can genuinely contain across snapshots -- silently bypass dedup
    whenever their resolved emails also happened to differ, producing two
    Gmail drafts for one person. No migration needed: this normalizes the
    comparison, not the stored value, so it matches existing rows regardless
    of how their linkedin_url was originally formatted."""
    with cursor() as cur:
        if linkedin_url:
            cur.execute(
                "SELECT * FROM contacted_pocs WHERE linkedin_url IS NOT NULL AND "
                "lower(rtrim(regexp_replace(linkedin_url, '^https?://(www\\.)?', ''), '/')) = "
                "lower(rtrim(regexp_replace(%s, '^https?://(www\\.)?', ''), '/')) "
                "ORDER BY contacted_at LIMIT 1",
                (linkedin_url,),
            )
            row = cur.fetchone()
            if row:
                return row
        if email:
            cur.execute(
                "SELECT * FROM contacted_pocs WHERE lower(email) = lower(%s) "
                "ORDER BY contacted_at LIMIT 1",
                (email,),
            )
            row = cur.fetchone()
            if row:
                return row
    return None


def find_within_run_contact(run_id: int, linkedin_url: Optional[str],
                            email: Optional[str]) -> Optional[dict]:
    """FIX 38: the WITHIN-run half of HARD RULE 7 / Feature 3 (the same real
    person appearing under 3+ target-list entries in ONE batch must still
    draft exactly once), kept structurally SEPARATE from find_prior_contact
    (now advisory-only for genuine cross-run history). Mirrors
    find_prior_contact's own matching logic exactly -- normalized LinkedIn
    URL first, email fallback -- but scoped to THIS run_id alone, via a join
    across wc_drafts -> wc_firms -> connections (no migration: contacted_pocs
    has no run_id column at all, so this is the only way to distinguish "an
    earlier firm in THIS SAME run already drafted this person" from "a
    genuinely different run drafted them, maybe years ago"). Only
    email_status='drafted' rows count -- a 'skipped_duplicate' row was never
    an actual contact, so it must never itself become something a THIRD
    occurrence collides with (matches find_prior_contact's own "the earliest
    real contact wins" semantics, just run-scoped). Returns a dict shaped
    exactly like a contacted_pocs row (poc_name/firm_name/contacted_at/
    source_workflow) so the existing _collision_note() formatter works on
    either return value unchanged."""
    with cursor() as cur:
        if linkedin_url:
            cur.execute(
                "SELECT c.poc_name AS poc_name, f.firm_name AS firm_name, "
                "d.created_at AS contacted_at, 'C' AS source_workflow "
                "FROM wc_drafts d JOIN wc_firms f ON f.id = d.firm_id "
                "JOIN connections c ON c.id = d.connection_id "
                "WHERE f.run_id = %s AND d.email_status = 'drafted' AND c.linkedin_url IS NOT NULL AND "
                "lower(rtrim(regexp_replace(c.linkedin_url, '^https?://(www\\.)?', ''), '/')) = "
                "lower(rtrim(regexp_replace(%s, '^https?://(www\\.)?', ''), '/')) "
                "ORDER BY d.created_at LIMIT 1",
                (run_id, linkedin_url),
            )
            row = cur.fetchone()
            if row:
                return row
        if email:
            cur.execute(
                "SELECT c.poc_name AS poc_name, f.firm_name AS firm_name, "
                "d.created_at AS contacted_at, 'C' AS source_workflow "
                "FROM wc_drafts d JOIN wc_firms f ON f.id = d.firm_id "
                "JOIN connections c ON c.id = d.connection_id "
                "WHERE f.run_id = %s AND d.email_status = 'drafted' AND lower(d.poc_email) = lower(%s) "
                "ORDER BY d.created_at LIMIT 1",
                (run_id, email),
            )
            row = cur.fetchone()
            if row:
                return row
    return None


def record_contacted(linkedin_url: Optional[str], email: Optional[str], poc_name: str,
                     firm_name: str, gmail_draft_id: str, source_workflow: str = "C") -> None:
    """FIX 38 Part B: was ON CONFLICT ... DO NOTHING (a repeat draft's insert
    silently no-opped, leaving contacted_pocs's one linkedin_url-unique row
    forever pointing at whatever gmail_draft_id happened to be recorded
    first -- stale/deleted once cross-run contact stopped blocking and
    started actually re-drafting). The partial unique index only constrains
    linkedin_url, so a genuinely NEW row is still possible (and inserted)
    whenever this contact's linkedin_url is null or doesn't already occupy
    that index -- e.g. an email-only match, or a different real person who
    happens to share an email. Only a literal linkedin_url repeat is
    structurally blocked from a second row; for that case alone, UPDATE the
    existing row to this latest contact event -- every field, not just the
    date, so the one row that CAN exist stays internally coherent (a
    gmail_draft_id pointing at the new draft while poc_name/firm_name still
    named the old contact would be a more confusing record than the field
    it's overwriting). This never loses the earlier date silently: the
    caller reads the PRIOR row via find_prior_contact() and builds its
    advisory message from it BEFORE this write ever runs."""
    with conn() as c:
        with c.cursor() as cur:
            cur.execute(
                """INSERT INTO contacted_pocs
                   (linkedin_url, email, poc_name, firm_name, source_workflow, gmail_draft_id)
                   VALUES (%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (linkedin_url) WHERE linkedin_url IS NOT NULL DO UPDATE SET
                     email = EXCLUDED.email,
                     poc_name = EXCLUDED.poc_name,
                     firm_name = EXCLUDED.firm_name,
                     contacted_at = now(),
                     gmail_draft_id = EXCLUDED.gmail_draft_id,
                     source_workflow = EXCLUDED.source_workflow""",
                (linkedin_url, email, poc_name, firm_name, source_workflow, gmail_draft_id),
            )
        c.commit()


# ---------------------------------------------------------------------------
# system_flags reuse (poller lock etc. already generic in shared_db)
# ---------------------------------------------------------------------------
get_flag = shared_db.get_flag
set_flag = shared_db.set_flag

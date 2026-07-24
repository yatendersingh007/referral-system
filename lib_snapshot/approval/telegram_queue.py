"""Telegram bot (POLLING mode, not webhooks).

Dispatches /prospect and /status end-to-end with staged progress messaging.
Long-running commands run in a background thread so the poller keeps receiving
the confirm/trim reply. Only TELEGRAM_USER_ID may command the bot.

Run:  python -m src.approval.telegram_queue
"""
from __future__ import annotations

import os
import re
import threading
import time
from typing import Optional

import requests

from .. import config, db, models, status
from ..enrichment import apollo
from ..prospect import competitor_engine
from ..researcher import exa_client, search_client

API = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}"
_TG_LIMIT = 3800  # keep under Telegram's 4096 char cap

# Stability thresholds. An active_prospect flag older than STALE (and/or with no
# live worker) is auto-cleared; a pending profile older than PENDING auto-expires.
_STALE_RUN_SEC = 300       # 5 min: a WORKER-phase run older than this is treated as stuck
_AWAITING_TTL_SEC = 1800   # 30 min: a USER-PACED awaiting_confirmation run only auto-clears this old
_PENDING_TTL_SEC = 180     # 3 min: a pending profile approval older than this expires

_busy = threading.Lock()   # one prospect job at a time (within this process)
_cancel = threading.Event()  # set by /cancel (or stale-reap) to abort the running worker

# Newly-derived client profiles awaiting human approval, keyed by chat_id, with a
# parallel dict of derive timestamps for the 3-minute auto-expiry. A derived
# profile is NOT saved and NO competitor search starts until /profile confirm.
_pending_profiles: dict[str, dict] = {}
_pending_ts: dict[str, float] = {}

# Ambiguous-client resolutions awaiting a /prospect pick, keyed by chat_id:
# {client, alternatives:[{name,description}], ts}. Set when client comprehension
# cannot confidently resolve the firm; blocks discovery until the user picks.
# Same 3-minute auto-expiry and /cancel clears it, mirroring pending profiles.
_pending_ambiguous: dict[str, dict] = {}


class _Cancelled(BaseException):
    """Raised inside a worker's progress callback to abort it on /cancel. Subclasses
    BaseException so it cuts through the workers' `except Exception` handlers."""


def _worker_progress(msg: str) -> None:
    """Progress callback for background workers: forwards to Telegram, but aborts
    the worker (raises _Cancelled) as soon as /cancel signals it. This is how a
    /cancel takes effect mid-discovery/mid-research without touching discovery logic."""
    if _cancel.is_set():
        raise _Cancelled()
    send(msg)


class PollerConflict(RuntimeError):
    """Raised when Telegram reports another getUpdates consumer (HTTP 409).

    Two pollers on one bot token cause every message to be handled twice.
    We refuse to start (and exit if it appears at runtime) so exactly one runs.
    """


# ---------------------------------------------------------------------------
# Low-level Telegram API
# ---------------------------------------------------------------------------
def send(text: str, chat_id: Optional[str] = None) -> None:
    chat_id = chat_id or config.TELEGRAM_USER_ID
    for i in range(0, len(text), _TG_LIMIT):
        chunk = text[i:i + _TG_LIMIT]
        try:
            requests.post(f"{API}/sendMessage",
                          json={"chat_id": chat_id, "text": chunk},
                          timeout=30)
        except requests.RequestException:
            pass


def get_updates(offset: Optional[int], timeout: int = 30) -> list[dict]:
    try:
        r = requests.get(f"{API}/getUpdates",
                         params={"offset": offset, "timeout": timeout},
                         timeout=timeout + 10)
        if r.status_code == 409:
            desc = ""
            try:
                desc = r.json().get("description", "")
            except Exception:  # noqa: BLE001
                pass
            raise PollerConflict(desc or "Conflict: another getUpdates request is running")
        r.raise_for_status()
        return r.json().get("result", [])
    except PollerConflict:
        raise
    except requests.RequestException:
        time.sleep(3)
        return []


# ---------------------------------------------------------------------------
# Background job runners
# ---------------------------------------------------------------------------
def _acquire_worker_slot() -> bool:
    """Take the single worker slot. Blocks briefly so that a JUST-cancelled prior
    worker can release it (cooperative cancel makes that near-instant); if it can't
    be taken, the caller reports a soft, non-blocking message (never 'wait forever').
    On success, clears the cancel flag so this fresh worker isn't self-aborted."""
    if not _busy.acquire(timeout=45):
        return False
    _cancel.clear()
    return True


def _run_discovery(client: str) -> None:
    if not _acquire_worker_slot():
        send("The previous run is still stopping. Send /cancel, then /prospect again.")
        return
    run = None
    try:
        # Client comprehension FIRST (premium, synthesize-first). If the client
        # can't be confidently identified, stop and ask — never run discovery on a
        # bad foundation. (Resolved clients cache-hit inside run_discovery below.)
        cr, cr_usage = competitor_engine.resolve_client(client, progress=_worker_progress)
        if _cancel.is_set():
            return
        if cr.get("resolved") is False:
            _set_pending_ambiguous(client, cr.get("alternatives") or [])
            send(_fmt_ambiguity(client, cr.get("alternatives") or []))
            db.clear_active_prospect()   # not proceeding; release the lock for /prospect pick
            return
        run = db.create_run(client)
        db.set_active_prospect(client, run["id"])  # now with the run id
        # Roll the premium client-comprehension cost into THIS run (Item 6).
        candidates = competitor_engine.run_discovery(run["id"], client,
                                                     progress=_worker_progress,
                                                     seed_usage=cr_usage)
        if not candidates:
            send("No competitors found. Try a more specific client firm name.")
            db.update_run(run["id"], status="done")
            db.clear_active_prospect()   # flow ended, unlock
            return
        lines = [f"Found {len(candidates)} competitors for {client}:"]
        for i, c in enumerate(candidates, 1):
            lines.append(f"{i}. {c['name']}" + (f" [{c['region']}]" if c.get("region") else ""))
        lines.append("")
        lines.append("Reply /prospect confirm to proceed with all, or")
        lines.append("/prospect trim <names or numbers> to drop some, e.g. /prospect trim 2, 5")
        send("\n".join(lines))
        # flag intentionally stays set: the flow is now awaiting confirmation.
    except _Cancelled:
        # /cancel already cleared the flag and cancelled the run; just stop quietly.
        pass
    except Exception as e:  # noqa: BLE001
        send(f"Discovery failed: {e}")
        try:
            if run:
                db.update_run(run["id"], status="failed", error=str(e))
        except Exception:  # noqa: BLE001
            pass
        db.clear_active_prospect()       # failed -> unlock
    finally:
        _busy.release()


def _run_processing(run: dict) -> None:
    if not _acquire_worker_slot():
        send("The previous run is still stopping. Send /cancel, then /prospect again.")
        return
    try:
        candidates = run.get("candidates") or []
        confirmed = run.get("confirmed_names")
        if confirmed:
            confirmed_set = {n.lower() for n in confirmed}
            candidates = [c for c in candidates if c["name"].lower() in confirmed_set]
        competitor_engine.run_processing(run["id"], run["client_name"], candidates,
                                         progress=_worker_progress)
    except _Cancelled:
        pass
    except Exception as e:  # noqa: BLE001
        send(f"Processing failed: {e}")
        db.update_run(run["id"], status="failed", error=str(e))
    finally:
        db.clear_active_prospect()   # processing is terminal (done/failed/cancelled) -> unlock
        _busy.release()


def _run_derive_profile(client: str) -> None:
    """Derive a proposed profile for a NEW client and send it for approval.
    Does NOT save the profile or start any search — that waits for /profile confirm."""
    if not _acquire_worker_slot():
        send("The previous run is still stopping. Send /cancel, then /prospect again.")
        return
    try:
        # Comprehend the client FIRST; if we can't confidently identify it, ask
        # (ambiguity gate) instead of proposing a profile for a guessed firm.
        cr, _cr_usage = competitor_engine.resolve_client(client, progress=_worker_progress)
        if _cancel.is_set():
            return
        if cr.get("resolved") is False:
            _set_pending_ambiguous(client, cr.get("alternatives") or [])
            send(_fmt_ambiguity(client, cr.get("alternatives") or []))
            return
        prof = competitor_engine.derive_profile(client, progress=_worker_progress)
        if _cancel.is_set():
            return   # /cancel arrived during research — don't resurrect a pending profile
        key = str(config.TELEGRAM_USER_ID)
        _pending_profiles[key] = prof
        _pending_ts[key] = time.time()
        send(_fmt_profile(prof))
    except _Cancelled:
        pass
    except Exception as e:  # noqa: BLE001
        send(f"Could not derive a profile for {client}: {e}")
    finally:
        _busy.release()


def _run_qualify(company_id: int, aum: float) -> None:
    """Worker for /qualify: gate the manually-verified AUM and, if it qualifies,
    run the full contacts -> Apollo -> draft tail (shared process_qualified_company)."""
    if not _acquire_worker_slot():
        send("The previous run is still stopping. Send /cancel, then /qualify again.")
        return
    try:
        result = competitor_engine.manual_qualify(company_id, aum, progress=_worker_progress)
        send(result["message"])
    except _Cancelled:
        pass
    except Exception as e:  # noqa: BLE001
        send(f"Qualify failed: {e}")
    finally:
        _busy.release()


def _start_thread(target, *args) -> None:
    threading.Thread(target=target, args=args, daemon=True).start()


# ---------------------------------------------------------------------------
# Cancellation + stale-state reaping (the emergency stop + auto-recovery)
# ---------------------------------------------------------------------------
def _clear_prospect_state() -> dict:
    """Tear down ALL in-flight prospect state: signal the running worker to abort,
    cancel non-terminal runs, clear the active flag, and drop pending profiles.
    Best-effort and idempotent. Returns a summary of what was cleared."""
    _cancel.set()   # aborts the current worker at its next progress() checkpoint
    info = {"flag": False, "runs": 0, "pending": False}
    try:
        info["flag"] = db.get_active_prospect() is not None
        db.clear_active_prospect()
    except Exception:  # noqa: BLE001
        pass
    try:
        info["runs"] = db.cancel_active_runs()
    except Exception:  # noqa: BLE001
        pass
    info["pending"] = bool(_pending_profiles) or bool(_pending_ambiguous)
    _pending_profiles.clear()
    _pending_ts.clear()
    _pending_ambiguous.clear()
    return info


def _worker_running() -> bool:
    """True if a background worker currently holds the slot (genuinely working)."""
    return _busy.locked()


def _reap_stale_state() -> None:
    """Auto-recover before handling a new /prospect: expire an old pending profile,
    and clear a stuck active-prospect flag. A WORKER-phase run clears when stale
    (>5 min) or orphaned (no live worker); a USER-PACED awaiting_confirmation run
    clears ONLY after a long idle (30 min), never on the no-worker check — so a
    fresh awaiting list survives until the user confirms. This is what stops the
    bot getting permanently stuck WITHOUT nuking a list the user is still deciding on."""
    key = str(config.TELEGRAM_USER_ID)
    now = time.time()
    if _pending_profiles and (now - _pending_ts.get(key, 0)) > _PENDING_TTL_SEC:
        _pending_profiles.clear()
        _pending_ts.clear()
    amb = _pending_ambiguous.get(key)
    if amb and (now - amb.get("ts", 0)) > _PENDING_TTL_SEC:
        _pending_ambiguous.pop(key, None)
    active = db.get_active_prospect()
    if not active:
        return
    age = db.flag_age_seconds(db.ACTIVE_PROSPECT_KEY) or 0

    # An awaiting_confirmation run is USER-PACED: the worker has correctly released
    # the slot and we are waiting for the human to /prospect confirm. It must NOT be
    # cleared on the short worker timeout or the no-worker check (that was the trap
    # that cancelled a 12-second-old awaiting run and broke reuse -> confirm). Only a
    # long idle (30 min) auto-clears it. Worker phases (discovering/processing) keep
    # the short timeout + orphaned-worker safety net.
    run = None
    try:
        rid = active.get("run_id") if isinstance(active, dict) else None
        run = db.get_run(rid) if rid else None
    except Exception:  # noqa: BLE001
        run = None
    awaiting = bool(run and run.get("status") == "awaiting_confirmation")

    if awaiting:
        stale = age > _AWAITING_TTL_SEC
    else:
        stale = age > _STALE_RUN_SEC or not _worker_running()

    if stale:
        mins, secs = int(age // 60), int(age % 60)
        send(f"Clearing stale run (last activity {mins}m {secs}s ago). Proceeding.")
        _clear_prospect_state()


def _band_str(p: dict) -> str:
    lo = (p.get("aum_floor") or config.AUM_MIN_USD) / 1e6
    hi = (p.get("aum_ceiling") or config.AUM_MAX_USD)
    hi_s = f"${hi / 1e9:.1f}B" if hi >= 1e9 else f"${hi / 1e6:.0f}M"
    return f"${lo:.0f}M–{hi_s}"


def _set_pending_ambiguous(client: str, alternatives: list) -> None:
    _pending_ambiguous[str(config.TELEGRAM_USER_ID)] = {
        "client": client, "alternatives": alternatives or [], "ts": time.time()}


def _fmt_ambiguity(client: str, alternatives: list) -> str:
    lines = [f'⚠️ I couldn\'t confidently identify "{client}". Closest matches:']
    for i, a in enumerate(alternatives or [], 1):
        nm = (a.get("name") or "?") if isinstance(a, dict) else str(a)
        desc = (a.get("description") or "") if isinstance(a, dict) else ""
        lines.append(f"{i}. {nm}" + (f" — {desc}" if desc else ""))
    if not alternatives:
        lines.append("(no distinct candidates extracted)")
    lines.append("")
    lines.append("Reply /prospect pick <n> to choose one, or re-run with more detail, e.g.")
    lines.append(f'/prospect "{client} Property Management <city>"')
    return "\n".join(lines)


def _fmt_profile(p: dict) -> str:
    return (
        f'📋 New client profile derived for "{p["client_name"]}":\n'
        f'• Type: {p.get("type") or p.get("niche_description") or "real estate investment / management"}\n'
        f'• Asset classes: {", ".join(p.get("asset_classes") or []) or "(none)"}\n'
        f'• Regions: {", ".join(p.get("target_regions") or []) or "North America"}\n'
        f'• AUM band: {_band_str(p)}\n'
        f'• Pitch focus: {p.get("pitch_service_focus") or "property accounting"}\n\n'
        "Reply /profile confirm to proceed, or\n"
        "/profile edit type=<x> assets=<a,b> regions=<r> to correct."
    )


_PROFILE_EDIT_RE = re.compile(
    r"\b(type|assets|regions|focus)\s*=\s*(.*?)(?=\s+\b(?:type|assets|regions|focus)\s*=|$)",
    re.IGNORECASE,
)


def _parse_profile_edits(s: str) -> dict:
    """Parse 'type=<x> assets=<a,b> regions=<r> focus=<f>' where values may
    contain spaces. assets/regions become lists; type/focus stay strings."""
    out: dict = {}
    for m in _PROFILE_EDIT_RE.finditer(s or ""):
        key = m.group(1).lower()
        val = m.group(2).strip()
        if not val:
            continue
        if key in ("assets", "regions"):
            out[key] = [x.strip() for x in val.split(",") if x.strip()]
        else:
            out[key] = val
    return out


def _handle_profile(arg: str) -> None:
    arg = arg.strip()
    low = arg.lower()
    key = str(config.TELEGRAM_USER_ID)
    # Expire a stale pending profile so approval can never block indefinitely (P5).
    if _pending_profiles.get(key) and (time.time() - _pending_ts.get(key, 0)) > _PENDING_TTL_SEC:
        _pending_profiles.pop(key, None)
        _pending_ts.pop(key, None)
        send('That profile approval expired (3 min idle). Start again with /prospect "<client firm>".')
        return
    pending = _pending_profiles.get(key)

    if low.startswith("confirm") or low in ("confirm", "yes", "ok", "go"):
        if not pending:
            send('No profile awaiting approval. Start with /prospect "<client firm>".')
            return
        # Don't clobber another in-progress run's active-prospect slot.
        if db.get_active_prospect():
            send("A prospect run is already in progress; confirm again once it finishes.")
            return
        client = pending["client_name"]
        db.upsert_client_profile(
            client,
            niche_description=(pending.get("type") or "real estate investment / management")[:500],
            asset_classes=pending.get("asset_classes") or ["commercial real estate"],
            target_regions=pending.get("target_regions") or ["North America"],
            aum_floor=pending.get("aum_floor") or config.AUM_MIN_USD,
            aum_ceiling=pending.get("aum_ceiling") or config.AUM_MAX_USD,
            pitch_service_focus=pending.get("pitch_service_focus") or "property accounting",
        )
        _pending_profiles.pop(key, None)
        send(f"Profile saved for {client}. Starting competitor discovery...")
        db.set_active_prospect(client)
        _start_thread(_run_discovery, client)
        return

    if low.startswith("edit"):
        if not pending:
            send('No profile awaiting approval. Start with /prospect "<client firm>".')
            return
        edits = _parse_profile_edits(arg[4:])
        if not edits:
            send("Nothing to change. Usage: /profile edit type=<x> assets=<a,b> regions=<r>")
            return
        if "type" in edits:
            pending["type"] = edits["type"]
        if "assets" in edits:
            pending["asset_classes"] = edits["assets"]
        if "regions" in edits:
            pending["target_regions"] = edits["regions"]
        if "focus" in edits:
            pending["pitch_service_focus"] = edits["focus"]
        _pending_profiles[key] = pending
        send("Updated — review:\n\n" + _fmt_profile(pending))
        return

    send("Usage: /profile confirm   OR   "
         "/profile edit type=<x> assets=<a,b> regions=<r> [focus=<f>]")


# ---------------------------------------------------------------------------
# Command handling
# ---------------------------------------------------------------------------
HELP = (
    "X Analyst Outreach bot (Workflow A, test mode).\n\n"
    "/prospect \"<client firm>\"  - find competitors -> Gmail drafts\n"
    "/prospect confirm           - proceed with all found competitors\n"
    "/prospect trim <n, n>       - drop some, then proceed with the rest\n"
    "/prospect refresh           - re-discover (ignore a prior cached list)\n"
    "/prospect pick <n>          - pick a firm when the name was ambiguous\n"
    "/cancel                     - EMERGENCY STOP: abort any run, ready immediately\n"
    "/profile confirm            - approve a newly-derived client profile\n"
    "/profile edit type=.. assets=a,b regions=r  - correct it before approving\n"
    "/qualify \"<firm>\" <AUM>     - rescue a PARKED firm you've verified (e.g. 800M, 1.2B)\n"
    "/disqualify \"<firm>\"        - reject a PARKED firm\n"
    "/requalify                  - re-run the ICP gate (deep re-verify parked)\n"
    "/clients                    - list client profiles (band + focus)\n"
    "/client \"<name>\"           - show one client profile\n"
    "/status                     - dashboard (totals, parked, spend)\n"
    "/status \"<client>\"         - full per-company detail for one run\n"
)


# Straight and curly/smart quotes (iPhone keyboards send U+201C/D, U+2018/9).
_QUOTES = "\"'“”‘’"


def _strip_quotes(s: str) -> str:
    s = s.strip()
    # strip any stray smart quotes, then peel matching wrapping quote pairs
    while len(s) >= 2 and s[0] in _QUOTES and s[-1] in _QUOTES:
        s = s[1:-1].strip()
    return s.strip()


def _parse_quoted(arg: str) -> tuple[str, str]:
    """Split a leading (ideally quoted) firm name from the remainder, for the
    /qualify + /disqualify operator commands. '"CFH Group" 800M' ->
    ('CFH Group', '800M'). If unquoted, only peel a trailing token that looks
    numeric (an AUM) so a multi-word unquoted firm stays intact; otherwise the
    whole string is the firm. (Operators are told to always quote firm names.)"""
    s = (arg or "").strip()
    if not s:
        return "", ""
    if s[0] in _QUOTES:
        for i in range(1, len(s)):
            if s[i] in _QUOTES:
                return s[1:i].strip(), s[i + 1:].strip()
        return s[1:].strip(), ""   # no closing quote: treat the rest as the firm
    parts = s.rsplit(maxsplit=1)
    if len(parts) == 2 and re.search(r"\d", parts[1]):
        return parts[0].strip(), parts[1].strip()
    return s, ""


def _begin_prospect(client: str) -> None:
    """Begin a fresh prospect for a resolved CLIENT NAME (never a subcommand).
    Shared by the /prospect client-name path and /prospect pick, so a picked firm
    name is never re-parsed as a command. Handles stale-state recovery, the
    genuinely-in-progress guard, reuse of a prior run, and the profile gate."""
    _reap_stale_state()
    active = db.get_active_prospect()
    if active and _worker_running():
        # A run is GENUINELY in progress. Never tell the user to "wait forever".
        send(f"A run for {active.get('client')} is in progress. Send /cancel to abort it.")
        return
    if not client:
        send('Usage: /prospect "<client firm name>"')
        return
    # A new prospect supersedes any pending approval/ambiguity so a half-finished
    # gate can never linger against this fresh request.
    key = str(config.TELEGRAM_USER_ID)
    _pending_profiles.pop(key, None)
    _pending_ts.pop(key, None)
    _pending_ambiguous.pop(key, None)

    # Reuse a prior completed run's candidates, re-filtered through the CURRENT
    # exclusion rules so a stale run can't re-serve junk. If nothing survives,
    # fall through to the profile gate / fresh discovery.
    prior = db.latest_completed_run_with_candidates(client)
    if prior:
        cands = [c for c in (prior.get("candidates") or [])
                 if not competitor_engine._is_excluded(c.get("name") or "", c.get("why") or "")]
        if cands:
            run = db.create_run(client)
            db.update_run(run["id"], candidates=cands, status="awaiting_confirmation",
                          niche_summary=prior.get("niche_summary"))
            db.set_active_prospect(client, run["id"])
            date = str(prior.get("created_at"))[:10]
            send(f"Found {len(cands)} candidates from a previous run on {date}. "
                 f"Reply /prospect confirm to reuse them, or /prospect refresh to re-discover.")
            return

    # NEW client with NO saved profile: derive one and gate on human approval.
    # Existing profiles skip this and proceed directly (client comprehension +
    # the ambiguity gate still run inside the worker).
    if not db.get_client_profile(client):
        send(f'No saved profile for "{client}". Deriving one for your approval...')
        _start_thread(_run_derive_profile, client)
        return

    db.set_active_prospect(client)
    send(f"Starting prospecting for: {client}")
    _start_thread(_run_discovery, client)


def _handle_pick(arg: str) -> None:
    """/prospect pick <n>: choose one of the ambiguous-client candidates and begin
    a fresh prospect for that specific firm (via _begin_prospect, NOT the command
    parser — so a firm name like 'Trimont'/'Yes Communities' is not misrouted)."""
    key = str(config.TELEGRAM_USER_ID)
    pend = _pending_ambiguous.get(key)
    # Enforce the same 3-minute expiry at use-time as pending profiles.
    if pend and (time.time() - pend.get("ts", 0)) > _PENDING_TTL_SEC:
        _pending_ambiguous.pop(key, None)
        pend = None
    if not pend:
        send('Nothing to pick. Start with /prospect "<client firm>".')
        return
    alts = pend.get("alternatives") or []
    tok = (arg or "").strip().split()[0] if (arg or "").strip() else ""
    if not tok.isdecimal() or not (1 <= int(tok) <= len(alts)):
        send(f"Usage: /prospect pick <1-{len(alts)}>." if alts
             else "No candidates to pick from. Re-run /prospect with more detail.")
        return
    chosen = alts[int(tok) - 1]
    name = ((chosen.get("name") if isinstance(chosen, dict) else str(chosen)) or "").strip()
    _pending_ambiguous.pop(key, None)
    if not name:
        send('That option has no firm name; re-run /prospect "<full firm name>".')
        return
    send(f"Picking: {name}")
    _begin_prospect(name)   # start the specific firm; bypasses subcommand parsing


def _handle_prospect(arg: str) -> None:
    arg = arg.strip()
    low = arg.lower()
    first_word = low.split()[0] if low.split() else ""

    # /prospect pick <n> -> resolve an earlier ambiguous client
    if first_word == "pick":
        _handle_pick(arg.split(maxsplit=1)[1] if len(arg.split()) > 1 else "")
        return

    # A leading confirm/proceed/yes/go is ALWAYS a confirm command, never a
    # client name (e.g. "confirm to proceed with all" must not become a firm).
    if first_word in ("confirm", "proceed", "yes", "go"):
        run = db.latest_run_awaiting_confirmation()
        if not run:
            send("Nothing awaiting confirmation. Start with /prospect \"<client firm>\".")
            return
        # Re-filter through current exclusion rules so a stale pre-fix awaiting
        # run can't push retailer/broker junk to processing on a bare confirm.
        names = [c["name"] for c in (run.get("candidates") or [])
                 if not competitor_engine._is_excluded(c.get("name") or "", c.get("why") or "")]
        claimed = db.claim_run_for_processing(run["id"], names)
        if not claimed:
            send("That run is already being processed.")
            return
        _start_thread(_run_processing, claimed)
        return

    # /prospect refresh -> force a full new discovery (continues the active
    # client; never treated as a client name).
    if first_word == "refresh":
        active = db.get_active_prospect()
        pending = db.latest_run_awaiting_confirmation()
        client = (active or {}).get("client") or (pending["client_name"] if pending else None)
        if not client:
            send("Nothing to refresh. Start with /prospect \"<client firm>\".")
            return
        # supersede the reuse/pending run so we don't leave two awaiting runs
        if pending and pending["client_name"].lower() == client.lower():
            db.update_run(pending["id"], status="done")
        # A refresh must still respect the profile-approval gate: without a saved
        # profile, run_discovery would silently auto-derive+save an UNAPPROVED
        # profile. Derive for approval instead.
        if not db.get_client_profile(client):
            send(f'No saved profile for "{client}". Deriving one for your approval...')
            _start_thread(_run_derive_profile, client)
            return
        send(f"Refreshing: running a full new discovery for {client}...")
        _start_thread(_run_discovery, client)
        return

    if low.startswith("trim"):
        run = db.latest_run_awaiting_confirmation()
        if not run:
            send("Nothing awaiting confirmation. Start with /prospect \"<client firm>\".")
            return
        candidates = run.get("candidates") or []
        raw = arg[4:].strip().lstrip(":").strip()
        drop_tokens = [t.strip() for t in raw.split(",") if t.strip()]
        keep = []
        dropped = []
        for i, c in enumerate(candidates, 1):
            drop = False
            for tok in drop_tokens:
                if tok.isdigit() and int(tok) == i:
                    drop = True
                elif not tok.isdigit() and tok.lower() in c["name"].lower():
                    drop = True
            (dropped if drop else keep).append(c["name"])
        claimed = db.claim_run_for_processing(run["id"], keep)
        if not claimed:
            send("That run is already being processed.")
            return
        send(f"Dropped {len(dropped)}: {', '.join(dropped) or '(none matched)'}\n"
             f"Proceeding with {len(keep)} firms.")
        _start_thread(_run_processing, claimed)
        return

    # otherwise: a client firm name -> begin a fresh prospect. Route through the
    # shared helper (NOT recursively through the command parser) so a picked firm
    # name is never re-interpreted as a subcommand.
    _begin_prospect(_strip_quotes(arg))


def _handle_cancel() -> None:
    """EMERGENCY STOP. Always works — even mid-discovery, even if the bot thinks
    it's busy. Aborts the running worker, cancels runs, clears the active flag and
    any pending profile, and releases the poller lock (re-taken by the next
    heartbeat). Never says 'wait for it to finish'."""
    _clear_prospect_state()
    try:
        db.release_poller_lock(os.getpid())   # re-acquired on the next poll heartbeat
    except Exception:  # noqa: BLE001
        pass
    send("Cancelled. Ready for a new /prospect.")


def _handle_qualify(arg: str) -> None:
    """/qualify "<firm>" <AUM>: rescue a PARKED firm whose AUM the operator has
    verified. Direct handler — the quoted firm name is an ARGUMENT and is NEVER
    re-dispatched through the command parser (no /prospect-pick collision class)."""
    firm, aum_raw = _parse_quoted(arg)
    if not firm:
        send('Usage: /qualify "<firm>" <AUM>   e.g.  /qualify "CFH Group" 800M')
        return
    aum = competitor_engine.parse_aum(aum_raw)
    if aum is None:
        send(f'I could not read an AUM from "{aum_raw or "(nothing given)"}". '
             f'Try:  /qualify "{firm}" 800M   (also ok: 1.2B, $500M, "800 million").')
        return
    company = db.get_company_by_name(firm, ci=True)
    if not company:
        send(f'No firm named "{firm}" on record. Check the spelling '
             '(see /status "<client>" for the firm list).')
        return
    state = (company.get("icp_result") or "").upper()
    if state != "PARKED":
        send(f'"{company["name"]}" is {state or "not gated"}, not parked — '
             "/qualify only rescues PARKED firms (unverified AUM). Nothing changed.")
        return
    if db.get_active_prospect() and _worker_running():
        send("A run is in progress. Send /cancel first, then /qualify again.")
        return
    human = competitor_engine._human_usd(aum) or f"${aum:,.0f}"
    send(f'Qualifying "{company["name"]}" at {human} — finding contacts, emails, draft...')
    _start_thread(_run_qualify, company["id"], aum)


def _handle_disqualify(arg: str) -> None:
    """/disqualify "<firm>": reject a PARKED firm. Direct handler (firm name is
    an argument, never re-dispatched as a command)."""
    firm, _rest = _parse_quoted(arg)
    if not firm:
        send('Usage: /disqualify "<firm>"   e.g.  /disqualify "Timberlane Partners"')
        return
    company = db.get_company_by_name(firm, ci=True)
    if not company:
        send(f'No firm named "{firm}" on record.')
        return
    state = (company.get("icp_result") or "").upper()
    if state != "PARKED":
        send(f'"{company["name"]}" is {state or "not gated"}, not parked — '
             "/disqualify only applies to PARKED firms. Nothing changed.")
        return
    result = competitor_engine.manual_disqualify(company["id"], progress=send)
    send(result["message"])


def _handle_status(arg: str) -> None:
    arg = _strip_quotes(arg)
    body = status.detail_text(arg) if arg else status.dashboard_text()
    warns = expiry_warnings()          # Fix 8: prepend <30-day expiry warnings
    send(("\n".join(warns) + "\n\n" + body) if warns else body)


def _dispatch(text: str) -> None:
    text = text.strip()
    if not text.startswith("/"):
        low = text.lower()
        # a bare "cancel"/"stop" is an emergency stop; "proceed"/"confirm" acts on a checkpoint
        if low in ("cancel", "stop", "abort"):
            _handle_cancel()
        elif low in ("proceed", "confirm", "yes", "go"):
            _handle_prospect("confirm")
        return
    parts = text.split(maxsplit=1)
    cmd = parts[0].lstrip("/").split("@")[0].lower()
    arg = parts[1] if len(parts) > 1 else ""

    if cmd == "prospect":
        _handle_prospect(arg)
    elif cmd == "cancel":
        _handle_cancel()
    elif cmd == "profile":
        _handle_profile(arg)
    elif cmd == "qualify":
        _handle_qualify(arg)
    elif cmd == "disqualify":
        _handle_disqualify(arg)
    elif cmd == "status":
        _handle_status(arg)
    elif cmd == "clients":
        send(status.clients_text())
    elif cmd == "client":
        send(status.client_text(_strip_quotes(arg)))
    elif cmd == "requalify":
        send("Requalifying (deep re-verify of parked firms)...")
        send(competitor_engine.format_requalify(competitor_engine.requalify_all(progress=send)))
    elif cmd in ("start", "help"):
        send(HELP)
    else:
        send(f"Unknown command: {cmd}\n\n{HELP}")


# ---------------------------------------------------------------------------
# Startup health check (Fix 1) + expiry warnings (Fix 8)
# ---------------------------------------------------------------------------
def _short(e: Exception, n: int = 60) -> str:
    s = str(e).replace("\n", " ").strip()
    return s[:n] + ("…" if len(s) > n else "")


def _gmail_token_days_left(validity_days: int = 180) -> Optional[int]:
    """Approx days left on the Gmail OAuth token = file mtime + ~6-month validity
    (the refresh-token expiry is not stored in the file, so this is the documented
    heuristic). None if the token file is absent. No API call."""
    try:
        mtime = os.path.getmtime(config.GMAIL_TOKEN_PATH)
    except OSError:
        return None
    return int(validity_days - (time.time() - mtime) / 86400)


def _azure_credits_days_left() -> Optional[int]:
    """Days until AZURE_CREDITS_END (a plain .env date), or None if unset/bad."""
    raw = config.AZURE_CREDITS_END
    if not raw:
        return None
    try:
        from datetime import date
        y, m, d = (int(x) for x in str(raw).strip().split("-"))
        return (date(y, m, d) - date.today()).days
    except (ValueError, TypeError):
        return None


def expiry_warnings() -> list[str]:
    """<30-day expiry warnings for Gmail token + Azure credits (shared by /status
    and startup). Empty when nothing is close to expiring."""
    out = []
    gd = _gmail_token_days_left()
    if gd is not None and gd < 30:
        out.append(f"⚠ Gmail token expires in {gd} days — re-auth soon.")
    ad = _azure_credits_days_left()
    if ad is not None and ad < 30:
        out.append(f"⚠ Azure credits end in {ad} days — top up soon.")
    return out


def _startup_health() -> str:
    """Ping each external API once (short timeout) and return a one-line status.
    A failed PING never blocks startup (only missing env vars do)."""
    parts = []

    def mark(name: str, fn) -> None:
        try:
            fn()
            parts.append(f"✓ {name}")
        except Exception as e:  # noqa: BLE001
            parts.append(f"✗ {name} ({_short(e)})")

    mark("Exa", lambda: exa_client.search("real estate investment firm", max_results=1))
    mark("Tavily", lambda: search_client.search("real estate investment firm", max_results=1))
    mark("LLM", models.ping)
    mark("Apollo", apollo.healthcheck)
    line = "Startup: " + " ".join(parts)
    gd = _gmail_token_days_left()
    if gd is not None:
        line += f" | Gmail token: {gd}d left"
    return line


# ---------------------------------------------------------------------------
# Poll loop
# ---------------------------------------------------------------------------
def run_bot() -> None:
    # Fix 1: verify the FULL env surface up front. Missing any -> announce (best
    # effort, in case Telegram creds are present) and refuse to start half-configured.
    missing = config.missing_keys(config.REQUIRED_FOR_STARTUP)
    if missing:
        # ASCII-only: this prints to stdout (which may be non-UTF-8 on Windows)
        # before we exit, so it must never itself raise UnicodeEncodeError.
        msg = "Refusing to start - missing config: " + ", ".join(missing)
        print(msg, flush=True)
        try:
            send(msg)
        except Exception:  # noqa: BLE001
            pass
        raise SystemExit(msg)
    try:
        loaded = db.sync_templates_from_folder()
    except Exception as e:  # noqa: BLE001
        raise SystemExit(f"Cannot reach the database: {e}")

    allowed = str(config.TELEGRAM_USER_ID)
    pid = os.getpid()

    # Single-poller guarantee (authoritative): a DB heartbeat lock. Refuse to
    # start while another instance holds a fresh lock. This is what actually
    # prevents the doubled messages / overlapping jobs that were reported.
    acquired, holder = db.acquire_poller_lock(pid)
    if not acquired:
        holder = holder or {}
        age = int(time.time() - holder.get("ts", 0)) if holder.get("ts") else "?"
        raise SystemExit(
            f"[pid {pid}] Refusing to start: another poller holds the lock "
            f"(pid {holder.get('pid')}, heartbeat {age}s ago). Stop it first, or "
            f"wait up to {db.POLLER_TTL_SEC}s for a stale lock to clear."
        )

    # Clean startup ALWAYS: cancel any non-terminal runs left by a dead process and
    # clear the active-prospect flag, so the bot never boots "permanently in
    # progress". Also drop any in-memory pending profiles. Never crash on leftover
    # state (this replaces the old reconcile path that re-asserted the flag and
    # crashed on `active['client']`).
    # Capture any interrupted run BEFORE cleanup wipes the flag (Fix 6.4).
    interrupted = None
    try:
        interrupted = db.get_active_prospect()
        inc = db.latest_incomplete_run()
    except Exception:  # noqa: BLE001
        inc = None
    try:
        cleaned = db.startup_cleanup()
    except Exception as e:  # noqa: BLE001
        cleaned = {"flag_cleared": False, "runs_cancelled": 0}
        print(f"[pid {pid}] startup_cleanup warning: {e}", flush=True)
    _pending_profiles.clear()
    _pending_ts.clear()
    _cancel.clear()
    if cleaned.get("flag_cleared") or cleaned.get("runs_cancelled"):
        print(f"[pid {pid}] cleared stale state: flag={cleaned.get('flag_cleared')}, "
              f"runs_cancelled={cleaned.get('runs_cancelled')}", flush=True)

    # Skip the backlog so a restart never replays old commands.
    offset: Optional[int] = None
    try:
        backlog = get_updates(None, timeout=0)
    except PollerConflict:
        backlog = []
    if backlog:
        offset = backlog[-1]["update_id"] + 1

    send(f"X Analyst bot online (test mode, pid {pid}). Templates loaded: {loaded}.\n\n{HELP}")
    # Fix 1/8: one health line (env already verified; pings never block startup).
    try:
        health = _startup_health()
        for w in expiry_warnings():
            health = f"{w}\n{health}"
        send(health)
        print(health, flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"[pid {pid}] startup health check error: {e}", flush=True)
    # Fix 6.4: announce an interrupted prior run so the operator can resume/discard.
    if interrupted:
        # startup_cleanup already cancelled the run + cleared the flag, so there is
        # nothing to confirm or cancel — just inform and point to a clean re-run.
        client = interrupted.get("client") if isinstance(interrupted, dict) else None
        counts = (inc or {}).get("counts") or {}
        done = counts.get("researched")
        total = len((inc or {}).get("candidates") or []) or None
        if client and done and total:
            send(f'⚠ Bot restarted. The previous run for "{client}" was interrupted at firm '
                 f'{done}/{total} and has been cleared. Re-run with /prospect "{client}" when ready.')
        elif client:
            send(f'⚠ Bot restarted. The previous run for "{client}" was interrupted and cleared. '
                 f'Re-run with /prospect "{client}" when ready.')
        else:
            send('⚠ Bot restarted after an interrupted run (now cleared). '
                 'Start a fresh /prospect "<client>".')
    print(f"Bot polling (pid {pid}). Ctrl-C to stop.", flush=True)

    try:
        while True:
            db.heartbeat_poller_lock(pid)
            try:
                updates = get_updates(offset)
            except PollerConflict as e:
                # A second poller displaced our long-poll. It should have been
                # refused by the lock; exit as a backstop to stay single.
                print(f"[pid {pid}] getUpdates 409: {e}. Exiting to keep one poller.",
                      flush=True)
                raise SystemExit(1)
            for upd in updates:
                offset = upd["update_id"] + 1
                msg = upd.get("message") or upd.get("edited_message")
                if not msg:
                    continue
                if str(msg.get("from", {}).get("id")) != allowed:
                    continue  # ignore everyone but the owner
                text = msg.get("text", "")
                if text:
                    try:
                        _dispatch(text)
                    except Exception as e:  # noqa: BLE001
                        send(f"Error handling command: {e}")
    finally:
        db.release_poller_lock(pid)


def whoami() -> None:
    """Deploy helper: poll once and print the chat_id / sender of the most
    recent message, so the operator can capture TELEGRAM_USER_ID for .env.
    Run this while NO bot poller is active (Harpreet messages the bot first)."""
    updates = get_updates(None, timeout=10)
    msgs = [u.get("message") or u.get("edited_message") for u in updates]
    msgs = [m for m in msgs if m]
    if not msgs:
        print("No messages found. Have the user send a message to the bot, then re-run.")
        return
    last = msgs[-1]
    frm = last.get("from", {})
    chat = last.get("chat", {})
    print(f"chat_id (put in TELEGRAM_USER_ID): {frm.get('id')}")
    print(f"  from: @{frm.get('username')} ({frm.get('first_name')}) | chat.id={chat.get('id')} type={chat.get('type')}")
    print(f"  text: {last.get('text','')[:80]!r}")


if __name__ == "__main__":
    import sys

    if "--whoami" in sys.argv:
        whoami()
    else:
        run_bot()

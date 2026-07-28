"""Telegram interface (§6). Polling mode, mirrors Workflow A's proven
mechanics (single-poller DB lock, 0.3s send pacing, backlog skip on restart)
but with Workflow C's own command set and its own operator allowlist.

HARD RULE / §10d: authorized operators come ONLY from .env
AUTHORIZED_USER_IDS (comma-separated). Never hardcode a single user id.

`_dispatch` is the testable core: a pure command router taking a `send`
callback, so command logic can be exercised without a live bot token.
`run_bot()` wires it to the real Telegram getUpdates/sendMessage polling loop.
"""
from __future__ import annotations

import json
import re
import sys
import threading
import time
from typing import Optional

import requests

from . import batch_upload, config, dbc, pipeline, status
from .loader import LoaderError, reload_connections

_TG_LIMIT = 3800
_QUOTES = "\"'“”‘’"

_busy = threading.Lock()

# Feature 2: ephemeral state for the >2000-row confirmation gate and the
# "awaiting a file after a bare /referral batch" handshake (same in-memory-
# dict pattern Workflow A uses for pending profiles/ambiguous-client state —
# no schema needed for something this transient). Keyed by a single-operator
# state key (matches send()'s existing default-to-first-authorized-id
# simplification — this codebase doesn't thread per-chat state anywhere else).
_pending_file_batch: dict[str, dict] = {}
_awaiting_batch_file: dict[str, float] = {}
_PENDING_FILE_BATCH_TTL_SEC = 600  # 10 min
_AWAITING_BATCH_FILE_TTL_SEC = 600  # 10 min

# FIX 23 (real-mode incident, repeated 4x in one session): "/referral batch
# confirm to proceed" (a natural, human way to reply) did not match the old
# exact `rest_low == "confirm"` check, fell through to the inline-firm-names
# path, and silently created a NEW pending batch of 1 firm literally named
# "confirm to proceed" -- overwriting whatever real batch was actually
# awaiting confirmation. `\b` (not just startswith) so "confirm"/"cancel"
# match as a whole leading word ("confirm to proceed", "confirm please",
# "confirm.", "CONFIRM") without also matching a word that merely starts
# with the same letters ("confirmation").
_BATCH_CONFIRM_RE = re.compile(r"^confirm\b", re.IGNORECASE)
_BATCH_CANCEL_RE = re.compile(r"^cancel\b", re.IGNORECASE)

# FIX 23 (adversarial re-review): the "batch" sub-command dispatch used a
# bare `low.startswith("batch")`, no word boundary at all -- a single-firm
# command whose name happens to start with the literal letters "batch"
# (e.g. "/referral Batchelor Real Estate Partners") was misrouted into the
# batch-parsing path and truncated by the arg[len("batch"):] slice.
_BATCH_PREFIX_RE = re.compile(r"^batch\b", re.IGNORECASE)

# Adversarial re-review of the first FIX 23 draft (same session) found the
# first design -- "confirm"/"cancel" as a leading word, blocked ONLY by a
# blanket newline/comma bail-out -- had an inverse blind spot: it couldn't
# tell a genuine confirm/cancel REPLY apart from a real, single-line firm
# name that merely STARTS with the same word ("Confirm Realty LLC"), which
# it then silently swallowed as a confirm/cancel command -- either firing
# against the WRONG pending batch (if an unrelated one happened to be
# pending) or discarding the firm entirely (if nothing was pending). The
# fix: only treat the keyword as a real confirm/cancel reply if EVERY word
# after it is common filler ("to proceed", "please", "thanks", ...) -- a
# real firm name's remainder ("Realty LLC") never is, so it always falls
# through to the firm-name path, alone or as part of a real multi-firm
# list, regardless of whether that list happens to contain a newline or a
# comma (which are no longer treated as a special "must be a list" signal
# -- "confirm, thanks" and "confirm to proceed\nthanks!" are just as much a
# genuine confirm reply as "confirm to proceed" is).
_CONFIRM_FILLER_WORDS = {"to", "proceed", "please", "now", "go", "ahead",
                        "thanks", "thank", "you", "yes", "ok", "sure"}
_FILLER_STRIP_CHARS = " \t\n\r,.!?;:'\""


def _remainder_is_filler_only(remainder: str) -> bool:
    """True if `remainder` (everything after the confirm/cancel keyword)
    consists ENTIRELY of common filler words -- i.e. is a natural
    continuation of a confirm/cancel reply, not the start of a real firm
    name. Empty remainder (bare "confirm"/"CONFIRM.") is vacuously True."""
    words = re.split(r"[\s,]+", remainder.strip(_FILLER_STRIP_CHARS))
    return all(w.strip(_FILLER_STRIP_CHARS).lower() in _CONFIRM_FILLER_WORDS
              for w in words if w)


# Bare command words that must never be researched as a company name, nor
# silently accepted as a batch entry -- if a name reduces to exactly one of
# these (case-insensitive), something upstream (a mistyped confirm/cancel
# reply, a mangled paste) produced a command fragment, not a firm.
_COMMAND_FRAGMENT_WORDS = {"confirm", "cancel", "proceed", "yes", "ok", "stop"}


def _state_key() -> str:
    return str(config.AUTHORIZED_USER_IDS[0]) if config.AUTHORIZED_USER_IDS else "default"


def _matches_batch_keyword(rest: str, keyword_re: "re.Pattern[str]") -> bool:
    """True if `rest` (already stripped) IS a confirm/cancel reply: the
    keyword as a leading whole word, followed by nothing but filler. A real
    firm name that merely STARTS with "Confirm"/"Cancel" (e.g. "Confirm
    Realty LLC", or "Confirm Realty LLC\\nOther Firm" as item 1 of a real
    multi-firm list) is never misread, because its remainder contains real
    words the filler allowlist doesn't recognize -- it falls through to the
    firm-name path intact either way."""
    m = keyword_re.match(rest)
    if not m:
        return False
    return _remainder_is_filler_only(rest[m.end():])


def _looks_like_command_fragment(name: str) -> bool:
    return (name or "").strip().lower() in _COMMAND_FRAGMENT_WORDS


def _command_fragment_rejection(names: list[str]) -> str:
    listed = ", ".join(f'"{n}"' for n in names)
    return (f"{listed} — looks like a leftover confirm/cancel word, not a firm name. "
           f"Nothing started. If you meant to respond to a pending batch, reply "
           f"/referral batch confirm or /referral batch cancel.")


def _get_active_pending_batch() -> Optional[dict]:
    """Returns the current operator's pending file/inline batch if one
    exists and hasn't expired -- popping (and discarding) it first if it
    HAS expired, so an expired entry never counts as "still pending" for
    either the confirm/cancel handlers or the new-batch overwrite guard
    below. None if there is no pending batch at all."""
    key = _state_key()
    pending = _pending_file_batch.get(key)
    if not pending:
        return None
    if (time.time() - pending["ts"]) > _PENDING_FILE_BATCH_TTL_SEC:
        _pending_file_batch.pop(key, None)
        return None
    return pending


# FIX 18 (real-mode incident): the bot's startup/help banner hardcoded the
# literal string "test mode" regardless of config.TEST_MODE's actual value --
# a live run (.env TEST_MODE=false) that created a REAL Gmail draft in
# Harpreet's account still announced itself as test mode. Read live, at
# render time, not baked into a module-level constant at import time, so it
# can never drift from the config value actually in effect for this process.
def _mode_banner() -> str:
    if config.TEST_MODE:
        return "TEST MODE - no real Gmail drafts, no API spend"
    return "LIVE - creates real Gmail drafts in Harpreet's account"


def _api_base() -> str:
    return f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN_C}"


def _split_for_telegram(text: str, limit: int = _TG_LIMIT) -> list[str]:
    """Splits `text` into <= `limit`-char chunks WITHOUT cutting a line in
    half wherever avoidable. FIX 25 (adversarial re-review): showing
    DISQUALIFIED/PARKED reasons in full (no more 70-char truncation) means
    a many-firm chunk verdict message can exceed `limit` -- the OLD raw
    `text[i:i+limit]` slicing would then cut a reason in the middle of a
    word, reproducing via message-splitting the exact "looks cut off
    mid-sentence" symptom this fix was meant to eliminate. Falls back to a
    raw slice ONLY for a single line that alone exceeds `limit` (nothing
    else to do with a line that long)."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
        if len(line) <= limit:
            current = line
        else:
            for i in range(0, len(line), limit):
                chunks.append(line[i:i + limit])
            current = ""
    if current:
        chunks.append(current)
    return chunks


def send(text: str, chat_id: Optional[str] = None) -> None:
    chat_id = chat_id or (config.AUTHORIZED_USER_IDS[0] if config.AUTHORIZED_USER_IDS else None)
    if not chat_id:
        return
    for chunk in _split_for_telegram(text):
        try:
            requests.post(f"{_api_base()}/sendMessage", json={"chat_id": chat_id, "text": chunk}, timeout=30)
        except requests.RequestException:
            pass
        time.sleep(0.3)  # Workflow A lesson: pace consecutive sends


# ---------------------------------------------------------------------------
# FIX 25 (real-mode incident): a 13-firm batch showed no Telegram output for
# ~5 minutes between the chunk header and the chunk result -- looked hung.
# send() is fire-and-forget (no return value) by design, used everywhere
# else in this module; the live batch-status message (below) is the first
# place this codebase needs the sent message's message_id back, so it can
# later editMessageText the SAME message in place instead of sending a new
# one per update -- Telegram rate-limits a bot to ~20 messages/minute per
# chat, and 8 phases x 10 firms would blow past that fast.
# ---------------------------------------------------------------------------
def _send_and_get_id(text: str, chat_id: Optional[str] = None) -> Optional[int]:
    chat_id = chat_id or (config.AUTHORIZED_USER_IDS[0] if config.AUTHORIZED_USER_IDS else None)
    if not chat_id:
        return None
    try:
        r = requests.post(f"{_api_base()}/sendMessage", json={"chat_id": chat_id, "text": text}, timeout=30)
        r.raise_for_status()
        return r.json()["result"]["message_id"]
    except (requests.RequestException, KeyError, ValueError, TypeError) as e:
        print(f"[LIVE STATUS] sendMessage failed, live status disabled for this run: {e}",
             file=sys.stderr, flush=True)
        return None


def _edit_message(message_id: int, text: str, chat_id: Optional[str] = None) -> bool:
    """Updates a previously-sent message in place via Telegram's
    editMessageText. NEVER raises -- a failed or rate-limited edit is
    logged and swallowed; the caller (the live batch-status updater) treats
    a False return as "this update didn't land, try again next time," never
    as a reason to slow down or abort the actual pipeline run."""
    chat_id = chat_id or (config.AUTHORIZED_USER_IDS[0] if config.AUTHORIZED_USER_IDS else None)
    if not chat_id:
        return False
    try:
        r = requests.post(f"{_api_base()}/editMessageText",
                          json={"chat_id": chat_id, "message_id": message_id, "text": text}, timeout=30)
        if r.status_code != 200:
            print(f"[LIVE STATUS] editMessageText failed ({r.status_code}): {r.text[:200]}",
                 file=sys.stderr, flush=True)
            return False
        return True
    except requests.RequestException as e:
        print(f"[LIVE STATUS] editMessageText failed: {e}", file=sys.stderr, flush=True)
        return False


LIVE_STATUS_MIN_INTERVAL_SEC = 3


def _parse_live_signal(msg: str) -> Optional[dict]:
    if not msg.startswith(pipeline.LIVE_STATUS_MARK):
        return None
    try:
        signal = json.loads(msg[len(pipeline.LIVE_STATUS_MARK):])
    except (ValueError, TypeError):
        return None
    return signal if isinstance(signal, dict) else None


def _dispatch_edit(message_id: int, text: str, chat_id: Optional[str]) -> None:
    """Runs _edit_message on a background daemon thread. FIX 25 (adversarial
    re-review): _LiveBatchStatus.__call__ IS the progress() callback
    drain_queue/process_referral_firm invoke directly and synchronously --
    a slow-but-eventually-200 editMessageText response (not just an
    outright failure) previously blocked the CALLING thread, i.e. the
    actual pipeline run, for however long that HTTP call took. This is the
    one seam that keeps the HTTP work off that thread entirely; the
    throttle window and dedup state are claimed by the caller BEFORE this
    is invoked (see _LiveBatchStatus._maybe_edit), never inside here."""
    threading.Thread(target=_edit_message, args=(message_id, text, chat_id), daemon=True).start()


_STOPPED_LABEL = {"done": "Done.", "cancelled": "Cancelled.", "crashed": "Crashed."}


class _LiveBatchStatus:
    """FIX 25 (real-mode incident): a 13-firm batch showed no Telegram
    output for ~5 minutes between the chunk header and the chunk result --
    looked hung. Telegram rate-limits a bot to ~20 messages/minute per
    chat; sending one message per firm per phase (8 phases x 10 firms)
    would blow past that and start failing mid-run. Instead: ONE status
    message, sent once at batch start via start(), edited in place via
    editMessageText as pipeline.py's progress() callback reports phase/
    count/stopped signals -- throttled to at most one edit attempt every
    LIVE_STATUS_MIN_INTERVAL_SEC seconds, and skipped entirely if the
    rendered text hasn't changed since the last edit attempt.

    Wired in as the `progress` argument to pipeline's drain_queue (NOT
    `notify`, the existing chunk-grouped Telegram channel -- see
    _drain_with_live_status below) so it sees every phase transition for
    every firm, not just chunk boundaries. Every message (recognized
    signal or not) is also forwarded to pipeline.server_log() so stderr
    debug visibility is unchanged from before this fix -- this class is a
    strict addition, not a replacement, of the existing progress channel.

    FIX 25 (adversarial re-review): the actual editMessageText HTTP call
    is dispatched on a background daemon thread (_maybe_edit below) --
    __call__ (which IS the progress() callback drain_queue/
    process_referral_firm invoke directly and synchronously) must return
    almost immediately no matter how slow or unreachable Telegram is;
    without this, a slow-but-eventually-200 response (not just an outright
    failure) blocked the CALLING thread -- i.e. the actual pipeline run --
    for however long that HTTP call took, directly violating "no pipeline
    call blocks on the HTTP request." The throttle window is also now
    claimed synchronously and unconditionally (before dispatching, not
    inside a success branch) -- previously it only advanced on a
    successful edit, so a sustained failure/rate-limit streak (the exact
    condition this feature exists to survive) caused every subsequent
    signal to retry immediately with no backoff at all, competing for the
    same rate-limit budget as the failures themselves."""

    def __init__(self, chat_id: Optional[str] = None):
        self._chat_id = chat_id
        self._message_id: Optional[int] = None
        self._last_edit_at: float = 0.0
        self._last_sent_text: Optional[str] = None
        self._firm = ""
        self._phase = ""
        self._ordinal = 0
        self._total: Optional[int] = None
        self._qualified = 0
        self._parked = 0
        self._disqualified = 0
        self._drafted = 0
        self._stopped_reason: Optional[str] = None

    def start(self, total: int) -> None:
        self._total = total
        text = self._render()
        self._message_id = _send_and_get_id(text, self._chat_id)
        self._last_sent_text = text
        self._last_edit_at = time.time()

    def __call__(self, msg: str) -> None:
        pipeline.server_log(msg)
        signal = _parse_live_signal(msg)
        if signal is None:
            return
        kind = signal.get("kind")
        if kind == "firm_start":
            self._firm = signal.get("firm") or self._firm
            self._ordinal = signal.get("ordinal", self._ordinal)
            self._total = signal.get("total", self._total)
            self._phase = ""
        elif kind == "phase":
            # Only trust a phase update for whichever firm firm_start most
            # recently announced -- guards against a stale/late signal for
            # a firm that's already finished from ever overwriting the
            # CURRENT firm's phase label.
            if signal.get("firm") == self._firm:
                self._phase = signal.get("phase") or self._phase
        elif kind == "counts":
            self._qualified = signal.get("qualified", self._qualified)
            self._parked = signal.get("parked", self._parked)
            self._disqualified = signal.get("disqualified", self._disqualified)
            self._drafted = signal.get("drafted", self._drafted)
        elif kind == "stopped":
            # FIX 25 (adversarial re-review): without this, the live
            # message froze at whatever firm/phase was last announced on
            # EVERY exit path (cancellation, circuit breaker, crash, and
            # even plain successful completion) -- looking exactly like
            # the "is this actually still running?" confusion this whole
            # feature exists to eliminate, just relocated to the stop/
            # abort moment instead of mid-batch.
            self._stopped_reason = signal.get("reason") or "done"
        else:
            return
        self._maybe_edit()

    def _render(self) -> str:
        total_s = f"{self._ordinal}/{self._total}" if self._total is not None else str(self._ordinal)
        lines = [f"Batch: {total_s} firms"]
        if self._stopped_reason is not None:
            lines.append(_STOPPED_LABEL.get(self._stopped_reason, f"Stopped: {self._stopped_reason}"))
        elif self._firm:
            now_line = f"Now: {self._firm}"
            if self._phase:
                now_line += f" — {self._phase}"
            lines.append(now_line)
        lines.append(f"Qualified {self._qualified} · Parked {self._parked} · "
                     f"Disqualified {self._disqualified} · Drafted {self._drafted}")
        return "\n".join(lines)

    def _maybe_edit(self) -> None:
        if self._message_id is None:
            return
        now = time.time()
        if now - self._last_edit_at < LIVE_STATUS_MIN_INTERVAL_SEC:
            return
        text = self._render()
        if text == self._last_sent_text:
            return
        # Claim the throttle window and the dedup state SYNCHRONOUSLY and
        # UNCONDITIONALLY, before the HTTP call even starts -- not inside a
        # "did it succeed" branch. This is what keeps the throttle honest
        # during a failure/rate-limit streak (the exact condition it
        # exists to survive): the NEXT signal always waits out the full
        # window regardless of whether THIS attempt lands. The tradeoff --
        # if this specific attempt fails, an identical future render could
        # be skipped as a false "unchanged" match -- is deliberately
        # accepted (an occasional missed update) over the alternative
        # (retrying every attempt, unbounded, or blocking the pipeline to
        # find out).
        self._last_edit_at = now
        self._last_sent_text = text
        _dispatch_edit(self._message_id, text, self._chat_id)


def _drain_with_live_status(run_id: int) -> dict:
    """FIX 25: wires a live-editing status message as the `progress`
    channel for drain_queue() -- `notify` (the existing chunk-grouped
    channel, still exactly the per-chunk verdict/enrichment/summary
    messages) is completely unchanged.

    FIX 25 (adversarial re-review): `total` is the PENDING queue count, not
    every wc_firms row ever created for the run -- a match_and_enqueue-
    stage parked_ambiguous firm is resolved before this is ever called and
    its queue row goes straight to "done", so counting it here would make
    the live message's denominator permanently unreachable by the
    numerator. Matches drain_queue's own `live_total` computation exactly
    (queried once, up front) -- this initial value is only a placeholder
    anyway; drain_queue's own first `firm_start` signal overwrites it
    moments later with the identical, authoritative number."""
    total = dbc.queue_counts(run_id).get("pending", 0)
    live = _LiveBatchStatus()
    live.start(total)
    return pipeline.drain_queue(run_id, progress=live, notify=send,
                                chunk_size=config.BATCH_CHUNK_SIZE, chunk_style=True)


def _strip_quotes(s: str) -> str:
    s = (s or "").strip()
    while len(s) >= 2 and s[0] in _QUOTES and s[-1] in _QUOTES:
        s = s[1:-1].strip()
    return s


def _parse_quoted(arg: str) -> tuple[str, str]:
    """'"Firm" 800M' -> ('Firm', '800M'). Unquoted: only peel a trailing
    numeric-looking token; otherwise the whole string is the firm name."""
    s = (arg or "").strip()
    if not s:
        return "", ""
    if s[0] in _QUOTES:
        for i in range(1, len(s)):
            if s[i] in _QUOTES:
                return s[1:i].strip(), s[i + 1:].strip()
        return s[1:].strip(), ""
    parts = s.rsplit(maxsplit=1)
    if len(parts) == 2 and re.search(r"\d", parts[1]):
        return parts[0].strip(), parts[1].strip()
    return s, ""


def _fmt_summary(run_id: int, totals: dict) -> str:
    firms = dbc.firms_for_run(run_id)
    total_firms = len(firms)
    parked = [f for f in firms if f.get("icp_status") == "parked"]
    drafts = [dbc.wc_draft_for_firm(f["id"]) for f in firms]
    drafts = [d for d in drafts if d]
    n_ppt = sum(1 for d in drafts if d.get("ppt_attached"))
    n_drafted = totals.get("drafted", 0)
    cancelled = bool(totals.get("cancelled"))

    header = (f"\U0001F6D1 Referral run cancelled — {total_firms} firm(s) processed before stopping"
             if cancelled else f"✅ Referral run done — {total_firms} firm(s) processed")
    lines = [header]
    lines.append(f"   ✉️ {n_drafted} draft(s) delivered to Gmail ({n_ppt} with PPT attached)")
    lines.append(f"   ⛔ {totals.get('disqualified', 0)} disqualified (ICP)")
    if parked:
        lines.append(f"   \U0001F17F️ {len(parked)} parked — REVIEW THESE:")
        for i, f in enumerate(parked, 1):
            lines.append(f"      {i}. {f['firm_name']} — {f.get('icp_reason') or f.get('park_reason')}")
    lines.append(f"   \U0001F4E7 {totals.get('no_email_found', 0)} qualified but no email found")
    by_firm_id = {f["id"]: f for f in firms}
    dup_drafts = [d for d in drafts if d.get("email_status") == "skipped_duplicate"]
    if dup_drafts:
        # Feature 3: report skips by name, not just a bare count — "single-firm
        # names who they collided with; batch lists them in the skipped section".
        lines.append(f"   \U0001F501 {len(dup_drafts)} skipped, already contacted:")
        for d in dup_drafts:
            firm = by_firm_id.get(d["firm_id"], {})
            note = d.get("collision_note") or "already contacted"
            lines.append(f"      - {firm.get('firm_name', '?')} — {note}")
    else:
        lines.append(f"   \U0001F501 {totals.get('skipped_duplicate', 0)} skipped, already contacted")
    # FIX 38 Part A: a cross-run prior-contact hit no longer skips -- it
    # drafts, and is flagged here loudly instead, naming the person/firm/
    # prior date, same wording the old skip message carried. Distinguished
    # from a plain draft by carrying a collision_note despite email_status
    # being 'drafted' (never possible before this fix).
    advisory_drafts = [d for d in drafts if d.get("email_status") == "drafted" and d.get("collision_note")]
    if advisory_drafts:
        lines.append(f"   ⚠️ {len(advisory_drafts)} drafted, previously drafted before "
                     f"-- check your sent mail before sending:")
        for d in advisory_drafts:
            firm = by_firm_id.get(d["firm_id"], {})
            lines.append(f"      - {firm.get('firm_name', '?')} — {d['collision_note']}")
    if n_drafted and not config.PITCH_MASTER_PPTX.exists():
        # §3 graceful degradation: exact required wording.
        lines.append("   PPT skipped: master template not found.")
    lines.append("")
    if cancelled:
        remaining = dbc.queue_counts(run_id).get("pending", 0)
        lines.append(f"{remaining} firm(s) remain unprocessed in the queue.")
        lines.append("/referral resume will continue from where it stopped.")
    else:
        lines.append("Check Gmail drafts folder.")
    return "\n".join(lines)


def _run_single_firm(firm_query: str) -> None:
    if not _busy.acquire(blocking=False):
        send("A run is already in progress. Please wait for it to finish.")
        return
    try:
        run = dbc.create_wc_run("single_firm", query_firm=firm_query)
        send(f"\U0001F517 Referral run started: matching \"{firm_query}\" against Manpreet's connections")
        summary = pipeline.match_and_enqueue(run["id"], [firm_query])
        if summary["ready"] == 0:
            if summary["no_connection"]:
                send(f'No connections found for "{firm_query}" in Manpreet\'s list.')
            elif summary["referrer_own_firm"]:
                send(f'"{firm_query}" is the referrer\'s own firm — never targeted.')
            elif summary["sender_own_firm"]:
                send(f'"{firm_query}" is our own firm — never targeted.')
            elif summary["parked_ambiguous"]:
                send(f'"{firm_query}" matched ambiguously — see /status c "{firm_query}" and '
                    f're-run with the exact firm name.')
            dbc.update_wc_run(run["id"], status="done")
            return
        # chunk_style defaults to False here: single-firm mode gets Feature 1's
        # prospect brief instead of Feature 4's grouped-chunk batch messaging.
        totals = pipeline.drain_queue(run["id"], notify=send, chunk_size=config.BATCH_CHUNK_SIZE)
        if not totals.get("aborted"):
            dbc.update_wc_run(run["id"], status="done")
        send(_fmt_summary(run["id"], totals))
        if totals.get("drafted", 0) >= 1:
            firms = dbc.firms_for_run(run["id"])
            drafted_firm = next((f for f in firms if (dbc.wc_draft_for_firm(f["id"]) or {})
                                .get("email_status") == "drafted"), None)
            if drafted_firm:
                send(status.prospect_brief_text(drafted_firm["id"]))
    except Exception as e:  # noqa: BLE001
        send(f"Referral run failed: {e}")
    finally:
        _busy.release()


def _run_batch(firm_names: list[str]) -> None:
    if not _busy.acquire(blocking=False):
        send("A run is already in progress. Please wait for it to finish.")
        return
    try:
        run = dbc.create_wc_run("batch")
        send(f"\U0001F517 Referral batch started: {len(firm_names)} firm(s) submitted")
        summary = pipeline.match_and_enqueue(run["id"], firm_names)
        send(f"Free filter: {summary['ready']} matched → paid pipeline, "
            f"{summary['no_connection']} no_connection, "
            f"{summary['referrer_own_firm']} referrer's own firm skipped, "
            f"{summary['sender_own_firm']} our own firm skipped, "
            f"{summary['parked_ambiguous']} ambiguous matches parked.")
        # Feature 4: chunk_style=True — grouped per-chunk verdict/enrichment/
        # summary messages, never one send per firm. All three batch entry
        # paths (text, file-upload, resume) share this SAME call site pattern.
        # FIX 25: `progress` is now a live-editing status message (see
        # _drain_with_live_status) instead of the default stderr-only
        # server_log -- `notify` (this exact chunk-grouped channel) is
        # unaffected.
        totals = _drain_with_live_status(run["id"])
        if not totals.get("aborted"):
            dbc.update_wc_run(run["id"], status="done")
        send(_fmt_summary(run["id"], totals))
    except Exception as e:  # noqa: BLE001
        send(f"Referral batch failed: {e}")
    finally:
        _busy.release()


def _run_resume() -> None:
    if not _busy.acquire(blocking=False):
        send("A run is already in progress.")
        return
    try:
        run = dbc.latest_resumable_run()
        if not run:
            send("Nothing to resume — no crashed or cancelled run on record.")
            return
        # Feature A mid-run key top-up: re-read .env from disk (not the
        # process's stale in-memory pool) so a key the operator just added
        # while the run was paused for quota exhaustion is picked up now.
        from lib_snapshot.researcher import search_client
        search_client.reload_keys()
        send(f"Resuming run #{run['id']} ({run['mode']}, was {run['status']})…")
        for q in dbc.infra_paused_firms(run["id"]):
            dbc.set_queue_status(q["id"], "pending")
        dbc.update_wc_run(run["id"], status="running", cancel_requested_at=None)
        # FIX 25: same live-editing status message as _run_batch (Feature 4
        # parity across all three batch entry paths — text, file-upload,
        # resume). `notify` is unaffected.
        totals = _drain_with_live_status(run["id"])
        if not totals.get("aborted"):
            dbc.update_wc_run(run["id"], status="done")
        send(_fmt_summary(run["id"], totals))
    except Exception as e:  # noqa: BLE001
        send(f"Resume failed: {e}")
    finally:
        _busy.release()


# ---------------------------------------------------------------------------
# Feature B: cancel a running batch cleanly. Authorization: inherited for
# free — every command reaches _dispatch/_handle_referral only after
# run_bot()'s polling loop has already filtered senders against
# AUTHORIZED_USER_IDS (§10d), so no separate check is needed here.
# ---------------------------------------------------------------------------
def _handle_cancel() -> None:
    run = dbc.latest_running_run()
    if not run:
        send("No run in progress to cancel.")
        return
    dbc.request_run_cancellation(run["id"])
    send(f"\U0001F6D1 Cancellation requested for run #{run['id']} — finishing the firm "
        f"currently in progress, then stopping. /referral resume will continue from where it stops.")


def _handle_referral(arg: str) -> None:
    arg = (arg or "").strip()
    low = arg.lower()
    if low == "reload":
        try:
            result = reload_connections()
            send(f"Connections reloaded: {result.loaded} rows loaded "
                f"({result.incomplete} incomplete), {result.distinct_companies} distinct companies.")
        except LoaderError as e:
            send(f"Reload failed: {e}")
        return
    if low == "resume":
        threading.Thread(target=_run_resume, daemon=True).start()
        return
    if low == "stop":  # alias for /cancel
        _handle_cancel()
        return
    # FIX 23 (adversarial re-review): "batch" was matched with a bare
    # .startswith, so a single-firm command whose name happens to start with
    # the literal letters "batch" (e.g. "/referral Batchelor Real Estate
    # Partners") was misrouted into the batch path and truncated ("elor Real
    # Estate Partners") by the arg[len("batch"):] slice below. _BATCH_PREFIX_RE
    # requires a word boundary right after "batch" (whitespace, punctuation,
    # or end of string all count -- only another word character, as in
    # "Batchelor", does not).
    if _BATCH_PREFIX_RE.match(low):
        rest = arg[len("batch"):].strip()
        if _matches_batch_keyword(rest, _BATCH_CONFIRM_RE):
            _confirm_pending_file_batch()
            return
        if _matches_batch_keyword(rest, _BATCH_CANCEL_RE):
            _cancel_pending_file_batch()
            return
        names = [n.strip() for part in rest.replace(",", "\n").split("\n") for n in [part.strip()] if n]
        if not names:
            # FIX 23 (adversarial re-review): a bare "/referral batch" while
            # a batch is ALREADY pending used to silently re-arm the
            # "awaiting a file" handshake with no mention of the pending
            # one -- the operator only found out later, indirectly, when a
            # follow-up upload got refused. Same guard, same message, as
            # _start_or_gate_batch's own overwrite check, just surfaced
            # immediately instead of after the fact.
            pending = _get_active_pending_batch()
            if pending:
                send(_pending_batch_refusal_message(pending))
                return
            _awaiting_batch_file[_state_key()] = time.time()
            send('Usage: /referral batch\\n<firm 1>\\n<firm 2>\\n...\n\n'
                "Or attach a .csv/.xlsx of firm names (one name per row, or a "
                'column headed "Firm"/"Company"/"Name") — send it now.')
            return
        _start_or_gate_batch(names)
        return
    firm = _strip_quotes(arg)
    if not firm:
        send('Usage: /referral "<Firm Name>"  |  /referral batch  |  /referral reload  |  /referral resume')
        return
    if _looks_like_command_fragment(firm):
        send(_command_fragment_rejection([firm]))
        return
    threading.Thread(target=_run_single_firm, args=(firm,), daemon=True).start()


# ---------------------------------------------------------------------------
# FIX 19 (real-mode incident): a mislabelled TEST_MODE banner plus a live CSV
# batch created real Gmail drafts against every firm in the list with NO
# confirmation at all -- /referral batch's inline-text path had no gate
# whatsoever, and the file-upload path only gated on row count, which says
# nothing about whether the run is live. This is the single choke point that
# actually launches _run_batch, for BOTH entry points (inline-typed names and
# file uploads): when config.TEST_MODE is false, it refuses to start until
# explicitly confirmed, stating the firm count and that real Gmail drafts
# will be created. INDEPENDENT of, and layered on top of, the pre-existing
# row-count gate (over batch_upload.CONFIRM_THRESHOLD, computed by
# _process_batch_file and passed in via extra_reasons) -- never weakens or
# bypasses it; if a batch is over threshold AND live, both reasons surface in
# ONE combined prompt rather than a double confirm/cancel round-trip, and a
# single /referral batch confirm satisfies whichever reason(s) applied.
def _live_batch_notice(count: int) -> str:
    return (f"LIVE MODE — this batch of {count} firm(s) will create REAL Gmail drafts in "
           f"Harpreet's account for every firm that qualifies.")


def _pending_batch_refusal_message(pending: dict) -> str:
    return (f"A batch of {len(pending['names'])} firm(s) is awaiting confirmation — "
           f"reply /referral batch confirm or /referral batch cancel first.")


# FIX 23 (real-mode incident): this is the single choke point for BOTH
# _pending_file_batch writes (inline-typed names AND file uploads), so the
# overwrite guard AND the command-fragment guard both live here once,
# rather than being duplicated (and inevitably drifting out of sync, as an
# adversarial re-review of the first draft found -- the fragment check had
# originally been bolted onto the inline-text caller only, so a fragment
# word uploaded via a CSV file, or riding along as one line inside an
# otherwise-real multi-firm list, slipped through untouched on this exact
# choke point).
#
# Overwrite guard: before this fix, a second call while a batch was already
# pending (whatever the cause -- a misparsed confirm/cancel reply, a second
# file upload, a second inline paste) silently REPLACED the old entry,
# discarding it with no trace; an operator who had just reviewed and was
# about to confirm a real 13-firm batch would have it silently swapped for
# a bogus 1-firm one with no warning at all.
#
# Fragment guard: ANY name in the list that's a bare command-fragment word
# (confirm/cancel/proceed/yes/ok/stop) refuses the WHOLE submission rather
# than silently researching it as a company or silently dropping just that
# one line -- the operator fixes the list and resubmits, rather than this
# code guessing which interpretation (a real fragment-named firm? a
# mis-pasted reply? a stray leftover line?) was intended.
def _start_or_gate_batch(names: list[str], *, filename: Optional[str] = None,
                        extra_reasons: Optional[list[str]] = None) -> None:
    pending = _get_active_pending_batch()
    if pending:
        send(_pending_batch_refusal_message(pending))
        return
    fragments = [n for n in names if _looks_like_command_fragment(n)]
    if fragments:
        send(_command_fragment_rejection(fragments))
        return
    reasons = list(extra_reasons or [])
    if not config.TEST_MODE:
        reasons.append(_live_batch_notice(len(names)))
    if not reasons:
        threading.Thread(target=_run_batch, args=(names,), daemon=True).start()
        return
    _pending_file_batch[_state_key()] = {"names": names, "filename": filename, "ts": time.time()}
    send("\n\n".join(reasons) + "\n\nReply /referral batch confirm to proceed, or /referral batch cancel.")


# ---------------------------------------------------------------------------
# Feature 2: CSV/Excel target-list upload for /referral batch
# ---------------------------------------------------------------------------
def _process_batch_file(file_bytes: bytes, filename: str) -> None:
    """Testable core: given raw file bytes + a filename, parses the firm
    list and either starts the batch directly or requests confirmation
    (over-threshold row count and/or live mode, via _start_or_gate_batch).
    Never touches the network — call this from a test with synthetic bytes,
    or from _handle_document_upload with a real Telegram download."""
    _awaiting_batch_file.pop(_state_key(), None)
    try:
        result = batch_upload.parse_firm_list_file(file_bytes, filename)
    except batch_upload.BatchUploadError as e:
        send(f'Could not read "{filename}": {e}')
        return

    for w in result.warnings:
        send(w)

    send(f"Loaded {result.raw_count} firm names from {filename} "
        f"({len(result.names)} after removing exact duplicate rows).")

    over_threshold_reasons = []
    if len(result.names) > batch_upload.CONFIRM_THRESHOLD:
        over_threshold_reasons.append(
            f'"{filename}" has {len(result.names)} firm names — over the '
            f'{batch_upload.CONFIRM_THRESHOLD:,} confirmation threshold.'
        )
    _start_or_gate_batch(result.names, filename=filename, extra_reasons=over_threshold_reasons)


def _confirm_pending_file_batch() -> None:
    pending = _get_active_pending_batch()
    if not pending:
        send("Nothing awaiting confirmation (or it expired). Re-upload the file to try again.")
        return
    _pending_file_batch.pop(_state_key(), None)
    source = f" from {pending['filename']}" if pending.get("filename") else ""
    send(f"Confirmed — starting batch of {len(pending['names'])} firms{source}...")
    threading.Thread(target=_run_batch, args=(pending["names"],), daemon=True).start()


def _cancel_pending_file_batch() -> None:
    if _get_active_pending_batch():
        _pending_file_batch.pop(_state_key(), None)
        send("Cancelled — batch upload discarded.")
    else:
        send("Nothing to cancel.")


def _download_telegram_file(file_id: str) -> bytes:
    """Real Telegram getFile + download. Not exercised by tests (no live
    token) — mirrors get_updates()/send()'s existing untested-by-design real
    network calls."""
    r = requests.get(f"{_api_base()}/getFile", params={"file_id": file_id}, timeout=30)
    r.raise_for_status()
    file_path = r.json()["result"]["file_path"]
    file_url = f"https://api.telegram.org/file/bot{config.TELEGRAM_BOT_TOKEN_C}/{file_path}"
    r2 = requests.get(file_url, timeout=60)
    r2.raise_for_status()
    return r2.content


def _handle_document_upload(document: dict, caption_text: str) -> None:
    low = (caption_text or "").strip().lower()
    key = _state_key()
    awaiting_ts = _awaiting_batch_file.get(key)
    is_awaiting = bool(awaiting_ts) and (time.time() - awaiting_ts) <= _AWAITING_BATCH_FILE_TTL_SEC
    if not (low.startswith("/referral batch") or is_awaiting):
        return  # a document with no batch context — not this bot's concern
    filename = document.get("file_name") or "upload"
    try:
        file_bytes = _download_telegram_file(document["file_id"])
    except Exception as e:  # noqa: BLE001
        send(f'Could not download "{filename}": {e}')
        return
    _process_batch_file(file_bytes, filename)


def _handle_qualify(arg: str) -> None:
    from lib_snapshot import models  # noqa: F401  (parity with Workflow A's parse_aum import site)

    firm, aum_raw = _parse_quoted(arg)
    if not firm:
        send('Usage: /qualify "<Firm>" <AUM>   e.g. /qualify "Aliya Capital Partners" 800M')
        return
    aum = _parse_aum(aum_raw)
    if aum is None:
        send(f'Could not read an AUM from "{aum_raw or "(nothing given)"}". Try 800M, 1.2B, or $500M.')
        return
    row = dbc.parked_firm_by_name(firm)
    if not row:
        send(f'No PARKED firm named "{firm}" on record. /qualify only rescues parked firms.')
        return
    research = dict(row.get("research_raw") or {})
    research["aum_value_usd"] = aum
    research["aum_display"] = _human_usd(aum)
    # FIX 22 (real-mode In-Rel Properties run): deck_filler.py/email_drafter.py
    # now read disclosed_figure/disclosed_label, not aum_display -- an
    # operator-verified AUM override must populate them too, or the deck/
    # email stat tile would silently fall back to the legacy path. The
    # label itself stays prospect-facing and clean ("assets under
    # management", not an internal "(operator-verified)" annotation --
    # that provenance detail belongs in icp_reason/audit trail, never in
    # copy a prospect might see).
    research["disclosed_figure"] = _human_usd(aum)
    research["disclosed_label"] = "assets under management"
    research["aum_tag"] = "AUM_CONFIRMED"
    in_band = config.AUM_MIN_USD <= aum <= config.AUM_MAX_USD
    if not in_band:
        send(f'{_human_usd(aum)} is outside the ${config.AUM_MIN_USD/1e6:.0f}M–'
            f'${config.AUM_MAX_USD/1e9:.1f}B band — still DISQUALIFIED. Nothing changed.')
        return
    dbc.update_wc_firm(row["id"], icp_status="qualified",
                       icp_reason=f"QUALIFIED (operator-verified AUM {_human_usd(aum)})",
                       aum_tag="AUM_CONFIRMED", park_reason=None, park_source=None,
                       research_raw=research)
    send(f'Qualified "{row["firm_name"]}" at {_human_usd(aum)} — continuing from Phase 3…')
    threading.Thread(target=lambda: send(_fmt_outcome(pipeline.process_referral_firm(row["id"]))),
                     daemon=True).start()


def _fmt_outcome(result: dict) -> str:
    return f"Result: {result.get('outcome')}" + (f" (draft {result['draft_id']})" if result.get("draft_id") else "")


def _handle_disqualify(arg: str) -> None:
    firm, _rest = _parse_quoted(arg)
    if not firm:
        send('Usage: /disqualify "<Firm>"')
        return
    row = dbc.parked_firm_by_name(firm)
    if not row:
        send(f'No PARKED firm named "{firm}" on record.')
        return
    dbc.update_wc_firm(row["id"], icp_status="disqualified",
                       icp_reason="Disqualified by operator override", park_reason=None, park_source=None)
    send(f'Disqualified "{row["firm_name"]}".')


_AUM_RE = re.compile(r"([\d.,]+)\s*(b|bn|billion|m|mm|million)?", re.IGNORECASE)


def _parse_aum(raw: str) -> Optional[float]:
    raw = (raw or "").strip().replace("$", "").replace(",", "")
    if not raw:
        return None
    m = _AUM_RE.match(raw)
    if not m:
        return None
    try:
        n = float(m.group(1))
    except ValueError:
        return None
    unit = (m.group(2) or "").lower()
    if unit in ("b", "bn", "billion"):
        return n * 1_000_000_000
    if unit in ("m", "mm", "million"):
        return n * 1_000_000
    return n


def _human_usd(n: float) -> str:
    if n >= 1_000_000_000:
        return f"${n/1_000_000_000:.1f}B"
    if n >= 1_000_000:
        return f"${n/1_000_000:.0f}M"
    return f"${n:,.0f}"


def _help_text() -> str:
    return (
    f"Referral Outreach bot (Workflow C, {_mode_banner()}).\n\n"
    '/referral "<Firm Name>"   - single-firm run\n'
    "/referral batch           - target-list batch (list firms after the command, one per line,\n"
    "                            or attach a .csv/.xlsx of firm names)\n"
    "/referral batch confirm/cancel - confirm or cancel an over-2,000-row file upload, or a live-mode batch\n"
    "/referral reload          - re-ingest data/connections.csv\n"
    "/referral resume          - continue a crashed OR cancelled run\n"
    "/cancel (or /referral stop) - stop the active run cleanly, after the in-flight firm finishes\n"
    '/qualify "<Firm>" <AUM>   - rescue a parked firm (800M, 1.2B, $500M)\n'
    '/disqualify "<Firm>"      - reject a parked firm\n'
    "/status c                 - dashboard\n"
    '/status c "<Firm>"        - drill-down\n'
    )


def _dispatch(text: str, chat_id: Optional[str] = None) -> None:
    """Pure-ish command router (network side effects go through send()).
    Testable without a live bot token."""
    text = (text or "").strip()
    if not text.startswith("/"):
        return
    parts = text.split(maxsplit=1)
    cmd = parts[0].lstrip("/").split("@")[0].lower()
    arg = parts[1] if len(parts) > 1 else ""

    if cmd == "referral":
        _handle_referral(arg)
    elif cmd == "cancel":
        _handle_cancel()
    elif cmd == "qualify":
        _handle_qualify(arg)
    elif cmd == "disqualify":
        _handle_disqualify(arg)
    elif cmd == "status":
        low = arg.strip().lower()
        if low.startswith("c"):
            rest = arg.strip()[1:].strip()
            send(status.detail_text(_strip_quotes(rest)) if rest else status.dashboard_text())
        else:
            send('Usage: /status c   or   /status c "<Firm>"')
    elif cmd in ("start", "help"):
        send(_help_text())
    else:
        send(f"Unknown command: {cmd}\n\n{_help_text()}")


def _dispatch_message(msg: dict) -> None:
    """Feature 2 entry point: routes a full Telegram message object to
    either the file-upload handler (if it carries a document) or the
    existing text command router. Testable without a live bot token — feed
    it a synthetic msg dict with a "document" key."""
    document = msg.get("document")
    if document:
        caption = msg.get("caption") or msg.get("text") or ""
        _handle_document_upload(document, caption)
        return
    _dispatch(msg.get("text", ""), chat_id=str(msg.get("chat", {}).get("id")) if msg.get("chat") else None)


# ---------------------------------------------------------------------------
# Real polling loop (not exercised in TEST_MODE verification — requires a
# live TELEGRAM_BOT_TOKEN_C from BotFather; see SETUP_TODO.md)
# ---------------------------------------------------------------------------
def get_updates(offset: Optional[int], timeout: int = 30) -> list[dict]:
    r = requests.get(f"{_api_base()}/getUpdates", params={"offset": offset, "timeout": timeout},
                     timeout=timeout + 10)
    r.raise_for_status()
    return r.json().get("result", [])


def run_bot() -> None:
    missing = config.missing_keys_c(config.REQUIRED_FOR_BOT_C)
    if missing:
        raise SystemExit(f"Refusing to start - missing config: {missing}")
    if not config.AUTHORIZED_USER_IDS:
        raise SystemExit("Refusing to start - AUTHORIZED_USER_IDS is empty in .env")

    allowed = set(config.AUTHORIZED_USER_IDS)
    offset: Optional[int] = None
    backlog = get_updates(None, timeout=0)
    if backlog:
        offset = backlog[-1]["update_id"] + 1
    send(f"Referral Outreach bot online ({_mode_banner()}).\n\n{_help_text()}")

    while True:
        for upd in get_updates(offset):
            offset = upd["update_id"] + 1
            msg = upd.get("message") or upd.get("edited_message")
            if not msg:
                continue
            sender = str(msg.get("from", {}).get("id"))
            if sender not in allowed:
                continue
            if msg.get("text") or msg.get("document"):
                try:
                    _dispatch_message(msg)
                except Exception as e:  # noqa: BLE001
                    send(f"Error handling command: {e}")


if __name__ == "__main__":
    run_bot()

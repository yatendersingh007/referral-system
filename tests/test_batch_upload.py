"""QA Part 1 (Feature 2) — CSV/Excel target-list upload for /referral batch.
Tests both the pure parser (src/batch_upload.py) and its Telegram wiring
(src/telegram_bot.py), closing the gap the prior QA_REPORT flagged.
"""
from __future__ import annotations

import io
import time

import openpyxl
import pytest

from scripts.reset_testmode import reset as reset_testmode
from src import batch_upload, config, telegram_bot
from src.batch_upload import BatchUploadError, parse_firm_list_file


@pytest.fixture(autouse=True)
def _clean_state():
    reset_testmode(verbose=False)
    telegram_bot._pending_file_batch.clear()
    telegram_bot._awaiting_batch_file.clear()
    yield
    telegram_bot._pending_file_batch.clear()
    telegram_bot._awaiting_batch_file.clear()


def _xlsx_bytes(rows: list[list]) -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Pure parser — the required cases
# ---------------------------------------------------------------------------
def test_csv_single_column_no_header():
    result = parse_firm_list_file(
        b"Kingbird Investment Management\nBlackstone\nAliya Capital Partners\n", "firms.csv"
    )
    assert result.names == ["Kingbird Investment Management", "Blackstone", "Aliya Capital Partners"]
    assert result.raw_count == 3


def test_xlsx_single_column_no_header():
    data = _xlsx_bytes([["Kingbird Investment Management"], ["Blackstone"], ["Aliya Capital Partners"]])
    result = parse_firm_list_file(data, "firms.xlsx")
    assert result.names == ["Kingbird Investment Management", "Blackstone", "Aliya Capital Partners"]


def test_xlsx_multi_column_with_company_header():
    data = _xlsx_bytes([
        ["Company", "Notes"],
        ["Kingbird Investment Management", "hot"],
        ["Blackstone", "too big"],
    ])
    result = parse_firm_list_file(data, "firms.xlsx")
    assert result.names == ["Kingbird Investment Management", "Blackstone"]
    assert result.warnings == []


def test_csv_multi_column_with_firm_name_header_case_insensitive():
    csv_bytes = b"Notes,FIRM NAME\nhot,Kingbird Investment Management\nbig,Blackstone\n"
    result = parse_firm_list_file(csv_bytes, "firms.csv")
    assert result.names == ["Kingbird Investment Management", "Blackstone"]


def test_bom_and_blank_and_trailing_rows():
    csv_bytes = "﻿Kingbird Investment Management\n\nBlackstone\n\n\n".encode("utf-8")
    result = parse_firm_list_file(csv_bytes, "firms.csv")
    assert result.names == ["Kingbird Investment Management", "Blackstone"]


def test_over_2000_rows_flagged_over_threshold():
    csv_bytes = ("\n".join(f"Firm {i}" for i in range(2500))).encode()
    result = parse_firm_list_file(csv_bytes, "huge.csv")
    assert len(result.names) == 2500  # "Firm 0".."Firm 2499" are all distinct, no dupes to remove
    assert len(result.names) > batch_upload.CONFIRM_THRESHOLD


def test_malformed_binary_file_rejected_not_crashed():
    with pytest.raises(BatchUploadError):
        parse_firm_list_file(b"\x00\x01\x02\x03 not a csv or xlsx at all", "garbage.csv")


def test_multi_column_plural_companies_header_picks_correct_column_not_first():
    """QA Part 9 adversarial re-attack finding: 'company' is not a substring
    of 'companies' (irregular pluralization), so a 'Target Companies' header
    in a NON-first column wasn't recognized by _pick_column's substring
    match, silently falling back to 'use the first column' -- which picked
    the WRONG column and substituted garbage (another column's data) as firm
    names. Firm names are in column 2 here, not column 1."""
    csv_bytes = b"Notes,Target Companies\nhot,Kingbird Investment Management\nbig,Blackstone\n"
    result = parse_firm_list_file(csv_bytes, "firms.csv")
    assert result.names == ["Kingbird Investment Management", "Blackstone"]
    assert result.warnings == []


def test_common_plural_and_list_style_single_column_headers_not_leaked_as_data():
    """QA Part 9 adversarial re-attack finding: the exact-match header fix for
    Bug 1 used a narrow singular-only vocabulary, so plausible real headers
    like 'Target Companies' or 'Firm List' weren't recognized and leaked into
    the batch as a bogus firm name."""
    for header in ("Target Companies", "Target Firms", "Companies", "Firms",
                  "Company Names", "Firm Names", "Company List", "Firm List"):
        csv_bytes = f"{header}\nKingbird Investment Management\nBlackstone\n".encode()
        result = parse_firm_list_file(csv_bytes, "firms.csv")
        assert result.names == ["Kingbird Investment Management", "Blackstone"], (
            f"header {header!r} leaked into the firm list: {result.names!r}"
        )


def test_non_tabular_but_valid_text_file_still_parses_as_one_name_per_line():
    """A plain-text file (not "malformed", just unstructured prose) with no
    control characters is still valid single-column input — one name per
    line is exactly the documented single-column format."""
    result = parse_firm_list_file(b"Just Some Firm Name\nAnother One Here\n", "notes.csv")
    assert result.names == ["Just Some Firm Name", "Another One Here"]


# ---------------------------------------------------------------------------
# Additional adversarial cases
# ---------------------------------------------------------------------------
def test_wrong_extension_rejected():
    with pytest.raises(BatchUploadError, match="Unsupported file type"):
        parse_firm_list_file(b"Kingbird\nBlackstone\n", "firms.txt")


def test_empty_file_rejected():
    with pytest.raises(BatchUploadError):
        parse_firm_list_file(b"", "empty.csv")


def test_no_extension_rejected():
    with pytest.raises(BatchUploadError):
        parse_firm_list_file(b"Kingbird\n", "firms")


def test_ambiguous_multi_column_uses_first_column_with_warning():
    result = parse_firm_list_file(b"Col1,Col2\nAcme,x\nBeta,y\n", "firms.csv")
    assert result.names == ["Acme", "Beta"]
    assert result.warnings and "first column" in result.warnings[0]
    assert "Col1" not in result.names  # the header row's own label must never leak in as data


def test_exact_duplicate_rows_removed_but_raw_count_preserved():
    csv_bytes = b"Kingbird Investment Management\nBlackstone\nKingbird Investment Management\n"
    result = parse_firm_list_file(csv_bytes, "firms.csv")
    assert result.raw_count == 3
    assert result.names == ["Kingbird Investment Management", "Blackstone"]


# ---------------------------------------------------------------------------
# FIX 32A Part C (real 17-row upload): dedup collapsed "Atlas Real Estate "
# (trailing space) fine, but did NOT collapse "in-rel properties" against
# "In-Rel Properties" -- that ran a full second research pass (6 searches +
# extraction) before the downstream contact-level dedup caught it.
# _dedupe_preserve_order must be case-insensitive while the surviving
# string keeps the FIRST occurrence's original casing.
# ---------------------------------------------------------------------------
def test_dedupe_case_insensitive_lowercase_vs_titlecase():
    result = batch_upload._dedupe_preserve_order(["In-Rel Properties", "in-rel properties"])
    assert result == ["In-Rel Properties"]


def test_dedupe_trailing_whitespace_still_collapses_as_before():
    """Regression guard: the ALREADY-working whitespace normalization (the
    same real upload DID collapse "Atlas Real Estate " against "Atlas Real
    Estate") must not regress."""
    result = batch_upload._dedupe_preserve_order(["Atlas Real Estate", "Atlas Real Estate "])
    assert result == ["Atlas Real Estate"]


def test_dedupe_mixed_case_plus_trailing_whitespace_together():
    result = batch_upload._dedupe_preserve_order(["In-Rel Properties", "  in-rel properties  "])
    assert result == ["In-Rel Properties"]


def test_dedupe_surviving_string_carries_first_occurrences_casing():
    """The surviving string must be exactly the FIRST occurrence's casing --
    it's what gets shown in Telegram and passed to the firm matcher
    downstream, regardless of which variant happened to come first."""
    result = batch_upload._dedupe_preserve_order(["in-rel properties", "In-Rel Properties"])
    assert result == ["in-rel properties"]


def test_dedupe_keeps_two_genuinely_different_firms_differing_by_more_than_casing():
    """Case-insensitivity must not become over-eager: a name that's a
    genuine variant (not just a casing change) of another must survive,
    while a true casing-only duplicate is still dropped in the same list."""
    names = ["Atlas Real Estate", "atlas real estate partners",
            "Homeland Capital Partners, LLP", "homeland capital partners, llp"]
    result = batch_upload._dedupe_preserve_order(names)
    assert result == ["Atlas Real Estate", "atlas real estate partners",
                      "Homeland Capital Partners, LLP"]


def test_dedupe_does_not_strip_punctuation_from_real_legitimate_firm_names():
    """Deliberately NOT normalizing punctuation -- these are real
    data/connections.csv entries where the comma/period/ampersand is part
    of the legitimate name, not noise."""
    names = ["Homeland Capital Partners, LLP", "C6 Capital, LLC.", "Das & Co.",
            "Keller Williams Realty, Inc."]
    assert batch_upload._dedupe_preserve_order(names) == names


def test_parse_firm_list_file_collapses_the_real_in_rel_properties_case():
    """End-to-end reproduction of the real 17-row upload's actual defect:
    a case-only variant of an already-listed firm must be collapsed at
    parse time, not survive to run a wasted second research pass."""
    csv_bytes = b"In-Rel Properties\nAtlas Real Estate\nin-rel properties\n"
    result = parse_firm_list_file(csv_bytes, "firms.csv")
    assert result.raw_count == 3
    assert result.names == ["In-Rel Properties", "Atlas Real Estate"]


def test_quoted_fields_with_embedded_commas():
    csv_bytes = b'Company\n"Smith, Jones & Co","note, with comma"\nBlackstone,plain\n'
    result = parse_firm_list_file(csv_bytes, "firms.csv")
    assert "Smith, Jones & Co" in result.names
    assert "Blackstone" in result.names


def test_xlsx_with_formula_cell_reads_as_literal_string_no_crash():
    """openpyxl with data_only=True never evaluates formulas (no execution
    risk) — a formula cell with no cached value reads as None/empty, and one
    WITH a cached value (as a real Excel save would have) reads as that
    cached string. Either way: no crash, no code execution."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Company"])
    ws.append(["=1+1"])  # a literal formula string, never evaluated by openpyxl
    ws.append(["Blackstone"])
    buf = io.BytesIO()
    wb.save(buf)
    result = parse_firm_list_file(buf.getvalue(), "firms.xlsx")
    # "=1+1" has no cached value (never opened in real Excel) -> None -> blank
    # -> dropped; Blackstone survives. The key assertion is simply "no crash".
    assert "Blackstone" in result.names


def test_xlsx_completely_empty_workbook_rejected():
    wb = openpyxl.Workbook()
    buf = io.BytesIO()
    wb.save(buf)
    with pytest.raises(BatchUploadError):
        parse_firm_list_file(buf.getvalue(), "empty.xlsx")


def test_corrupt_xlsx_bytes_rejected_not_crashed():
    with pytest.raises(BatchUploadError):
        parse_firm_list_file(b"this is not a real xlsx zip archive", "fake.xlsx")


# ---------------------------------------------------------------------------
# Telegram wiring: _process_batch_file, confirm/cancel, _dispatch_message
#
# IMPORTANT: never monkeypatch the real `threading.Thread` class. telegram_bot
# imports the actual stdlib `threading` module (not a private copy), so
# patching its `Thread` attribute patches it PROCESS-WIDE — including for
# psycopg_pool's own background maintenance thread, which can be lazily
# created during a test and crash with a confusing unrelated error (found the
# hard way: "'T' object has no attribute 'join'" surfaced from deep inside
# pipeline code, not from anything these tests call directly). Instead,
# `_recording_thread` below replaces Thread with a fake that RECORDS
# (target, args) and never executes or schedules anything — assert on the
# recorded call, don't let it run.
# ---------------------------------------------------------------------------
class _RecordedThread:
    def __init__(self, target=None, args=(), daemon=True):
        self.target = target
        self.args = args

    def start(self):
        pass  # deliberately never executes — see module docstring above


@pytest.fixture
def recorded_threads(monkeypatch):
    calls: list[_RecordedThread] = []

    def _fake_thread(target=None, args=(), daemon=True):
        t = _RecordedThread(target=target, args=args, daemon=daemon)
        calls.append(t)
        return t

    monkeypatch.setattr(telegram_bot.threading, "Thread", _fake_thread)
    return calls


def test_process_batch_file_under_threshold_starts_batch_directly(monkeypatch, recorded_threads):
    sent = []
    monkeypatch.setattr(telegram_bot, "send", lambda text, chat_id=None: sent.append(text))

    telegram_bot._process_batch_file(b"Kingbird Investment Management\nBlackstone\n", "firms.csv")
    assert "Loaded 2 firm names from firms.csv (2 after removing exact duplicate rows)" in sent[0]
    assert len(recorded_threads) == 1
    assert recorded_threads[0].target is telegram_bot._run_batch
    assert recorded_threads[0].args == (["Kingbird Investment Management", "Blackstone"],)


def test_process_batch_file_over_threshold_requires_confirm(monkeypatch, recorded_threads):
    sent = []
    monkeypatch.setattr(telegram_bot, "send", lambda text, chat_id=None: sent.append(text))
    csv_bytes = ("\n".join(f"Firm {i}" for i in range(2500))).encode()

    telegram_bot._process_batch_file(csv_bytes, "huge.csv")
    assert "confirmation threshold" in sent[-1]
    assert telegram_bot._state_key() in telegram_bot._pending_file_batch
    assert recorded_threads == []  # must NOT start the batch before confirmation


def test_confirm_pending_file_batch_starts_the_run(monkeypatch, recorded_threads):
    sent = []
    monkeypatch.setattr(telegram_bot, "send", lambda text, chat_id=None: sent.append(text))

    csv_bytes = ("\n".join(f"Firm {i}" for i in range(2500))).encode()
    telegram_bot._process_batch_file(csv_bytes, "huge.csv")
    sent.clear()
    telegram_bot._dispatch("/referral batch confirm")
    assert "Confirmed" in sent[0]
    assert len(recorded_threads) == 1
    assert recorded_threads[0].target is telegram_bot._run_batch
    assert len(recorded_threads[0].args[0]) == 2500
    assert telegram_bot._state_key() not in telegram_bot._pending_file_batch


def test_cancel_pending_file_batch_discards_it(monkeypatch, recorded_threads):
    sent = []
    monkeypatch.setattr(telegram_bot, "send", lambda text, chat_id=None: sent.append(text))
    csv_bytes = ("\n".join(f"Firm {i}" for i in range(2500))).encode()
    telegram_bot._process_batch_file(csv_bytes, "huge.csv")
    sent.clear()

    telegram_bot._dispatch("/referral batch cancel")
    assert "Cancelled" in sent[0]
    assert telegram_bot._state_key() not in telegram_bot._pending_file_batch
    assert recorded_threads == []  # cancelled — never started

    sent.clear()
    telegram_bot._dispatch("/referral batch confirm")
    assert "Nothing awaiting confirmation" in sent[0]


def test_malformed_upload_replies_clearly_never_crashes(monkeypatch, recorded_threads):
    sent = []
    monkeypatch.setattr(telegram_bot, "send", lambda text, chat_id=None: sent.append(text))
    telegram_bot._process_batch_file(b"\x00\x01\x02\x03", "garbage.csv")
    assert "Could not read" in sent[0]
    assert not telegram_bot._pending_file_batch  # a rejected file must never leave pending state
    assert recorded_threads == []


def test_dispatch_message_routes_document_to_upload_handler(monkeypatch, recorded_threads):
    sent = []
    monkeypatch.setattr(telegram_bot, "send", lambda text, chat_id=None: sent.append(text))
    downloaded = []
    monkeypatch.setattr(telegram_bot, "_download_telegram_file",
                       lambda file_id: (downloaded.append(file_id), b"Kingbird\nBlackstone\n")[1])

    msg = {
        "text": "",
        "caption": "/referral batch",
        "document": {"file_id": "ABC123", "file_name": "firms.csv"},
        "chat": {"id": 999},
    }
    telegram_bot._dispatch_message(msg)
    assert downloaded == ["ABC123"]
    assert any("Loaded 2 firm names" in s for s in sent)
    assert len(recorded_threads) == 1


def test_dispatch_message_document_without_batch_context_ignored(monkeypatch):
    """A random document sent with no /referral batch caption and no prior
    'awaiting a file' state is not this bot's concern — it must not silently
    try to process it as a target list."""
    sent = []
    monkeypatch.setattr(telegram_bot, "send", lambda text, chat_id=None: sent.append(text))
    downloaded = []
    monkeypatch.setattr(telegram_bot, "_download_telegram_file", lambda file_id: downloaded.append(file_id))

    msg = {"text": "", "caption": "just sharing a file", "document": {"file_id": "XYZ", "file_name": "random.csv"},
          "chat": {"id": 999}}
    telegram_bot._dispatch_message(msg)
    assert downloaded == []
    assert sent == []


def test_awaiting_batch_file_flag_set_by_bare_batch_command(monkeypatch, recorded_threads):
    """/referral batch with no names sets the 'awaiting a file' flag, so a
    document sent as the NEXT message (no caption at all) is still routed
    correctly — matching the mockup's two-message pattern (command, then file)."""
    sent = []
    monkeypatch.setattr(telegram_bot, "send", lambda text, chat_id=None: sent.append(text))
    telegram_bot._dispatch("/referral batch")
    assert telegram_bot._state_key() in telegram_bot._awaiting_batch_file

    downloaded = []
    monkeypatch.setattr(telegram_bot, "_download_telegram_file",
                       lambda file_id: (downloaded.append(file_id), b"Kingbird\n")[1])
    msg = {"text": "", "caption": "", "document": {"file_id": "NOCAP", "file_name": "firms.csv"}, "chat": {"id": 1}}
    telegram_bot._dispatch_message(msg)
    assert downloaded == ["NOCAP"]


def test_text_batch_path_still_works_unchanged(monkeypatch, recorded_threads):
    """Feature 2 must not break the existing text-paste batch path."""
    sent = []
    monkeypatch.setattr(telegram_bot, "send", lambda text, chat_id=None: sent.append(text))

    telegram_bot._dispatch("/referral batch\nKingbird Investment Management\nBlackstone")
    assert len(recorded_threads) == 1
    assert recorded_threads[0].target is telegram_bot._run_batch
    assert recorded_threads[0].args == (["Kingbird Investment Management", "Blackstone"],)


# ---------------------------------------------------------------------------
# FIX 18 (real-mode incident): the bot's startup/help banner hardcoded the
# literal string "test mode" regardless of config.TEST_MODE -- a live run
# (.env TEST_MODE=false) that created a REAL Gmail draft in Harpreet's
# account still announced itself as test mode.
# ---------------------------------------------------------------------------
def test_mode_banner_reads_test_mode_true():
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(config, "TEST_MODE", True)
        banner = telegram_bot._mode_banner()
    assert banner == "TEST MODE - no real Gmail drafts, no API spend"
    assert "LIVE" not in banner


def test_mode_banner_reads_test_mode_false():
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(config, "TEST_MODE", False)
        banner = telegram_bot._mode_banner()
    assert banner == "LIVE - creates real Gmail drafts in Harpreet's account"
    assert "TEST MODE" not in banner


def test_help_text_contains_right_phrase_test_mode_true():
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(config, "TEST_MODE", True)
        text = telegram_bot._help_text()
    assert "TEST MODE - no real Gmail drafts, no API spend" in text
    assert "LIVE - creates real Gmail drafts in Harpreet's account" not in text


def test_help_text_contains_right_phrase_test_mode_false():
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(config, "TEST_MODE", False)
        text = telegram_bot._help_text()
    assert "LIVE - creates real Gmail drafts in Harpreet's account" in text
    assert "TEST MODE - no real Gmail drafts, no API spend" not in text


def test_run_bot_startup_banner_uses_mode_banner_not_hardcoded_string():
    """run_bot()'s own startup send() isn't safely callable in a test (real
    getUpdates polling) -- verified at the source level, matching this
    codebase's existing pattern for run_bot() properties (test_cancel.py).
    The literal "test mode" string must not appear anywhere in the function;
    it must render via _mode_banner()."""
    import inspect
    src = inspect.getsource(telegram_bot.run_bot)
    assert "test mode" not in src.lower() or "_mode_banner()" in src
    assert "_mode_banner()" in src


# ---------------------------------------------------------------------------
# FIX 19 (real-mode incident): /referral batch is the actual hazard -- a
# mislabelled banner plus a CSV means real drafts against every firm in the
# list. This gate is independent of the pre-existing row-count gate and
# applies to ANY live batch, inline-typed or file-uploaded, regardless of
# size.
# ---------------------------------------------------------------------------
def test_inline_batch_live_mode_refuses_to_start_until_confirmed(monkeypatch, recorded_threads):
    monkeypatch.setattr(config, "TEST_MODE", False)
    sent = []
    monkeypatch.setattr(telegram_bot, "send", lambda text, chat_id=None: sent.append(text))

    telegram_bot._dispatch("/referral batch\nKingbird Investment Management\nBlackstone")

    assert recorded_threads == [], "must NOT start the batch before live-mode confirmation"
    assert telegram_bot._state_key() in telegram_bot._pending_file_batch
    msg = sent[-1]
    assert "2" in msg  # firm count
    assert "LIVE MODE" in msg
    assert "REAL Gmail drafts" in msg
    assert "Harpreet's account" in msg
    assert "confirm" in msg.lower()


def test_inline_batch_live_mode_confirm_starts_it(monkeypatch, recorded_threads):
    monkeypatch.setattr(config, "TEST_MODE", False)
    sent = []
    monkeypatch.setattr(telegram_bot, "send", lambda text, chat_id=None: sent.append(text))

    telegram_bot._dispatch("/referral batch\nKingbird Investment Management\nBlackstone")
    assert recorded_threads == []
    sent.clear()

    telegram_bot._dispatch("/referral batch confirm")
    assert "Confirmed" in sent[0]
    assert "None" not in sent[0]  # no filename for an inline-typed batch -- must not leak into the message
    assert len(recorded_threads) == 1
    assert recorded_threads[0].target is telegram_bot._run_batch
    assert recorded_threads[0].args == (["Kingbird Investment Management", "Blackstone"],)


def test_inline_batch_live_mode_cancel_discards_it(monkeypatch, recorded_threads):
    monkeypatch.setattr(config, "TEST_MODE", False)
    monkeypatch.setattr(telegram_bot, "send", lambda text, chat_id=None: None)

    telegram_bot._dispatch("/referral batch\nKingbird Investment Management\nBlackstone")
    assert telegram_bot._state_key() in telegram_bot._pending_file_batch

    telegram_bot._dispatch("/referral batch cancel")
    assert telegram_bot._state_key() not in telegram_bot._pending_file_batch
    assert recorded_threads == []


def test_file_batch_under_threshold_live_mode_still_gated(monkeypatch, recorded_threads):
    """Before FIX 19, an under-threshold file batch always started
    immediately regardless of TEST_MODE -- the actual hazard the incident
    report described."""
    monkeypatch.setattr(config, "TEST_MODE", False)
    sent = []
    monkeypatch.setattr(telegram_bot, "send", lambda text, chat_id=None: sent.append(text))

    telegram_bot._process_batch_file(b"Kingbird Investment Management\nBlackstone\n", "firms.csv")

    assert recorded_threads == [], "must NOT start the batch before live-mode confirmation"
    assert telegram_bot._state_key() in telegram_bot._pending_file_batch
    msg = sent[-1]
    assert "LIVE MODE" in msg
    assert "REAL Gmail drafts" in msg
    assert "confirmation threshold" not in msg  # row-count gate must not fire when under threshold


def test_file_batch_over_threshold_and_live_mode_both_reasons_in_one_prompt(monkeypatch, recorded_threads):
    """Both gates are independent and neither weakens the other -- when both
    apply, both reasons surface, and a SINGLE confirm satisfies both rather
    than requiring two separate confirmation round-trips."""
    monkeypatch.setattr(config, "TEST_MODE", False)
    sent = []
    monkeypatch.setattr(telegram_bot, "send", lambda text, chat_id=None: sent.append(text))
    csv_bytes = ("\n".join(f"Firm {i}" for i in range(2500))).encode()

    telegram_bot._process_batch_file(csv_bytes, "huge.csv")

    assert recorded_threads == []
    msg = sent[-1]
    assert "confirmation threshold" in msg  # row-count gate still fires
    assert "LIVE MODE" in msg               # live-mode gate ALSO fires, independently
    assert "2500" in msg

    sent.clear()
    telegram_bot._dispatch("/referral batch confirm")
    assert "Confirmed" in sent[0]
    assert len(recorded_threads) == 1
    assert recorded_threads[0].target is telegram_bot._run_batch
    assert len(recorded_threads[0].args[0]) == 2500


def test_file_batch_over_threshold_test_mode_unchanged_row_count_gate_only(monkeypatch, recorded_threads):
    """TEST_MODE=true (the existing test suite's default): the row-count
    gate alone still fires exactly as before FIX 19 -- not weakened, not
    bypassed, and no live-mode reason is added since nothing is live."""
    assert config.TEST_MODE is True
    sent = []
    monkeypatch.setattr(telegram_bot, "send", lambda text, chat_id=None: sent.append(text))
    csv_bytes = ("\n".join(f"Firm {i}" for i in range(2500))).encode()

    telegram_bot._process_batch_file(csv_bytes, "huge.csv")

    assert recorded_threads == []
    msg = sent[-1]
    assert "confirmation threshold" in msg
    assert "LIVE MODE" not in msg


# ---------------------------------------------------------------------------
# FIX 23 (real-mode incident, repeated 4x in one session): "/referral batch
# confirm to proceed" did not match the old exact `rest_low == "confirm"`
# check, fell through to the inline-firm-names path, and silently created a
# NEW pending batch of 1 firm literally named "confirm to proceed" --
# overwriting a pending 13-firm batch that was actually awaiting
# confirmation. Four fixes, tested separately below (the first draft of
# fix 1 was itself adversarially re-reviewed and found to have an inverse
# blind spot -- see the "1b" section -- fixed before landing):
#   1. Tolerant confirm/cancel parsing (whitespace, case, trailing filler),
#      WITHOUT misreading a real firm name that starts with the same word.
#   2. A pending batch can never be silently replaced by a new one.
#   3. A command-fragment "firm name" (confirm/cancel/proceed/yes/ok/stop)
#      is rejected, never researched as a company nor batched, on EVERY
#      entry point (inline text, file upload) and in EVERY position (sole
#      entry or riding along inside a real multi-firm list).
#   4. The pre-existing "batch" prefix match required a word boundary, so a
#      single-firm command literally beginning with "batch" isn't misrouted.
# ---------------------------------------------------------------------------

# --- 1. Tolerant confirm/cancel parsing -------------------------------------

@pytest.mark.parametrize("suffix", [
    "confirm to proceed",
    "confirm please",
    "CONFIRM",
    "Confirm",
    "confirm.",
    "confirm!",
    "  confirm  ",
    "confirm   to proceed please go ahead",
])
def test_batch_confirm_tolerant_of_case_whitespace_and_trailing_filler(monkeypatch, recorded_threads, suffix):
    sent = []
    monkeypatch.setattr(telegram_bot, "send", lambda text, chat_id=None: sent.append(text))
    csv_bytes = ("\n".join(f"Firm {i}" for i in range(2500))).encode()
    telegram_bot._process_batch_file(csv_bytes, "huge.csv")
    sent.clear()

    telegram_bot._dispatch(f"/referral batch {suffix}")
    assert "Confirmed" in sent[0], f"suffix {suffix!r} did not confirm: {sent}"
    assert len(recorded_threads) == 1
    assert recorded_threads[0].target is telegram_bot._run_batch
    assert len(recorded_threads[0].args[0]) == 2500
    assert telegram_bot._state_key() not in telegram_bot._pending_file_batch


@pytest.mark.parametrize("suffix", [
    "cancel to proceed",
    "cancel please",
    "CANCEL",
    "Cancel",
    "cancel.",
    "  cancel  ",
])
def test_batch_cancel_tolerant_of_case_whitespace_and_trailing_filler(monkeypatch, recorded_threads, suffix):
    sent = []
    monkeypatch.setattr(telegram_bot, "send", lambda text, chat_id=None: sent.append(text))
    csv_bytes = ("\n".join(f"Firm {i}" for i in range(2500))).encode()
    telegram_bot._process_batch_file(csv_bytes, "huge.csv")
    sent.clear()

    telegram_bot._dispatch(f"/referral batch {suffix}")
    assert "Cancelled" in sent[0], f"suffix {suffix!r} did not cancel: {sent}"
    assert telegram_bot._state_key() not in telegram_bot._pending_file_batch
    assert recorded_threads == []


def test_batch_confirm_exact_bug_reproduction_does_not_create_bogus_new_batch(monkeypatch, recorded_threads):
    """The literal incident: a pending 13-firm batch is awaiting confirmation,
    the operator replies "/referral batch confirm to proceed", and the fix
    must CONFIRM the real pending batch -- never fall through to the
    inline-names path and silently create/start a fake 1-firm batch named
    "confirm to proceed"."""
    monkeypatch.setattr(config, "TEST_MODE", False)
    sent = []
    monkeypatch.setattr(telegram_bot, "send", lambda text, chat_id=None: sent.append(text))
    thirteen_firms = "\n".join(f"Firm {i}" for i in range(13))

    telegram_bot._dispatch(f"/referral batch\n{thirteen_firms}")
    assert recorded_threads == []
    assert telegram_bot._state_key() in telegram_bot._pending_file_batch
    sent.clear()

    telegram_bot._dispatch("/referral batch confirm to proceed")

    assert "Confirmed" in sent[0], sent
    assert len(recorded_threads) == 1
    assert recorded_threads[0].target is telegram_bot._run_batch
    assert len(recorded_threads[0].args[0]) == 13
    assert "confirm to proceed" not in recorded_threads[0].args[0]
    assert telegram_bot._state_key() not in telegram_bot._pending_file_batch


@pytest.mark.parametrize("text", [
    "/referral batch\nConfirm Realty LLC\nOther Firm",
    "/referral batch Confirm Realty LLC, Other Firm",
    "/referral batch\nCancel Capital Partners\nOther Firm",
])
def test_batch_keyword_match_never_fires_on_a_multi_firm_list(monkeypatch, recorded_threads, text):
    """A real firm whose name happens to start with "Confirm"/"Cancel" must
    never be misread as a confirm/cancel reply -- "Realty LLC"/"Capital
    Partners" aren't filler words, so the tolerant matcher correctly falls
    through to the firm-name parser regardless of the newline/comma shape."""
    sent = []
    monkeypatch.setattr(telegram_bot, "send", lambda text, chat_id=None: sent.append(text))
    telegram_bot._dispatch(text)
    assert len(recorded_threads) == 1
    assert recorded_threads[0].target is telegram_bot._run_batch
    assert len(recorded_threads[0].args[0]) == 2


# --- 1b. Adversarial re-review of the first draft: a real, SOLE firm name --
# starting with "Confirm"/"Cancel" (no second line, no comma) is NOT a
# multi-item list, and the first draft's blanket "bail on any newline/comma"
# guard did nothing for this shape -- it was swallowed as a confirm/cancel
# reply, either firing against an unrelated pending batch or silently
# discarding the firm entirely. Fixed by checking whether the REMAINDER
# after the keyword is filler-only, not by looking for a newline/comma.
# ---------------------------------------------------------------------------

def test_sole_firm_name_starting_with_confirm_is_never_misread_when_nothing_pending(monkeypatch, recorded_threads):
    sent = []
    monkeypatch.setattr(telegram_bot, "send", lambda text, chat_id=None: sent.append(text))

    telegram_bot._dispatch("/referral batch\nConfirm Realty LLC")

    assert len(recorded_threads) == 1, sent
    assert recorded_threads[0].target is telegram_bot._run_batch
    assert recorded_threads[0].args == (["Confirm Realty LLC"],)


def test_sole_firm_name_starting_with_cancel_is_never_misread_when_nothing_pending(monkeypatch, recorded_threads):
    sent = []
    monkeypatch.setattr(telegram_bot, "send", lambda text, chat_id=None: sent.append(text))

    telegram_bot._dispatch("/referral batch\nCancel Capital Partners")

    assert len(recorded_threads) == 1, sent
    assert recorded_threads[0].target is telegram_bot._run_batch
    assert recorded_threads[0].args == (["Cancel Capital Partners"],)


def test_sole_firm_name_starting_with_confirm_refused_not_misfired_against_unrelated_pending_batch(
        monkeypatch, recorded_threads):
    """The dangerous variant: a DIFFERENT batch is already pending, and the
    operator submits a brand-new single-firm batch that happens to start
    with "Confirm". Must be refused by the ordinary overwrite guard (the
    pending batch is untouched, the new firm is never silently discarded
    into confirming the wrong thing) -- never treated as if the operator
    had just confirmed the unrelated pending batch."""
    monkeypatch.setattr(config, "TEST_MODE", False)
    sent = []
    monkeypatch.setattr(telegram_bot, "send", lambda text, chat_id=None: sent.append(text))
    thirteen_firms = "\n".join(f"Firm {i}" for i in range(13))
    telegram_bot._dispatch(f"/referral batch\n{thirteen_firms}")
    assert telegram_bot._state_key() in telegram_bot._pending_file_batch
    original = telegram_bot._pending_file_batch[telegram_bot._state_key()]
    sent.clear()

    telegram_bot._dispatch("/referral batch\nConfirm Realty LLC")

    assert recorded_threads == [], "must not launch the unrelated pending 13-firm batch"
    assert "13 firm(s)" in sent[-1]
    assert "awaiting confirmation" in sent[-1]
    assert telegram_bot._pending_file_batch[telegram_bot._state_key()] is original
    assert original["names"] == [f"Firm {i}" for i in range(13)]


@pytest.mark.parametrize("text", [
    "/referral batch confirm, thanks",
    "/referral batch confirm to proceed\nthanks!",
    "/referral batch cancel, thanks",
])
def test_confirm_cancel_reply_with_one_extra_clause_still_recognized_as_a_reply(monkeypatch, recorded_threads, text):
    """A confirm/cancel reply that carries one extra natural clause (a
    trailing ", thanks" or a second line "thanks!") must still resolve
    SAFELY as a confirm/cancel attempt (verified here by nothing pending),
    not silently fall through and start/queue a real batch on garbage
    "firm names" like ["confirm", "thanks"]."""
    sent = []
    monkeypatch.setattr(telegram_bot, "send", lambda text, chat_id=None: sent.append(text))

    telegram_bot._dispatch(text)

    assert recorded_threads == [], f"{text!r} must never silently start a batch"
    assert sent[-1] in (
        "Nothing awaiting confirmation (or it expired). Re-upload the file to try again.",
        "Nothing to cancel.",
    )


def test_confirm_with_one_extra_clause_still_confirms_the_real_pending_batch(monkeypatch, recorded_threads):
    monkeypatch.setattr(config, "TEST_MODE", False)
    sent = []
    monkeypatch.setattr(telegram_bot, "send", lambda text, chat_id=None: sent.append(text))
    telegram_bot._dispatch("/referral batch\nKingbird Investment Management\nBlackstone")
    sent.clear()

    telegram_bot._dispatch("/referral batch confirm, thanks")

    assert "Confirmed" in sent[0], sent
    assert len(recorded_threads) == 1
    assert recorded_threads[0].args == (["Kingbird Investment Management", "Blackstone"],)


# --- 4. "batch" prefix requires a word boundary -----------------------------

def test_single_firm_command_starting_with_the_word_batch_is_not_misrouted(monkeypatch, recorded_threads):
    """A single-firm command whose name happens to start with the literal
    letters "batch" (no space in between) must route to the single-firm
    path under its FULL, uncorrupted name -- not be misrouted into batch
    parsing and truncated by the arg[len("batch"):] slice."""
    sent = []
    monkeypatch.setattr(telegram_bot, "send", lambda text, chat_id=None: sent.append(text))

    telegram_bot._dispatch("/referral Batchelor Real Estate Partners")

    assert len(recorded_threads) == 1, sent
    assert recorded_threads[0].target is telegram_bot._run_single_firm
    assert recorded_threads[0].args == ("Batchelor Real Estate Partners",)


def test_bare_batch_command_still_routes_correctly(monkeypatch, recorded_threads):
    """The word-boundary fix must not break the ordinary bare "batch" command
    itself (exactly 5 characters, nothing after it)."""
    sent = []
    monkeypatch.setattr(telegram_bot, "send", lambda text, chat_id=None: sent.append(text))

    telegram_bot._dispatch("/referral batch")

    assert telegram_bot._state_key() in telegram_bot._awaiting_batch_file
    assert recorded_threads == []


# --- 2. A pending batch can never be silently replaced ----------------------

def test_pending_batch_refuses_new_inline_batch_and_keeps_the_original(monkeypatch, recorded_threads):
    monkeypatch.setattr(config, "TEST_MODE", False)
    sent = []
    monkeypatch.setattr(telegram_bot, "send", lambda text, chat_id=None: sent.append(text))

    telegram_bot._dispatch("/referral batch\nKingbird Investment Management\nBlackstone")
    assert telegram_bot._state_key() in telegram_bot._pending_file_batch
    original = telegram_bot._pending_file_batch[telegram_bot._state_key()]
    sent.clear()

    telegram_bot._dispatch("/referral batch\nSome Other Firm")

    assert recorded_threads == [], "must not start ANY batch while one is pending"
    msg = sent[-1]
    assert "2 firm(s)" in msg
    assert "awaiting confirmation" in msg
    assert "/referral batch confirm" in msg
    assert "/referral batch cancel" in msg
    # the ORIGINAL pending batch must be untouched, not replaced
    assert telegram_bot._pending_file_batch[telegram_bot._state_key()] is original
    assert original["names"] == ["Kingbird Investment Management", "Blackstone"]


def test_pending_batch_refuses_new_file_upload_and_keeps_the_original(monkeypatch, recorded_threads):
    """The overwrite guard lives in _start_or_gate_batch, the single choke
    point both the inline-names path AND the file-upload path funnel
    through -- so a second file upload while one is pending is refused too,
    not just a second inline paste. Captures the `original` reference
    BEFORE the second call (matching test_pending_batch_refuses_new_inline_
    batch_and_keeps_the_original's identity-check rigor) so an object-
    replacement regression (a new dict with coincidentally-identical
    content) would be caught, not just a names-content diff."""
    monkeypatch.setattr(config, "TEST_MODE", False)
    sent = []
    monkeypatch.setattr(telegram_bot, "send", lambda text, chat_id=None: sent.append(text))

    telegram_bot._dispatch("/referral batch\nKingbird Investment Management\nBlackstone")
    assert telegram_bot._state_key() in telegram_bot._pending_file_batch
    original = telegram_bot._pending_file_batch[telegram_bot._state_key()]
    sent.clear()

    telegram_bot._process_batch_file(b"A Different Firm\nYet Another Firm\n", "second.csv")

    assert recorded_threads == []
    assert "awaiting confirmation" in sent[-1]
    assert telegram_bot._pending_file_batch[telegram_bot._state_key()] is original
    assert original["names"] == ["Kingbird Investment Management", "Blackstone"]


def test_bare_batch_command_while_pending_warns_immediately_instead_of_reprompting(monkeypatch, recorded_threads):
    """A bare "/referral batch" sent while a batch is already pending must
    say so immediately, rather than silently re-arming the "awaiting a
    file" handshake and only surfacing the conflict later, indirectly, when
    a follow-up upload gets refused."""
    monkeypatch.setattr(config, "TEST_MODE", False)
    sent = []
    monkeypatch.setattr(telegram_bot, "send", lambda text, chat_id=None: sent.append(text))

    telegram_bot._dispatch("/referral batch\nKingbird Investment Management\nBlackstone")
    sent.clear()

    telegram_bot._dispatch("/referral batch")

    assert "2 firm(s)" in sent[-1]
    assert "awaiting confirmation" in sent[-1]
    assert telegram_bot._state_key() not in telegram_bot._awaiting_batch_file, (
        "must not re-arm the file-upload handshake while a batch is already pending"
    )


def test_pending_batch_guard_does_not_block_confirm_or_cancel_themselves(monkeypatch, recorded_threads):
    """The overwrite guard must only block a NEW batch attempt -- confirm
    and cancel still work normally against the one already pending."""
    monkeypatch.setattr(config, "TEST_MODE", False)
    monkeypatch.setattr(telegram_bot, "send", lambda text, chat_id=None: None)

    telegram_bot._dispatch("/referral batch\nKingbird Investment Management\nBlackstone")
    assert telegram_bot._state_key() in telegram_bot._pending_file_batch

    telegram_bot._dispatch("/referral batch confirm")
    assert len(recorded_threads) == 1
    assert recorded_threads[0].target is telegram_bot._run_batch
    assert recorded_threads[0].args == (["Kingbird Investment Management", "Blackstone"],)
    assert telegram_bot._state_key() not in telegram_bot._pending_file_batch


def test_expired_pending_batch_does_not_block_a_new_one(monkeypatch, recorded_threads):
    """An expired pending entry is functionally gone -- it must not block a
    fresh batch from starting (matching the pre-existing TTL semantics
    _confirm_pending_file_batch already had)."""
    sent = []
    monkeypatch.setattr(telegram_bot, "send", lambda text, chat_id=None: sent.append(text))
    key = telegram_bot._state_key()
    telegram_bot._pending_file_batch[key] = {
        "names": ["Stale Firm"], "filename": None,
        "ts": time.time() - telegram_bot._PENDING_FILE_BATCH_TTL_SEC - 1,
    }

    telegram_bot._dispatch("/referral batch\nKingbird Investment Management\nBlackstone")

    assert len(recorded_threads) == 1
    assert recorded_threads[0].target is telegram_bot._run_batch
    assert recorded_threads[0].args == (["Kingbird Investment Management", "Blackstone"],)
    assert not any("awaiting confirmation" in s for s in sent)


# --- 3. A single command-fragment "firm name" is rejected, not researched --

@pytest.mark.parametrize("word", ["proceed", "yes", "ok", "stop", "Yes", "  ok  ", "STOP"])
def test_inline_batch_single_command_fragment_rejected_not_researched(monkeypatch, recorded_threads, word):
    sent = []
    monkeypatch.setattr(telegram_bot, "send", lambda text, chat_id=None: sent.append(text))

    telegram_bot._dispatch(f"/referral batch\n{word}")

    assert recorded_threads == [], f"{word!r} must never start a 1-firm batch"
    assert telegram_bot._state_key() not in telegram_bot._pending_file_batch
    assert "leftover confirm/cancel word" in sent[-1]
    assert word.strip() in sent[-1]


@pytest.mark.parametrize("word", ["confirm", "cancel"])
def test_inline_batch_bare_confirm_or_cancel_word_never_starts_a_bogus_batch(monkeypatch, recorded_threads, word):
    """A bare "confirm"/"cancel" typed where a firm name is expected is
    intercepted earlier by the tolerant confirm/cancel handler itself (req
    1, since "batch\\nconfirm" strips down to the single line "confirm" --
    indistinguishable from "batch confirm") rather than reaching the
    fragment-rejection backstop (req 3). The OUTCOME the incident report
    actually cares about is identical either way: nothing is researched as
    a company, and no phantom 1-firm batch is created or left pending."""
    sent = []
    monkeypatch.setattr(telegram_bot, "send", lambda text, chat_id=None: sent.append(text))

    telegram_bot._dispatch(f"/referral batch\n{word}")

    assert recorded_threads == []
    assert telegram_bot._state_key() not in telegram_bot._pending_file_batch
    assert sent[-1] in (
        "Nothing awaiting confirmation (or it expired). Re-upload the file to try again.",
        "Nothing to cancel.",
    )


def test_bare_confirm_with_trailing_comma_resolves_as_a_safe_confirm_attempt(monkeypatch, recorded_threads):
    """"confirm," (comma-suffixed, nothing else) reduces to an empty
    remainder once trailing punctuation is stripped -- it's just "confirm"
    with a stray comma, so it resolves as a genuine (here, no-op) confirm
    attempt, never as a literal "confirm" firm name."""
    sent = []
    monkeypatch.setattr(telegram_bot, "send", lambda text, chat_id=None: sent.append(text))

    telegram_bot._dispatch("/referral batch confirm,")

    assert recorded_threads == []
    assert telegram_bot._state_key() not in telegram_bot._pending_file_batch
    assert sent[-1] == "Nothing awaiting confirmation (or it expired). Re-upload the file to try again."


@pytest.mark.parametrize("word", ["confirm", "cancel", "proceed", "yes", "ok", "stop"])
def test_single_firm_command_fragment_rejected_not_researched(monkeypatch, recorded_threads, word):
    """Same guard applies to the bare /referral "<name>" single-firm path --
    a bare "/referral confirm" (no "batch") must not be researched as a
    company either."""
    sent = []
    monkeypatch.setattr(telegram_bot, "send", lambda text, chat_id=None: sent.append(text))

    telegram_bot._dispatch(f'/referral "{word}"')

    assert recorded_threads == [], f"{word!r} must never launch a single-firm research run"
    assert "leftover confirm/cancel word" in sent[-1]
    assert word in sent[-1], f"the specific rejected word {word!r} must appear in the message: {sent[-1]!r}"


# --- 3b. Adversarial re-review: the fragment guard must apply on EVERY -----
# entry point (inline text AND file upload) and in EVERY position (sole
# entry, or riding along inside an otherwise-real multi-firm list) -- the
# first draft only checked `len(names) == 1` on the inline-text branch,
# missing both the file-upload path entirely and any multi-item list with a
# stray fragment word mixed in. Centralized into _start_or_gate_batch, the
# single choke point both callers already funnel through for the overwrite
# guard, so there's exactly one place this can drift out of sync.
# ---------------------------------------------------------------------------

def test_file_upload_single_command_fragment_row_rejected_not_researched(monkeypatch, recorded_threads):
    sent = []
    monkeypatch.setattr(telegram_bot, "send", lambda text, chat_id=None: sent.append(text))

    telegram_bot._process_batch_file(b"confirm\n", "firms.csv")

    assert recorded_threads == [], "a lone \"confirm\" row must never start a real batch"
    assert telegram_bot._state_key() not in telegram_bot._pending_file_batch
    assert "leftover confirm/cancel word" in sent[-1]
    assert "confirm" in sent[-1]


@pytest.mark.parametrize("dispatch_fn", ["inline", "file"])
def test_fragment_word_riding_along_in_a_multi_firm_list_is_refused_not_silently_batched(
        monkeypatch, recorded_threads, dispatch_fn):
    """A stray "confirm"/"cancel"/etc. line pasted alongside real firm names
    (the same human habit that caused the original incident, just appended
    to a real list instead of standing alone) must refuse the WHOLE
    submission -- never silently research "confirm" as a company, and never
    silently drop the stray line and proceed with the rest unannounced."""
    sent = []
    monkeypatch.setattr(telegram_bot, "send", lambda text, chat_id=None: sent.append(text))

    if dispatch_fn == "inline":
        telegram_bot._dispatch("/referral batch\nKingbird Investment Management\nconfirm")
    else:
        telegram_bot._process_batch_file(b"Kingbird Investment Management\nconfirm\n", "firms.csv")

    assert recorded_threads == [], "must not start a batch containing a bare command-fragment entry"
    assert telegram_bot._state_key() not in telegram_bot._pending_file_batch
    assert "leftover confirm/cancel word" in sent[-1]
    assert "confirm" in sent[-1]
    assert "Kingbird Investment Management" not in sent[-1]  # only the offending entry is named


def test_command_fragment_rejection_lists_every_offending_entry():
    msg = telegram_bot._command_fragment_rejection(["confirm", "yes"])
    assert '"confirm"' in msg
    assert '"yes"' in msg
    assert "leftover confirm/cancel word" in msg


def test_command_fragment_rejection_does_not_reject_real_firm_names(monkeypatch, recorded_threads):
    """The guard is scoped to the exact fragment words -- a real single-name
    batch or single-firm command is completely unaffected."""
    sent = []
    monkeypatch.setattr(telegram_bot, "send", lambda text, chat_id=None: sent.append(text))

    telegram_bot._dispatch("/referral batch\nKingbird Investment Management")
    assert len(recorded_threads) == 1
    assert recorded_threads[0].target is telegram_bot._run_batch
    assert recorded_threads[0].args == (["Kingbird Investment Management"],)

    recorded_threads.clear()
    telegram_bot._dispatch('/referral "Blackstone"')
    assert len(recorded_threads) == 1
    assert recorded_threads[0].target is telegram_bot._run_single_firm
    assert recorded_threads[0].args == ("Blackstone",)


def test_looks_like_command_fragment_helper_exact_match_only():
    """Guards the exact scoping decision: only a BARE fragment word is
    rejected, never a multi-word phrase containing one (that's the tolerant
    confirm/cancel regex's job upstream, not this backstop's)."""
    for word in ("confirm", "cancel", "proceed", "yes", "ok", "stop"):
        assert telegram_bot._looks_like_command_fragment(word)
        assert telegram_bot._looks_like_command_fragment(word.upper())
        assert telegram_bot._looks_like_command_fragment(f"  {word}  ")
    assert not telegram_bot._looks_like_command_fragment("confirm to proceed")
    assert not telegram_bot._looks_like_command_fragment("Kingbird Investment Management")
    assert not telegram_bot._looks_like_command_fragment("")
    assert not telegram_bot._looks_like_command_fragment(None)


@pytest.mark.parametrize("remainder,expected", [
    ("", True),
    (" to proceed", True),
    (" please", True),
    (", thanks", True),
    (" to proceed\nthanks!", True),
    (".", True),
    (" Realty LLC", False),
    (" Capital Partners", False),
    ("\nOther Firm", False),
    (", Other Firm", False),
])
def test_remainder_is_filler_only_distinguishes_reply_filler_from_a_real_firm_continuation(remainder, expected):
    assert telegram_bot._remainder_is_filler_only(remainder) is expected


@pytest.mark.parametrize("rest,expected", [
    ("confirm", True),
    ("confirm to proceed", True),
    ("Confirm Realty LLC", False),
    ("Confirm Realty LLC\nOther Firm", False),
    ("confirmation", False),  # not a word-boundary match at all
    ("cancel", False),  # wrong keyword for the confirm regex
])
def test_matches_batch_keyword_confirm(rest, expected):
    assert telegram_bot._matches_batch_keyword(rest, telegram_bot._BATCH_CONFIRM_RE) is expected

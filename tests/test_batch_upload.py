"""QA Part 1 (Feature 2) — CSV/Excel target-list upload for /referral batch.
Tests both the pure parser (src/batch_upload.py) and its Telegram wiring
(src/telegram_bot.py), closing the gap the prior QA_REPORT flagged.
"""
from __future__ import annotations

import io

import openpyxl
import pytest

from scripts.reset_testmode import reset as reset_testmode
from src import batch_upload, telegram_bot
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

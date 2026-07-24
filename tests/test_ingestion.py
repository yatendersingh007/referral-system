"""QA Part 1 — adversarial input validation for the connections loader
(src/loader.py). Exercises §3's header alias map, encoding handling, and
malformed-row tolerance against both synthetic CSVs and the real 30,000-row
data/connections.csv.

This file mutates the shared `connections` table (it's testing the loader
itself). A module-scoped fixture restores the REAL file at teardown so later
test modules that depend on real connection data aren't left with synthetic
leftovers.
"""
from __future__ import annotations

import csv
from pathlib import Path

import pytest

from src import config, dbc
from src.loader import LoaderError, parse_connections_file, reload_connections


@pytest.fixture(scope="module", autouse=True)
def restore_real_connections_after_module():
    yield
    reload_connections()  # leave the shared table as the real 30k-row file for later test modules


def _write_csv(path: Path, header: list[str], rows: list[list[str]]) -> Path:
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        for r in rows:
            w.writerow(r)
    return path


# ---------------------------------------------------------------------------
# Well-formed real file
# ---------------------------------------------------------------------------
def test_real_file_loads_with_correct_distinct_company_count():
    rows, result = parse_connections_file(config.CONNECTIONS_CSV)
    assert result.total_rows == 30_000
    assert result.loaded == len(rows)
    assert 22_000 < result.distinct_companies < 24_000, (
        f"expected ~22-23k distinct companies (§3: '~22,000 distinct companies'), "
        f"got {result.distinct_companies}"
    )
    # every loaded row has a non-empty linkedin_url (the DB's UNIQUE NOT NULL key)
    assert all(r["linkedin_url"] for r in rows)


# ---------------------------------------------------------------------------
# Header drift (§3: case-insensitive alias map)
# ---------------------------------------------------------------------------
def test_header_alias_profile_url_and_company_name_and_title(tmp_path):
    path = _write_csv(
        tmp_path / "drift.csv",
        ["First Name", "Last Name", "Profile URL", "Email Address", "Company Name", "Title"],
        [["Jane", "Doe", "https://linkedin.com/in/janedoe", "", "Acme Capital Partners", "CFO"]],
    )
    rows, result = parse_connections_file(path)
    assert result.loaded == 1
    assert rows[0]["linkedin_url"] == "https://linkedin.com/in/janedoe"
    assert rows[0]["company_name_raw"] == "Acme Capital Partners"
    assert rows[0]["designation"] == "CFO"
    assert rows[0]["poc_name"] == "Jane Doe"


def test_header_alias_profile_link_variant(tmp_path):
    path = _write_csv(
        tmp_path / "driftlink.csv",
        ["First Name", "Last Name", "Profile Link", "Email Address", "Company", "Position"],
        [["Jo", "Smith", "https://linkedin.com/in/josmith", "", "Beta Realty", "Controller"]],
    )
    rows, result = parse_connections_file(path)
    assert result.loaded == 1
    assert rows[0]["linkedin_url"] == "https://linkedin.com/in/josmith"


def test_missing_url_column_hard_fails(tmp_path):
    path = _write_csv(
        tmp_path / "nourl.csv",
        ["First Name", "Last Name", "Email Address", "Company", "Position"],
        [["Jane", "Doe", "", "Acme", "CFO"]],
    )
    with pytest.raises(LoaderError, match="url"):
        parse_connections_file(path)


def test_missing_company_column_hard_fails(tmp_path):
    path = _write_csv(
        tmp_path / "nocompany.csv",
        ["First Name", "Last Name", "URL", "Email Address", "Position"],
        [["Jane", "Doe", "https://linkedin.com/in/janedoe", "", "CFO"]],
    )
    with pytest.raises(LoaderError, match="company"):
        parse_connections_file(path)


# ---------------------------------------------------------------------------
# Encoding: non-ASCII, accents, ®, emoji — must load as UTF-8, never mangled
# ---------------------------------------------------------------------------
def test_non_ascii_names_and_emoji_load_without_mangling(tmp_path):
    path = _write_csv(
        tmp_path / "unicode.csv",
        ["First Name", "Last Name", "URL", "Email Address", "Company", "Position"],
        [
            ["José", "García, CFP®", "https://linkedin.com/in/josegarcia", "",
             "Tree \U0001F333 Ventures", "Managing Partner"],
            ["Göran", "Espelund", "https://linkedin.com/in/goranespelund", "",
             "Lannebo Fonder", "Board Member"],
        ],
    )
    rows, result = parse_connections_file(path)
    assert result.loaded == 2
    assert rows[0]["poc_name"] == "José García, CFP®"
    assert rows[0]["company_name_raw"] == "Tree \U0001F333 Ventures"
    assert rows[1]["poc_name"] == "Göran Espelund"


def test_real_file_known_mojibake_is_source_data_not_introduced_by_loader():
    """The real file already contains a corrupted name ('Cristina Nu√±ez'
    at 'True Beauty Ventures') — verified at the byte level to be genuinely
    present in the source CSV (UTF-8 bytes for U+221A/U+00B1, not a decode
    artifact). The loader must pass this through FAITHFULLY (neither crash
    nor further mangle it) rather than "fixing" or worsening it — confirms
    §3's "read as UTF-8, do not re-encode" instruction is honored."""
    rows, _ = parse_connections_file(config.CONNECTIONS_CSV)
    hit = [r for r in rows if r["linkedin_url"] == "https://www.linkedin.com/in/cristinamnunez"]
    assert hit, "expected the known Cristina Nu√±ez row by its stable linkedin_url"
    assert hit[0]["poc_name"] == "Cristina Nu√±ez", (
        "loader must pass the source file's bytes through unchanged, mojibake and all "
        f"— got {hit[0]['poc_name']!r}"
    )


# ---------------------------------------------------------------------------
# Malformed rows
# ---------------------------------------------------------------------------
def test_missing_company_flags_incomplete_but_keeps_row(tmp_path):
    path = _write_csv(
        tmp_path / "nocompanyval.csv",
        ["First Name", "Last Name", "URL", "Email Address", "Company", "Position"],
        [["Jane", "Doe", "https://linkedin.com/in/janedoe2", "", "", "CFO"]],
    )
    rows, result = parse_connections_file(path)
    assert result.loaded == 1
    assert rows[0]["incomplete_row"] is True
    assert result.incomplete == 1
    assert result.distinct_companies == 0  # incomplete rows excluded from matching


def test_missing_url_drops_row_entirely(tmp_path):
    path = _write_csv(
        tmp_path / "nourlval.csv",
        ["First Name", "Last Name", "URL", "Email Address", "Company", "Position"],
        [
            ["Jane", "Doe", "", "", "Acme Capital", "CFO"],
            ["Jo", "Smith", "https://linkedin.com/in/josmith2", "", "Beta Realty", "Controller"],
        ],
    )
    rows, result = parse_connections_file(path)
    assert result.total_rows == 2
    assert result.loaded == 1  # the no-URL row can't be stored (UNIQUE NOT NULL key)
    assert result.incomplete == 1
    assert rows[0]["linkedin_url"] == "https://linkedin.com/in/josmith2"


def test_blank_lines_do_not_crash(tmp_path):
    path = tmp_path / "blanklines.csv"
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write("First Name,Last Name,URL,Email Address,Company,Position\r\n")
        f.write("\r\n")
        f.write("Jane,Doe,https://linkedin.com/in/janedoe3,,Acme Capital,CFO\r\n")
        f.write("\r\n")
    rows, result = parse_connections_file(path)
    # a fully blank CSV line has no URL/Company -> incomplete/dropped, never a crash
    assert result.loaded >= 1
    assert any(r["linkedin_url"] == "https://linkedin.com/in/janedoe3" for r in rows)


def test_ragged_rows_fewer_and_more_columns_do_not_crash(tmp_path):
    path = tmp_path / "ragged.csv"
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write("First Name,Last Name,URL,Email Address,Company,Position\n")
        # fewer columns than header (Position missing entirely)
        f.write("Jane,Doe,https://linkedin.com/in/ragged1,,Acme Capital\n")
        # more columns than header (extra trailing field)
        f.write("Jo,Smith,https://linkedin.com/in/ragged2,,Beta Realty,Controller,ExtraJunk\n")
    rows, result = parse_connections_file(path)
    assert result.loaded == 2
    by_url = {r["linkedin_url"]: r for r in rows}
    assert by_url["https://linkedin.com/in/ragged1"]["designation"] is None
    assert by_url["https://linkedin.com/in/ragged2"]["designation"] == "Controller"


def test_duplicate_linkedin_url_upserts_without_crashing(tmp_path):
    path = _write_csv(
        tmp_path / "dup.csv",
        ["First Name", "Last Name", "URL", "Email Address", "Company", "Position"],
        [
            ["Old", "Name", "https://linkedin.com/in/dupurl", "", "Old Company", "Analyst"],
            ["New", "Name", "https://linkedin.com/in/dupurl", "", "New Company Partners", "CFO"],
        ],
    )
    rows, result = parse_connections_file(path)
    assert result.total_rows == 2
    assert result.loaded == 2  # both parsed rows present in the batch...
    n = dbc.bulk_insert_connections(rows)
    assert n == 2  # ...but upserted by linkedin_url, not duplicated
    final = dbc.connection_by_url("https://linkedin.com/in/dupurl")
    assert final["poc_name"] == "New Name"  # last-write-wins
    assert final["company_name_raw"] == "New Company Partners"


# ---------------------------------------------------------------------------
# Empty / tiny / large files
# ---------------------------------------------------------------------------
def test_empty_data_file_header_only_no_crash(tmp_path):
    path = _write_csv(
        tmp_path / "headeronly.csv",
        ["First Name", "Last Name", "URL", "Email Address", "Company", "Position"],
        [],
    )
    rows, result = parse_connections_file(path)
    assert result.total_rows == 0
    assert result.loaded == 0
    assert result.distinct_companies == 0


def test_completely_empty_file_hard_fails(tmp_path):
    path = tmp_path / "empty.csv"
    path.write_text("", encoding="utf-8")
    with pytest.raises(LoaderError):
        parse_connections_file(path)


def test_single_row_file(tmp_path):
    path = _write_csv(
        tmp_path / "single.csv",
        ["First Name", "Last Name", "URL", "Email Address", "Company", "Position"],
        [["Solo", "Person", "https://linkedin.com/in/solo", "", "Solo Capital", "CEO"]],
    )
    rows, result = parse_connections_file(path)
    assert result.total_rows == 1
    assert result.loaded == 1
    assert result.distinct_companies == 1


def test_large_real_file_reload_via_dbc_no_crash_sane_counts():
    result = reload_connections()
    assert result.total_rows == 30_000
    assert dbc.connections_count() == result.loaded


def test_reload_survives_a_historical_wc_firms_reference(tmp_path):
    """QA-found bug (fixed in migrations/005): /referral reload calls
    dbc.clear_connections() (DELETE FROM connections). Once ANY firm has ever
    been drafted, wc_firms.top_poc_connection_id / wc_drafts.connection_id
    reference a connections row — with the Postgres-default ON DELETE NO
    ACTION, the very next reload would crash with a ForeignKeyViolation,
    permanently breaking the reload command in production. Reproduces the
    exact scenario and confirms the historical row survives (reference goes
    NULL, the audit row itself is untouched) rather than blocking the reload."""
    reload_connections()  # ensure a real connection row exists to reference
    real_conn = dbc.all_connections()[0]

    run = dbc.create_wc_run("single_firm", query_firm="FK Regression Test Co")
    firm = dbc.create_wc_firm(run["id"], "FK Regression Test Co")
    dbc.update_wc_firm(firm["id"], top_poc_connection_id=real_conn["id"])
    draft = dbc.create_wc_draft(firm["id"], real_conn["id"], email_status="drafted")

    result = reload_connections()  # must NOT raise ForeignKeyViolation
    assert result.total_rows == 30_000

    firm_row = dbc.get_wc_firm(firm["id"])
    assert firm_row is not None, "the historical wc_firms row must survive a connections reload"
    assert firm_row["top_poc_connection_id"] is None, "the stale connection reference should go NULL"

    with dbc.cursor() as cur:
        cur.execute("SELECT connection_id FROM wc_drafts WHERE id = %s", (draft["id"],))
        assert cur.fetchone()["connection_id"] is None

    # cleanup: this test's own synthetic rows, so later modules see a clean slate
    with dbc.conn() as c:
        with c.cursor() as cur:
            cur.execute("DELETE FROM wc_drafts WHERE id = %s", (draft["id"],))
            cur.execute("DELETE FROM wc_firms WHERE id = %s", (firm["id"],))
            cur.execute("DELETE FROM wc_runs WHERE id = %s", (run["id"],))
        c.commit()


# ---------------------------------------------------------------------------
# Excel/CSV batch-input ingestion (§4: "Telegram-listed OR uploaded CSV of
# firm names — can be 1,000+"). GAP CLOSED — Feature 2 (see PR notes) added
# real .csv/.xlsx target-list upload via Telegram getFile. The canary that
# used to document this as an open gap has been replaced with the real
# adversarial tests in tests/test_batch_upload.py, per the QA report's own
# instruction: "if file-upload support is ever added, this assertion should
# be replaced with real .xlsx/.csv batch-upload tests."
# ---------------------------------------------------------------------------
def test_batch_upload_getfile_and_openpyxl_wiring_now_exists():
    """Confirms the gap is actually closed (not just that the old canary was
    deleted): the real download path and .xlsx parser dependency exist."""
    import inspect

    from src import batch_upload, telegram_bot

    src_text = inspect.getsource(telegram_bot)
    assert "getFile" in src_text
    assert "_download_telegram_file" in src_text
    assert hasattr(batch_upload, "parse_firm_list_file")

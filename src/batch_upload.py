"""Feature 2 — target-firm-list file upload for /referral batch (§4 Phase 1:
"a firm list (Telegram-listed OR uploaded CSV of firm names)"). Closes the
gap the QA_REPORT flagged: batch input was Telegram-text-only.

Parses a .csv or .xlsx file of firm names, tolerant of header/no-header,
blank/trailing rows, BOM, quoted fields, and multi-column layouts (picks the
column whose header contains "firm"/"company"/"name"; falls back to the
first column with a warning if ambiguous). Never touches data/connections.csv
or its loader — this is a DIFFERENT file (a target list) with a different,
free-form column schema, not the fixed LinkedIn export schema.
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field

# QA finding (adversarial re-attack, Part 9): "company" is NOT a substring of
# "companies" (irregular pluralization -- differs at "compan[y|ies]"), so a
# multi-column file headed "Target Companies" silently fell through to "no
# clear header, use the first column" -- and when the firm names AREN'T in
# the first column, that picks the WRONG column and quietly substitutes
# garbage strings (another column's data) as firm names. "firm"/"firms" and
# "name"/"names" already match via substring (firm/name are prefixes of
# their plurals); "companies" needed adding explicitly.
_HEADER_KEYWORDS = ("firm", "company", "companies", "name")
# For the single-column "does row 1 look like a header?" check: an EXACT
# (trimmed, case-insensitive) match against a known label, never a substring
# check. QA finding: a substring check on _HEADER_KEYWORDS wrongly treated a
# perfectly good single-column firm name as a header whenever the name
# happened to CONTAIN "firm"/"name" anywhere — e.g. "Firm Capital
# Corporation" (a real company) or this file's own test data ("Just Some
# Firm Name") would be silently dropped as if it were a column label. Only
# multi-column files use the looser substring match (via _pick_column), and
# only to pick WHICH already-confirmed header cell to use, not to decide
# header-vs-data in the first place.
# QA finding (adversarial re-attack, Part 9): the exact-match fix for Bug 1
# was necessary but too narrow -- plausible real single-column header labels
# ("Target Companies", "Firms", "Company Names", "Firm List", ...) aren't
# exact matches for the original small singular-only set, so they were
# silently kept as row 1 DATA and leaked into the batch as a bogus firm name.
# Expanded to cover the common plural/list phrasings. This can never be
# exhaustive for arbitrary header wording (e.g. "Prospect Firms") -- when a
# header truly isn't recognized, the safer failure mode is to treat it as
# data (one wasted no_connection cycle) rather than risk dropping a real
# single-word firm name that happens to match a header label.
_EXACT_HEADER_LABELS = {
    "firm", "firm name", "firm names", "firms", "firm list",
    "company", "company name", "company names", "companies", "company list",
    "name", "names", "target firm", "target firms", "target company", "target companies",
}
CONFIRM_THRESHOLD = 2000


class BatchUploadError(RuntimeError):
    pass


@dataclass
class ParsedBatchFile:
    raw_count: int              # names found before dedup
    names: list[str]            # deduped, in order
    warnings: list[str] = field(default_factory=list)


def _looks_like_header(row: list[str]) -> bool:
    return any((cell or "").strip().lower() in _EXACT_HEADER_LABELS for cell in row)


def _pick_column(header_row: list[str]) -> tuple[int, list[str]]:
    """Returns (column_index, warnings)."""
    if len(header_row) <= 1:
        return 0, []
    for i, cell in enumerate(header_row):
        if any(kw in (cell or "").strip().lower() for kw in _HEADER_KEYWORDS):
            return i, []
    return 0, ["Multiple columns found with no clear firm-name header "
              "(looked for \"firm\"/\"company\"/\"name\"); used the first column."]


def _dedupe_preserve_order(names: list[str]) -> list[str]:
    seen: set[str] = set()
    out = []
    for n in names:
        key = n.strip()
        if key and key not in seen:
            seen.add(key)
            out.append(key)
    return out


def _rows_to_names(rows: list[list[str]]) -> ParsedBatchFile:
    rows = [r for r in rows if any((c or "").strip() for c in r)]  # drop fully-blank rows
    if not rows:
        return ParsedBatchFile(raw_count=0, names=[])

    warnings: list[str] = []
    first_row = rows[0]
    multi_col = len(first_row) > 1
    # §4/Feature 2: multi-column support is spec'd as "multi-column WHERE a
    # column header contains firm/company/name" — multi-column files are
    # assumed to carry a header row by design, whether or not a keyword
    # matches it. Without this, an ambiguous multi-column file with no
    # recognizable header (e.g. "Col1,Col2") silently included the header
    # row's own generic label ("Col1") as if it were a firm name — found via
    # a hands-on parsing test, fixed here. Single-column files stay
    # optionally-headered (a bare list of names with no header at all is the
    # common case there).
    header = multi_col or _looks_like_header(first_row)
    data_rows = rows[1:] if header else rows
    col, col_warnings = _pick_column(first_row) if multi_col else (0, [])
    warnings.extend(col_warnings)

    raw_names = []
    for row in data_rows:
        if col < len(row):
            val = (row[col] or "").strip()
            if val:
                raw_names.append(val)
    deduped = _dedupe_preserve_order(raw_names)
    return ParsedBatchFile(raw_count=len(raw_names), names=deduped, warnings=warnings)


# QA finding (hands-on adversarial test): csv.reader is extremely permissive
# — a binary/garbage file with embedded NUL bytes decodes fine as UTF-8 (NUL
# is a valid codepoint) and parses as "1 row, 1 firm name" containing control
# characters. That's not a crash, but it's not a "clear rejection" either —
# it silently hands a nonsense string into the pipeline as a real target
# firm. Reject any file whose text contains NUL or other non-printable
# control characters (beyond ordinary whitespace) before ever handing it to
# csv.reader.
_BAD_CONTROL_CHARS = {chr(i) for i in range(32) if chr(i) not in ("\t", "\n", "\r")} | {"\x00"}


def _looks_non_tabular_binary(text: str) -> bool:
    return any(c in _BAD_CONTROL_CHARS for c in text)


def _parse_csv(file_bytes: bytes) -> ParsedBatchFile:
    try:
        text = file_bytes.decode("utf-8-sig")  # tolerates a BOM transparently
    except UnicodeDecodeError as e:
        raise BatchUploadError(f"Could not decode file as UTF-8: {e}") from e
    if _looks_non_tabular_binary(text):
        raise BatchUploadError("File contains binary/control characters — doesn't look like a valid CSV.")
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        raise BatchUploadError("CSV file has no rows.")
    return _rows_to_names(rows)


def _parse_xlsx(file_bytes: bytes) -> ParsedBatchFile:
    try:
        import openpyxl
    except ImportError as e:  # pragma: no cover - dependency always pinned
        raise BatchUploadError("openpyxl is not installed; cannot parse .xlsx files.") from e

    try:
        # data_only=True: read the CACHED value of formula cells (openpyxl
        # never evaluates a formula — it just parses the workbook XML — so
        # there is no execution risk from a "=cmd|...!A1"-style cell; the
        # cached value is whatever Excel last computed, and if there is none
        # (e.g. never opened in real Excel) the cell reads as None/empty).
        wb = openpyxl.load_workbook(io.BytesIO(file_bytes), read_only=True, data_only=True)
    except Exception as e:  # noqa: BLE001 - openpyxl raises several exception types for bad files
        raise BatchUploadError(f"Could not read this as a valid .xlsx file: {e}") from e

    sheet = wb.active
    if sheet is None:
        raise BatchUploadError("Workbook has no active sheet.")
    rows = []
    for row in sheet.iter_rows(values_only=True):
        rows.append(["" if c is None else str(c) for c in row])
    wb.close()
    if not rows:
        raise BatchUploadError("Excel file has no rows.")
    return _rows_to_names(rows)


def parse_firm_list_file(file_bytes: bytes, filename: str) -> ParsedBatchFile:
    """Entry point. Raises BatchUploadError for anything unparseable — never
    silently returns an empty/garbage list."""
    if not file_bytes:
        raise BatchUploadError("Empty file.")
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    if ext == "csv":
        result = _parse_csv(file_bytes)
    elif ext == "xlsx":
        result = _parse_xlsx(file_bytes)
    else:
        raise BatchUploadError(f'Unsupported file type "{filename}" — attach a .csv or .xlsx of firm names.')
    if not result.names:
        raise BatchUploadError(f'No firm names found in "{filename}" (file may be empty or non-tabular).')
    return result

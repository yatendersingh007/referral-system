"""Connections loader (§3, §7 step 3). Reads the read-only ground-truth
Connections.csv export, normalizes it, and loads it into the `connections`
table. The file itself is NEVER mutated — all matching happens against the DB
copy (HARD RULE 10).

Header drift tolerance: a case-insensitive alias map covers the column-name
variants LinkedIn's export tool has shipped over time. Missing URL or Company
after alias resolution is a hard failure (there's nothing to match on).
"""
from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path

from . import config, dbc
from .matching.normalize import normalize as normalize_company

_ALIASES = {
    "url": {"url", "profile url", "profile link"},
    "company": {"company", "company name"},
    "position": {"position", "title", "designation"},
    "first_name": {"first name"},
    "last_name": {"last name"},
    "email": {"email address", "email"},
}


class LoaderError(RuntimeError):
    pass


@dataclass
class LoadResult:
    total_rows: int = 0
    loaded: int = 0
    incomplete: int = 0
    distinct_companies: int = 0
    errors: list[str] = field(default_factory=list)


def _resolve_headers(fieldnames: list[str]) -> dict[str, str]:
    """Map canonical field -> actual CSV header name, case-insensitively."""
    lower_to_actual = {h.strip().lower(): h for h in (fieldnames or [])}
    resolved: dict[str, str] = {}
    for canonical, variants in _ALIASES.items():
        for v in variants:
            if v in lower_to_actual:
                resolved[canonical] = lower_to_actual[v]
                break
    missing_required = [c for c in ("url", "company") if c not in resolved]
    if missing_required:
        raise LoaderError(
            f"Connections file is missing required column(s): {missing_required} "
            f"(looked for aliases {[_ALIASES[c] for c in missing_required]}). "
            f"Actual headers: {fieldnames}"
        )
    return resolved


def _poc_name(row: dict, headers: dict[str, str]) -> str:
    first = (row.get(headers.get("first_name", ""), "") or "").strip()
    last = (row.get(headers.get("last_name", ""), "") or "").strip()
    return " ".join(p for p in (first, last) if p)


def parse_connections_file(path: Path) -> tuple[list[dict], LoadResult]:
    """Parse the CSV (UTF-8, never re-encoded) into row dicts ready for
    dbc.bulk_insert_connections. Never mutates the source file."""
    result = LoadResult()
    rows_out: list[dict] = []
    companies: set[str] = set()

    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        headers = _resolve_headers(reader.fieldnames or [])
        for raw in reader:
            result.total_rows += 1
            url = (raw.get(headers["url"], "") or "").strip()
            company_raw = (raw.get(headers["company"], "") or "").strip()
            incomplete = not url or not company_raw
            poc_name = _poc_name(raw, headers)
            designation = (raw.get(headers.get("position", ""), "") or "").strip() or None
            email_seed = (raw.get(headers.get("email", ""), "") or "").strip() or None
            company_norm = normalize_company(company_raw) if company_raw else ""

            if not url:
                # No stable identity key at all -> can't even store the row
                # (linkedin_url is UNIQUE NOT NULL); count it as incomplete and skip.
                result.incomplete += 1
                continue

            rows_out.append({
                "linkedin_url": url,
                "poc_name": poc_name or "(unknown)",
                "designation": designation,
                "company_name_raw": company_raw or None,
                "company_name_norm": company_norm or None,
                "poc_email_seed": email_seed,
                "incomplete_row": incomplete,
            })
            if incomplete:
                result.incomplete += 1
            else:
                companies.add(company_norm)

    result.loaded = len(rows_out)
    result.distinct_companies = len(companies)
    return rows_out, result


def reload_connections(path: Path | None = None) -> LoadResult:
    """/referral reload: re-ingest data/connections.csv. Clears and reloads
    the `connections` table (the CSV file itself is the ground truth; the DB
    is just its queryable mirror — HARD RULE 10)."""
    path = path or config.CONNECTIONS_CSV
    if not path.exists():
        raise LoaderError(f"Connections file not found: {path}")
    rows, result = parse_connections_file(path)
    dbc.clear_connections()
    dbc.bulk_insert_connections(rows)
    return result

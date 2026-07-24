"""Phase 3a — POC selection (§4 Phase 3). Ranks a firm's matched connections
by seniority/relevance from Designation and picks the top 1 to draft to; the
rest are stored as alternate_pocs for future use.

icp_scoring_v2_2.md's "Who to target" section already enumerates the accepted
higher-level-management titles (CFO, VP Finance, Controller, Managing
Director/Partner, Head of Finance/Operations) as
lib_snapshot.config.MGMT_TITLE_HINTS, ordered roughly senior-first. Ranking by
position in that list is deterministic and needs no LLM round-trip — a
simplification from the spec's "an LLM call ranks them" framing, made because
the accepted-title set is already a fixed, exhaustive list rather than free
text needing judgment. Anyone whose designation matches none of those hints
(juniors, interns, unrelated departments) is dropped entirely, per
icp_scoring_v2_2.md's targeting rule.
"""
from __future__ import annotations

from typing import Optional

from lib_snapshot import config as shared_config


def _seniority_rank(designation: str) -> Optional[int]:
    title = (designation or "").lower()
    for i, hint in enumerate(shared_config.MGMT_TITLE_HINTS):
        if hint in title:
            return i
    return None


def rank_pocs(connections: list[dict]) -> tuple[Optional[dict], list[dict]]:
    """Returns (top_poc, alternate_pocs) — both are connection rows (dicts),
    sorted most-senior-first. (None, []) if nobody on the list qualifies as
    higher-level management."""
    scored = [(r, c) for c in connections if (r := _seniority_rank(c.get("designation"))) is not None]
    scored.sort(key=lambda pair: pair[0])
    if not scored:
        return None, []
    ranked = [c for _, c in scored]
    return ranked[0], ranked[1:]

"""One-time setup task (§9): locate every per-firm slot in the master PPTX by
its known Kingbird string (the master IS a real prior Kingbird deck, so its
current values are themselves the Kingbird fill) and write
templates/pitch_slots.json.

Run: python scripts/locate_pptx_slots.py

Each run in the master is found by a small distinguishing ANCHOR substring
(not by hand-transcribing the full sentence, which risks a transcription typo
against the real XML — including its entity escaping, e.g. "Kingbird&apos;s").
The full raw <a:t> run text becomes the locator; HARD RULE 16a requires every
locator be found EXACTLY ONCE in its slide, which this script asserts at
generation time (and deck_filler.py re-asserts at every run's preflight).
"""
from __future__ import annotations

import json
import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src import config  # noqa: E402

_RUN_RE = re.compile(r"<a:t>(.*?)</a:t>", re.DOTALL)

# (slide_number, anchor_substring, key, slot_type, is_name_swap)
# slot_type: "name_swap" -> template stores {{firm_short}} in place of the
# anchor; "full_replace" -> the whole run is replaced by the data-contract
# field named `key` at fill time.
_SPECS = [
    (1, "Built for Kingbird", "cover_title", "name_swap", "Kingbird"),
    (1, "PREPARED FOR", None, None, None),  # label only, fixed; value handled next
    (1, "Kingbird Investments", "firm_name_display", "full_replace", None),

    (2, "extend Kingbird", "agenda_subtitle", "name_swap", "Kingbird"),
    (2, "EXACT:Kingbird", "agenda_section01_label", "full_replace", None),
    (2, "maps to Kingbird", "agenda_fit_line", "name_swap", "Kingbird"),

    (3, "What we know about Kingbird", "profile_title", "name_swap", "Kingbird"),
    (3, "Kingbird Investments is a vertically integrated", "firm_oneliner", "full_replace", None),
    (3, "over a century of family office heritage", "profile_bullet_1", "full_replace", None),
    (3, "Manages $4.4 billion in gross asset value", "profile_bullet_2", "full_replace", None),
    (3, "Specializes in multifamily workforce housing", "profile_bullet_3", "full_replace", None),
    (3, "Investment approach prioritizes income", "profile_bullet_4", "full_replace", None),
    (3, "Appointed Director of Capital Markets", "profile_bullet_5", "full_replace", None),
    (3, "Acts as fiduciary for the", "profile_bullet_6", "full_replace", None),
    (3, "$4.4B", "aum_value", "full_replace", None),
    (3, "Gross Asset Value", "aum_label", "full_replace", None),
    (3, "as of Dec 31, 2025", "aum_asof", "full_replace", None),
    (3, "2008", "founded_year", "full_replace", None),
    (3, "LP, GP, Co-GP, Preferred Equity", "capital_stack", "full_replace", None),

    (12, "Where this fits Kingbird specifically", "fit_title", "name_swap", "Kingbird"),
    (12, "Scalable Property Accounting", "fit_paragraph_1_heading", "fixed", None),
    (12, "With $4.4B in gross assets", "fit_paragraph_1", "full_replace", None),
    (12, "Complex Fund Structures", "fit_paragraph_2_heading", "fixed", None),
    (12, "use of LP, GP, Co-GP", "fit_paragraph_2", "full_replace", None),
    (12, "Tailored Investor Reporting", "fit_paragraph_3_heading", "fixed", None),
    (12, "Serving family offices and institutional investors", "fit_paragraph_3", "full_replace", None),
    (12, "Underwriting &amp; Acquisitions Support", "fit_paragraph_4_heading", "fixed", None),
    (12, "research-driven acquisition strategy", "fit_paragraph_4", "full_replace", None),
    (12, "Capital Markets Expertise", "fit_paragraph_5_heading", "fixed", None),
    (12, "dedicated Director of Capital Markets", "fit_paragraph_5", "full_replace", None),

    (14, "Kingbird&apos;s next chapter", "closing_line", "name_swap", "Kingbird"),
]


def _slide_text(z: zipfile.ZipFile, slide_no: int) -> str:
    return z.read(f"ppt/slides/slide{slide_no}.xml").decode("utf-8")


def main() -> None:
    if not config.PITCH_MASTER_PPTX.exists():
        raise SystemExit(f"master PPTX not found: {config.PITCH_MASTER_PPTX}")

    z = zipfile.ZipFile(config.PITCH_MASTER_PPTX)
    slides: dict[str, list[dict]] = {}
    problems: list[str] = []

    for slide_no, anchor, key, slot_type, name_token in _SPECS:
        if key is None:
            continue  # label-only anchor (e.g. "PREPARED FOR"), not a slot
        xml = _slide_text(z, slide_no)
        runs = _RUN_RE.findall(xml)
        if anchor.startswith("EXACT:"):
            want = anchor[len("EXACT:"):]
            matches = [r for r in runs if r == want]
        else:
            matches = [r for r in runs if anchor in r]
        if len(matches) != 1:
            problems.append(f"slide{slide_no}: anchor {anchor!r} matched {len(matches)} run(s), expected 1")
            continue
        locator = matches[0]
        # HARD RULE 16a: the locator must be found exactly once in the slide's
        # raw XML — checked on the WRAPPED <a:t>...</a:t> form, since that's
        # what fill-time replacement actually targets. The bare inner text can
        # legitimately recur as a substring inside a DIFFERENT run's longer
        # sentence (e.g. "Kingbird" inside "extend Kingbird's operating team")
        # without that being a real ambiguity for replacement purposes.
        wrapped = f"<a:t>{locator}</a:t>"
        if xml.count(wrapped) != 1:
            problems.append(f"slide{slide_no}: wrapped locator {wrapped!r} appears "
                            f"{xml.count(wrapped)}x in raw XML, expected exactly 1")
            continue

        entry = {"key": key, "type": slot_type, "locator": locator}
        if slot_type == "name_swap":
            entry["template"] = locator.replace(name_token, "{{firm_short}}")
        slides.setdefault(str(slide_no), []).append(entry)

    if problems:
        for p in problems:
            print("PROBLEM:", p)
        raise SystemExit(f"{len(problems)} slot(s) failed to locate uniquely — fix _SPECS above")

    out = {"master_pptx": config.PITCH_MASTER_PPTX.name, "slides": slides}
    config.PITCH_SLOTS_JSON.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    n_slots = sum(len(v) for v in slides.values())
    print(f"Wrote {config.PITCH_SLOTS_JSON} ({n_slots} slots across {len(slides)} slides).")


if __name__ == "__main__":
    main()

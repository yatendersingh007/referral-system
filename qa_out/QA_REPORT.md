# Workflow C — Adversarial QA Report

**Scope:** full-workflow adversarial audit against BUILD_SPEC_C.md,
templates/referral_email.md, and prompts/icp_scoring_v2_2.md, covering FOUR
sessions: the original 10-part audit (154 tests), a "build 4 features + full
re-audit" pass (45 new tests, 3 new bugs found+fixed), a "two operational
features" pass (26 new tests, Tavily key-pool rotation + /cancel — see
§2b), and a manual security review + fix-application pass (see
`qa_out/SECURITY_REPORT.md` and §9b below — no new tests, existing 225 kept
green). All Layer 1 (TEST_MODE logic/plumbing) tests are automated, green,
and stable across repeated runs. Layer 2 (real-mode research/email/deck
accuracy) is scaffolded but intentionally not executed — see §5.

**Bottom line:** **225 automated tests, all passing** (154 → 199 → 225,
unchanged by the security-fix pass). **6 features built total** (single-firm
prospect brief, CSV/Excel batch-list upload, batch-scale person-level dedup,
grouped per-chunk batch messaging, Tavily key-pool rotation, clean /cancel),
each with tests proving it. **3 genuine production bugs found and fixed in
the second session's adversarial re-attack** (on top of the original 9)
**+ 1 deliberate message-wording update to a pre-existing test** (not a
regression — see §2b) **+ 2 real security fixes applied in the fourth
session** (missing `.gitignore`, verbose search-log query text — see §9b),
**3 more requested "fixes" investigated and found not applicable** (they
referenced files/functions/libraries that don't exist in this codebase —
see §9b for the specific mismatch found in each case, reported rather than
silently applied or silently skipped). One residual gap flagged, not
silently patched (§6d, unrelated). Zero assertions were weakened to force a
pass — every change either fixed genuine behavior or reflected a
deliberate, spec-required change in what a message says.

---

## 1. Summary table

| Part | Area | File(s) | Tests | Result |
|---|---|---|---|---|
| 1 | Input validation & ingestion (+ Feature 2 file upload) | `tests/test_ingestion.py`, `tests/test_batch_upload.py` | 18 + 28 = 46 | PASS (2 NEW bugs found+fixed this session) |
| 2 | Firm matching correctness | `tests/test_firm_matcher.py` | 36 | PASS (unchanged this session — re-confirmed green, not touched) |
| 3 | ICP gate & AUM proxy logic | `tests/test_icp_gate.py` | 26 | PASS (unchanged this session) |
| 4 | Email generation quality | `tests/test_email.py` | 32 | PASS (unchanged this session) |
| 5 | Deck fill & QA | `tests/test_deck.py` | 23 | PASS (unchanged this session; money-leak re-attacked, 1 residual gap flagged §6d) |
| 6 | Dedup (+ Feature 3 batch-scale + URL-normalization) | `tests/test_dedup.py` | 13 (was 6) | PASS (1 NEW bug found+fixed this session) |
| 7 | Orchestration/resume/circuit breaker (+ Feature 1/4 wiring) | `tests/test_pipeline.py`, `tests/test_status.py`, `tests/test_telegram_bot.py` | 14 + 5 + 4 = 23 | PASS |
| 8 | Real-mode dry-run harness | `scripts/realmode_smoke.py` | n/a (scaffold) | BUILT, NOT RUN (by design); refusal guards re-verified this session |
| 9 | Adversarial review (matcher + deck re-attack, NEW: file parsing + dedup) | manual, documented in §6 | n/a | 3 NEW bugs found+fixed, 1 gap flagged |
| — | End-to-end (§7 step 12, 5 outcomes) | `scripts/run_e2e_test.py` | 1 script | PASS, re-verified after every fix |

**199/199 automated tests green** (154 carried forward + 45 new this
session). Full suite + e2e re-run after every fix this session, always
green, no flakiness observed across repeated runs.

---

## 2. Features added (prior session)

Each feature is ONE function/call-site (never forked logic), wired into the
existing single-source `process_referral_firm` / `drain_queue` pipeline.

### 2.1 Feature 1 — single-firm-mode prospect brief
`src/status.py:prospect_brief_text(firm_id)`. Fires from
`src/telegram_bot.py:_run_single_firm` ONLY, after the existing summary,
ONLY when at least one draft was created — never from `_run_batch` or
`_run_resume`. Covers: firm details (oneliner, AUM value + verification
method + proxy basis when estimated, geography, capital stack, founded
year), why-this-prospect (icp_reason), who-we're-reaching (top POC name/
title/why picked + alternate count), personalization hooks with sources (or
the thin-personalization fallback note), and draft status (Gmail draft id,
with/without deck).
**Verified:** `tests/test_status.py` (5 tests, unit-level — every section of
the brief text, both the confirmed and estimated-AUM-with-proxy-basis
cases, the thin-personalization note, the no-draft-on-record case, and the
missing-firm case) + `tests/test_telegram_bot.py::test_single_firm_qualified_run_emits_full_prospect_brief_for_kingbird_like_firm`
(end-to-end through the real `_run_single_firm`) and
`test_batch_run_never_emits_a_per_firm_prospect_brief` (proves batch mode
never calls it, real 2-firm batch run, asserts zero "PROSPECT BRIEF" sends).

### 2.2 Feature 2 — CSV/Excel batch target-list upload
`src/batch_upload.py` (new module) + `src/telegram_bot.py` wiring
(`_process_batch_file`, `_download_telegram_file` via Telegram `getFile`,
`_handle_document_upload`, `_dispatch_message`, `_confirm_pending_file_batch`/
`_cancel_pending_file_batch` for the >2000-row confirm gate). Closes the
gap flagged in the original audit (§4b of the prior edition of this report
— see §7 below, now closed). Parses `.csv` (stdlib `csv`, `utf-8-sig` for
BOM tolerance) and `.xlsx` (`openpyxl`, `data_only=True`, pinned in
`requirements.txt`), tolerant of header/no-header, blank/trailing rows,
duplicates, quoted fields, and multi-column layouts (header containing
firm/company/name, case-insensitive; ambiguous → first column with a
warning). The existing text-paste batch path is unchanged and shares the
exact same `_run_batch` entry point.
**Verified:** `tests/test_batch_upload.py` (28 tests): every required case
(csv/xlsx single-column, multi-column with header, BOM+blank+trailing rows,
>2000-row confirm gate, malformed/non-tabular rejection) plus the Telegram
wiring (confirm/cancel, document-message routing, the "awaiting a file"
two-message flow) and the 2 new adversarial header-detection bugs this
session found (§4).

### 2.3 Feature 3 — batch-scale person-level dedup
No new code path — this is the EXISTING `dbc.find_prior_contact` / HARD
RULE 7 dedup mechanism (matched on LinkedIn URL first, email fallback),
proven at BATCH SCALE and mixed with genuinely-different people, per this
session's explicit "hammer it" instruction. Checked immediately after Apollo
resolves an email, committed to `contacted_pocs` immediately after each
draft — no end-of-run batching, so within-batch collisions are caught
correctly regardless of processing order. Skips are reported by name via
`collision_note` (added in `migrations/006_wc_drafts_collision_note.sql`),
never a bare count.
**Verified:** `tests/test_dedup.py` — 4 new batch-scale tests exercising the
REAL `pipeline.drain_queue(chunk_style=True)` batch drain (not sequential
direct calls): same LinkedIn URL across 2 firms in one batch (2nd skipped),
same Apollo-resolved email across 2 firms (2nd skipped), the same person
appearing 3× in one batch (only the 1st drafts, both others skipped and
both correctly cite the FIRST draft as the collision — not each other,
since skips never write `contacted_pocs`), and a 9-entry mixed batch
combining all three patterns plus 2 genuinely different people — asserts
`total Gmail drafts == unique-person count (5)`, not entry count (9) and
not distinct-firm-name count. Plus 3 more adversarial tests from the
URL-normalization bug fix (§4.3 below).

### 2.4 Feature 4 — grouped per-chunk batch messaging
`src/pipeline.py`: `_fmt_chunk_verdict`, `_fmt_chunk_enrich_draft`,
`_fmt_chunk_summary`, and `drain_queue`'s new `chunk_style: bool` parameter.
Replaces the old per-firm-flooding-adjacent "checkpoint every 10" bare-count
behavior with exactly 3 grouped Telegram sends per chunk of 25 (one verdict
message with all firms' ✅/❌/◐ lines, one enrichment+draft message, one
short chunk summary), matching `workflow_c_telegram_v2_no_cost.html`. All
three batch entry paths (text batch, Feature 2 file batch, `/referral
resume`) share the identical `pipeline.drain_queue(..., chunk_style=True)`
call site — proven directly by source inspection
(`test_all_three_batch_entry_paths_share_the_same_drain_call_site`), not
just by behavioral similarity. Single-firm mode never sets `chunk_style=True`
(`test_single_firm_never_uses_chunk_style`).
**Verified:** `tests/test_pipeline.py::test_grouped_chunk_messages_not_one_per_firm_and_not_a_bare_count`
(25-firm chunk → exactly 3 sends, all 25 verdict lines present, bounded
send count) and `test_single_firm_mode_gets_no_grouped_chunk_messages`. The
OLD checkpoint-every-10 bare-count test was replaced with these two, per
this session's explicit instruction to update anti-flooding tests to assert
grouped-verdict output.

---

## 2b. Operational features added (this session)

Two features for long bulk runs, requested and built after first checking
(grep, reported before any change) that neither already existed: no key-pool
or rotation logic anywhere in the Tavily client, and the only "cancel" in the
codebase was Feature 2's unrelated pending-file-upload discard.

### 2b.1 Feature A — Tavily API key-pool rotation
`lib_snapshot/researcher/search_client.py` gained a generic `_KeyPool` class
(current-key index, `advance()`/`reset()`/`size()`) built from
`config.TAVILY_API_KEYS` (comma-separated, backward-compatible with the
existing single `TAVILY_API_KEY`). Architecture note: this is the one place
this session touched `lib_snapshot/` rather than `src/` — confirmed with the
user first (not assumed) because "transparent to the caller" is only
achievable if the retry-on-next-key happens INSIDE `_post()`, below both
`icp_gate.py` and `personalization.py`'s call sites, not around them; the
user's own BUILD_SPEC_C.md HARD RULE 4 ("reuse, don't fork") argues the same
way. On a response signaling THIS key is exhausted, `_post()` transparently
advances and retries the same search — the firm being researched never
fails or parks because of it. A transient 5xx/timeout stays on the same key
via the pre-existing retry logic, unchanged. The circuit breaker (HARD RULE
15) now only ever trips for Tavily once the WHOLE pool is dead (a single
key's death is invisible above `search_client.py` by construction), with a
distinct, more specific message than other services get. `/referral resume`
re-reads `.env` from disk so a key added mid-pause is picked up without a
process restart.

**Which status code triggers rotation:** 432 (Tavily's documented
quota-exceeded code, per BUILD_SPEC_C.md's own record of a real prior
Workflow A incident — not the 401/402/403/429 list the request itself
suggested, which doesn't appear in this codebase's incident history) plus
401/403 (dead/revoked key). 429 deliberately does NOT rotate — it's
Tavily's ordinary rate limit, already handled by the existing same-key
retry, and rotating away from a merely-throttled-but-valid key would waste
it for nothing. **This has not been confirmed against a live Tavily
response** — flagging exactly as asked: the raw response body is logged
(stderr only, never Telegram) on every exhaustion event so the signature
can be verified in the first real-mode run, and adjusted then if 432 turns
out not to be exactly right.

**Verified:** `tests/test_tavily_pool.py` (15 tests, `requests.post`
monkeypatched — no real Tavily calls): a 3-key pool where key 1 returns 432
and key 2 completes the search (proven both against `search_client.search()`
directly AND end-to-end through `icp_gate._run_research`, the real function
`icp_gate.py`'s real-mode path calls, so the firm's research genuinely
completes rather than being mocked around); a transient 500 and a 429 both
stay on the same key (two separate tests, since 429 has its own pre-existing
handling); 401 also rotates; all-keys-exhausted raises naming the pool size;
the exact Telegram rotation message; `reload_keys()` picking up a
freshly-added key (via mocking the config-layer function it calls, not real
file I/O); backward-compat single-key parsing; an empty pool's clear error.
Plus one end-to-end test through `drain_queue` asserting the EXACT required
"All 3 Tavily keys exhausted — run paused..." wording, and one sibling test
in `test_pipeline.py` confirming non-Tavily services (LLM, Apollo, Exa) keep
the original generic "Circuit breaker tripped" message — Feature A's
Tavily-specific wording must not leak onto services with no key pool.

### 2b.2 Feature B — clean /cancel for a running batch
`/cancel` (alias `/referral stop`) sets `wc_runs.cancel_requested_at`
(`migrations/007_wc_runs_cancel_flag.sql`), a persisted DB flag so it
survives both the drain loop's between-firm checks and a bot restart.
`drain_queue` checks it at the top of every per-firm iteration — strictly
BETWEEN firms, never mid-firm: the in-flight firm's draft/`contacted_pocs`
write always completes first, then draining stops; everything already
processed stays intact, everything not yet reached stays `pending`. Run
status becomes `cancelled` (distinct from `done`/`crashed`); the final
summary (reusing `_fmt_summary`, not a parallel implementation) reports
processed counts, how many firms remain queued, and that `/referral resume`
continues from there. `/referral resume` now looks up the latest `crashed`
OR `cancelled` run (`dbc.latest_resumable_run`, replacing the old
crashed-only `dbc.latest_crashed_run`, which had exactly one caller) and
clears the flag on resume so it doesn't immediately re-cancel itself.
Authorization needed no new code: every command reaches `_dispatch` only
after `run_bot()`'s polling loop has already filtered the sender against
`AUTHORIZED_USER_IDS`, uniformly — confirmed by source inspection, not
assumed.

**Verified:** `tests/test_cancel.py` (10 tests): cancellation triggered
while a firm is genuinely mid-real-processing (not mocked) stops draining
after it, leaves the rest `pending`, marks the run cancelled, and the real
Gmail drafts from before the stop stay intact; the flag being set BEFORE
`drain_queue` even starts means `process_referral_firm` is never called at
all; the cancel summary reports processed + remaining + the resume note;
`/cancel` with no active run replies "No run in progress to cancel."; both
`/cancel` and the `/referral stop` alias set the flag and acknowledge;
authorization inheritance confirmed by source inspection; and a full
resume-after-cancel test — the 2 firms drafted before cancellation are NOT
re-drafted (same `gmail_draft_id`), the remaining 3 firms complete, and
`/referral resume` is confirmed to actually call `search_client.reload_keys()`
(Feature A's mid-run key top-up), not just happen to work when mocked out.

**One pre-existing test updated, not a regression:** Feature A changes what
the circuit breaker's Telegram message says specifically for Tavily (see
above), which broke `test_pipeline.py::test_circuit_breaker_aborts_after_3_consecutive_infra_failures`'s
old assertion that the GENERIC "Circuit breaker tripped" text would appear
for a Tavily failure. Updated that assertion to the new, deliberately
different wording, and added a sibling test
(`test_circuit_breaker_message_stays_generic_for_non_tavily_services`) so
the original generic-message coverage isn't silently lost for services
Feature A doesn't touch.

---

## 3. Confirmation: none of the original 9 fixes regressed

Every test file from the original audit (`test_ingestion.py`,
`test_firm_matcher.py`, `test_icp_gate.py`, `test_email.py`, `test_deck.py`)
passes unchanged and untouched this session (154/154 still green — see
§8's reproduce block). `test_dedup.py`'s original 6 dedup tests and
`test_pipeline.py`'s orchestration tests were re-run after every change in
this session and stayed green throughout; the two tests that changed
content (`test_ingestion.py`'s gap-canary → gap-closed-confirmation,
`test_pipeline.py`'s checkpoint-every-10 → grouped-chunk assertions) changed
because the underlying behavior they tested was DELIBERATELY changed by
this session's spec (closing the Excel gap; replacing bare-count batch
messaging with grouped messaging) — never because an assertion was
weakened.

---

## 4. New bugs found and fixed this session (Phase B re-audit)

### 4.1 Plausible single-column headers ("Target Companies", "Firm List", …) leaked into the batch as bogus firm names
**Found in:** Part 9 adversarial re-attack on Feature 2, after the original
session's own fix for the substring-header false-positive (a real firm name
like "Firm Capital Corporation" being wrongly dropped as a header) had
narrowed header detection to an EXACT match against a small singular-only
vocabulary (`{"firm", "firm name", "company", "company name", "name", ...}`).
**Failure scenario:** a very plausible real single-column upload header —
"Target Companies", "Firms", "Company Names", "Firm List", etc. (all
plural/list phrasings, none in the narrow exact-match set) — was NOT
recognized as a header, so it fell through to "row 1 is data" and the
header text itself became a fake firm name fed into the batch pipeline
(wasting a research/Apollo cycle on a nonsense string, and confusing the
operator).
**Fix:** `src/batch_upload.py:_EXACT_HEADER_LABELS` expanded to include the
common plural/list phrasings (`"firms"`, `"companies"`, `"names"`, `"firm
list"`, `"company list"`, etc.) alongside the existing singular forms. This
can never be fully exhaustive for arbitrary header wording (e.g. "Prospect
Firms") — the safer failure mode when a header truly isn't recognized is to
treat it as data (one wasted `no_connection` cycle) rather than risk
dropping a real single-word firm name that happens to match a header label,
so the vocabulary is deliberately bounded, not stemmed/fuzzy.
**Verified:** `test_common_plural_and_list_style_single_column_headers_not_leaked_as_data`
(8 header variants, all correctly recognized).

### 4.2 Multi-column "Target Companies" header (not in first column) caused the WRONG column to be silently picked
**Found in:** same adversarial pass, testing the multi-column path with the
same plural header.
**Failure scenario:** worse than §4.1 — `_pick_column`'s substring keyword
match (`_HEADER_KEYWORDS = ("firm", "company", "name")`) relies on
`"company" in cell.lower()`, but **"company" is not a substring of
"companies"** (irregular pluralization: `compan` + `y` vs `compan` + `ies`
diverge at the 7th character). So a multi-column file headed `Notes,Target
Companies` with firm names in the SECOND column fell through to "no clear
header, use the first column" — silently substituting the Notes column's
garbage strings ("hot", "big") as firm names while the real firm names were
dropped entirely. `"firm"`/`"firms"` and `"name"`/`"names"` already worked
via substring (the singular is a literal prefix of its plural); only
`"companies"` needed adding explicitly.
**Fix:** `src/batch_upload.py:_HEADER_KEYWORDS` — added `"companies"`
alongside `"firm"`/`"company"`/`"name"`.
**Verified:** `test_multi_column_plural_companies_header_picks_correct_column_not_first`
(firm names in column 2, not column 1 — proves the fix, not just the
same-column-1 case that would have passed by accident either way).

### 4.3 LinkedIn URL dedup match was a raw exact string comparison — a trailing-slash/protocol/www formatting variant of the SAME profile could bypass Feature 3's dedup
**Found in:** Part 9's dedicated adversarial attempt to make the same
person get two drafts — the task's explicit "try to break Feature 3"
instruction.
**Failure scenario:** `dbc.find_prior_contact` matched `linkedin_url` with a
plain `WHERE linkedin_url = %s`. Two connections rows for the same real
LinkedIn profile — one stored as `https://linkedin.com/in/person`, the
other as `https://linkedin.com/in/person/` (trailing slash) or
`http://www.linkedin.com/in/person` (protocol + www prefix) — are
plausible connections-export formatting inconsistencies, not synthetic
edge cases (they can arise across different export snapshots, or between
Workflow A's and Workflow C's own contacted_pocs writes). When BOTH the URL
strings differed AND the two rows' resolved emails also genuinely differed
(e.g. two work-email addresses on file, or Apollo resolving different
identifiers), dedup had NO remaining signal to catch it — two Gmail drafts
were created for the same real person. Reproduced directly:
`process_referral_firm` on two such firms produced `drafted`/`drafted`
(should be `drafted`/`skipped_duplicate`).
**Fix:** `src/dbc.py:find_prior_contact` — the URL comparison now
normalizes BOTH sides (lowercase, `http://`/`https://`/`www.` collapsed,
trailing slash stripped) via a SQL expression, rather than a raw exact
match. No migration needed: this normalizes the COMPARISON, not the stored
value, so it correctly matches existing rows regardless of how their
`linkedin_url` was originally formatted, with no backfill required.
**Verified:** `test_same_person_url_trailing_slash_variant_with_different_emails_still_deduped`
(the exact failure scenario — was producing 2 drafts before the fix, now 1),
`test_same_person_url_protocol_and_www_prefix_variant_still_deduped` (isolates
the URL match alone, no seed email on either side, no email fallback
available), and `test_genuinely_different_people_different_urls_not_falsely_merged_by_normalization`
(negative control — proves the normalization isn't over-aggressive; two
genuinely different real people at different URL paths still both draft).

---

## 5. Test-harness bugs (my own QA code, not the product) — noted for transparency

- A `test_single_firm_never_uses_chunk_style` assertion I wrote first
  checked `"chunk_style" not in source_text`, which false-failed on the
  EXPLANATORY COMMENT in `_run_single_firm` that mentions `chunk_style` by
  name (to explain why it's not passed) — not an actual call-site bug.
  Fixed by checking for the literal `"chunk_style=True"` call-site string
  instead of the bare word.
- Two early ad-hoc (non-pytest) smoke scripts I wrote while designing the
  Feature 3 batch-scale tests had setup mistakes of my own, not product
  bugs: (1) tried to give the same synthetic person two different
  `company_name_raw` values under the SAME `linkedin_url`, not realizing
  `bulk_insert_connections` upserts by URL (`ON CONFLICT DO UPDATE`), so the
  second insert silently overwrote the first company assignment; (2) a
  later script appeared to show dedup failing, but the real cause was STALE
  synthetic `connections` rows left over from the first broken script (since
  `reset_testmode()` never touches `connections`, by design). Both were
  diagnosed by direct table inspection and resolved by switching to the
  established, already-proven pattern from the original session's
  `test_dedup.py` (test-owned company-name purge fixture, before AND after
  every test) — this is also why `_TEST_COMPANIES` in `test_dedup.py` keeps
  growing as new synthetic companies are added; every one of them is
  cleaned up by the same autouse fixture.

---

## 6. Adversarial review, Part 9 (this session)

### 6a. Firm matcher — re-attack
`src/matching/firm_matcher.py` was not touched this session. Re-ran the
full 36-case suite (27 synthetic adversarial cases + 9 real-corpus cases) —
all green, zero false-positive auto-matches (explicitly asserted in
`test_pass_rate_and_zero_false_positives`). No fresh attack attempted here
beyond re-confirming the untouched code still holds, since the highest-value
adversarial effort this session belonged on the two genuinely NEW surfaces
(Feature 2, Feature 3) — see §6b/§6c.

### 6b. Deck money-leak — re-attack
`src/deck_filler.py` was not touched this session (aside from being
exercised, unchanged, by the existing 23-test suite, all green). Re-ran
`_BIG_MONEY_RE` against every format in the original catalog ($500MM, 1.2B,
USD 1.2B, "1.2 billion", $1.2bn, 1,200M) plus new adversarial variants this
session: `"$1.20B"`, `"roughly $1.2 bn"`, `"1.2 billion dollars"`, `"assets
north of $500 million"` — all caught. See §6d for the one residual miss
found and NOT fixed (a deliberate decision, not an oversight).

### 6c. Feature 2 file parsing — adversarial attempts
Beyond the 2 bugs in §4.1/§4.2, tried and could NOT break: a formula-cell
DDE/cmd-injection-style string (`=cmd|'/c calc'!A1`) in an xlsx cell —
`openpyxl` with `data_only=True` never evaluates formulas (parses workbook
XML only), so this reads as an inert cached value or blank, never executed;
a completely corrupt/non-zip `.xlsx` byte string — rejected cleanly, no
crash; a binary/control-character-laden file — rejected cleanly (the
original session's own fix); wrong extension, empty file, no extension —
all rejected with a clear message. **Known, accepted, NOT fixed:** a
maliciously crafted `.xlsx` (e.g. a zip/XML decompression bomb) could still
cause a slow parse or memory spike — this is an inherent risk of ANY
zip-based spreadsheet format, not something this codebase introduces, and
Telegram's own Bot API file-size limit (~20MB) bounds the practical worst
case. No pre-parse size/entry cap exists in `src/batch_upload.py`; adding
one would be a reasonable follow-up but is a new architectural control the
spec doesn't ask for, so it's flagged here rather than added unilaterally.

### 6d. Feature 3 dedup — adversarial attempts (this is where a real bug was found: §4.3)
Beyond the URL-normalization bug (§4.3, found and fixed), tried and could
NOT break: case-difference in the email fallback (already case-insensitive
— `lower(email) = lower(%s)` — confirmed still holds); the "same firm name
submitted 3× in one batch" pattern (correctly collapses to 1 draft, §2.3);
a batch mixing all three collision patterns with genuinely different people
in the same run (correctly drafts exactly the unique-person count). **One
separate, unrelated residual gap flagged, not fixed:** the shared
`_BIG_MONEY_RE` regex (imported verbatim from the external `lib_snapshot`
library, per the original session's own deliberate reuse decision — see
HANDOVER.md) does not catch a fully spelled-out, comma-separated dollar
figure like `"$1,200,000,000"` (as opposed to the abbreviated `"$1.2B"`
form it does catch). This is a deck-money-leak gap, not a dedup gap; it's
listed here because it surfaced during this session's adversarial pass.
**Not fixed** because `_BIG_MONEY_RE` lives in `lib_snapshot`, a library
shared across other workflows outside this repo — patching a shared
dependency is an architectural decision the spec doesn't cover, so per the
QA ground rules ("ask before any architectural choice the spec doesn't
cover") this is flagged for your decision rather than patched unilaterally.
In practice this format is a low-probability LLM output (LLMs near-
universally express AUM in abbreviated form, which the existing regex
already covers in both numeric and word form), so the residual risk is
judged low, but it is a real, reproducible miss and is reported as such.

---

## 7. Deviations from BUILD_SPEC_C.md

### 7a. Auto-fixed — the spec already covers these
All 9 bugs from the original audit, plus the 3 new bugs in §4 above, are
things the spec explicitly requires. None needed a judgment call about
intent — the spec already said what correct behavior is, the code didn't do
it, now it does.

### 7b. Previously an open gap — NOW CLOSED this session
**Batch target-list input was Telegram-message-body text only — no file
upload.** This was the one open item from the original audit (§4b of that
edition). Feature 2 (§2.2 above) closes it: `.csv`/`.xlsx` upload via
Telegram's `getFile` API, `openpyxl` (now pinned in `requirements.txt`) for
`.xlsx`, stdlib `csv` for `.csv`. The original gap-canary test
(`test_batch_target_list_ingestion_is_telegram_text_only_documented_gap`,
which asserted NO `getFile`/`openpyxl` reference existed) was replaced with
`test_batch_upload_getfile_and_openpyxl_wiring_now_exists`, which asserts
the opposite — that the gap is actually closed, not just that the old
canary was deleted.

---

## 8. What remains UNPROVABLE in TEST_MODE (Layer 2)

Unchanged from the original audit — TEST_MODE fixtures are canned data; they
prove the pipeline transforms and routes data correctly, not that a REAL
research call finds the right AUM, or that a REAL LLM-generated email/deck
reads well and states only true facts:

- **Research accuracy** — whether Tavily/Exa searches actually surface a
  firm's real AUM and the LLM extraction categorizes it correctly.
- **Email quality** — whether a real LLM's Block 2/4 actually reads as
  "indistinguishable from Harp's fixed blocks," not just mechanically clean.
- **Deck factual correctness** — whether real profile_bullets/fit_paragraphs
  are actually TRUE about the firm.
- **NEW this session — Feature 2's real Telegram `getFile` download path**
  (`src/telegram_bot.py:_download_telegram_file`) is not exercised by any
  automated test (no live bot token in TEST_MODE) — exactly like the
  existing, pre-session `get_updates()`/`send()` real-network calls. The
  PARSING logic it feeds into (`batch_upload.parse_firm_list_file`) is
  fully covered; only the actual HTTP round-trip to Telegram's file-serving
  endpoint is unverified by automated tests, by the same design as every
  other real-network call in this codebase.

`scripts/realmode_smoke.py` (Part 8) is exactly how to close the research/
email/deck gaps — unchanged from the original session, not touched this
session. Re-verified this session (without touching the network): compiles
cleanly, refuses correctly with `TEST_MODE=true` (exit 1, no API calls),
refuses correctly with 0 or 2+ firm-name arguments (exit 1, no API calls). I
have not run it in real mode.
```
TEST_MODE=false python scripts/realmode_smoke.py "Some Real Firm Name"
```

---

## 9. Spec issues found (not code bugs)

Unchanged from the original audit (not re-litigated this session, still
valid observations):
- `templates/referral_email.md`'s own word-budget arithmetic doesn't add up
  (fixed blocks total 204 words against a stated "~160" estimate,
  structurally exceeding the stated 230-word cap even before Block 2/4 are
  added). Recommend tightening Blocks 3/5/6 or relaxing the cap to ~260–270.
- §9's 5 fit-card headings in BUILD_SPEC_C.md don't match the real master
  PPTX's actual (longer, branded) text — documentation drift, not a bug,
  since the real master's strings are correctly used as the canonical
  fixed headings.

---

## 9b. Security fixes applied (this session)

A follow-up session asked for "all 6 fixes from the GStack security review
report" (C1, C2, C3, M1, M2, L1). Before touching anything, each item was
checked against the ACTUAL current codebase — three of the six referenced
files/functions/libraries that don't exist here, meaning whatever produced
that list was not run against this repository in its current state. Applying
them literally would have ranged from impossible to actively wrong. Applied
the two that are real, reported the mismatch rather than silently guessing
or silently skipping, left the one explicitly deferred alone.

**Applied:**

- **C3 — `.gitignore` created at the project root.** Was completely missing
  (this is the exact gap `qa_out/SECURITY_REPORT.md` Finding 1 already
  flagged). Covers `.env`/`.env.*` (keeping `.env.example`), `token.json`,
  `credentials.json`, `__pycache__/`, `*.pyc`, `.pytest_cache/`, `qa_out/`,
  and — per this session's own good addition — `data/connections.csv`
  (Manpreet's real ~30,000-row LinkedIn export; real PII, must never be
  committed).
- **M2 — `lib_snapshot/researcher/search_client.py`'s per-request logging
  tightened to a summary line on success.** Previously logged the full raw
  query text (which can carry a firm/POC name) before every attempt. Now
  logs, only after a successful response: a 12-char SHA-256 query hash (not
  the query text itself), result count, key index (`N/total`), and latency
  in ms — e.g. `key 1/3 query=a1b2c3d4e5f6 results=6 latency_ms=412`. Full
  detail (truncated response body) is still logged, unchanged, on the
  exhaustion/error path only, per the request's own "keep full logging only
  on error paths" instruction. Verified: all 15 `tests/test_tavily_pool.py`
  tests and the full 225-test suite still pass (no test asserted on the old
  log format, so nothing needed updating besides the source).

**NOT applied, with the specific mismatch found for each:**

- **C1 — "change `src/gmail_drafter.py` to use a fixed
  `redirect_uri=http://localhost:8080` and `run_local_server(port=8080)`."**
  `src/gmail_drafter.py` does not exist; the real file is
  `src/gmail_client.py`, and it already calls
  `flow.run_local_server(port=0)`. That's Google's own documented pattern
  for OAuth "Desktop app" clients (RFC 8252's installed-app flow) — those
  clients are deliberately registered without a fixed redirect URI, since a
  desktop app can't reliably reserve one specific port across different
  user machines. The loopback-only binding plus the OAuth library's own
  state/PKCE handling is what actually prevents redirect hijacking, not
  which port number is used; pinning to 8080 doesn't add security and only
  risks a port conflict with something else running locally. Judged not a
  real finding — not applied.
- **C2 — "in `src/dbc.py` `_upsert_firm`, use `psycopg2.sql.Identifier`/
  `sql.SQL` instead of f-strings."** No function named `_upsert_firm` exists
  anywhere in this codebase (grepped to confirm). `psycopg2` isn't a
  dependency either — this project uses `psycopg` v3 throughout
  (`psycopg[binary]==3.2.3` in `requirements.txt`); importing `psycopg2`
  alongside it would add a second, unrelated Postgres driver for no reason.
  More importantly: the actual dynamic-SQL pattern in `dbc.py` (the f-string
  `UPDATE`/`INSERT` builders in `update_wc_run`/`update_wc_firm`/
  `create_wc_firm`/`create_wc_draft`) was already directly verified safe in
  `qa_out/SECURITY_REPORT.md`'s "Checked and clean" section — the
  interpolated column names come from fixed Python keyword-argument names
  written in the calling source code (e.g.
  `update_wc_run(run_id, status="cancelled")`), never from a firm name,
  Telegram message, or uploaded file. There is no injection surface here to
  fix. Not applied.
- **M1 — "redact Telegram `chat.id` in debug logs to the last 4 digits."**
  Grepped every `chat_id`/`chat.get("id")` reference in
  `src/telegram_bot.py`: `chat_id` is used only to route an outgoing
  Telegram message to its destination (passed to Telegram's own
  `sendMessage` API) — it is never printed, logged, or written to
  stderr/journalctl anywhere in this codebase. There is no existing debug
  log containing `chat.id` to redact. Not applied — nothing to change.

**Deferred, per explicit instruction:**

- **L1 — the async model for file downloads.** Not touched this session, as
  instructed.

---

## 9c. Codecheck fixes applied + first commit + real-mode dry run (follow-up session)

A follow-up session asked to apply "three fixes from the codecheck report"
(broken test imports, unused imports via `ruff check --fix`, a
`pyproject.toml` with mypy/ruff config), then re-verify, then `git init` +
commit, then a real-mode dry run. Same discipline as §9b: checked every
claim against the actual codebase before applying anything, since the
`/health` dashboard from the immediately prior turn explicitly reported "no
ruff/flake8/pylint/mypy installed, no config found" and the user chose NOT
to install a linter at that time — meaning there was no real "codecheck
report" with specific findings for this request to be drawing from.

**Investigated first:**
- **"2 broken test imports"** — `pytest tests/ --collect-only` collects all
  225 tests with zero import errors. Not real; nothing to fix.
- **"unused imports flagged in the codecheck report"** — no such report
  existed yet. Installed `ruff` fresh and ran `ruff check . --select F401`
  to find out what's REALLY there: 6 genuine unused imports (`json` in
  `lib_snapshot/approval/telegram_queue.py` and `src/dbc.py`, `pathlib.Path`
  in `src/config.py`, `time` in `src/pipeline.py`, `typing.Callable` in
  `src/telegram_bot.py`, `GateResult` in `tests/test_icp_gate.py`). None in
  an `__init__.py` file, so the "don't remove `__init__.py` re-exports"
  caveat never actually applied. Fixed with `ruff check . --select F401
  --fix` — scoped to F401 specifically, NOT a bare `ruff check --fix`: the
  default/full ruff rule set found 228 issues on this codebase (180
  auto-fixable), which would have blown far past "unused imports only" and
  directly conflicted with the request's own "nothing else" scope.
- **"`run_batch_referral` refactor," "PII in error paths," "try/except:
  pass patterns"** (all in the request's skip-list) — grepped for each:
  `run_batch_referral` doesn't exist anywhere in this codebase; no PII-in-
  error-paths finding was ever produced; exactly one `try/except: pass`
  exists (`telegram_bot.py:57-58`, catching a Telegram send failure so one
  bad notification doesn't crash a run — unrelated to "circuit-breaker
  isolation" as the skip-list described it). None of this needed action
  since all three were already in the "don't touch" list, but confirmed
  they don't correspond to real code either, for the same transparency
  reason as §9b.

**Applied:**
- `ruff check . --select F401 --fix` — the 6 real unused imports above,
  removed.
- `pyproject.toml` created with the exact requested `[tool.mypy]` block
  (`python_version = "3.10"`, `ignore_missing_imports = true`,
  `check_untyped_defs = false`, `warn_return_any = false`) plus a
  `[tool.ruff]`/`[tool.ruff.lint]` section pinning `target-version = "py312"`,
  `line-length = 100`, and an explicit `select = ["E4", "E7", "E9", "F"]` —
  deliberately narrow (pyflakes + a few stable pycodestyle categories), not
  ruff's full default/recommended set, so a future `ruff check .` stays
  reproducible without silently picking up new rule categories or the 228
  broader findings this session found but didn't touch.
- **Note:** with the pinned config active, `ruff check .` now surfaces 2
  pre-existing findings that are NOT unused imports — `E741` (ambiguous
  variable name `l` in `lib_snapshot/enrichment/apollo.py`) and `F841`
  (unused local variable `conn` in `tests/test_dedup.py`). Left untouched:
  out of this session's explicit "unused imports only" scope. Flagged here
  rather than silently fixed or silently hidden by narrowing the rule
  selection further.

**Verified:** full 225-test suite green after the import removals; both
HARD RULE 1/2 greps re-confirmed clean (`src/` + `lib_snapshot/`).

**First commit to this project.** Running `git status` before committing
(per this session's own safety discipline) surfaced something important:
an EARLIER `/gstack` invocation this session had already run `git init` in
`referral-system` (fixing the long-standing git-root-at-`C:\` issue from
prior sessions — see the memory file) and staged **everything** via what
looks like an automated `git add -A`, including `.env` (real Tavily/LLM/
Apollo/Telegram/DB credentials), `credentials.json`, `token.json`, and
`data/connections.csv` (real PII, ~30k LinkedIn contacts) — before this
session's `.gitignore` (§9b) had a chance to prevent it. No commit had
happened yet. Had `git commit` been run as literally instructed at that
point, all four would have been permanently embedded in git history on
commit #1. Unstaged them immediately (`git rm --cached`, working tree
untouched) before doing anything else, along with `__pycache__`/`*.pyc`
binaries, `generated_decks_c/` (regenerable test output), and
`.gstack-tmp/` (gstack's own internal scratch fixtures, unrelated to this
project — has its own nested `.git`). Also caught and fixed a mistake in
this session's OWN `.gitignore` from §9b: it blanket-excluded `qa_out/`,
which would have hidden `QA_REPORT.md`/`SECURITY_REPORT.md` themselves, not
just the generated test artifacts underneath — corrected to exclude only
`qa_out/deck_visual_qa/` and `qa_out/*.pptx`. Confirmed the final staged
list (71 files) was clean, then confirmed with the user before running the
actual commit (repo-local git identity also needed setting — done via
plain repo-scoped `git config`, not `--global`, with the exact
name/email the user confirmed). Result: one clean root commit, 72 files,
14,815 insertions, re-verified afterward to contain none of the four
secret-bearing files.

**Real-mode dry run — actually run, with explicit confirmation.** Unlike
every prior session (which only verified the refusal guards), this session
ran `scripts/realmode_smoke.py "Kingbird Investment Management"` for real
(`TEST_MODE=false`, set inline for that one process only — `.env` itself
was never edited), after explicit user confirmation given the real API
spend involved. Result: real preflight passed (LLM/Tavily/Apollo all
reachable), Phase 1 matched exactly (`Kingbird Investment Management`,
confidence 1.0, real POC Lei Farrand found), Phase 2 research ran for real
against live Tavily (4 search calls, $0.0038, this session's new Feature A
per-request summary logging — query hash/result count/latency — confirmed
working against genuine live responses, not just mocks) and correctly
**parked** the firm ("AUM not found, no dollar proxies in sources") rather
than fabricating a figure — real sources cited (`kingbird.com` pages +
LinkedIn company page, never a personal profile fetch). Since the gate
never reached "qualified," Phase 3+ (Apollo/personalization/Gmail draft)
never ran — no unnecessary spend, exactly the fail-cheap design intent.
Full report: `qa_out/realmode_Kingbird_Investment_Management.md`. This is
the first real confirmation that HARD RULE 15's Tavily key-pool rotation
(Feature A) and its logging (M2 fix) work correctly against a genuine live
Tavily response, not just the synthetic 432/401/403 responses used in
`tests/test_tavily_pool.py`.

---

## 10. Reproduce this audit

```
python scripts/reset_testmode.py
python -m pytest tests/ -q            # 225 passed
python scripts/run_e2e_test.py        # ALL 5 OUTCOMES VERIFIED CORRECTLY
grep -rn "\.send(" src/ lib_snapshot/   # HARD RULE 1: only drafts().create(), no messages.send
grep -rln "linkedin.com" src/ lib_snapshot/  # HARD RULE 2: only guards/comments, no fetch
```

Last run: 225/225 passed, e2e all 5 outcomes verified, both grep checks
clean (only comments/docstrings/defensive guards match, `lib_snapshot/`
included). Re-confirmed again after the fourth session's security fixes
(§9b) — the `.gitignore` addition and the `search_client.py` logging change
don't touch drafting/fetch logic, so this was expected, but re-verified
rather than assumed.

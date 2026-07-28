# HANDOVER — Workflow C (Referral Outreach)

Built end-to-end in TEST_MODE per BUILD_SPEC_C.md §7. This document covers
what's built, what's deliberately simplified vs. the spec (and why), and what
still needs a human before this goes live.

**Update (2 sessions ago):** 4 features added (single-firm prospect brief,
CSV/Excel batch-list upload, batch-scale person-level dedup, grouped
per-chunk batch messaging) plus a full adversarial re-audit — 45 new tests
(199 total, all green), 3 new bugs found and fixed. Full detail in
`qa_out/QA_REPORT.md`.

**Update (this session):** 2 operational features for long bulk runs —
Tavily API key-pool rotation (Feature A) and a clean `/cancel` for an active
batch (Feature B). 26 new tests (225 total, all green), 1 pre-existing test
updated for a deliberate message-wording change (not a regression — see
`qa_out/QA_REPORT.md`). Both features and the `TAVILY_API_KEYS` .env format
are documented below.

## Running it

```
pip install -r requirements.txt rapidfuzz==3.10.1 pymupdf openpyxl==3.1.5
python -c "from src import dbc; dbc.apply_migrations()"   # non-destructive, safe to re-run
python scripts/locate_pptx_slots.py                        # only if templates/Pitch_Master.pptx is ever re-exported
pytest tests/                                               # 225 tests (was 27-case firm-matcher suite at first ship)
python scripts/run_e2e_test.py                              # §7 step 12 five-outcome scenario
```

`openpyxl==3.1.5` is pinned in `requirements.txt` (added this session) —
it's the `.xlsx` parser for Feature 2's batch file upload (see below).
`data_only=True` mode is used throughout, which never evaluates cell
formulas (it only reads the workbook XML and any cached value Excel last
computed) — there is no code-execution risk from a malicious formula cell.

`AUTHORIZED_USER_IDS` and `TEST_MODE` are in `.env`. `TELEGRAM_BOT_TOKEN_C` is
still the SETUP_TODO.md placeholder — the bot's command logic (`src/telegram_bot.py:_dispatch`)
is fully testable without a live token; only `run_bot()`'s polling loop needs
the real token from BotFather.

## Architecture note: lib_snapshot is a partial snapshot

`lib_snapshot/` (as provided) has the low-level, genuinely reusable clients:
`config.py`, `db.py` (connection pool + generic helpers), `models.py` (LLM
client), `researcher/{search_client,exa_client}.py`, `enrichment/apollo.py`,
`drafting/pitch_deck.py` (data-safety guards + `_short_name`).

It does NOT include Workflow A's `competitor_engine.py` (research+gate
orchestration), `style_guard.py`, `prompts/gmail_draft.md`, or
`deck_gen/build_deck.js` — those are either Workflow-A-specific business logic
that doesn't apply to Workflow C's different flow (firm-verification gate vs.
competitor discovery; XML-edit deck fill vs. pptxgenjs rebuild) or simply
weren't part of the snapshot. Per the build instruction ("§7 step 1 extraction
is SKIPPED... use lib_snapshot as the library"), Workflow C's Phase 2/4/5a/5b
orchestration (`src/icp_gate.py`, `src/personalization.py`,
`src/email_drafter.py`, `src/deck_filler.py`) was written fresh, reusing the
low-level clients above and reusing verbatim the two things HARD RULE 14 and
§9 explicitly call out for reuse: `_BIG_MONEY_RE`/`_AUM_LABEL_RE` and
`_short_name` from `lib_snapshot/drafting/pitch_deck.py`.

`lib_snapshot/db.py`'s `init_db()`/`reset_db()` are never called — `reset_db()`
DROPs every table and would be catastrophic against the shared `outreach`
database. Workflow C's schema (`migrations/002-004_*.sql`) goes through
`lib_snapshot.db.migrate()`, which only applies un-applied, additive
`CREATE TABLE IF NOT EXISTS` / `ALTER TABLE ADD COLUMN IF NOT EXISTS` files —
safe to re-run, never destructive.

**One deliberate exception to "lib_snapshot stays untouched" (this session):**
Feature A's Tavily key-pool rotation needed to live INSIDE
`lib_snapshot/researcher/search_client.py`'s own `_post`/`search` functions,
not a Workflow-C-owned wrapper — that's the only way a key rotating
mid-request is genuinely transparent to `icp_gate.py`/`personalization.py`
(both call `search_client.search()` directly; the retry has to happen below
them, not around them). This was confirmed with the user rather than assumed
(BUILD_SPEC_C.md's own HARD RULE 4, "reuse, don't fork," argues the same
way). `lib_snapshot/config.py` also gained `TAVILY_API_KEYS`/
`reload_tavily_keys()`. This `lib_snapshot/` here is a local copy bundled
into referral-system's own tree (not a live shared deployment reachable by
Workflow A) — see the `_KeyPool` class's own docstring for how it's kept
generic (no Tavily-specific code) in case Exa adopts the same pattern later.

## Deliberate simplifications (documented, not asked about mid-build)

1. **POC ranking is deterministic, not an LLM call** (`src/poc_ranking.py`).
   icp_scoring_v2_2.md's "who to target" list is already a fixed, exhaustive
   set of accepted titles (`lib_snapshot.config.MGMT_TITLE_HINTS`); ranking by
   position in that list is more reliable than an LLM round-trip and needs no
   TEST_MODE fixture.

2. **Kingbird's TEST_MODE AUM is $1.1B, not the master deck's literal $4.4B.**
   `templates/Pitch_Master.pptx` is a real prior Kingbird deck reused here only
   as the PPT *layout/locator* reference (§9's own framing: "the master is
   itself the reference"). Its actual $4.4B figure would DISQUALIFY under
   Workflow C's $200M–$2B band. The TEST_MODE fixture (`src/testmode.py`)
   deliberately uses an in-band AUM so the qualify → draft path is exercised
   end-to-end rather than reproducing a number that would never qualify.

3. **CLOSED this session, was previously an open gap:** batch-mode firm list
   used to come from the Telegram message body only (one firm per line or
   comma-separated). Feature 2 (`src/batch_upload.py` + `_process_batch_file`/
   `_download_telegram_file`/`_handle_document_upload` in `src/telegram_bot.py`)
   now also accepts an attached `.csv` or `.xlsx` file of firm names via
   Telegram's `getFile` API — send `/referral batch` with the file attached
   (or as a caption), or send `/referral batch` alone and then the file as
   the next message. Tolerant of header/no-header, blank/trailing rows, BOM,
   duplicates, quoted fields, and multi-column layouts (picks the column
   whose header contains firm/company/name; ambiguous → first column with a
   warning). Files over 2000 names require an explicit `/referral batch
   confirm` before draining. The original text-paste path is unchanged and
   both paths funnel into the identical `_run_batch` entry point — see
   `tests/test_batch_upload.py` for the full adversarial coverage (28 tests,
   including 2 new header-detection bugs found and fixed this session: a
   plural/list-style header like "Target Companies" wasn't recognized and
   either leaked into the batch as a fake firm name, or — worse, in a
   multi-column file where the firm names weren't in the first column —
   caused the wrong column to be silently picked).

4. **AUM_ESTIMATED firms get a blank stat card on slide 3, not a substituted
   stat.** §9 suggests substituting a verified non-money stat (unit count,
   founding year) when AUM is only estimated — but Workflow A's flexible
   pptxgenjs renderer supports a variable stats array, while this master's XML
   has exactly two FIXED stat-card slots (AUM, and Founded — which is already
   its own separate slot). There's no free slot to substitute into without
   duplicating the Founded card. Blanking the AUM card (never showing the
   estimated number) is the safe, HARD-RULE-14-compliant choice given that
   fixed layout.

5. **Email length: the template's own arithmetic didn't quite add up — fixed.**
   `templates/referral_email.md`'s fixed blocks (1/3/5/6/7) total 204 words by
   plain word-count, not the "~160" the template itself used to estimate. Its
   generation rules also say Block 2 should add 15-30 words and Block 4
   30-50 — which, on top of the real 204, structurally exceeded the old
   230-word cap even at the minimum (204+15+30=249). `src/email_drafter.py`
   trims whole trailing sentences (never mid-sentence — a truncated clause
   reads as broken, not concise) from Block 4 first, then Block 2, and logs a
   warning if still over budget rather than mutilating a sentence to force
   the number. Confirmed live (real-mode Kairos Real Estate Partners run,
   the first real-mode firm in this engagement to reach Phase 5a at all): a
   correctly-generated, single-sentence-per-block email shipped at 236 words
   with nothing left to trim. `EMAIL_MAX_WORDS` raised from 230 to 260 (this
   recommendation, applied) — both `src/config.py` and the template's own
   stated cap were updated together so the model's own length-guard
   instruction matches the code's actual enforcement.

6. **Single-firm mode sends a "prospect brief" after the draft.** `src/status.py:prospect_brief_text(firm_id)`, sent from
   `_run_single_firm` only (never batch/resume) right after the existing
   summary, and only when a draft was actually created. Reads like a human
   colleague explaining the prospect: firm details (AUM value + how it was
   verified + the proxy basis when estimated + geography + capital stack +
   founded year), why it qualified, who's being reached and why they were
   picked (plus how many alternates are on file), the personalization hooks
   with sources (or an explicit note when research was too thin for
   firm-specific hooks), and the draft status (Gmail draft id, with/without
   the PDF deck).

7. **Feature 3's dedup guarantee is now TWO separate guarantees, not one — FIX 38.**
   Person-level identity matching (LinkedIn URL first, email fallback, HARD RULE 7)
   is unchanged; what it DOES on a match now depends on WHEN the match happened:

   - **Within-run (same batch, same `wc_runs.run_id`) — still BLOCKS.** The same
     real person can appear under 3+ different target-list entries in one
     uploaded batch and will still draft exactly once, correctly reported by
     name (`collision_note`, `migrations/006_wc_drafts_collision_note.sql`).
     Checked via the NEW `dbc.find_within_run_contact(run_id, ...)`, scoped to
     THIS run via a join across `wc_drafts` → `wc_firms` → `connections` (no
     migration — `contacted_pocs` itself carries no `run_id` at all, which is
     exactly why this had to be a separate function rather than a parameter
     on the old one).
   - **Cross-run (a different/earlier run, possibly days ago) — now ADVISORY,
     never blocks.** A live 15-firm batch (2026-07-27) found 4 of 5 qualified
     firms silently skipped as "already contacted ... (a prior Workflow C
     run)" purely because that SAME day's own now-deleted smoke-test drafts
     had already written `contacted_pocs` rows — the entire batch's real
     output was lost to the guard firing on its own test residue.
     `contacted_pocs` only ever proved a Gmail **draft** was once created
     (HARD RULE 1: this system never sends — Harp reviews and sends by hand,
     and is comfortable checking his own sent history before sending, so he
     would have caught/deleted an unwanted repeat himself). The firm now
     proceeds through enrichment and drafting normally; the per-firm line
     reads "drafted-with-prior-contact" instead of "SKIPPED", the end-of-run
     summary gets its own "⚠️ N drafted, previously drafted before — check
     your sent mail before sending" bucket naming person/firm/prior date, and
     it counts as **drafted** in every total (not as a duplicate).
     `dbc.record_contacted()` also changed: it used to silently no-op
     (`ON CONFLICT ... DO NOTHING`) on a repeat contact for the same
     LinkedIn URL, leaving that row's `gmail_draft_id` pointing at whatever
     draft was recorded first — increasingly stale/deleted now that repeats
     actually happen. It now `DO UPDATE`s that one row (every field, so it
     stays internally coherent) to the LATEST contact event; the earlier
     date is never lost silently, since the caller reads it via
     `find_prior_contact()` and reports it in the advisory message BEFORE
     this write ever runs. A repeat matched only by email (no shared
     LinkedIn URL) has no unique constraint blocking it and gets a genuinely
     new row instead, growing the audit trail rather than overwriting it.

   This session's earlier adversarial pass also found and fixed a real gap —
   the URL match was a raw exact string comparison, so a trailing-slash or
   http-vs-https/www. formatting difference between two rows for the SAME
   real profile could bypass dedup entirely if the two rows' resolved
   emails also happened to differ (`src/dbc.py:find_prior_contact` now
   normalizes both sides of the URL comparison; `find_within_run_contact`
   does the same). **The remaining, inherent caveat:** identity matching is
   only as good as the identifiers it has. If the SAME real person appears
   twice with a genuinely different LinkedIn URL path (not just a
   formatting variant) AND Apollo/the seed email resolves to two genuinely
   different addresses for them — with no shared identifier at all — there
   is no signal left to catch it, within a run or across one. This isn't a
   bug so much as the fundamental limit of identity-matching without a
   stronger key (e.g. a person's name/company alone was deliberately never
   used for dedup, since two different real people very plausibly share a
   name).

8. **NEW this session — Tavily key-pool rotation (Feature A) so a big batch
   run keeps moving across quota exhaustion.** Set `TAVILY_API_KEYS` in
   `.env` as a comma-separated list:
   ```
   TAVILY_API_KEYS=tvly-key-one,tvly-key-two,tvly-key-three
   ```
   Backward compatible — if `TAVILY_API_KEYS` is unset, the existing
   `TAVILY_API_KEY` is used as a one-key pool; nothing changes for a
   single-key setup. `lib_snapshot/researcher/search_client.py` now holds a
   `_KeyPool` (current-key index) inside its own `_post()` — when a response
   signals THIS key is exhausted, it transparently advances to the next key
   and retries the SAME search; `icp_gate.py`/`personalization.py` never see
   an error and the firm being researched never fails or parks because of
   it. Telegram gets a notice on rotation ("Tavily key 1 exhausted, switched
   to key 2 of 3, continuing.") and a different, more specific one when the
   WHOLE pool is dead ("All N Tavily keys exhausted — run paused. Add more
   keys or wait for quota reset, then /referral resume.") — the circuit
   breaker (HARD RULE 15) now only ever trips in that second case, since a
   single key's death is invisible above `search_client.py` by design.
   `/referral resume` re-reads `.env` from disk (`search_client.reload_keys()`
   → `config.reload_tavily_keys()`), so a key the operator adds while a run
   is paused is picked up without a bot restart.

   **Which HTTP status triggers rotation, and why:** 432 (Tavily's own
   quota-exceeded code — documented in BUILD_SPEC_C.md from a real prior
   Workflow A incident, not a guess) plus 401/403 (a dead/revoked key).
   **429 deliberately does NOT rotate keys** — it's Tavily's ordinary rate
   limit, already handled by the pre-existing same-key sleep-and-retry;
   rotating away from a merely-rate-limited-but-still-valid key would waste
   a key for no reason. This has not been confirmed against a LIVE Tavily
   response — `search_client.py` logs the raw response body (stderr only,
   not Telegram) on every exhaustion event specifically so the signature can
   be verified/adjusted in the first real-mode run. Which key served which
   query is also logged to stderr only, per spec (never Telegram).

9. **NEW this session — `/cancel` (alias `/referral stop`) stops an active
   run cleanly.** Sets `wc_runs.cancel_requested_at`
   (`migrations/007_wc_runs_cancel_flag.sql`) — a persisted DB flag, so it
   survives both the drain loop's between-firm checks and a bot restart.
   `drain_queue` checks it at the top of every per-firm iteration (never
   mid-firm): the in-flight firm always finishes cleanly (its draft or
   `contacted_pocs` row is never abandoned half-written), then draining
   stops; everything already drafted stays intact, everything not yet
   reached stays `pending` in the queue. The run's status becomes
   `cancelled` (distinct from `done`/`crashed`), and the final summary
   reports what was processed, how many firms remain queued, and that
   `/referral resume` will continue from there. `/referral resume` now
   looks up the latest `crashed` OR `cancelled` run (`dbc.latest_resumable_run`,
   replacing the old crashed-only lookup) and picks up right where it
   stopped, without re-drafting anything already done (reuses the existing
   `draft_exists_for_connection` idempotency). Authorization is inherited
   for free: every command (including `/cancel`) only ever reaches
   `_dispatch` after `run_bot()`'s polling loop has already filtered the
   sender against `AUTHORIZED_USER_IDS` — no separate check was needed.

## Deployment (§10e, when this leaves TEST_MODE)

- Target: same Azure VM as Workflow A (`/opt/referral-system` or co-located
  with `/opt/outreach-system`). **The VM's `.env` is source of truth** — after
  any local key rotation, SSH in and update it manually; keys do not sync.
- `rsync` deploy excludes `.env`, `token.json`, `credentials.json`.
- **Templates load at startup.** Changing `referral_email.md`, `Pitch_Master.pptx`,
  or `pitch_slots.json` requires a service restart to take effect. If the
  master PPTX is ever re-exported, re-run `python scripts/locate_pptx_slots.py`
  BEFORE restarting — HARD RULE 16a preflight will otherwise abort the first
  run naming exactly which slot locator broke.
- Flip `TEST_MODE=false` and fill in real `TELEGRAM_BOT_TOKEN_C`,
  `AUTHORIZED_USER_IDS` (comma-separated, real operator Telegram IDs), and
  confirm `GMAIL_TOKEN_PATH`/`GMAIL_CREDENTIALS_PATH` point at a real OAuth
  token scoped to `gmail.compose` only.
- `google-auth`/`google-auth-oauthlib`/`google-api-python-client` and
  `pymupdf` (used only for this handover's visual QA, not required at
  runtime) aren't yet pinned in `requirements.lock.txt` alongside `rapidfuzz`
  — add them when moving off TEST_MODE.

## What's verified

- **225 automated tests, all green** (154 at original ship, +45 in the
  4-features-and-re-audit session, +26 this session across two new test
  files — `test_tavily_pool.py`, `test_cancel.py` — plus one pre-existing
  `test_pipeline.py` test updated for Feature A's deliberate Tavily-specific
  message wording, and one new sibling test confirming non-Tavily services
  keep the original generic circuit-breaker message). Full breakdown and
  every bug found+fixed (12 across the first two sessions) in
  `qa_out/QA_REPORT.md`.
- `tests/test_firm_matcher.py`: 36/36 cases pass (27 original + 9 real-corpus),
  zero false-positive auto-matches (explicitly asserted), unchanged and
  re-confirmed this session.
- `scripts/run_e2e_test.py`: all 5 §7-step-12 outcomes verified (Kingbird
  drafted with a correctly-filled, visually-inspected PDF deck attached;
  Blackstone disqualified; Aliya Capital Partners parked
  `aum_unverified`; Singh Capital Partners excluded as the referrer's own
  firm; an unknown firm reports `no_connection`), plus the §10c research
  quality smoke test (≥3 sourced facts, non-empty AUM+tag, non-empty
  firm_oneliner). Re-run and re-verified after every change this session.
- Grep-verified (re-confirmed this session): no code path calls Gmail
  `.send()` (only `.drafts().create()` exists, anywhere); no code path
  fetches a linkedin.com URL (search-result snippets only, with a
  defensive `assert_no_linkedin_fetch` guard and a belt-and-suspenders strip
  on any hook whose source slipped through as a linkedin.com URL).
- `scripts/realmode_smoke.py`'s refusal guards re-verified this session
  (TEST_MODE=true refuses, 0 or 2+ firm-name args refuses) without touching
  the network. Still not run in real mode — that's your call, when ready.
- **One residual, flagged-not-fixed gap:** `_BIG_MONEY_RE` (reused verbatim
  from `lib_snapshot`, per the architecture note above) doesn't catch a
  fully spelled-out comma-separated dollar figure like `"$1,200,000,000"`
  in a deck bullet/fit-paragraph (it does catch the abbreviated `"$1.2B"`
  form). Judged low-risk since LLM output near-universally uses the
  abbreviated form, but it's a real, reproducible miss in a SHARED library
  outside this repo — flagged for a decision rather than patched
  unilaterally. See `qa_out/QA_REPORT.md` §6d.

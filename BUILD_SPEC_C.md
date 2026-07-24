# BUILD_SPEC_C.md — Workflow C: Referral Outreach (Manpreet's Network)

**Version:** 1.0
**Status:** Ready to build
**Depends on:** Workflow A codebase (researcher, ICP module, Apollo enrichment, Gmail drafter, Telegram bot, Postgres)
**Scope:** Workflow C ONLY. This spec contains nothing about Workflow B (LinkedIn sender, Playwright/Patchright, connections matcher for sending). Do not build or reference any of that here.

---

## 1. What this workflow is

Harpreet's brother Manpreet has a LinkedIn network. We have a hardcoded export of his connections (CSV/Excel: LinkedIn URL, POC Name, Designation, Company Name). This workflow takes either a single firm name or the full connections file, finds which of Manpreet's connections belong to target firms, validates each firm against the ICP, enriches the POC's email via Apollo, personalizes an outreach email (provided template + researched hooks) that leverages Manpreet as the warm referral, personalizes the pitch deck PPT per firm, and lands everything as Gmail drafts with the PPT attached.

**The pipeline is fully autonomous end-to-end. No approval checkpoints mid-run.** The only human touchpoints are: (1) triggering the run from Telegram, (2) reading the final summary in Telegram, (3) reviewing parked firms listed in that summary, and (4) manually sending the drafts from Gmail. **Drafts only. The system NEVER sends an email. Ever.**

---

## 2. HARD RULES (Claude Code: never violate these)

1. **NEVER auto-send email.** Gmail API is used for draft creation only. Any code path that calls `messages.send` is a spec violation.
2. **NEVER touch LinkedIn.** No scraping, no fetching, no browser automation, no cloud agent (including TinyFish) against linkedin.com — logged-in or logged-out. The LinkedIn URLs in the connections file are used ONLY as (a) an identity key for Apollo enrichment and (b) a stored reference field. They are never fetched.
3. **No silent failures.** Every crash, API error, or skipped firm produces a Telegram message and a Postgres row explaining what happened and where.
4. **Reuse, don't fork.** ICP scoring uses the existing `prompts/icp_scoring_v2_2.md` and the existing ICP module from Workflow A. Same Postgres server. Same LLM client, Tavily/Exa clients, Apollo client, Gmail drafter, Telegram utilities from the shared `lib/`. If shared `lib/` doesn't exist yet, extract it from Workflow A as the first build step — do not copy-paste code into a second tree.
5. **Fail cheap.** Order of operations per firm is: firm match → ICP gate → Apollo email discovery → personalization research → draft + PPT. Never run expensive research on a firm that already failed the gate, and never run personalization research on a POC with no discoverable email.
6. **Three-state ICP outcome:** `qualified` / `disqualified` / `parked`. Parked = AUM or geography could not be verified with confidence. Parked firms are NEVER silently dropped and NEVER drafted — they go to the summary for human review.
7. **Cross-workflow dedup.** Before drafting, check the shared `contacted_pocs` table. If this POC (matched by LinkedIn URL, fallback email) was already drafted/contacted by Workflow A or a previous Workflow C run, skip the draft, record `skipped_duplicate`, and count it in the summary. Never double-tap a prospect.
8. **Referral claim discipline.** The email may state the connection to Manpreet as fact (they ARE connected — it's from his export). It must NEVER fabricate specifics Manpreet didn't say (no invented quotes, no "Manpreet told me about your Q3 fund"). The fixed blocks in `referral_email.md` are the source of truth for referral phrasing and X Analyst copy — the LLM must reproduce them verbatim. LLM-generated blocks (firm hook, pain point) use only verified facts from the research object with source URLs. Every firm-specific claim is auditable.
9. **Budget guards.** Per-firm hard caps: max 12 search calls (Tavily+Exa combined), max 25k LLM tokens. Log `tokens_used`, `search_calls`, `research_cost_usd` per firm, same as Workflow A. If a cap is hit, park the firm with reason `budget_cap_hit` rather than burning more.
10. **The connections file is read-only ground truth.** Never mutate it. Load it into Postgres at run start; all matching happens against the DB copy.
11. **Never target the referrer's own firm.** Any firm matching the `referrer_firms` exclusion list (Singh Capital Partners) is skipped with reason `referrer_own_firm`. You don't pitch X Analyst to Manpreet's own company.
12. **No whole-network runs.** Batch input is always a user-supplied target-firm list (any size, queue-drained in chunks of 25). There is no code path that iterates all ~22k distinct companies in the connections file as targets.
13. **If the export already has an email, use it and skip Apollo** for that POC — don't spend an Apollo credit re-finding an address you already have.
14. **Estimated numbers NEVER appear as hard stats on the deck.** If AUM (or any money figure) is estimated/derived rather than verified from a source, it must not land in a deck slot as an authoritative number. Reuse Workflow A's `pitch_deck.py` data-safety guard (`_BIG_MONEY_RE` + AUM-label strip on `AUM_ESTIMATED`) — it was adversarially reviewed and proven; do not re-implement it.
15. **Quota exhaustion is a run-abort, not a park-everything.** Distinguish infrastructure failures (LLM 429/empty response, Tavily 432, Exa quota, Apollo 403) from genuine "couldn't verify" outcomes. Circuit breaker: 3 consecutive research failures with quota/rate-limit signatures → abort the run, Telegram alert naming the dead service, mark run `crashed(quota:<service>)`. Never let a dead API silently convert 40 firms into parked rows (this exact failure produced a wall of bogus AUM_UNVERIFIED parks in Workflow A).
16. **Preflight before every run — two classes.** (a) **Hard blockers, abort immediately, no retry:** missing env key (`config.missing_keys()`), missing `referral_email.md`, master PPTX absent/corrupt, any §9 slot-locator string not found exactly once in its slide XML. Deterministic failures don't fix themselves. (b) **Transient checks, retry then decide:** LLM ping (`models.ping()`) and search/Apollo reachability fire up to 3 attempts, 10s timeout, 5s gap — one pass = healthy; 3 failures = abort naming the service. A transient blip passes attempt 2; a dead endpoint fails all 3. Gmail token validity is class (a) if the token file is missing, class (b) if the refresh call fails.

---

## 3. Inputs

| Input | Format | Provided by | When |
|---|---|---|---|
| Connections export | Standard **LinkedIn `Connections.csv` export**: `First Name, Last Name, URL, Email Address, Company, Position` (UTF-8, CRLF). ~30,000 rows. | User (hardcoded file in `data/connections.csv`) | Before first run; replaceable anytime |
| Target firm list | For batch mode: a short list of firm names the user actually wants to pitch (Telegram arg or small CSV). NOT the whole network. | User | Per batch run |
| Trigger | Telegram command (see §6) | User | Per run |
| Email template | Text file `templates/referral_email.md` with `{{slot}}` placeholders | User | Before first run |
| Pitch deck | `templates/pitch_master.pptx` | User | Before first run |
| ICP definition | Existing `prompts/icp_scoring_v2_2.md` | Already exists | — |

**Real column mapping (verified against the uploaded file):**

| File column | Maps to | Notes |
|---|---|---|
| `First Name` + `Last Name` | `poc_name` | Concatenate. Handle non-ASCII (accents, emoji, `®`) — read as UTF-8, do not re-encode. |
| `URL` | `linkedin_url` | Apollo key + reference only. Never fetched. |
| `Email Address` | `poc_email` (seed) | **~91% blank** (27,283 / 30,000 in the uploaded file). When present, use it and SKIP Apollo for that POC. When blank, Apollo enriches. |
| `Company` | `company_name_raw` → `company_name_norm` | The firm to match on. |
| `Position` | `designation` | Used for POC seniority ranking. |

Loader keeps a case-insensitive alias map for header drift (`URL`/`Profile URL`/`Profile Link`; `Company`/`Company Name`; `Position`/`Title`/`Designation`) and hard-fails with a clear Telegram error if `URL` or `Company` can't be mapped. Rows missing `Company` or `URL` are loaded, flagged `incomplete_row`, and excluded from matching (count in summary).

**Scale reality — this changes batch mode.** The file has ~30,000 connections across **~22,000 distinct companies**. `/referral all` as originally specced (group the whole file, run every firm) would attempt ~22k firms × (research + ICP + Apollo + draft) — hundreds of dollars and instant exhaustion of Tavily/Apollo/Exa quotas. **That mode is removed.** See revised Phase 1 batch mode below: batch is driven by a user-supplied target-firm list of any size, loaded into a persistent queue, free-filtered against connections first, drained in chunks with quota-pause/resume.

**Self-firm exclusion.** The referrer's own firm appears in the connections (Singh Capital Partners, ~24 rows — interns/analysts/staff). Never target the referrer's own company. Maintain a `referrer_firms` exclusion list (default: `Singh Capital Partners`, `Singh Capital`); any firm matching it is skipped with reason `referrer_own_firm`.

**Graceful degradation:** If `templates/pitch_master.pptx` is missing, the run proceeds email-only and the summary says "PPT skipped: master template not found." If `templates/referral_email.md` is missing, the run refuses to start (there is nothing to draft) with a clear Telegram error.

---

## 4. Pipeline phases

### Phase 1 — Gatekeeper: firm matching (AI-assisted fuzzy match)

**Two entry modes:**

- **Single-firm mode:** `/referral "Kingbird"` → find that firm's connections in the file.
- **Batch mode (queue-driven, no size limit):** `/referral batch` with a firm list (Telegram-listed or uploaded CSV of firm names — can be 1,000+). The list loads into a persistent Postgres queue (`wc_queue`). **Free filter first:** firm-matching against the connections table is a local lookup with zero API cost — the full list is matched immediately, firms with no connection are reported `no_connection` in bulk and never touch research. Only matched firms enter the paid pipeline, draining in **chunks of 25** with a checkpoint message per chunk. **Quota exhaustion mid-drain = pause, not failure:** the circuit breaker (HARD RULE 15) freezes the queue, Telegram names the dead service, remaining firms stay queued; `/referral resume` (and an automatic retry probe every 6h) continues from the exact next firm. The queue survives bot restarts. There is still no "process my entire network" mode — batch input is always a user-supplied target list, not the 22k-company file itself.

**Matching logic — adversarially tested against the real 23,048-company CSV (25/27 suite pass, zero false positives). Implement EXACTLY this; every rule below closes a demonstrated hole:**

**Guard 0 — degenerate input (free):** Empty/whitespace target → reject. Target whose meaningful tokens are ALL suffixes ("Capital Partners", "Investment Management", "LLC", "The Group", "Real Estate") → reject as `degenerate_name`. (Without this guard these matched "Partners Group" at 100 confidence.)

**Pass 1 — deterministic (free):**
- **1a exact:** normalized equality → match.
- **1b stripped equality:** suffix-stripped equality → match, EXCEPT when the stripped form is a single token with corpus frequency > 2 — then → LLM band with candidate pinned. (Closes: "Apex Capital Partners" ↔ "Apex Group Ltd", both strip to "apex".)
- **1c subset:** smaller token set fully inside larger AND ≥ 2 shared meaningful tokens → match. Exactly 1 shared token → match ONLY if that token is rare (corpus frequency ≤ 2) AND the target's meaningful tokens equal exactly that token (no contradicting extras); otherwise → LLM band with candidate pinned. (Closes: "Monarch Alternative Capital" auto-matching "Alternative Investments"; "Kingbird Realty Brokers" auto-matching "Kingbird Investment Management".)

Build once at load time: a meaningful-token frequency index over all connection company names, and an initials index (full initials + leading-bigram variants) for the acronym pass.

**Pass 2 — fuzzy triage on SUFFIX-STRIPPED strings (free):** `token_sort_ratio` computed on stripped forms (raw forms inflate scores via shared suffix words). Top score ≥ 85 + ≥2 shared meaningful tokens → auto-match; ≥ 85 without token sanity → LLM band. Score 55–84 → LLM band. Score < 55 → `no_connection` — unless the acronym pass hits.

**Acronym pass (free):** Target looks like an acronym (all-caps ≤6 chars, or dotted initials) → look it up in the initials index. Any hit forces the firm into the LLM band AND injects the initials-hit companies into the LLM candidate list (critical: the LLM candidate list is otherwise fuzzy-ranked, and the correct acronym expansion is usually NOT in the fuzzy top-10 — "SCP"'s fuzzy top-10 doesn't contain Singh Capital Partners; the injection is what puts it there). Documented limitation: intra-word acronyms ("KBIM" = King+Bird) can't be generated locally and will honestly miss; operator falls back to `/referral "<full name>"`.

**Pass 3 — batched LLM (LLM band only):** Batch 15–20 firms per call, each with its top-10 fuzzy candidates PLUS any pinned candidate from Pass 1 PLUS any acronym-injected candidates. LLM returns `{target, matched_to, confidence}`. Anti-merge instruction mandatory: "Two firms with similar names but different businesses are DIFFERENT firms — never merge. If ambiguous, return both with confidence < 0.8."

**LLM confidence thresholds:** ≥ 0.8 match · 0.5–0.79 parked `firm_match_ambiguous` (both candidates listed) · < 0.5 `no_connection`.

**Cost profile (measured):** 1,247-firm input → ~60 Pass-1 auto-matches, ~1,000+ free rejects, ~100–160 LLM-band firms → **5–8 batched LLM calls total.** Obvious junk ("Sunrise Dental Group") may still reach the LLM band via coincidental similarity — accepted, it costs 1/20th of a batched call.

**Edge case — one target, multiple different firms:** "Monarch" matching both "Monarch Alternative Capital" and "Monarch Realty Partners" must be flagged ambiguous with both listed, so the operator can run `/referral "<exact name>"`.

**Regression suite:** the 27-case adversarial suite from this session (exact/suffix/truncation/acronym/degenerate/anti-merge/trap cases) becomes `tests/test_firm_matcher.py`, run against a frozen 200-company sample of the real CSV. Any matcher change must keep 25/27+ with zero false-positive auto-matches.

**Output per firm:** list of matched connection rows (POCs). If single-firm mode finds zero matches: Telegram message "No connections found for <firm> in Manpreet's list" and stop — that is a normal outcome, not an error.

### Phase 2 — ICP gate (reused from Workflow A)

- ICP: **AUM $200M–$2B, US/North America geography.** Uses the existing `icp_scoring_v2_2.md` prompt and existing research-and-gate module.
- Research the FIRM (not the POC yet): AUM, geography, asset focus, recent news. This research output is stored and reused in Phase 4 — do not re-search the same facts later.
- Outcome: `qualified` → continue. `disqualified` → record reason (e.g., "AUM ~$4B, exceeds ceiling"), stop this firm. `parked` → record reason (e.g., "AUM unverifiable from public sources"), stop this firm, surface in summary.

### Phase 3 — POC selection + email discovery (Apollo)

- **POC selection:** if a firm has multiple matched connections, an LLM call ranks them by seniority/relevance from Designation (e.g., "Managing Partner" > "VP Finance" > "Analyst"). **v1 drafts to the top 1 POC per firm only.** The others are stored as `alternate_pocs` on the firm row for future use.
- **Apollo lookup keyed on LinkedIn URL** (primary), fallback to name + company domain. **Proven constraints from Workflow A: use `people/match` (single, sequential) — `people/bulk_match` returns 403 on the current plan. Always include the company `domain` hint in the payload (biggest lever on hit rate; resolve the firm's domain during Phase 2 research and store it). Retry transient 429/5xx/timeout; re-raise 403 and catch per-lead.** Verified email → continue. No email found → record `no_email_found`, park the POC-level outcome (firm stays qualified, but no draft), count in summary. Do NOT proceed to Phase 4 for that POC (fail cheap).

### Phase 4 — Personalization research (public web only)

- Tavily/Exa searches on the POC and firm for **public** hooks: interviews, podcasts, panels, company blog posts, press quotes, announced deals, published articles. LinkedIn is never fetched (HARD RULE 2); if a search result URL is linkedin.com, use only the search-snippet text, never fetch the page.
- Merge with the Phase 2 firm research already stored.
- LLM extracts a structured `personalization` object: `{hook_1, hook_2, firm_niche, recent_event, pain_point_angle}` with a source URL per item. **Every hook must carry a source.** If nothing usable is found, the draft still proceeds using firm-level facts from Phase 2 only, and the row is flagged `thin_personalization` so it's visible in `/status`.

### Phase 5 — Draft creation (email + PPT)

**Email — firm-personalized, not a name-swap template.**
Every recipient is Manpreet's connection. The email must read like Harp personally wrote it after learning about their firm — not like a mail merge. The template (`referral_email.md` v2) is an **LLM generation prompt** with fixed structural anchors and two LLM-generated blocks:

- **Subject** is **fixed**: `Intro via Manpreet Singh (Singh Capital)` — constant across firms.
- **Fixed blocks (verbatim, LLM cannot alter):** Manpreet referral line, X Analyst credentials paragraph, X Analyst value prop, CTA, signature, P.S.
- **LLM-generated Block 2 (firm hook):** 1–2 sentences referencing a specific, verifiable fact about the firm from Phase 2/4 research. Not a compliment — a fact that proves homework.
- **LLM-generated Block 4 (pain point):** 2–3 sentences tying the "switching inertia" pain to the firm's actual operations — their asset class, fund structures, or portfolio complexity.
- **Length guard ≤ 220 words total.** Fixed blocks are ~160 words; generated blocks add ~40–60.
- **Fallback (thin_personalization):** If research is too thin for credible firm-specific content, Block 2 uses the Phase 2 firm_oneliner, Block 4 reverts to Harp's original generic pain paragraph. The email still works — the deck carries the heavy personalization regardless.
- **Anti-AI-slop rules** are embedded in the template: no em dashes in generated blocks, no "I noticed/was impressed," no "leverage/optimize/streamline," no superlatives about the firm, no questions except the CTA. See `referral_email.md` for the full generation rules.
- **Source discipline:** Every firm-specific claim in Block 2 must trace to a source URL in the research object. Unverifiable claims are spec violations.

**Phase 4 research feeds BOTH the email and the deck.** The email gets a light touch (Block 2 hook + Block 4 pain angle). The heavy personalization still lives on deck slides 3 and 12 (see §9 slot map).

**PPT — master now provided (`XAnalyst_Kingbird_Investments_Pitch.pptx`), slot map is §9.**
- The master is a 14-slide deck. Slides 1, 2, 12, 14 have simple name-swap slots; **slide 3 (firm profile) and slide 12 (firm fit) are the research-heavy ones**. Full mapping in §9.
- **Editing approach:** unzip → edit `ppt/slides/slideN.xml` in place → re-zip (per pptx skill). Do NOT rebuild the deck from scratch and do NOT use `python-pptx` `text_frame.text =` (it strips the run formatting — the deck's typography matters). Replace at the `<a:t>` run level, preserving `<a:rPr>`.
- Per firm: fill slots, save `XAnalyst_<FirmName>_Pitch.pptx`, convert to PDF via `scripts/office/soffice.py --headless --convert-to pdf`, attach the **PDF** to the draft (recipients get a flat file, not an editable deck).
- **Visual QA is mandatory** after fill: the firm profile and fit slides have fixed-size cards. A firm with a longer name or a 5-line description can overflow. Render slides 1/2/3/12/14 to images and check overflow before the draft is created. If text overflows, the fill step must auto-fit (shrink font within bounds) or truncate the profile bullets to fit — never ship an overflowing slide.
- Graceful degradation (§3) still applies: if the master is ever missing at runtime, run email-only.

**Gmail draft:** existing `gmail_drafter.py`, with attachment. Record `draft_id` in Postgres. Insert the POC into `contacted_pocs`.

---

## 5. Schema additions (same Postgres server, new database `workflow_c` — or same DB with `wc_` prefix, match whatever convention Workflow A deployment used)

```sql
CREATE TABLE connections (
  id SERIAL PRIMARY KEY,
  linkedin_url TEXT UNIQUE NOT NULL,
  poc_name TEXT NOT NULL,
  designation TEXT,
  company_name_raw TEXT,          -- exactly as in the file
  company_name_norm TEXT,         -- normalized for matching
  incomplete_row BOOLEAN DEFAULT FALSE,
  loaded_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE wc_runs (
  id SERIAL PRIMARY KEY,
  mode TEXT NOT NULL,             -- 'single_firm' | 'batch'
  query_firm TEXT,                -- null in batch mode
  started_at TIMESTAMPTZ DEFAULT now(),
  finished_at TIMESTAMPTZ,
  status TEXT DEFAULT 'running',  -- running | done | crashed
  crash_detail TEXT
);

CREATE TABLE wc_queue (
  id SERIAL PRIMARY KEY,
  run_id INT REFERENCES wc_runs(id),
  firm_name_input TEXT NOT NULL,       -- as supplied by user
  queue_status TEXT DEFAULT 'pending', -- pending | no_connection | processing | done | infra_paused
  position INT,
  updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE wc_firms (
  id SERIAL PRIMARY KEY,
  run_id INT REFERENCES wc_runs(id),
  firm_name TEXT NOT NULL,
  match_method TEXT,              -- 'exact' | 'normalized' | 'llm' 
  match_confidence NUMERIC,
  icp_status TEXT,                -- qualified | disqualified | parked
  icp_reason TEXT,
  research_confidence NUMERIC,
  source_count INT,
  search_calls INT DEFAULT 0,
  tokens_used INT DEFAULT 0,
  research_cost_usd NUMERIC DEFAULT 0,
  park_reason TEXT,               -- firm_match_ambiguous | aum_unverified | budget_cap_hit | ...
  park_source TEXT,               -- 'research' (genuine verification failure) | 'infrastructure' (quota/rate-limit — auto-re-research on resume)
  created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE wc_drafts (
  id SERIAL PRIMARY KEY,
  firm_id INT REFERENCES wc_firms(id),
  connection_id INT REFERENCES connections(id),
  poc_email TEXT,
  email_status TEXT,              -- drafted | no_email_found | skipped_duplicate
  thin_personalization BOOLEAN DEFAULT FALSE,
  personalization JSONB,          -- hooks with source URLs
  gmail_draft_id TEXT,
  ppt_attached BOOLEAN DEFAULT FALSE,
  created_at TIMESTAMPTZ DEFAULT now()
);

-- SHARED across workflows (create in the shared/common database if one exists,
-- otherwise here, and point Workflow A at it too):
CREATE TABLE IF NOT EXISTS contacted_pocs (
  id SERIAL PRIMARY KEY,
  linkedin_url TEXT,
  email TEXT,
  poc_name TEXT,
  firm_name TEXT,
  source_workflow TEXT NOT NULL,  -- 'A' | 'C'
  gmail_draft_id TEXT,
  contacted_at TIMESTAMPTZ DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS contacted_pocs_url_idx ON contacted_pocs (linkedin_url) WHERE linkedin_url IS NOT NULL;
```

---

## 6. Telegram interface

**Commands:**
- `/referral "<Firm Name>"` — single-firm mode.
- `/referral batch` — target-list batch (any size; queued, chunked ×25, quota-pauses and resumes).
- `/referral reload` — re-ingest `data/connections.csv` (after Manpreet's export is refreshed).
- `/referral resume` — continue a crashed run idempotently.
- `/qualify "<Firm>" <AUM>` and `/disqualify "<Firm>"` — **operator overrides for parked firms, inherited from Workflow A.** Reuse the existing `manual_qualify`/`manual_disqualify` + tolerant `parse_aum` (handles 800M / $1.2B / "800 million" / bare numbers). Same rules: parked-only guard, the quoted firm name is an ARGUMENT and is never re-dispatched through the command parser (the /prospect-pick collision class), manual AUM goes into `research_raw.aum`, gate re-runs, in-band → qualified → the C pipeline continues from Phase 3 for that firm automatically.
- `/status c` — Workflow C dashboard: last run summary, running totals, parked firms with reasons, thin-personalization drafts, spend.
- `/status c "<Firm>"` — drill-down: match method, ICP reason, POC chosen, email status, hooks used with sources.

**Telegram mechanics (Workflow A lessons):** 0.3s sleep between consecutive sends (rate-limit protection). Per-firm no-email misses aggregate into ONE message per run, never one message per lead. Per-lookup debug output goes to service logs (stderr/journalctl), never Telegram.

**Progress messaging (autonomous — informational only, never waits for a reply):**

```
🔗 Referral run started: 47 firms found in Manpreet's connections
🔍 [10/47] Gated 10 firms: 6 qualified, 3 disqualified, 1 parked
🔍 [20/47] ...
```
Checkpoint messages every ~10 firms in batch mode; in single-firm mode, one message per phase. No confirmation prompts anywhere. The bot never blocks on user input mid-run.

**Final summary (the one message that matters):**

```
✅ Referral run done — 47 firms processed
   ✉️ 18 drafts delivered to Gmail (16 with PPT attached)
   ⛔ 14 disqualified (ICP)
   🅿️ 9 parked — REVIEW THESE:
      1. Meridian Cap — AUM unverifiable
      2. Northgate RE — firm match ambiguous (2 candidates)
      ...
   📧 4 qualified but no email found (POCs listed in /status c)
   🔁 2 skipped, already contacted (Workflow A)

Check Gmail drafts folder.
```

**Crash behavior:** if the run dies, the bot reports the firm index and error immediately, marks the run `crashed` in `wc_runs`, and already-completed drafts remain valid. `/referral resume` continues from the last completed firm (idempotent — re-running a completed firm must not create a duplicate draft; check `wc_drafts` first).

---

## 7. Build order (TODAY)

1. Extract shared `lib/` from Workflow A if not already done (LLM client, Tavily/Exa, Apollo, Gmail drafter, Telegram utils, DB helpers). Workflow A must still pass its smoke test after extraction.
2. Schema: apply §5. Create `contacted_pocs` as shared; backfill it from Workflow A's existing drafted contacts.
3. Connections loader + `/referral reload` (header alias map, normalization, incomplete-row flagging).
4. Phase 1 matcher (deterministic tiers + LLM disambiguation + park-on-ambiguity).
5. Phase 2 ICP gate wired to the existing module, three-state outcome persisted.
6. Phase 3 POC ranking + Apollo-by-URL enrichment.
7. Phase 4 personalization research with source-per-hook and the linkedin.com no-fetch guard.
8. Phase 5 email drafting from `templates/referral_email.md` v2 (LLM-generated firm-personalized email with fixed structural anchors, anti-slop rules, and thin_personalization fallback).
9. Phase 5 PPT fill from the provided master using the §9 slot map: XML-level run replacement, per-firm PDF, overflow QA, attach to draft.
10. Telegram: `/referral` commands, checkpoint messages, final summary, crash reporting, `/referral resume`.
11. `/status c` dashboard + drill-down.
12. End-to-end TEST MODE run seeded from the REAL connections file, targeting: Kingbird Investment Management (known present — Lei Farrand), one firm known to exceed the AUM ceiling (disqualify), one firm with unverifiable AUM (park), Singh Capital Partners (must be excluded as referrer's own firm), and one firm not in the file (report `no_connection`). All five outcomes must appear correctly in the final summary, and the Kingbird draft must carry a correctly-filled PDF deck.

**Note:** the master PPT is provided. The one remaining user artifact is the exact email template text as a file (`templates/referral_email.md`) — the sample image gives the copy; transcribe it verbatim with the two slots marked.

## 8. Definition of done

- `/referral "<Firm>"` (single) and `/referral batch` (target-list queue, any size, chunked ×25) run start-to-finish with zero human input, produce Gmail drafts (never sends), and end with the final summary exactly in the §6 shape.
- Parked firms always appear in the summary with reasons; nothing is ever silently dropped.
- No code path fetches linkedin.com. No code path calls Gmail send. Grep-verifiable.
- Duplicate POCs across Workflows A and C are never double-drafted.
- Crash mid-run → Telegram error with location, `/referral resume` completes the run without duplicate drafts.
- Every draft row carries hooks with source URLs and a `gmail_draft_id`. Token and search counts logged to Postgres for internal tracking (never shown in Telegram).

---

## 9. PPT slot map (`templates/pitch_slots.json`)

Derived from the provided master `XAnalyst_Kingbird_Investments_Pitch.pptx` (14 slides). "Fixed" = identical for every firm, never touched. "Slot" = replaced per firm. Edit at the `<a:t>` run level in `ppt/slides/slideN.xml`; preserve `<a:rPr>`.

| Slide | Type | What changes |
|---|---|---|
| 1 — Cover | name-swap | `Built for {{firm_short}}.` · `PREPARED FOR: {{firm_name}}`. Rest fixed. |
| 2 — Agenda | name-swap | "extend **{{firm_short}}**'s operating team" · section-01 label "{{firm_short}}" · fit line "Why this maps to **{{firm_short}}**'s operating stack". Rest fixed. |
| 3 — **Firm profile** | **research-heavy** | Title "What we know about {{firm_short}}"; `{{firm_oneliner}}`; big stat `{{aum_value}}` + `{{aum_label}}` (e.g. "Gross Asset Value"/"AUM") + `{{aum_asof}}`; `{{founded_year}}`; **`{{profile_bullets}}` = 4–6 researched facts**; bottom bar `{{capital_stack}}`. All from Phase 2 research. |
| 4–11 | fixed | X Analyst model, services, service detail, why-us, engagement, onboarding. Never touched. |
| 12 — **Firm fit** | **research-heavy** | Title "Where this fits {{firm_short}} specifically"; **5 cards, each a fixed service heading + a `{{fit_paragraph_N}}` tailored to the firm** (references its AUM, asset class, capital structure, investor base, notable roles). From Phase 2 + Phase 4 research. |
| 13 — Pilot | fixed | Never touched. |
| 14 — Close | name-swap | "what **{{firm_short}}**'s next chapter … looks like". Footer names fixed. |

**Slot data contract** — the per-firm fill object the drafter must produce before touching XML:

```json
{
  "firm_name": "Kingbird Investment Management",
  "firm_short": "Kingbird",
  "firm_oneliner": "A vertically integrated real estate investment manager specializing in workforce and market-rate multifamily housing across US secondary markets.",
  "aum_value": "$4.4B",
  "aum_label": "Gross Asset Value",
  "aum_asof": "as of Dec 31, 2025",
  "founded_year": "2008",
  "capital_stack": "LP, GP, Co-GP, Preferred Equity",
  "profile_bullets": ["…4 to 6 researched facts…"],
  "fit_paragraphs": ["…5 firm-tailored paragraphs, one per card…"]
}
```

**Rules for the fill:**
- Every value comes from Phase 2/4 research with a source. If a field can't be verified (e.g. no public AUM), the firm is **parked** (HARD RULE 6) — do NOT invent a number to fill the slot. An unfillable slot is a park signal, not a guess.
- **Estimated ≠ verified (HARD RULE 14).** If the gate qualified a firm on an *estimated* AUM (e.g. "est. $1.0B from 15K units" — Workflow A allows this), the firm may proceed, but the deck's big-stat card must NOT show the estimated money figure as a hard number. Options in priority order: (a) substitute a verified non-money stat (unit count, property count, founding year — counts and years are safe per the existing guard), (b) drop the AUM card. Run every slide-3 stat through the existing `_BIG_MONEY_RE` guard when `aum_tag = AUM_ESTIMATED`.
- `firm_short` is the LLM-derived common name (strip legal suffix): "Kingbird Investment Management" → "Kingbird". Used in all name-swap slots for natural phrasing.
- `profile_bullets`: cap at 6, each ≤ ~18 words (they sit in a fixed column — overflow QA enforces this).
- `fit_paragraphs`: exactly 5, each ≤ ~40 words to fit the cards. The 5 card headings stay fixed (Property Accounting, Fund Structures, Investor Reporting, Underwriting, Capital Markets); only the paragraph under each is tailored.
- After fill, render slides 1/2/3/12/14 and run overflow QA before creating the draft.

**One-time setup task (before first PPT run):** run the exact-string locator once to record, per slot, which `slideN.xml` and which run text to target, and write `templates/pitch_slots.json`. The Kingbird master is itself the reference — its current values ARE the Kingbird fill, so the locator can find each slot by its known Kingbird string (e.g. find `$4.4B`, `2008`, `LP, GP, Co-GP, Preferred Equity`).

---

## 10. Audit-derived additions (from Workflow A production audit — close these before build)

**10a. Floor-proxy AUM language + proxy extraction taxonomy.**
Two related gaps from Workflow A production:

*Floor-proxy language:* Research returns "over $2B" / "north of $3B" — the gate must treat the floor as the AUM and apply the ceiling/floor check deterministically. Already added to `icp_scoring_v2_2.md` Condition 2.

*Proxy extraction:* When no explicit AUM label exists, research often finds dollar figures under other names ("$800M+ invested", "portfolio valued at $X", "closed $X fund"). The ICP prompt now defines four proxy categories (a–d) in priority order, each with its own tagging rule. Key rules:
- Square footage or asset counts ALONE (no dollar figure) are NOT proxies — value_usd stays null, firm PARKS.
- Multiple conflicting proxies → use the most recent dated; if undated, use the largest.
- The proxy basis string ("invested capital proxy", "fund size proxy") must appear in icp_reason so Harpreet can audit WHY a firm qualified.

*Asymmetric gate for floor-type proxies below $200M:* Cumulative invested capital is a floor — the true AUM could be higher. So ESTIMATED below $200M from a floor-type proxy (rule b) → PARK for manual review, not DISQUALIFY. Non-floor proxies (portfolio value, fund size) below $200M → DISQUALIFY as normal. This prevents false disqualifications of firms like Seyon Group where "$800M+ invested" is the only signal.

**10b. Tavily/Exa preflight must verify result content, not just HTTP 200.**
Quota-exhausted Tavily returns 200 OK with zero results — indistinguishable from "firm not found." HARD RULE 16 class (b) preflight must use a **known-good test query** (e.g. "Blackstone Group AUM") and verify the response contains ≥1 result with non-empty content. A 200 with zero results = FAIL. This was the root cause of Workflow A's mass-park incident.

**10c. Research quality smoke test (not just unit tests).**
Unit tests never catch research-quality regressions — 128/128 green tests survived alongside a bug that parked 6 out of 8 firms. Add to §7 step 12 (end-to-end test): after the 5-outcome functional test, verify the Kingbird test firm's research output contains ≥3 sourced facts, a non-empty AUM with confidence tag, and a non-empty firm_oneliner. If any field is empty/null, the test fails.

**10d. Telegram operator allowlist.**
Authorized operators are read from `.env` as a comma-separated `AUTHORIZED_USER_IDS` list (e.g. `1904980154,HARPREET_ID`). Never hardcode a single user ID in source. All `/referral` and `/status` commands check against this list. Adding or removing an operator = edit `.env` + restart.

**10e. Deployment target and .env source of truth.**
Production target: same Azure VM (`/opt/referral-system` or co-located with outreach-system). The **VM `.env` is source of truth.** `rsync` deploy excludes `.env`, `token.json`, `credentials.json`. Document in `HANDOVER.md`: "after any local key rotation, SSH in and update the VM `.env` manually — they do not sync automatically." The local/VM key mismatch caused hours of debugging in Workflow A.

**10f. AUM research source priority order.**
Add to Phase 2 research instructions (or as a note in `icp_scoring_v2_2.md`):
- Priority 1: SEC/RIA ADV filings (most reliable).
- Priority 2: Firm website "about" or investor page.
- Priority 3: Press releases with deal sizes.
- Priority 4: Industry database mentions in free press (PitchBook, Preqin references).
- Priority 5: Proxy estimation from portfolio size (unit count × avg value, property count × avg cap).
- Sources 1–4 = `AUM_CONFIRMED`. Source 5 = `AUM_ESTIMATED`. No source = `AUM_UNVERIFIED` → PARK.

---

## 11. Production lessons from Workflow A (apply during build — these all actually happened)

1. **Quota death disguised as research failure.** A batch run during Tavily (HTTP 432) / LLM (429, free-tier model limit) exhaustion converted dozens of firms into `AUM_UNVERIFIED / conf=0.00` parks with "research failed after 4 attempts" summaries. The pipeline "worked," the output was garbage. → HARD RULE 15 circuit breaker; also tag every park with `park_source: research | infrastructure` so an infra park is never mistaken for a real verification failure, and `/referral resume` after quota restore automatically re-researches infra-parked firms.
2. **Estimated AUM leaking onto slides as hard fact.** Adversarial review found money forms ($500MM, 1.2B, USD 1.2B, "1.2 billion", $1.2bn, 1,200M) reaching deck stats for AUM-estimated firms under non-AUM labels ("Portfolio", "Holdings"). Fixed via currency-agnostic `_BIG_MONEY_RE` + label match + drop-overlong-values (never slice). That guard is proven — import it, don't rewrite it. Counts and years pass through intentionally.
3. **Empty/malformed LLM responses.** "research failed: empty LLM response" happened in production. Use the existing `complete_json_retry` (retry-then-escalate) in `models.py` for every structured LLM call in C — firm matching, POC ranking, slot-fill object.
4. **Env drift between machines.** Keys rotated on the VM but not locally (and vice versa) produced hours of confusing "quota" errors that were actually stale keys. → HARD RULE 16 preflight; also: C reads the same `/opt/outreach-system/.env` conventions, deploy is rsync (exclude `.env`, `token.json`, `credentials.json`) + `systemctl restart` — never stop→start separately (caused duplicate-poller lock conflicts twice).
5. **Templates load at startup.** `sync_templates_from_folder()` runs on bot restart. Changing `referral_email.md`, the master PPTX, or `pitch_slots.json` requires a service restart to take effect — put this in HANDOVER.md so the operator knows.
6. **Command-parser collisions.** Quoted arguments (`/qualify "Firm" 800M`) must be consumed as arguments by the handler, never fall through to command re-dispatch. All `/referral` and `/status c` argument parsing follows this.
7. **Telegram flooding.** Per-lead messages flood the chat and hit rate limits. Aggregate misses into one message, 0.3s pacing between sends, debug detail to stderr/journalctl only.
8. **Deck-gen asset preflight.** Missing icons broke deck rendering mid-run in A; an icon-preflight helper now runs before rendering. C's equivalent (HARD RULE 16): before a run, verify the master PPTX unzips cleanly and every slot-locator string in `pitch_slots.json` is found exactly once in its target slide XML. If the master was ever re-exported/edited, locator strings may have shifted — fail loud at preflight, not at firm 12.
9. **Adversarial review before production on new logic.** The data-safety guarantee only exists because new logic got an adversarial pass (which found real holes twice). For C, the two components that warrant it: the firm-matcher (false-merge risk: two different "Monarch" firms) and the deck slot-fill (invented-number risk). Run the review before first real batch.
10. **Single-source functions.** A's funnel tail is one function (`process_qualified_company`) called by both batch and operator paths — one copy of the logic. C mirrors this: one `process_referral_firm(firm_id, ...)` called by single-firm mode, batch mode, and post-`/qualify` continuation. Never fork the per-firm pipeline across entry points.

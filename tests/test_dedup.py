"""QA Part 6 — cross-workflow / cross-run dedup (HARD RULE 7), against
process_referral_firm end-to-end (not just dbc.is_contacted in isolation) so
the ACTUAL call ordering (Apollo resolves an email -> re-check -> draft ->
record) is what gets tested, not an idealized version of it.

QA FINDING (fixed): the dedup check only ran ONCE, before Apollo, using the
connections export's seed email (blank ~91% of the time — §3). Two different
LinkedIn URLs that Apollo resolves to the SAME real email were never
re-checked after resolution, so HARD RULE 7's "fallback to email" dedup
silently didn't apply to the one email actually used for the send. Fixed in
src/pipeline.py: process_referral_firm now re-checks dbc.is_contacted with
the Apollo-resolved email before proceeding to Phase 4/draft.
"""
from __future__ import annotations

import pytest

from scripts.reset_testmode import reset as reset_testmode
from src import apollo_enrich, dbc, gmail_client, pipeline

FIRM_TYPE_TITLE = "Managing Director"  # an accepted title (config.MGMT_TITLE_HINTS)

# This file inserts SYNTHETIC connections directly (reset_testmode() never
# touches `connections` — by design it's the stable ground-truth mirror, not
# something to nuke between tests). Left uncleaned, these accumulate across
# repeated runs (and any ad-hoc manual debugging using the same fixture
# names) and can silently leak a stale POC into a later test's "different
# person" assertion. Clean ONLY these test-owned company names, before and
# after every test.
_TEST_COMPANIES = [
    "Acme Capital Partners", "Firm Alpha LLC", "Firm Beta Partners",
    "Gamma Realty Partners", "Delta Holdings LP", "Zeta Family Office",
    "Eta Capital Advisors", "Theta Capital Advisors",
    # Feature 3 batch-scale (QA Part 6) synthetic companies:
    "Batch URL Dup Firm", "Batch Distinct Firm",
    "Batch Email Firm One", "Batch Email Firm Two", "Batch Email Distinct Firm",
    "Batch Triple Firm",
    "Mix Firm P", "Mix Firm Q", "Mix Firm R", "Mix Firm S", "Mix Firm T", "Mix Firm U",
    # URL-normalization adversarial re-attack (QA Part 9) synthetic companies:
    "Iota Capital Partners", "Kappa Capital Partners",
    "Lambda Capital Advisors", "Mu Capital Advisors",
    "Nu Capital Partners", "Xi Capital Partners",
]


def _purge_test_connections() -> None:
    with dbc.conn() as c:
        with c.cursor() as cur:
            cur.execute("DELETE FROM connections WHERE company_name_raw = ANY(%s)", (_TEST_COMPANIES,))
        c.commit()


@pytest.fixture(autouse=True)
def _clean_state():
    reset_testmode(verbose=False)
    _purge_test_connections()
    yield
    _purge_test_connections()


def _make_connection(url: str, name: str, company: str, email_seed: str | None = None,
                     designation: str = FIRM_TYPE_TITLE) -> dict:
    rows = [{
        "linkedin_url": url, "poc_name": name, "designation": designation,
        "company_name_raw": company, "company_name_norm": company.lower(),
        "poc_email_seed": email_seed, "incomplete_row": False,
    }]
    dbc.bulk_insert_connections(rows)
    return dbc.connection_by_url(url)


def _qualified_firm(name: str) -> dict:
    run = dbc.create_wc_run("single_firm", query_firm=name)
    return dbc.create_wc_firm(
        run["id"], name, icp_status="qualified",
        icp_reason="AUM ~$500M confirmed, qualifies", aum_tag="AUM_CONFIRMED",
        research_raw={"firm_oneliner": f"{name} is a test firm.", "sources": []},
    )


@pytest.fixture(autouse=True)
def _fake_gmail(monkeypatch):
    """A fresh FakeGmailService per test so created_drafts counts are exact."""
    from src import testmode

    fake = testmode.FakeGmailService()
    monkeypatch.setattr(gmail_client, "get_service", lambda: fake)
    monkeypatch.setattr(gmail_client, "_service_singleton", None, raising=False)
    return fake


def _patch_apollo(monkeypatch, email_by_url: dict[str, str]):
    def _fake_find_email(poc_name, company_name, linkedin_url=None, domain=None,
                         seed_email=None, progress=None):
        if seed_email:
            return seed_email, "from_export"
        email = email_by_url.get(linkedin_url)
        return (email, "apollo") if email else (None, None)

    monkeypatch.setattr(apollo_enrich, "find_email", _fake_find_email)


# ---------------------------------------------------------------------------
# 1. Same person, same firm, across two separate runs
#
# FIX 38 (live 15-firm batch, 2026-07-27): a cross-run prior-contact hit used
# to SKIP unconditionally. contacted_pocs only proves a Gmail DRAFT was once
# created (HARD RULE 1: this system never sends -- Harp reviews and sends by
# hand, and would catch/delete an unwanted repeat himself), so it is now
# ADVISORY -- the firm drafts again, flagged loudly via prior_contact_note,
# rather than being silently skipped. The within-run guarantee below (test 4
# and the batch-scale tests) is UNCHANGED -- only this cross-run half moved.
# ---------------------------------------------------------------------------
def test_same_person_same_firm_across_two_runs_drafts_again_with_advisory(monkeypatch, _fake_gmail):
    conn = _make_connection("https://linkedin.com/in/person1", "Pat Jones", "Acme Capital Partners",
                            email_seed="pat@acme.example")
    firm1 = _qualified_firm("Acme Capital Partners")

    result1 = pipeline.process_referral_firm(firm1["id"])
    assert result1["outcome"] == "drafted"
    assert "prior_contact_note" not in result1
    assert len(_fake_gmail.created_drafts) == 1

    # A second, independent run targeting the SAME firm/person (e.g. operator
    # re-runs /referral "Acme Capital Partners" the next day).
    firm2 = _qualified_firm("Acme Capital Partners")
    result2 = pipeline.process_referral_firm(firm2["id"])
    assert result2["outcome"] == "drafted", "cross-run history is advisory now, not blocking -- it must still draft"
    assert "Pat Jones" in result2["prior_contact_note"]
    assert "Acme Capital Partners" in result2["prior_contact_note"]
    assert len(_fake_gmail.created_drafts) == 2, "the second run must create its own real Gmail draft"

    draft2 = dbc.wc_draft_for_firm(firm2["id"])
    assert draft2["email_status"] == "drafted"
    assert draft2["collision_note"] == result2["prior_contact_note"]


# ---------------------------------------------------------------------------
# 2. Same email, different firm name (the email-fallback catch)
# ---------------------------------------------------------------------------
def test_same_email_under_a_different_firm_name_flagged_advisory_by_email_fallback(_fake_gmail):
    _make_connection("https://linkedin.com/in/person2a", "Sam Lee", "Firm Alpha LLC",
                     email_seed="sam@shared.example")
    firm_a = _qualified_firm("Firm Alpha LLC")
    result_a = pipeline.process_referral_firm(firm_a["id"])
    assert result_a["outcome"] == "drafted"

    # A DIFFERENT LinkedIn URL, DIFFERENT company, but the SAME seed email
    # (e.g. someone dual-listed, or a data entry quirk) — must be caught by
    # the email fallback even though the linkedin_url doesn't match. Cross-run
    # -> advisory, not a skip.
    _make_connection("https://linkedin.com/in/person2b", "Sam Lee", "Firm Beta Partners",
                     email_seed="sam@shared.example")
    firm_b = _qualified_firm("Firm Beta Partners")
    result_b = pipeline.process_referral_firm(firm_b["id"])
    assert result_b["outcome"] == "drafted"
    assert "Sam Lee" in result_b["prior_contact_note"]
    assert len(_fake_gmail.created_drafts) == 2


# ---------------------------------------------------------------------------
# 3. Same LinkedIn URL, null email on both sides
# ---------------------------------------------------------------------------
def test_same_linkedin_url_with_no_email_still_flagged_advisory(monkeypatch, _fake_gmail):
    _make_connection("https://linkedin.com/in/person3", "Jamie Fox", "Gamma Realty Partners",
                     email_seed=None)
    _patch_apollo(monkeypatch, {"https://linkedin.com/in/person3": "jamie@gamma.example"})

    firm1 = _qualified_firm("Gamma Realty Partners")
    result1 = pipeline.process_referral_firm(firm1["id"])
    assert result1["outcome"] == "drafted"

    # Re-process the same URL again (no seed email either run) — the
    # cross-run prior-contact match must still be found via linkedin_url
    # alone, without needing an email at all, but it is now advisory.
    firm2 = _qualified_firm("Gamma Realty Partners")
    result2 = pipeline.process_referral_firm(firm2["id"])
    assert result2["outcome"] == "drafted"
    assert "Jamie Fox" in result2["prior_contact_note"]
    assert len(_fake_gmail.created_drafts) == 2


# ---------------------------------------------------------------------------
# 4. Same person under two firms WITHIN one batch (write-after-each-draft
# ordering — the second firm's check must see the first firm's COMMITTED
# contacted_pocs row, with no batching/lazy-commit race)
#
# FIX 38: this is the guarantee that must NOT change -- deliberately given a
# SHARED run_id (via _qualified_firm_in_run, defined below) so it's a genuine
# within-run case under the new split, rather than the cross-run scenario
# test 1 above now covers (this test previously used two separate single_firm
# runs, which -- pre-FIX-38 -- didn't matter, since run_id was irrelevant to
# the old, unsplit dedup check).
# ---------------------------------------------------------------------------
def test_same_person_two_firms_within_one_batch_ordering(_fake_gmail):
    """Simulates a batch where the operator's target list resolves two
    entries to the SAME real company (e.g. 'Delta Holdings LP' typed twice,
    or two near-duplicate names the matcher both resolves to the same
    canonical firm) — two wc_firms rows under ONE run_id, same underlying
    connection, same top POC. The second row's dedup check must see the
    first row's contacted_pocs write from earlier in the SAME sequential
    batch drain, and must still BLOCK (not just advise)."""
    _make_connection("https://linkedin.com/in/person4", "Morgan Blake", "Delta Holdings LP",
                     email_seed="morgan@delta.example")
    run = dbc.create_wc_run("batch")
    firm_first = _qualified_firm_in_run(run["id"], "Delta Holdings LP")
    firm_second = _qualified_firm_in_run(run["id"], "Delta Holdings LP")

    result_first = pipeline.process_referral_firm(firm_first["id"])
    assert result_first["outcome"] == "drafted"
    result_second = pipeline.process_referral_firm(firm_second["id"])
    assert result_second["outcome"] == "skipped_duplicate", (
        "the second firm row's dedup check must see the first row's contacted_pocs "
        "write from earlier in the SAME batch drain, not just across separate runs -- "
        "and, being WITHIN the same run, must still BLOCK"
    )
    assert len(_fake_gmail.created_drafts) == 1


# ---------------------------------------------------------------------------
# 4b. Direct unit coverage of dbc.find_within_run_contact() and
# dbc.record_contacted()'s new write behavior -- FIX 38 Part B/DIAGNOSE-3's
# split, exercised at the dbc layer directly rather than only through
# process_referral_firm end-to-end (matches this module's own stated
# preference for end-to-end coverage where possible, but these two functions
# are new enough, and their run-scoping/conflict behavior specific enough,
# to warrant a direct check too).
# ---------------------------------------------------------------------------
def test_find_within_run_contact_is_scoped_to_run_id_not_global(_fake_gmail):
    """find_within_run_contact must find a match for the SAME run_id (still
    blocking) and MUST NOT find one for a DIFFERENT run_id, even though
    find_prior_contact (the cross-run, now-advisory check) finds it either
    way -- that contrast IS the split FIX 38 introduces."""
    _make_connection("https://linkedin.com/in/within-run-direct", "Direct Test Person", "Firm Alpha LLC",
                     email_seed="direct@example.com")
    run_a = dbc.create_wc_run("batch")
    firm_a = _qualified_firm_in_run(run_a["id"], "Firm Alpha LLC")
    result_a = pipeline.process_referral_firm(firm_a["id"])
    assert result_a["outcome"] == "drafted"

    within = dbc.find_within_run_contact(run_a["id"], "https://linkedin.com/in/within-run-direct", None)
    assert within is not None
    assert within["poc_name"] == "Direct Test Person"

    run_b = dbc.create_wc_run("batch")
    within_other_run = dbc.find_within_run_contact(run_b["id"], "https://linkedin.com/in/within-run-direct", None)
    assert within_other_run is None, "a different run_id must never be caught by the WITHIN-run check"
    assert dbc.find_prior_contact("https://linkedin.com/in/within-run-direct", None) is not None, (
        "the CROSS-run check must still find it -- that's exactly what makes it advisory, not absent"
    )


def test_record_contacted_updates_existing_row_on_a_repeat_linkedin_url_contact():
    """FIX 38 Part B: the unique index on linkedin_url makes a second literal
    row impossible for a repeat contact of the SAME person -- must UPDATE in
    place (contacted_at + gmail_draft_id) rather than silently no-op (the
    OLD 'DO NOTHING', which left the row pointing at a possibly-deleted
    first draft forever once cross-run contact stopped blocking)."""
    dbc.record_contacted("https://linkedin.com/in/repeat-url-person", "first@example.com",
                         "Repeat Person", "First Firm", "TESTMODE-DRAFT-A")
    first = dbc.find_prior_contact("https://linkedin.com/in/repeat-url-person", None)
    assert first["gmail_draft_id"] == "TESTMODE-DRAFT-A"
    assert first["firm_name"] == "First Firm"

    dbc.record_contacted("https://linkedin.com/in/repeat-url-person", "first@example.com",
                         "Repeat Person", "Second Firm", "TESTMODE-DRAFT-B")
    with dbc.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM contacted_pocs WHERE linkedin_url = %s",
                    ("https://linkedin.com/in/repeat-url-person",))
        count = cur.fetchone()["n"]
    assert count == 1, "the unique linkedin_url index allows only one row -- must UPDATE, never insert a second"

    second = dbc.find_prior_contact("https://linkedin.com/in/repeat-url-person", None)
    assert second["gmail_draft_id"] == "TESTMODE-DRAFT-B", "must reflect the LATEST contact, not the stale first one"
    assert second["firm_name"] == "Second Firm"


def test_record_contacted_inserts_a_second_row_for_an_email_only_repeat_contact():
    """No unique constraint on email alone -- a repeat contact matched only
    by email (no linkedin_url) gets its OWN new row, growing the audit
    trail rather than colliding with anything, per Part B's "insert a
    second row whenever the schema allows it" branch."""
    dbc.record_contacted(None, "shared-repeat@example.com", "Person One", "Firm One", "TESTMODE-DRAFT-C")
    dbc.record_contacted(None, "shared-repeat@example.com", "Person One", "Firm Two", "TESTMODE-DRAFT-D")
    with dbc.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM contacted_pocs WHERE email = %s",
                    ("shared-repeat@example.com",))
        count = cur.fetchone()["n"]
    assert count == 2, "email carries no unique constraint -- each draft event must get its own row"


# ---------------------------------------------------------------------------
# 5. Different people at the same firm must NOT be deduped
# ---------------------------------------------------------------------------
def test_different_people_same_firm_not_deduped(_fake_gmail):
    _make_connection("https://linkedin.com/in/personX", "X Person", "Zeta Family Office",
                     email_seed="x.person@zeta.example")
    firm1 = _qualified_firm("Zeta Family Office")
    result1 = pipeline.process_referral_firm(firm1["id"])
    assert result1["outcome"] == "drafted"

    # A DIFFERENT real person, different URL, different email — must never
    # be flagged as a duplicate just because a prior run drafted to
    # someone else at the same firm.
    with dbc.conn() as c:
        with c.cursor() as cur:
            cur.execute("DELETE FROM connections WHERE company_name_raw = %s", ("Zeta Family Office",))
        c.commit()
    _make_connection("https://linkedin.com/in/personY", "Y Person", "Zeta Family Office",
                     email_seed="y.person@zeta.example")
    firm2 = _qualified_firm("Zeta Family Office")
    result2 = pipeline.process_referral_firm(firm2["id"])
    assert result2["outcome"] == "drafted", "a genuinely different person must never be caught by firm-level dedup"
    assert len(_fake_gmail.created_drafts) == 2


# ---------------------------------------------------------------------------
# 6. Apollo resolving the SAME email for two different LinkedIn URLs
# (the exact bug the ORIGINAL fix addressed in src/pipeline.py -- now
# exercised as a cross-run advisory hit, per FIX 38)
# ---------------------------------------------------------------------------
def test_apollo_same_email_two_different_urls_flagged_advisory_by_email_fallback(monkeypatch, _fake_gmail):
    _make_connection("https://linkedin.com/in/eta1", "Eta One", "Eta Capital Advisors", email_seed=None)
    _make_connection("https://linkedin.com/in/eta2", "Eta Two", "Theta Capital Advisors", email_seed=None)
    _patch_apollo(monkeypatch, {
        "https://linkedin.com/in/eta1": "shared.inbox@example.com",
        "https://linkedin.com/in/eta2": "shared.inbox@example.com",  # Apollo resolves BOTH to the same email
    })

    firm_eta = _qualified_firm("Eta Capital Advisors")
    result_eta = pipeline.process_referral_firm(firm_eta["id"])
    assert result_eta["outcome"] == "drafted"

    firm_theta = _qualified_firm("Theta Capital Advisors")
    result_theta = pipeline.process_referral_firm(firm_theta["id"])
    assert result_theta["outcome"] == "drafted", (
        "two different LinkedIn URLs resolving to the same real email via Apollo, across "
        "separate runs, is now an advisory cross-run hit caught by the post-Apollo "
        "email-fallback check -- it must still draft"
    )
    assert "Eta One" in result_theta["prior_contact_note"]
    assert len(_fake_gmail.created_drafts) == 2

    draft_theta = dbc.wc_draft_for_firm(firm_theta["id"])
    assert draft_theta["email_status"] == "drafted"
    assert draft_theta["poc_email"] == "shared.inbox@example.com"
    assert draft_theta["collision_note"] == result_theta["prior_contact_note"]


# ---------------------------------------------------------------------------
# Feature 3 — batch-scale person-level dedup (QA re-audit Part 6: "the
# highest-risk new surface — hammer it"). Same underlying guarantee as tests
# 1-6 above, but now exercised through the ACTUAL batch drain
# (pipeline.drain_queue with chunk_style=True, mirroring a real /referral
# batch run processing many firms in one call) instead of sequential direct
# process_referral_firm calls — so the promise that dedup is checked and
# committed per-firm INSIDE one drain (no end-of-run batching) holds at
# scale, mixed with unrelated firms in the same chunk.
# ---------------------------------------------------------------------------
def _qualified_firm_in_run(run_id: int, name: str) -> dict:
    return dbc.create_wc_firm(
        run_id, name, icp_status="qualified",
        icp_reason="AUM ~$500M confirmed, qualifies", aum_tag="AUM_CONFIRMED",
        research_raw={"firm_oneliner": f"{name} is a test firm.", "sources": []},
    )


def _drain_batch(names: list[str]) -> tuple[dict, dict, list[dict]]:
    """Enqueues `names` (may contain repeats -- e.g. the same canonical firm
    name 3x, representing Phase 1 matching resolving several near-duplicate
    operator entries to one real company) as ONE batch run and drains it for
    real via pipeline.drain_queue(chunk_style=True). Returns (run, totals,
    wc_firms rows re-fetched post-drain, in submission order) so a test can
    inspect each entry's individual outcome, not just an aggregate."""
    run = dbc.create_wc_run("batch")
    dbc.enqueue_firms(run["id"], names)
    qrows = sorted(dbc.next_pending_chunk(run["id"], len(names) + 1), key=lambda q: q["position"])
    firm_ids = []
    for qrow, name in zip(qrows, names):
        firm = _qualified_firm_in_run(run["id"], name)
        dbc.set_queue_firm(qrow["id"], firm["id"])
        firm_ids.append(firm["id"])
    totals = pipeline.drain_queue(run["id"], chunk_size=len(names) + 1, chunk_style=True,
                                  notify=lambda *_a, **_k: None)
    firms = [dbc.get_wc_firm(fid) for fid in firm_ids]
    return run, totals, firms


def _draft_for(firm: dict) -> dict:
    return dbc.wc_draft_for_firm(firm["id"])


def test_batch_scale_same_linkedin_url_across_firms_second_skipped(_fake_gmail):
    """Two batch entries resolve (via Phase 1 matching, simulated here by
    reusing the same canonical firm_name string) to the SAME LinkedIn URL/
    person, mixed with a genuinely different firm in the same batch."""
    _make_connection("https://linkedin.com/in/batch-url-1", "Avery Chen", "Batch URL Dup Firm",
                     email_seed="avery@batchurl.example")
    _make_connection("https://linkedin.com/in/batch-url-2", "Robin Diaz", "Batch Distinct Firm",
                     email_seed="robin@batchdistinct.example")

    names = ["Batch URL Dup Firm", "Batch URL Dup Firm", "Batch Distinct Firm"]
    run, totals, firms = _drain_batch(names)

    assert totals["processed"] == 3
    d0, d1, d2 = (_draft_for(f) for f in firms)
    assert d0["email_status"] == "drafted"
    assert d1["email_status"] == "skipped_duplicate"
    assert "Avery Chen" in (d1["collision_note"] or ""), "skip must be reported by the colliding person's name"
    assert d2["email_status"] == "drafted"
    assert len(_fake_gmail.created_drafts) == 2, "2 unique people (Avery, Robin) => 2 drafts, not 3"


def test_batch_scale_same_apollo_email_across_firms_second_skipped(monkeypatch, _fake_gmail):
    """Two DIFFERENT firms/URLs/people-on-paper where Apollo resolves both to
    the SAME real inbox -- the fallback-to-email leg of HARD RULE 7, at
    batch scale, mixed with a genuinely distinct third firm."""
    _make_connection("https://linkedin.com/in/batch-email-1", "Casey Kim", "Batch Email Firm One",
                     email_seed=None)
    _make_connection("https://linkedin.com/in/batch-email-2", "Drew Patel", "Batch Email Firm Two",
                     email_seed=None)
    _make_connection("https://linkedin.com/in/batch-email-3", "Emery Nash", "Batch Email Distinct Firm",
                     email_seed=None)
    _patch_apollo(monkeypatch, {
        "https://linkedin.com/in/batch-email-1": "shared.batch@example.com",
        "https://linkedin.com/in/batch-email-2": "shared.batch@example.com",
        "https://linkedin.com/in/batch-email-3": "emery@distinct.example",
    })

    names = ["Batch Email Firm One", "Batch Email Firm Two", "Batch Email Distinct Firm"]
    run, totals, firms = _drain_batch(names)

    d0, d1, d2 = (_draft_for(f) for f in firms)
    assert d0["email_status"] == "drafted"
    assert d1["email_status"] == "skipped_duplicate"
    assert "Casey Kim" in (d1["collision_note"] or "")
    assert d2["email_status"] == "drafted"
    assert len(_fake_gmail.created_drafts) == 2


def test_batch_scale_same_person_three_times_only_first_drafts(_fake_gmail):
    """The same person appears 3+ times in one uploaded list (e.g. the
    operator's file has near-duplicate rows Phase 1 all resolves to one
    firm) -- must draft exactly once, not once per occurrence."""
    _make_connection("https://linkedin.com/in/batch-triple", "Harper Lin", "Batch Triple Firm",
                     email_seed="harper@batchtriple.example")
    names = ["Batch Triple Firm", "Batch Triple Firm", "Batch Triple Firm"]
    run, totals, firms = _drain_batch(names)

    d0, d1, d2 = (_draft_for(f) for f in firms)
    assert d0["email_status"] == "drafted"
    assert d1["email_status"] == "skipped_duplicate"
    assert d2["email_status"] == "skipped_duplicate"
    assert "Harper Lin" in (d1["collision_note"] or "")
    assert "Harper Lin" in (d2["collision_note"] or ""), (
        "the THIRD occurrence must also be caught -- it must match against the "
        "first draft's committed contacted_pocs row, not just the immediately-prior one"
    )
    assert len(_fake_gmail.created_drafts) == 1, "the same person 3x in one list must draft exactly once"


def test_batch_scale_mixed_duplicates_and_genuinely_different_people(monkeypatch, _fake_gmail):
    """The full hammer case in one batch: a duplicate-URL pair, a duplicate-
    Apollo-email pair, a triple-listed person, AND genuinely different
    people -- every duplicate collapses, every distinct person still gets
    exactly one draft, and total Gmail drafts == unique-person count."""
    # Person P: same canonical firm name twice (URL/person dup)
    _make_connection("https://linkedin.com/in/mix-p", "Parker Reyes", "Mix Firm P",
                     email_seed="parker@mixp.example")
    # Person Q/R: two different firms/URLs, Apollo resolves both to one email
    _make_connection("https://linkedin.com/in/mix-q", "Quinn Ortiz", "Mix Firm Q", email_seed=None)
    _make_connection("https://linkedin.com/in/mix-r", "Rowan Ali", "Mix Firm R", email_seed=None)
    # Person S: the same canonical firm name three times (triple-listed)
    _make_connection("https://linkedin.com/in/mix-s", "Sasha Novak", "Mix Firm S",
                     email_seed="sasha@mixs.example")
    # Person T and U: genuinely different people -- must both draft
    _make_connection("https://linkedin.com/in/mix-t", "Taylor Wren", "Mix Firm T",
                     email_seed="taylor@mixt.example")
    _make_connection("https://linkedin.com/in/mix-u", "Uma Singh", "Mix Firm U",
                     email_seed="uma@mixu.example")
    _patch_apollo(monkeypatch, {
        "https://linkedin.com/in/mix-q": "shared.mix@example.com",
        "https://linkedin.com/in/mix-r": "shared.mix@example.com",
    })

    names = [
        "Mix Firm P", "Mix Firm P",                  # duplicate URL/person
        "Mix Firm Q", "Mix Firm R",                  # duplicate via Apollo email
        "Mix Firm S", "Mix Firm S", "Mix Firm S",     # same person 3x
        "Mix Firm T", "Mix Firm U",                   # genuinely different, both draft
    ]
    run, totals, firms = _drain_batch(names)
    assert totals["processed"] == len(names) == 9

    statuses = [_draft_for(f)["email_status"] for f in firms]
    expected = ["drafted", "skipped_duplicate",
               "drafted", "skipped_duplicate",
               "drafted", "skipped_duplicate", "skipped_duplicate",
               "drafted", "drafted"]
    assert statuses == expected, "outcome per batch entry, in submission order"

    unique_people = {"Parker Reyes", "Quinn Ortiz", "Sasha Novak", "Taylor Wren", "Uma Singh"}
    assert len(_fake_gmail.created_drafts) == len(unique_people) == 5, (
        "total Gmail drafts must equal the count of unique PEOPLE, not the count of batch entries "
        "and not the count of distinct firm names"
    )

    # Every skip must be reported BY NAME (Feature 3's own requirement),
    # never as a bare count.
    collisions = [(_draft_for(f) or {}).get("collision_note") or "" for f in firms]
    assert "Parker Reyes" in collisions[1]
    assert "Quinn Ortiz" in collisions[3]
    assert "Sasha Novak" in collisions[5]
    assert "Sasha Novak" in collisions[6], (
        "the third occurrence collides with the FIRST draft's committed contact, "
        "not the second (skipped) occurrence, since skips never write contacted_pocs"
    )


# ---------------------------------------------------------------------------
# QA re-audit Part 9 adversarial re-attack finding: dbc.find_prior_contact's
# LinkedIn URL match was a raw exact string comparison, so the SAME real
# profile stored with a trailing-slash / http-vs-https / www.-prefix
# formatting difference across two connections rows silently bypassed dedup
# whenever the two rows' resolved emails also happened to differ -- two
# Gmail drafts for one real person. Fixed by normalizing BOTH sides of the
# comparison in the query (no migration -- it normalizes the comparison, not
# the stored value).
# ---------------------------------------------------------------------------
def test_same_person_url_trailing_slash_variant_with_different_emails_flagged_advisory(monkeypatch, _fake_gmail):
    """The exact scenario that broke dedup before the ORIGINAL fix: no seed
    email, Apollo resolves each URL variant to a genuinely DIFFERENT email,
    and the only remaining signal is the LinkedIn URL itself -- which
    differs only by a trailing slash. FIX 38: cross-run, so this is now
    advisory (drafts again) rather than a skip -- the normalization guarantee
    itself (that the match is found AT ALL) is what this test still proves."""
    _make_connection("https://linkedin.com/in/url-variant-person", "Drew Okafor", "Iota Capital Partners",
                     email_seed=None)
    _make_connection("https://linkedin.com/in/url-variant-person/", "Drew Okafor", "Kappa Capital Partners",
                     email_seed=None)

    def _fake_find_email(poc_name, company_name, linkedin_url=None, domain=None, seed_email=None, progress=None):
        mapping = {
            "https://linkedin.com/in/url-variant-person": "drew.work@example.com",
            "https://linkedin.com/in/url-variant-person/": "drew.personal@example.com",
        }
        return mapping.get(linkedin_url), "apollo"

    monkeypatch.setattr(apollo_enrich, "find_email", _fake_find_email)

    firm_iota = _qualified_firm("Iota Capital Partners")
    result_iota = pipeline.process_referral_firm(firm_iota["id"])
    assert result_iota["outcome"] == "drafted"

    firm_kappa = _qualified_firm("Kappa Capital Partners")
    result_kappa = pipeline.process_referral_firm(firm_kappa["id"])
    assert result_kappa["outcome"] == "drafted", (
        "a trailing-slash URL formatting difference must not defeat the prior-contact match when "
        "the resolved emails ALSO differ -- the URL match itself must be normalized"
    )
    assert "Drew Okafor" in result_kappa["prior_contact_note"]
    assert len(_fake_gmail.created_drafts) == 2


def test_same_person_url_protocol_and_www_prefix_variant_still_flagged_advisory(monkeypatch, _fake_gmail):
    """Same guarantee, different formatting variance: http vs https and a
    www. prefix. FIX 38: cross-run history is advisory now, so BOTH entries
    draft -- Apollo is patched for the second (no seed email) so the run
    reaches drafting and the advisory note becomes observable end-to-end,
    proving the URL match itself (not the email fallback) is what found it."""
    _make_connection("http://www.linkedin.com/in/protocol-variant-person", "Sasha Kim", "Lambda Capital Advisors",
                     email_seed="sasha.k@example.com")
    _make_connection("https://linkedin.com/in/protocol-variant-person", "Sasha Kim", "Mu Capital Advisors",
                     email_seed=None)
    _patch_apollo(monkeypatch, {"https://linkedin.com/in/protocol-variant-person": "sasha.k@example.com"})

    firm_lambda = _qualified_firm("Lambda Capital Advisors")
    result_lambda = pipeline.process_referral_firm(firm_lambda["id"])
    assert result_lambda["outcome"] == "drafted"

    firm_mu = _qualified_firm("Mu Capital Advisors")
    result_mu = pipeline.process_referral_firm(firm_mu["id"])
    assert result_mu["outcome"] == "drafted"
    assert "Sasha Kim" in result_mu["prior_contact_note"]
    assert len(_fake_gmail.created_drafts) == 2


def test_genuinely_different_people_different_urls_not_falsely_merged_by_normalization(_fake_gmail):
    """Negative control for the normalization fix: two REAL different people
    at different URL paths (not a formatting variant of each other) must
    still both draft -- proves the normalization isn't over-aggressive."""
    _make_connection("https://linkedin.com/in/genuinely-person-nu", "Nu Person", "Nu Capital Partners",
                     email_seed="nu@example.com")
    _make_connection("https://linkedin.com/in/genuinely-person-xi", "Xi Person", "Xi Capital Partners",
                     email_seed="xi@example.com")

    firm_nu = _qualified_firm("Nu Capital Partners")
    result_nu = pipeline.process_referral_firm(firm_nu["id"])
    assert result_nu["outcome"] == "drafted"

    firm_xi = _qualified_firm("Xi Capital Partners")
    result_xi = pipeline.process_referral_firm(firm_xi["id"])
    assert result_xi["outcome"] == "drafted"
    assert len(_fake_gmail.created_drafts) == 2

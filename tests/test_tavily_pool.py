"""Feature A — Tavily API key-pool rotation. TEST_MODE, no real Tavily calls:
`requests.post` is monkeypatched to return synthetic responses simulating
the exhaustion signal (HTTP 432, per BUILD_SPEC_C.md's own documented
Workflow A incident history — see the module docstring in
lib_snapshot/researcher/search_client.py for why 432 was chosen over the
432/401/402/403/429 candidates and why 429 is deliberately excluded).

Every test resets the pool's in-memory index before and after, since it's a
module-level singleton (_pool) shared across the whole process — a leftover
non-zero index from one test would silently change which key the NEXT test
starts on.
"""
from __future__ import annotations

import pytest

from lib_snapshot import config as base_config
from lib_snapshot.researcher import search_client
from scripts.reset_testmode import reset as reset_testmode
from src import dbc, icp_gate, pipeline


class _FakeResponse:
    def __init__(self, status_code: int, json_data: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._json = json_data if json_data is not None else {}
        self.text = text or ""

    def json(self):
        return self._json

    def raise_for_status(self):
        if 500 <= self.status_code < 600:
            import requests
            raise requests.HTTPError(f"{self.status_code} error")


@pytest.fixture(autouse=True)
def _clean_state():
    reset_testmode(verbose=False)
    yield


@pytest.fixture(autouse=True)
def _reset_pool():
    search_client._pool.reset(["key-1", "key-2", "key-3"])
    search_client.set_notify(None)
    yield
    search_client._pool.reset(base_config.TAVILY_API_KEYS)
    search_client.set_notify(None)


def _ok_response(n: int = 1) -> _FakeResponse:
    return _FakeResponse(200, {"results": [{"title": "T", "url": "https://x.example", "content": "c"}] * n})


# ---------------------------------------------------------------------------
# Core rotation behavior
# ---------------------------------------------------------------------------
def test_key_exhaustion_advances_pool_and_completes_the_search(monkeypatch):
    """Key 1 always returns the quota signal (432); key 2 works. The search
    must still return real results -- never raise -- proving Feature A's
    core promise: 'transparent to the caller'."""
    calls = []

    def _fake_post(url, json=None, timeout=None):
        calls.append(json["api_key"])
        if json["api_key"] == "key-1":
            return _FakeResponse(432, text="Usage limit exceeded")
        return _ok_response()

    monkeypatch.setattr(search_client.requests, "post", _fake_post)
    result = search_client.search("Some Firm AUM")

    assert result, "search must return real results, not an empty/failed result"
    assert calls[0] == "key-1"
    assert calls[-1] == "key-2"
    assert search_client._pool.position() == (2, 3), "pool must have advanced to key 2 and stayed there"


def test_same_firm_research_completes_on_key_2_not_parked(monkeypatch):
    """End-to-end transparency proof through icp_gate._run_research (the
    REAL function icp_gate.py's real-mode path calls) -- not just
    search_client in isolation. Key 1 exhausts on the FIRST search angle;
    the firm's research must still complete normally, never raising
    InfrastructureError (which would otherwise park/crash the firm)."""
    def _fake_post(url, json=None, timeout=None):
        if json["api_key"] == "key-1":
            return _FakeResponse(432, text="Usage limit exceeded")
        return _ok_response()

    monkeypatch.setattr(search_client.requests, "post", _fake_post)

    results, calls = icp_gate._run_research("Some Firm With A Long Name", progress=lambda *_: None)
    assert calls >= 1
    assert results, "research must complete with real results, not be silently empty"
    # If this had raised InfrastructureError, the calling test would already
    # have failed with an exception -- reaching here IS the proof.


def test_transient_5xx_does_not_advance_key_uses_normal_retry(monkeypatch):
    """A 500 must NOT rotate keys -- it stays on the SAME key and uses the
    existing same-key sleep-and-retry, exactly as before Feature A."""
    monkeypatch.setattr(search_client.time, "sleep", lambda *_: None)  # don't actually sleep in tests
    calls = []

    def _fake_post(url, json=None, timeout=None):
        calls.append(json["api_key"])
        if len(calls) == 1:
            return _FakeResponse(500, text="internal error")
        return _ok_response()

    monkeypatch.setattr(search_client.requests, "post", _fake_post)
    result = search_client.search("Some Firm AUM")

    assert result
    assert calls == ["key-1", "key-1"], "a transient 5xx must retry on the SAME key, never rotate"
    assert search_client._pool.position() == (1, 3), "pool must NOT have advanced for a transient error"


def test_429_does_not_advance_key_either(monkeypatch):
    """429 is Tavily's ordinary rate limit (already handled by the existing
    same-key sleep-retry) -- NOT the quota-exhaustion signature (432). It
    must not rotate keys."""
    monkeypatch.setattr(search_client.time, "sleep", lambda *_: None)
    calls = []

    def _fake_post(url, json=None, timeout=None):
        calls.append(json["api_key"])
        if len(calls) == 1:
            return _FakeResponse(429)
        return _ok_response()

    monkeypatch.setattr(search_client.requests, "post", _fake_post)
    search_client.search("Some Firm AUM")
    assert calls == ["key-1", "key-1"]
    assert search_client._pool.position() == (1, 3)


def test_all_keys_exhausted_raises_naming_pool_size(monkeypatch):
    def _fake_post(url, json=None, timeout=None):
        return _FakeResponse(432, text="Usage limit exceeded")

    monkeypatch.setattr(search_client.requests, "post", _fake_post)
    with pytest.raises(search_client.SearchAPIError) as exc_info:
        search_client.search("Some Firm AUM")
    assert "3" in exc_info.value.message
    assert "exhausted" in exc_info.value.message.lower()


def test_auth_dead_key_401_also_rotates(monkeypatch):
    """A revoked/invalid key (401) is treated the same as quota exhaustion
    -- the key doesn't work either way, so rotate away from it."""
    def _fake_post(url, json=None, timeout=None):
        if json["api_key"] == "key-1":
            return _FakeResponse(401, text="Unauthorized")
        return _ok_response()

    monkeypatch.setattr(search_client.requests, "post", _fake_post)
    result = search_client.search("Some Firm AUM")
    assert result
    assert search_client._pool.position() == (2, 3)


# ---------------------------------------------------------------------------
# Telegram notice on rotation (mid-run only -- never on total exhaustion,
# which is handled by drain_queue's own circuit-breaker message instead)
# ---------------------------------------------------------------------------
def test_key_rotation_sends_telegram_notice_via_registered_callback(monkeypatch):
    notified = []
    search_client.set_notify(notified.append)

    def _fake_post(url, json=None, timeout=None):
        if json["api_key"] == "key-1":
            return _FakeResponse(432, text="Usage limit exceeded")
        return _ok_response()

    monkeypatch.setattr(search_client.requests, "post", _fake_post)
    search_client.search("Some Firm AUM")

    assert len(notified) == 1
    assert notified[0] == "Tavily key 1 exhausted, switched to key 2 of 3, continuing."


def test_no_notify_registered_never_crashes(monkeypatch):
    """set_notify(None) (the default/reset state) must not raise when a
    rotation happens with nothing listening."""
    search_client.set_notify(None)

    def _fake_post(url, json=None, timeout=None):
        if json["api_key"] == "key-1":
            return _FakeResponse(432, text="Usage limit exceeded")
        return _ok_response()

    monkeypatch.setattr(search_client.requests, "post", _fake_post)
    result = search_client.search("Some Firm AUM")
    assert result


# ---------------------------------------------------------------------------
# Config: backward compatibility + mid-run reload
# ---------------------------------------------------------------------------
def test_backward_compat_single_tavily_api_key_becomes_one_key_pool():
    keys = base_config._parse_tavily_keys(None, "tvly-solo-key")
    assert keys == ["tvly-solo-key"]


def test_comma_separated_pool_parses_and_trims_whitespace():
    keys = base_config._parse_tavily_keys("key1, key2 ,key3", "ignored-single-key")
    assert keys == ["key1", "key2", "key3"], "TAVILY_API_KEYS, when set, takes priority over the singular key"


def test_no_keys_at_all_is_an_empty_pool():
    assert base_config._parse_tavily_keys(None, None) == []


def test_reload_keys_picks_up_a_freshly_added_key(monkeypatch):
    """Mid-run key top-up: /referral resume calls search_client.reload_keys(),
    which must call config.reload_tavily_keys() and reset the pool from
    WHATEVER it returns -- proven here by mocking that one function (the
    exact contract between the two modules), not by touching real env vars
    or lib_snapshot.config's module-global state (which would leak into
    other tests sharing this process)."""
    monkeypatch.setattr(base_config, "reload_tavily_keys", lambda: ["key-1", "key-2", "key-3", "key-4"])
    search_client.reload_keys()
    assert search_client.pool_size() == 4


def test_config_reload_tavily_keys_re_reads_env(monkeypatch):
    """Unit test for config.reload_tavily_keys()'s own mechanism, isolated
    from search_client: load_dotenv is mocked (never touches the real .env
    file) to simulate the operator adding a key, using monkeypatch.setenv so
    the change is automatically reverted -- never a permanent os.environ
    mutation."""
    monkeypatch.setattr(base_config, "TAVILY_API_KEY", base_config.TAVILY_API_KEY)
    monkeypatch.setattr(base_config, "TAVILY_API_KEYS", base_config.TAVILY_API_KEYS)

    def _fake_load_dotenv(*_a, **_kw):
        monkeypatch.setenv("TAVILY_API_KEYS", "solo-key-1,solo-key-2")

    monkeypatch.setattr(base_config, "load_dotenv", _fake_load_dotenv)
    result = base_config.reload_tavily_keys()
    assert result == ["solo-key-1", "solo-key-2"]
    assert base_config.TAVILY_API_KEYS == ["solo-key-1", "solo-key-2"]


def test_empty_pool_raises_a_clear_configuration_error():
    search_client._pool.reset([])
    with pytest.raises(search_client.SearchAPIError, match="No Tavily API key configured"):
        search_client.search("Some Firm AUM")


# ---------------------------------------------------------------------------
# End-to-end through drain_queue: the exact required wording when the WHOLE
# pool (not just one key) is dead, and the circuit breaker trips.
# ---------------------------------------------------------------------------
def test_all_keys_exhausted_trips_breaker_with_exact_required_message(monkeypatch):
    run = dbc.create_wc_run("batch")
    names = [f"Pool Dead Firm {i}" for i in range(5)]
    firms = {name: dbc.create_wc_firm(run["id"], name) for name in names}
    dbc.enqueue_firms(run["id"], names)
    for q in dbc.next_pending_chunk(run["id"], len(names) + 1):
        dbc.set_queue_firm(q["id"], firms[q["firm_name_input"]]["id"])

    def _boom(firm_name, progress=None):
        raise icp_gate.InfrastructureError("Tavily", "All 3 Tavily key(s) exhausted.")

    monkeypatch.setattr(icp_gate, "research_and_gate", _boom)
    notify_calls: list[str] = []
    totals = pipeline.drain_queue(run["id"], notify=notify_calls.append)

    assert totals["aborted"] is True
    assert totals["dead_service"] == "Tavily"
    assert ("⚠️ All 3 Tavily keys exhausted — run paused. Add more keys or wait for quota "
           "reset, then /referral resume.") in notify_calls

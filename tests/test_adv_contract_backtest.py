"""Adversarial cross-service CONTRACT tests — BACKTEST side (vortex_backtest).

Surface: contract consistency (error codes / auth / clock / as_of) of the
session backtest HTTP API (design/18 §C). We pin the documented status codes
and the stable machine-readable error bodies, and we attack the corners:

  400  /sessions bad level (unsupported_level); advance missing to+end_date
       (missing_to_or_end_date).
  401  VORTEX_BACKTEST_TOKEN set + write call without/with WRONG token.
       Correct Bearer AND X-Auth-Token -> ok.
  403  no token + non-loopback host (VORTEX_BACKTEST_HOST public) -> write_disabled.
       no token + loopback -> allowed.
  404  advance/close/get unknown session_id; create_session unknown account.
  409  advance on CLOSED session (session_closed); advance to < sim_time
       (non_monotonic_clock).
  422  GET /symbols/{bad} crosswalk; out-of-range query params (limit/offset).
  503  /sessions/{id}/data without VORTEX_DATA_URL (gateway_not_configured).
  502  gateway error mapping.
  idempotency: same request_id -> duplicate no-op (no double advance/fill).

Run:
  /Users/zyukyunman/Documents/vortex/vortex_backtest/.venv/bin/python -m pytest \
      tests/test_adv_contract_backtest.py -q

Tests that EXPOSE a real defect are marked xfail(strict=False) so the file stays
green; the failing assertion + traceback is the bug repro.
"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from vortex_backtest.app import create_app


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Default: no auth token, loopback host, no data gateway configured."""
    monkeypatch.delenv("VORTEX_BACKTEST_TOKEN", raising=False)
    monkeypatch.delenv("VORTEX_BACKTEST_HOST", raising=False)
    monkeypatch.delenv("VORTEX_DATA_URL", raising=False)


@pytest.fixture
def client(tmp_path):
    # raise_server_exceptions=False so a 500 surfaces as a real HTTP 500 (like in
    # production) instead of re-raising into the test — required to ASSERT on the
    # contract-violating 500s that bugs FUT/N8-side produce.
    app = create_app(state_dir=tmp_path / "state", run_worker=False)
    return TestClient(app, raise_server_exceptions=False)


def _make_account(client, account_id="acc1", cash=1_000_000.0):
    r = client.post("/accounts", json={"account_id": account_id, "initial_cash": cash})
    assert r.status_code == 201, r.text
    return account_id


def _open_session(client, account_id, **kw):
    body = {"account_id": account_id, "level": "1min",
            "start_date": "2026-05-12", "end_date": "2026-05-20",
            "universe": ["000001.SZ"]}
    body.update(kw)
    r = client.post("/sessions", json=body)
    assert r.status_code == 201, r.text
    return r.json()["session_id"]


# ===========================================================================
# 400 — validation
# ===========================================================================

def test_create_session_bad_level_400(client):
    acc = _make_account(client)
    r = client.post("/sessions", json={"account_id": acc, "level": "tick",
                                       "start_date": "2026-05-12"})
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "unsupported_level"


def test_advance_first_step_no_to_no_end_date_400(client):
    """First advance (sim_time None) with no 'to' and no session end_date
    -> _resolve_to returns '' -> missing_to_or_end_date."""
    acc = _make_account(client)
    sid = _open_session(client, acc, start_date="2026-05-12", end_date=None)
    r = client.post(f"/sessions/{sid}/advance", json={})
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "missing_to_or_end_date"


# ===========================================================================
# 401 / 403 — auth contract (app.py require_write_auth)
# ===========================================================================

def test_write_without_token_loopback_allowed(client):
    """No token configured + loopback host -> writes allowed."""
    r = client.post("/accounts", json={"account_id": "ok1", "initial_cash": 1000.0})
    assert r.status_code == 201


def test_write_no_token_nonloopback_host_403(monkeypatch, client):
    monkeypatch.setenv("VORTEX_BACKTEST_HOST", "10.0.0.5")
    r = client.post("/accounts", json={"account_id": "x", "initial_cash": 1000.0})
    assert r.status_code == 403
    assert r.json()["detail"]["error"] == "write_disabled"


def test_write_token_required_missing_401(monkeypatch, client):
    monkeypatch.setenv("VORTEX_BACKTEST_TOKEN", "sekret")
    r = client.post("/accounts", json={"account_id": "x", "initial_cash": 1000.0})
    assert r.status_code == 401
    assert r.json()["detail"]["error"] == "unauthorized"


def test_write_token_wrong_401(monkeypatch, client):
    monkeypatch.setenv("VORTEX_BACKTEST_TOKEN", "sekret")
    r = client.post("/accounts", json={"account_id": "x", "initial_cash": 1000.0},
                    headers={"X-Auth-Token": "nope"})
    assert r.status_code == 401


def test_write_token_correct_bearer_ok(monkeypatch, client):
    monkeypatch.setenv("VORTEX_BACKTEST_TOKEN", "sekret")
    r = client.post("/accounts", json={"account_id": "b1", "initial_cash": 1000.0},
                    headers={"Authorization": "Bearer sekret"})
    assert r.status_code == 201


def test_write_token_correct_xauth_ok(monkeypatch, client):
    monkeypatch.setenv("VORTEX_BACKTEST_TOKEN", "sekret")
    r = client.post("/accounts", json={"account_id": "b2", "initial_cash": 1000.0},
                    headers={"X-Auth-Token": "sekret"})
    assert r.status_code == 201


def test_advance_requires_token_when_configured_401(monkeypatch, client):
    """The write-auth must cover /advance too, not just /accounts and /sessions."""
    # Build account+session WITHOUT a token first (loopback), then turn auth on.
    acc = _make_account(client)
    sid = _open_session(client, acc)
    monkeypatch.setenv("VORTEX_BACKTEST_TOKEN", "sekret")
    r = client.post(f"/sessions/{sid}/advance", json={"to": "2026-05-12"})
    assert r.status_code == 401


def test_session_data_requires_token_when_configured_401(monkeypatch, client):
    acc = _make_account(client)
    sid = _open_session(client, acc)
    monkeypatch.setenv("VORTEX_BACKTEST_TOKEN", "sekret")
    r = client.post(f"/sessions/{sid}/data", json={"datasets": []})
    assert r.status_code == 401


# ===========================================================================
# 404 — unknown resources
# ===========================================================================

def test_create_session_unknown_account_404(client):
    r = client.post("/sessions", json={"account_id": "ghost", "level": "1min",
                                       "start_date": "2026-05-12"})
    assert r.status_code == 404
    assert r.json()["detail"] == "account not found"


def test_advance_unknown_session_404(client):
    r = client.post("/sessions/does-not-exist/advance", json={"to": "2026-05-12"})
    assert r.status_code == 404
    assert r.json()["detail"] == "session not found"


def test_close_unknown_session_404(client):
    r = client.post("/sessions/does-not-exist/close")
    assert r.status_code == 404


def test_get_unknown_session_404(client):
    r = client.get("/sessions/does-not-exist")
    assert r.status_code == 404


def test_unknown_account_bad_level_is_404_not_400(client):
    """Order-of-checks contract: account existence is validated BEFORE level,
    so an unknown account with a bad level yields 404 (account not found),
    never 400 unsupported_level (don't leak validation before authz/existence)."""
    r = client.post("/sessions", json={"account_id": "ghost", "level": "tick",
                                       "start_date": "2026-05-12"})
    assert r.status_code == 404


# ===========================================================================
# 409 — lifecycle / monotonic clock
# ===========================================================================

def test_advance_on_closed_session_409(client):
    acc = _make_account(client)
    sid = _open_session(client, acc)
    r = client.post(f"/sessions/{sid}/close")
    assert r.status_code == 200, r.text
    r2 = client.post(f"/sessions/{sid}/advance", json={"to": "2026-05-12"})
    assert r2.status_code == 409
    assert r2.json()["detail"]["error"] == "session_closed"


@pytest.mark.parametrize("bad_to", ["garbage-not-a-date", "2026-13-99", "2026-05-32"])
def test_advance_malformed_to_is_400_not_500(client, bad_to):
    """A client-supplied unparseable 'to' must be a clean 400, not a 500 stacktrace.
    advance_session parses pd.Timestamp(to_ts) at app.py:268 OUTSIDE the try/except
    that only wraps session_advance, so a DateParseError (ValueError subclass)
    escapes uncaught -> 500 Internal Server Error."""
    acc = _make_account(client)
    sid = _open_session(client, acc)
    r = client.post(f"/sessions/{sid}/advance", json={"to": bad_to})
    assert r.status_code == 400, f"got {r.status_code}: {r.text[:120]}"
    assert isinstance(r.json().get("detail"), dict)


def test_advance_non_monotonic_clock_409(client):
    """Establish a clock at 2026-05-15, then advance to an earlier date ->
    session_advance raises ValueError -> 409 non_monotonic_clock."""
    acc = _make_account(client)
    sid = _open_session(client, acc)
    # First advance (no data gateway -> empty bars, but sim_time still set to 'to').
    r1 = client.post(f"/sessions/{sid}/advance", json={"to": "2026-05-15"})
    assert r1.status_code == 200, r1.text
    r2 = client.post(f"/sessions/{sid}/advance", json={"to": "2026-05-13"})
    assert r2.status_code == 409
    assert r2.json()["detail"]["error"] == "non_monotonic_clock"


# ===========================================================================
# 422 — crosswalk + query-param bounds
# ===========================================================================

def test_symbols_bad_crosswalk_422(client):
    r = client.get("/symbols/NOTASYMBOL")
    assert r.status_code == 422


def test_symbols_good_crosswalk_200(client):
    r = client.get("/symbols/000001.SZ")
    assert r.status_code == 200
    assert r.json()["board"] == "main"


def test_trades_limit_out_of_range_422(client):
    acc = _make_account(client)
    sid = _open_session(client, acc)
    r = client.get(f"/sessions/{sid}/trades", params={"limit": 0})
    assert r.status_code == 422
    r2 = client.get(f"/sessions/{sid}/trades", params={"limit": 99999})
    assert r2.status_code == 422


def test_trades_negative_offset_422(client):
    acc = _make_account(client)
    sid = _open_session(client, acc)
    r = client.get(f"/sessions/{sid}/trades", params={"offset": -1})
    assert r.status_code == 422


def test_minutes_limit_out_of_range_422(client):
    acc = _make_account(client)
    sid = _open_session(client, acc)
    r = client.get(f"/sessions/{sid}/minutes", params={"limit": 0})
    assert r.status_code == 422
    r2 = client.get(f"/sessions/{sid}/minutes", params={"limit": 10**9})
    assert r2.status_code == 422


# ===========================================================================
# 503 / 502 — data gateway wiring
# ===========================================================================

def test_session_data_without_gateway_503(client):
    acc = _make_account(client)
    sid = _open_session(client, acc)
    r = client.post(f"/sessions/{sid}/data", json={"datasets": []})
    assert r.status_code == 503
    assert r.json()["detail"]["error"] == "gateway_not_configured"


def test_session_data_gateway_error_502_wrapped(monkeypatch, client):
    """Regression: a GatewayDataError (non-2xx status from gateway) -> 502.
    This is the path that IS handled today (app.py:400 catches GatewayDataError)."""
    acc = _make_account(client)
    sid = _open_session(client, acc)
    monkeypatch.setenv("VORTEX_DATA_URL", "http://127.0.0.1:1")

    import vortex_backtest.gateway_adapter as ga

    def _boom(self, as_of, datasets):
        raise ga.GatewayDataError("gateway 500: boom")

    monkeypatch.setattr(ga.GatewayDataAdapter, "_query", _boom)
    r = client.post(f"/sessions/{sid}/data", json={"datasets": [
        {"dataset": "stk_mins", "symbols": "universe"}]})
    assert r.status_code == 502
    assert r.json()["detail"]["error"] == "gateway_error"


def test_session_data_gateway_unreachable_is_502(monkeypatch, client):
    """The MOST COMMON gateway failure — the data service is down / unreachable —
    must map to 502 gateway_error, not a 500. GatewayDataAdapter._query does not
    wrap httpx.ConnectError into GatewayDataError, so it escapes the 502 handler."""
    acc = _make_account(client)
    sid = _open_session(client, acc)
    # Port 9 (discard) typically refuses immediately -> httpx.ConnectError.
    monkeypatch.setenv("VORTEX_DATA_URL", "http://127.0.0.1:9")
    r = client.post(f"/sessions/{sid}/data", json={"datasets": [
        {"dataset": "stk_mins", "symbols": "universe"}]})
    assert r.status_code == 502, f"got {r.status_code}: {r.text[:120]}"
    assert r.json()["detail"]["error"] == "gateway_error"


def test_advance_gateway_unreachable_degrades_gracefully(monkeypatch, client):
    """app.py:35 promises graceful degradation (empty bars) when data is missing.
    But a transient gateway outage (ConnectError) is NOT caught by the
    (ValueError, GatewayDataError) guard, so advance 500s on a network blip
    instead of completing the step with empty bars."""
    acc = _make_account(client)
    sid = _open_session(client, acc)
    monkeypatch.setenv("VORTEX_DATA_URL", "http://127.0.0.1:9")
    r = client.post(f"/sessions/{sid}/advance", json={"to": "2026-05-12"})
    # Should NOT be a 500; either a clean 200 (degraded, empty bars) or a mapped 502.
    assert r.status_code != 500, f"got 500: {r.text[:120]}"


# ===========================================================================
# idempotency / monotonic clock as contract
# ===========================================================================

def test_advance_idempotent_request_id_duplicate(client):
    """Same request_id replays as a no-op (duplicate: true), not a double advance."""
    acc = _make_account(client)
    sid = _open_session(client, acc)
    r1 = client.post(f"/sessions/{sid}/advance",
                     json={"to": "2026-05-15", "request_id": "rq-1"})
    assert r1.status_code == 200, r1.text
    sim1 = client.get(f"/sessions/{sid}").json()
    r2 = client.post(f"/sessions/{sid}/advance",
                     json={"to": "2026-05-18", "request_id": "rq-1"})
    assert r2.status_code == 200
    assert r2.json().get("duplicate") is True
    # The duplicate must NOT advance the clock past where rq-1 left it.
    sim2 = client.get(f"/sessions/{sid}").json()
    assert sim1.get("sim_time") == sim2.get("sim_time")


# ===========================================================================
# error-body SHAPE stability (machine-readable)
# ===========================================================================

def test_error_bodies_are_structured(monkeypatch, client):
    """A representative sweep: every documented client error carries a stable
    detail.error code (dict), not a bare string, so callers can branch on it."""
    acc = _make_account(client)
    # 400 unsupported_level
    b400 = client.post("/sessions", json={"account_id": acc, "level": "x",
                                          "start_date": "2026-05-12"}).json()
    assert isinstance(b400["detail"], dict) and "error" in b400["detail"]
    # 503 gateway_not_configured
    sid = _open_session(client, acc)
    b503 = client.post(f"/sessions/{sid}/data", json={"datasets": []}).json()
    assert isinstance(b503["detail"], dict) and "error" in b503["detail"]

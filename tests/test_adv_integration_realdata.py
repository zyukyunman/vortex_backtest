"""Adversarial cross-service integration tests over REAL workspace data.

These tests stand up BOTH services end-to-end:

- vortex_data dashboard (stdlib http.server) launched as a SUBPROCESS using the
  *data* venv against the REAL workspace (``$VORTEX_WORKSPACE``, default
  ``~/vortex/workspace``) on port 8791, with a known token via
  ``VORTEX_DATA_DASHBOARD_TOKEN``.
- vortex_backtest driven in-process via ``TestClient(create_app(state_dir=tmp))``
  with ``VORTEX_DATA_URL`` pointing at the live data service.

The goal is to BREAK the design's PIT / accounting / graceful-degradation
invariants on inputs that only show up on real data:

- A position held across the 2026-06-08 ex-day (adj_factor jump) on 000630.SZ:
  RAW price drops 6.49 -> 5.93, and the real dividend dataset has NO ``ex_date``
  column so the N8 corporate-action credit is DORMANT. Assert NAV stays
  continuous (no insufficient_cash blowup) and nothing crashes, while
  DOCUMENTING the unbacked NAV gap that RAW-without-N8 leaves.
- A suspended symbol (000004.SZ on 2026-05-06) cannot be traded that day.
- ``symbols:"universe"`` expands and the REAL gateway only returns rows
  ``<= sim_time`` (a same-day daily close is NOT visible intraday).
- 503 when ``VORTEX_DATA_URL`` is unset on ``/sessions/{id}/data``.

A pure in-process fallback (no socket) is also included so the PIT-on-real-data
and load_dividends contract assertions run even if the live server is flaky.

Run (backtest venv):
  .venv/bin/python -m pytest tests/test_adv_integration_realdata.py -q
"""
from __future__ import annotations

import os
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

# fastapi is only present in the *backtest* venv. The in-process tests at the
# bottom of this file run under the *data* venv (which has duckdb but no
# fastapi), so defer the import: only the live-server tests touch TestClient.
try:
    from fastapi.testclient import TestClient
except ImportError:  # data venv: live-server tests will skip via the fixture
    TestClient = None  # type: ignore[assignment, misc]

# ---------------------------------------------------------------------------
# Constants tied to the REAL workspace on disk (read-only).
#
# These point at a sibling vortex_data checkout + its real workspace. Both are
# read from the environment (with home-relative defaults) so no one machine's
# absolute path is baked in; the fixtures below pytest.skip when either is
# absent. Override with VORTEX_DATA_REPO / VORTEX_WORKSPACE.
# ---------------------------------------------------------------------------
DATA_REPO = Path(
    os.environ.get("VORTEX_DATA_REPO", os.path.expanduser("~/vortex/vortex_data"))
)
DATA_VENV_PY = DATA_REPO / ".venv" / "bin" / "python"
REAL_WORKSPACE = Path(
    os.environ.get("VORTEX_WORKSPACE", os.path.expanduser("~/vortex/workspace"))
)
# Real-data tests run only when an explicit real workspace is provided via
# $VORTEX_WORKSPACE (and it exists). Unset -> skip; never run against an
# incidental home-default workspace.
_HAS_REAL_WS = bool(os.environ.get("VORTEX_WORKSPACE")) and REAL_WORKSPACE.exists()
_NO_REAL_WS_REASON = "需要真实 workspace（设 VORTEX_WORKSPACE 指向含 data/ 的工作区）"
DATA_PORT = 8791
DATA_TOKEN = "testtok_integ_8791"

# 000630.SZ: the only clean ex-day-with-minute symbol. adj_factor jumps
# 20260605=30.3727 -> 20260608=30.6085. RAW close 20260605 15:00=6.49 ->
# 20260608 09:30 open=6.08, 15:00 close=5.93.
EXDAY_SYMBOL = "000630.SZ"
# 000004.SZ on 20260506: suspend_type "S", flat phantom bars (vol=0, close=2.76).
SUSPEND_SYMBOL = "000004.SZ"
# A clean tradeable name with full bars on 20260512.. (no ST / no halt / no jump).
CLEAN_SYMBOL = "000001.SZ"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def _wait_health(port: int, token: str, timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    url = f"http://127.0.0.1:{port}/api/health"
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                if resp.status == 200:
                    return True
        except (urllib.error.URLError, ConnectionError, OSError):
            time.sleep(0.4)
    return False


_LAUNCH_SRC = (
    "from vortex_data.service.dashboard import start_dashboard;"
    "import time;"
    "s=start_dashboard({ws!r}, '127.0.0.1', {port});"
    "print('UP', flush=True);"
    "time.sleep(36000)"
)


@pytest.fixture(scope="session")
def live_data_service():
    """Launch the real vortex_data dashboard as a subprocess; tear down hard."""
    if TestClient is None:
        pytest.skip("fastapi not available (data venv): live-server tests need the backtest venv")
    if not DATA_VENV_PY.exists():
        pytest.skip(f"data venv missing: {DATA_VENV_PY}")
    if not _HAS_REAL_WS:
        pytest.skip(_NO_REAL_WS_REASON)
    if not _port_free(DATA_PORT):
        pytest.skip(f"port {DATA_PORT} already in use")

    env = dict(os.environ)
    env["VORTEX_DATA_DASHBOARD_TOKEN"] = DATA_TOKEN
    env.setdefault("VORTEX_WORKSPACE", str(REAL_WORKSPACE))
    src = _LAUNCH_SRC.format(ws=str(REAL_WORKSPACE), port=DATA_PORT)
    # start_new_session so we can signal the whole process group on teardown
    # (the dashboard spawns daemon threads; a bare terminate can leave them).
    proc = subprocess.Popen(
        [str(DATA_VENV_PY), "-c", src],
        cwd=str(DATA_REPO),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )

    def _hard_kill() -> None:
        import signal
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(os.getpgid(proc.pid), sig)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                proc.wait(timeout=6)
                return
            except subprocess.TimeoutExpired:
                continue

    try:
        if not _wait_health(DATA_PORT, DATA_TOKEN, timeout=40.0):
            _hard_kill()
            try:
                out = proc.communicate(timeout=3)[0] or b""
            except Exception:  # noqa: BLE001
                out = b""
            pytest.skip(f"data service did not become healthy; log:\n{out.decode(errors='replace')[:2000]}")
        yield f"http://127.0.0.1:{DATA_PORT}"
    finally:
        _hard_kill()


@pytest.fixture
def bt_client(tmp_path, monkeypatch, live_data_service):
    """A backtest TestClient wired to the live data gateway."""
    monkeypatch.setenv("VORTEX_DATA_URL", live_data_service)
    monkeypatch.setenv("VORTEX_DATA_DASHBOARD_TOKEN", DATA_TOKEN)
    # loopback host => write endpoints open without a backtest token.
    monkeypatch.delenv("VORTEX_BACKTEST_TOKEN", raising=False)
    monkeypatch.setenv("VORTEX_BACKTEST_HOST", "127.0.0.1")
    from vortex_backtest.app import create_app

    app = create_app(state_dir=tmp_path / "state")
    return TestClient(app)


def _mk_account(client: TestClient, cash: float = 1_000_000.0) -> str:
    aid = "acct-integ"
    r = client.post("/accounts", json={"account_id": aid, "name": "integ", "initial_cash": cash, "engine": "replay"})
    assert r.status_code in (201, 409), r.text
    return aid


def _mk_session(client: TestClient, account_id: str, universe: list[str], start: str, end: str) -> str:
    r = client.post("/sessions", json={
        "account_id": account_id, "level": "1min", "universe": universe,
        "start_date": start, "end_date": end,
        "fill_timing": "this_bar", "default_price_type": "close",
    })
    assert r.status_code == 201, r.text
    return r.json()["session_id"]


# ===========================================================================
# 1. EX-DAY NAV CONTINUITY (RAW price jump, dormant N8)
# ===========================================================================
@pytest.mark.integration
@pytest.mark.slow
def test_exday_position_nav_continuous_no_insufficient_cash(bt_client):
    """Hold 000630.SZ across the 2026-06-08 ex-day; RAW close drops ~9%.

    With RAW pricing and a real dividend dataset that has NO ex_date (N8 dormant),
    we must NOT see an insufficient_cash blowup or a crash. We BUY on 20260605
    (this_bar @ 15:00 close ~6.49), then advance over the ex-day. The held
    position's market value drops on 20260608 RAW close; NAV must stay finite,
    positive, and continuous (no exception, no negative cash).
    """
    client = bt_client
    aid = _mk_account(client, cash=1_000_000.0)
    sid = _mk_session(client, aid, [EXDAY_SYMBOL], "2026-06-05", "2026-06-08")

    # Step 1: advance to 20260605 15:00, buying 1000 shares this_bar at close.
    r1 = client.post(f"/sessions/{sid}/advance", json={
        "to": "2026-06-05T15:00:00",
        "orders": [{"symbol": EXDAY_SYMBOL, "side": 1, "quantity": 1000,
                    "exec_time": "14:57:00", "price_type": "close"}],
    })
    assert r1.status_code == 200, r1.text
    ctx1 = r1.json()
    filled = ctx1.get("filled", [])
    # If the buy was rejected we cannot test the cross-ex-day hold; surface why.
    assert filled, f"buy not filled; rejections={ctx1.get('rejected')}"
    nav_before = ctx1["nav"]
    assert nav_before > 0
    # Position is held.
    pos = {p["symbol"]: p for p in ctx1["positions"]}
    assert EXDAY_SYMBOL in pos and pos[EXDAY_SYMBOL]["quantity"] == 1000

    # Step 2: advance across the ex-day to 20260608 10:31 (RAW price has jumped).
    r2 = client.post(f"/sessions/{sid}/advance", json={"to": "2026-06-08T10:31:00"})
    assert r2.status_code == 200, r2.text
    ctx2 = r2.json()
    nav_after = ctx2["nav"]
    assert ctx2["cash"] >= 0, f"cash went negative: {ctx2['cash']}"
    assert nav_after > 0
    # NAV must not explode or vanish: RAW drop is ~9%, so NAV should move by a
    # small fraction of total (position is a tiny slice of 1M cash).
    assert abs(nav_after - nav_before) < nav_before * 0.5

    # N8 is DORMANT on real data (dividend lacks ex_date) => no corporate action.
    assert ctx2.get("corporate_actions") == [], ctx2.get("corporate_actions")

    # Close cleanly.
    rc = client.post(f"/sessions/{sid}/close")
    assert rc.status_code == 200, rc.text


@pytest.mark.integration
@pytest.mark.slow
def test_exday_raw_leaves_unbacked_nav_gap(bt_client):
    """DOCUMENT the design gap: RAW pricing without N8 credit on a real ex-day
    leaves a phantom NAV drop equal to the dividend that was never credited.

    On 000630.SZ the real cash dividend is cash_div_tax=0.05/share. Holding
    1000 shares, a faithful account would gain ~50 cash on the ex-day to offset
    the price drop. Because the real dividend has no ex_date, load_dividends->[]
    and NO cash is credited. We measure the position MV drop across the ex-day
    and assert that NO compensating cash appeared.

    This test asserts the CORRECT real-account expectation (cash should rise by
    ~qty*cash_div_tax). On the current RAW-only path that does not happen, so it
    is marked xfail to capture the gap as a documented, reproducible defect.
    """
    client = bt_client
    aid = _mk_account(client, cash=1_000_000.0)
    sid = _mk_session(client, aid, [EXDAY_SYMBOL], "2026-06-05", "2026-06-08")

    r1 = client.post(f"/sessions/{sid}/advance", json={
        "to": "2026-06-05T15:00:00",
        "orders": [{"symbol": EXDAY_SYMBOL, "side": 1, "quantity": 1000,
                    "exec_time": "14:57:00", "price_type": "close"}],
    })
    assert r1.status_code == 200, r1.text
    assert r1.json().get("filled"), r1.json().get("rejected")
    cash_after_buy = r1.json()["cash"]

    r2 = client.post(f"/sessions/{sid}/advance", json={"to": "2026-06-08T15:00:00"})
    assert r2.status_code == 200, r2.text
    cash_after_exday = r2.json()["cash"]

    # No new trades across the ex-day => the only way cash changes is a dividend
    # credit. A faithful real account would credit ~ qty*cash_div_tax = 1000*0.05.
    cash_credited = cash_after_exday - cash_after_buy

    # CORRECT behavior: real account credits the cash dividend on ex-day.
    assert cash_credited == pytest.approx(1000 * 0.05, abs=1.0), (
        "ex-day cash dividend not credited under RAW-only path "
        f"(credited={cash_credited}); expected ~50.0"
    )


# Mark the documented-gap test xfail (it asserts intended N8 behavior that the
# RAW-only real-data path does not deliver because the dividend lacks ex_date).
test_exday_raw_leaves_unbacked_nav_gap = pytest.mark.xfail(
    reason="BUG-RAWGAP: real dividend has no ex_date so N8 credit is dormant; "
           "RAW price drop on ex-day is uncompensated cash (NAV gap).",
    strict=False,
)(test_exday_raw_leaves_unbacked_nav_gap)


# ===========================================================================
# 2. SUSPENDED SYMBOL CANNOT TRADE (end to end through real gateway)
# ===========================================================================
@pytest.mark.integration
@pytest.mark.slow
def test_suspended_symbol_rejected_end_to_end(bt_client):
    """000004.SZ on 2026-05-06 is suspend_type 'S' (flat phantom bars, vol=0).

    A BUY routed through the real gateway must be rejected (suspended or
    zero_volume) and produce NO fill.
    """
    client = bt_client
    aid = _mk_account(client, cash=1_000_000.0)
    sid = _mk_session(client, aid, [SUSPEND_SYMBOL], "2026-05-06", "2026-05-06")

    r = client.post(f"/sessions/{sid}/advance", json={
        "to": "2026-05-06T15:00:00",
        "orders": [{"symbol": SUSPEND_SYMBOL, "side": 1, "quantity": 1000,
                    "exec_time": "09:31:00", "price_type": "close"}],
    })
    assert r.status_code == 200, r.text
    ctx = r.json()
    assert ctx.get("filled") == [], f"suspended symbol should not fill: {ctx.get('filled')}"
    rejected = ctx.get("rejected", [])
    assert rejected, "expected a rejection for the suspended symbol"
    reasons = {row.get("reason") for row in rejected}
    assert reasons & {"suspended", "zero_volume"}, f"unexpected reasons: {reasons}"
    # No position acquired.
    assert all(p["symbol"] != SUSPEND_SYMBOL or p["quantity"] == 0 for p in ctx["positions"])


# ===========================================================================
# 3. universe EXPANSION + PIT VISIBILITY THROUGH REAL GATEWAY
# ===========================================================================
@pytest.mark.integration
@pytest.mark.slow
def test_session_data_universe_expands_and_is_pit(bt_client):
    """POST /sessions/{id}/data with symbols:'universe' expands to the session
    universe and only returns rows <= sim_time (server uses sim_time as as_of).

    Spot-check: at sim_time 09:31 on a trading day, a minute bar dataset
    truncates to bars <= 09:31 (so the 15:00 close bar is NOT visible).
    """
    client = bt_client
    aid = _mk_account(client, cash=1_000_000.0)
    sid = _mk_session(client, aid, [CLEAN_SYMBOL], "2026-05-12", "2026-05-12")

    # Advance to 09:31 to establish a sim_time clock without buying anything.
    r0 = client.post(f"/sessions/{sid}/advance", json={"to": "2026-05-12T09:31:00"})
    assert r0.status_code == 200, r0.text
    assert r0.json()["sim_time"].startswith("2026-05-12T09:31")

    # symbols:'universe' must be expanded by the backtest service (data is
    # stateless), and the gateway must clip rows to <= sim_time (09:31).
    r = client.post(f"/sessions/{sid}/data", json={"datasets": [
        {"dataset": "stk_mins", "symbols": "universe",
         "window": {"range": {"start": "20260512", "end": "20260512"}}, "level": "1min"},
    ]})
    assert r.status_code == 200, r.text
    block = r.json()["results"]["stk_mins"]
    rows = block["rows"]
    assert rows, "universe expansion returned no rows"
    # Every returned row belongs to the universe symbol.
    syms = {row["symbol"] for row in rows}
    assert syms == {CLEAN_SYMBOL}, syms
    # PIT: no bar after 09:31 is visible (future-function leak guard).
    times = [str(row["trade_time"]) for row in rows]
    assert max(times) <= "2026-05-12 09:31:00", f"future bar leaked: max={max(times)}"
    assert "2026-05-12 15:00:00" not in times


@pytest.mark.integration
@pytest.mark.slow
def test_session_data_daily_field_not_visible_intraday(bt_client):
    """A genuine "daily result" dataset (stk_limit is open@09:30 by design, so
    use stk_mins close-of-day) must not leak its 15:00 close intraday.

    We assert the 15:00 close bar for the universe symbol is invisible at a
    sim_time of 10:31 on the same trading day.
    """
    client = bt_client
    aid = _mk_account(client, cash=1_000_000.0)
    sid = _mk_session(client, aid, [CLEAN_SYMBOL], "2026-05-12", "2026-05-12")
    r0 = client.post(f"/sessions/{sid}/advance", json={"to": "2026-05-12T10:31:00"})
    assert r0.status_code == 200, r0.text

    r = client.post(f"/sessions/{sid}/data", json={"datasets": [
        {"dataset": "stk_mins", "symbols": "universe",
         "window": {"range": {"start": "20260512", "end": "20260512"}}, "level": "1min"},
    ]})
    assert r.status_code == 200, r.text
    times = [str(row["trade_time"]) for row in r.json()["results"]["stk_mins"]["rows"]]
    assert times, "no rows"
    assert max(times) <= "2026-05-12 10:31:00"


# ===========================================================================
# 4. 503 WHEN GATEWAY UNCONFIGURED
# ===========================================================================
@pytest.mark.integration
def test_session_data_503_when_gateway_unset(tmp_path, monkeypatch):
    """/sessions/{id}/data must 503 (gateway_not_configured) when
    VORTEX_DATA_URL is unset, regardless of whether a data service is running.
    """
    if TestClient is None:
        pytest.skip("fastapi not available (data venv): needs the backtest venv")
    monkeypatch.delenv("VORTEX_DATA_URL", raising=False)
    monkeypatch.delenv("VORTEX_BACKTEST_TOKEN", raising=False)
    monkeypatch.setenv("VORTEX_BACKTEST_HOST", "127.0.0.1")
    from vortex_backtest.app import create_app

    client = TestClient(create_app(state_dir=tmp_path / "state"))
    aid = _mk_account(client)
    sid = _mk_session(client, aid, [CLEAN_SYMBOL], "2026-05-12", "2026-05-12")
    r = client.post(f"/sessions/{sid}/data", json={"datasets": [
        {"dataset": "stk_mins", "symbols": "universe", "window": {"range": {"start": "20260512"}}}]})
    assert r.status_code == 503, r.text
    assert r.json()["detail"]["error"] == "gateway_not_configured"


# ===========================================================================
# 5. IN-PROCESS PIT + load_dividends CONTRACT (no socket; always runs)
# ===========================================================================
def _query_service():
    """In-process QueryService on the real workspace (skips if duckdb absent)."""
    try:
        from vortex_data.service.query import QueryService  # noqa: F401
    except Exception:  # noqa: BLE001 - duckdb not installed in backtest venv
        pytest.skip("vortex_data / duckdb not importable in this venv")
    from vortex_data.service.query import QueryService

    if not _HAS_REAL_WS:
        pytest.skip(_NO_REAL_WS_REASON)
    return QueryService(str(REAL_WORKSPACE))


def test_inprocess_minute_pit_clips_future_bars():
    """Direct gateway_query: at as_of 10:31 the ex-day minute bars are clipped
    to <= 10:31 (62 bars), and the 15:00 close is invisible (no future leak)."""
    qs = _query_service()
    r = qs.gateway_query({"as_of": "2026-06-08T10:31:00", "datasets": [
        {"dataset": "stk_mins", "symbols": [EXDAY_SYMBOL],
         "window": {"range": {"start": "20260608", "end": "20260608"}}, "level": "1min"},
    ]})
    rows = r["results"]["stk_mins"]["rows"]
    assert rows
    times = sorted(str(row["trade_time"]) for row in rows)
    assert times[-1] <= "2026-06-08 10:31:00", times[-1]
    assert "2026-06-08 15:00:00" not in times


def test_inprocess_adj_factor_exday_visible_at_open_not_leak():
    """FALSE-POSITIVE GUARD: same-day (ex-day) adj_factor IS returned at 09:30+
    by design (override at=09:30). This is intended, not a leak."""
    qs = _query_service()
    r = qs.gateway_query({"as_of": "2026-06-08T09:30:00", "datasets": [
        {"dataset": "adj_factor", "symbols": [EXDAY_SYMBOL],
         "window": {"range": {"start": "20260608", "end": "20260608"}}},
    ]})
    rows = r["results"]["adj_factor"]["rows"]
    assert rows, "ex-day adj_factor must be visible at open (intraday backtest needs it)"
    assert any(int(row["date"]) == 20260608 for row in rows)


def test_inprocess_load_dividends_contract_on_real_data():
    """gateway_adapter.load_dividends documents (gateway_adapter.py:167-206) that
    it returns [] when the dividend dataset has no ex_date column (graceful
    degradation). On the REAL workspace the dividend dataset HAS no ex_date, but
    the gateway routes the field-list request to read_window which raises
    KeyError('字段不存在: ex_date') -> 400 bad_request -> GatewayDataError, so
    load_dividends RAISES instead of returning [].

    This asserts the CORRECT/intended contract (return []); it currently fails
    because the missing-column path errors out before the dataframe is inspected.
    Marked xfail to capture the divergence as a reproducible defect.
    """
    qs = _query_service()
    # Reproduce exactly what load_dividends sends.
    req = {"as_of": "2026-06-08T10:31:00", "datasets": [{
        "dataset": "dividend", "symbols": [EXDAY_SYMBOL],
        "fields": ["symbol", "ex_date", "cash_div_tax", "stk_div", "stk_bo_rate", "stk_co_rate"],
    }]}
    # CORRECT: the gateway should degrade (return an empty/ex_date-less block),
    # letting load_dividends return []. Instead it raises KeyError.
    result = qs.gateway_query(req)  # expected (intended): no exception
    block = result["results"]["dividend"]
    assert "ex_date" not in block["columns"] or block["row_count"] == 0


test_inprocess_load_dividends_contract_on_real_data = pytest.mark.xfail(
    reason="BUG-DIVFIELD: gateway_query routes a field-list dividend request to "
           "read_window, which raises KeyError on the missing ex_date column "
           "(400 bad_request) instead of degrading to []. load_dividends' "
           "documented graceful-[] path is unreachable on real data.",
    strict=False,
    raises=KeyError,
)(test_inprocess_load_dividends_contract_on_real_data)

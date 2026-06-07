"""回归测试：钉住已确认的缺陷（不改源码）。

每个用例断言**正确行为**，因此在当前代码上会失败，用 `@pytest.mark.xfail(strict=True)`
标记：
- 当前实现仍有 bug → 测试 xfail（CI 保持绿）。
- 一旦有人修复 → 测试 XPASS，strict 模式把 XPASS 判为失败，强制提醒移除该标记并确认修复。

详细复现与影响分析见 docs/code-review-findings.md。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from vortex_backtest.app import create_app
from vortex_backtest.worker import drain_jobs


def _client(tmp_path: Path, monkeypatch, workspace: Path) -> TestClient:
    monkeypatch.setenv("VORTEX_DATA_WORKSPACE", str(workspace))
    return TestClient(create_app(tmp_path / "state", run_worker=False))


def _drain_summary(client: TestClient, payload: dict[str, Any]) -> dict[str, Any]:
    job_id = client.post("/backtests", json=payload).json()["job_id"]
    drain_jobs(client.app.state.store)
    return client.get(f"/backtests/{job_id}/summary").json()


# ============================================================================
# Bug #1（严重）：滑点击穿买入现金校验 → 成交后现金为负
#   validate_order 的现金充足性按 fill_price(无滑点) 判定，
#   但 execute_order 按 fill_price*(1+slippage) 扣款 → 临界单会把现金打到负数。
#   位置：market_rules.AShareRuleEngine.validate_order(BUY 分支) /
#         replay_engine.execute_order。
# ============================================================================

@pytest.mark.xfail(strict=True, reason="bug#1 滑点未计入买入现金校验，临界单导致现金为负")
def test_slippage_must_not_drive_cash_negative(tmp_path, monkeypatch, workspace_builder):
    ws = workspace_builder.day(
        "2026-01-02", "000001.SZ", open=10.0, close=10.0, volume=1_000_000, up_limit=99.0, down_limit=1.0
    ).build()
    client = _client(tmp_path, monkeypatch, ws)
    client.post("/accounts", json={"account_id": "a", "initial_cash": 9010})
    # 现金 9010 恰好够 900 股@10（含费 9005.09），但 50bps 滑点会让实际成交≈9050.09。
    client.post("/accounts/a/orders", json={
        "request_id": "edge", "trade_date": "2026-01-02", "symbol": "000001.SZ", "side": 1, "quantity": 900,
    })
    summary = _drain_summary(client, {
        "account_id": "a", "start_date": "2026-01-02", "end_date": "2026-01-02",
        "strategies": [{"strategy_id": "s", "strategy_type": "order_replay", "initial_cash": 9010, "symbols": ["000001.SZ"]}],
        "execution": {"slippage_bps": 50},
    })
    # 正确不变量：现金永不为负（滑点应纳入现金校验，否则该单应被 insufficient_cash 拒绝）。
    assert summary["cash"] >= -1e-6, f"现金被滑点打成负数: {summary['cash']}"


# ============================================================================
# Bug #2（严重）：多策略日级聚合在“日期缺口”处把缺席策略当作凭空蒸发
#   backtrader 引擎仅对“该策略当日有分钟 bar”的日期产出 daily 快照；
#   某策略标的停牌/当日无数据时该日无快照。aggregate_daily 按日期分组后只把
#   “当日有快照的策略”相加 → 缺席策略的现金/市值被漏掉 → 组合净值虚降、
#   daily_pnl/最大回撤被严重扭曲。（qlib 引擎按交易日并集补齐，不受影响 → 两引擎不一致。）
#   位置：replay_engine.aggregate_daily。
# ============================================================================

@pytest.mark.xfail(strict=True, reason="bug#2 aggregate_daily 在日期缺口处漏算缺席策略，组合净值失真")
def test_multi_strategy_daily_aggregation_handles_missing_dates(tmp_path, monkeypatch, workspace_builder):
    # A 标的两天都有 bar；B 标的仅 01-05 有 bar（01-02 视作停牌/无数据）。两策略各持 1 万现金、都不下单。
    ws = (
        workspace_builder
        .day("2026-01-02", "000001.SZ", open=10.0, close=10.0, volume=1_000_000, up_limit=99.0, down_limit=1.0)
        .day("2026-01-05", "000001.SZ", open=10.0, close=10.0, volume=1_000_000, up_limit=99.0, down_limit=1.0)
        .day("2026-01-05", "000002.SZ", open=10.0, close=10.0, volume=1_000_000, up_limit=99.0, down_limit=1.0)
        .build()
    )
    client = _client(tmp_path, monkeypatch, ws)
    client.post("/accounts", json={"account_id": "a", "initial_cash": 20000})
    summary = _drain_summary(client, {
        "account_id": "a", "start_date": "2026-01-02", "end_date": "2026-01-05",
        "strategies": [
            {"strategy_id": "A", "strategy_type": "order_replay", "initial_cash": 10000, "symbols": ["000001.SZ"]},
            {"strategy_id": "B", "strategy_type": "order_replay", "initial_cash": 10000, "symbols": ["000002.SZ"]},
        ],
    })
    daily = {d["trade_date"]: d for d in summary["daily"]}
    # 两策略都没交易、全程空仓持现金 → 任何交易日组合净值都应是 20000。
    assert daily["2026-01-02"]["total_value"] == pytest.approx(20000.0), (
        f"01-02 组合净值应为 20000（B 空仓持 1 万），实得 {daily['2026-01-02']['total_value']}：B 被漏算"
    )
    # 没有任何真实盈亏 → 最大回撤应≈0，而非缺口造成的虚构暴跌。
    assert summary["max_drawdown"] == pytest.approx(0.0, abs=1e-9)

"""会话式回测 · 多场景示例（design/18）。

给使用者参考调试：每个场景演示**不同的交互流程**，对着真实 HTTP 接口走一遍。

前置：
  1. 起 data 网关（vortex_data）：默认 127.0.0.1:8765；建议设 `VORTEX_DATA_DASHBOARD_TOKEN`。
  2. 起 backtest 服务（vortex_backtest）：`vortex-backtest serve`，默认 127.0.0.1:8767；
     并让它能访问网关——设 `VORTEX_DATA_URL=http://127.0.0.1:8765`（不设则回退本地直读 parquet）。
  3. 跑本脚本：
        python examples/session_scenarios.py daily        # 日频选股
        python examples/session_scenarios.py minute        # 分钟择时
        python examples/session_scenarios.py scan          # 全市场扫描选股（算子下推）
        python examples/session_scenarios.py progressive   # 循序渐进取数（缩股池）
        python examples/session_scenarios.py replay        # A 特例：订单全预提交一次跑完
        python examples/session_scenarios.py all

环境变量：
  VORTEX_BACKTEST_URL  默认 http://127.0.0.1:8767
  VORTEX_BACKTEST_TOKEN 若 backtest 配了写 token，则带上
"""
from __future__ import annotations

import os
import sys

import httpx

BASE = os.getenv("VORTEX_BACKTEST_URL", "http://127.0.0.1:8767").rstrip("/")
TOKEN = os.getenv("VORTEX_BACKTEST_TOKEN")
# 服务对服务直连，绕过环境 HTTP(S)/SOCKS 代理。
_C = httpx.Client(trust_env=False, timeout=120.0,
                  headers={"X-Auth-Token": TOKEN} if TOKEN else {})


def _post(path: str, body: dict) -> dict:
    r = _C.post(f"{BASE}{path}", json=body)
    r.raise_for_status()
    return r.json()


def _get(path: str) -> dict | list:
    r = _C.get(f"{BASE}{path}")
    r.raise_for_status()
    return r.json()


def _account(name: str, cash: float = 1_000_000) -> str:
    # 账户已存在(409)就复用
    try:
        _post("/accounts", {"account_id": name, "initial_cash": cash})
    except httpx.HTTPStatusError as e:
        if e.response.status_code != 409:
            raise
    return name


def _open(account: str, **kw) -> str:
    return _post("/sessions", {"account_id": account, **kw})["session_id"]


def _report(sid: str) -> None:
    s = _get(f"/sessions/{sid}/summary")
    print(f"  → summary: 收益 {s['total_return']:.4%} | 已实现 {s['realized_pnl']:.2f} | 净值 {s['total_value']:.2f}")
    print(f"  → 成交 {len(_get(f'/sessions/{sid}/trades'))} 笔")


# ───────────────────────────────────────────── 场景 1：日频选股
def scenario_daily() -> None:
    """日频：收盘出信号 → 次日开盘成交（fill_timing=next_bar，to=next_day）。"""
    print("[日频选股] 收盘决策、次日开盘成交")
    acc = _account("demo_daily")
    sid = _open(acc, level="daily", start_date="2026-06-03", end_date="2026-06-05",
                universe=["600519.SH"], fill_timing="next_bar")
    # 第1天收盘决策买入（next_bar → 次日开盘成交）
    ctx = _post(f"/sessions/{sid}/advance", {
        "orders": [{"request_id": "d1", "symbol": "600519.SH", "side": 1, "quantity": 100}],
        "to": "next_day"})
    print(f"  day1 决策→次日成交: 持仓 {[ (p['symbol'],p['quantity']) for p in ctx['positions']]}")
    _post(f"/sessions/{sid}/advance", {"to": "2026-06-05T15:00:00"})  # 推到末尾
    _post(f"/sessions/{sid}/close", {})
    _report(sid)


# ───────────────────────────────────────────── 场景 2：分钟择时
def scenario_minute() -> None:
    """分钟：精确分钟下单（this_bar + exec_time）。"""
    print("[分钟择时] 09:35 买、14:55 卖，当根成交")
    acc = _account("demo_minute")
    sid = _open(acc, level="1min", start_date="2026-06-03", end_date="2026-06-03",
                universe=["600519.SH"], fill_timing="this_bar")
    _post(f"/sessions/{sid}/advance", {
        "orders": [{"request_id": "m1", "symbol": "600519.SH", "side": 1, "quantity": 100, "exec_time": "09:35"}],
        "to": "2026-06-03T10:00:00"})
    _post(f"/sessions/{sid}/advance", {
        "orders": [{"request_id": "m2", "symbol": "600519.SH", "side": 2, "quantity": 100, "exec_time": "14:55"}],
        "to": "2026-06-03T15:00:00"})
    _post(f"/sessions/{sid}/close", {})
    _report(sid)


# ───────────────────────────────────────────── 场景 3：全市场扫描选股
def scenario_scan() -> None:
    """全市场扫描：用 /data 的算子下推取"某指标 topN" → set_universe → 再交易。

    注意：op 下推在 data 网关侧算，只回结果行（不传全市场原始行）。
    """
    print("[全市场扫描] /data op=topN 选股 → set_universe → 交易")
    acc = _account("demo_scan")
    sid = _open(acc, level="1min", start_date="2026-06-03", end_date="2026-06-04",
                universe=[], fill_timing="next_bar")
    # 第1步：先推进到某时刻，再问"全市场涨幅/估值 topN"（这里示意 valuation 的 pe_ttm 反向 topN=低估）
    scan = _post(f"/sessions/{sid}/data", {
        "datasets": [{"dataset": "valuation", "symbols": "all", "fields": ["symbol", "pe_ttm"],
                      "op": {"kind": "topn", "by": "pe_ttm", "n": 5, "order": "ASC"}}]})
    # op 在 ≤as_of 的所有行上排名；选股需去重到"每只一条"——这里客户端按出现顺序去重取标的。
    # （更严谨可在 op 里配 instant 过滤到某一日快照；此处演示流程。）
    picks = list(dict.fromkeys(r["symbol"] for r in scan["results"].get("valuation", {}).get("rows", [])))
    print(f"  扫描选出 {len(picks)} 只: {picks[:5]}")
    if picks:
        _post(f"/sessions/{sid}/advance", {
            "set_universe": picks,
            "orders": [{"request_id": "s1", "symbol": picks[0], "side": 1, "quantity": 100}],
            "to": "next_day"})
    _post(f"/sessions/{sid}/advance", {"to": "2026-06-04T15:00:00"})
    _post(f"/sessions/{sid}/close", {})
    _report(sid)


# ───────────────────────────────────────────── 场景 4：循序渐进取数
def scenario_progressive() -> None:
    """循序渐进：先小股池取历史窗口 → 据此缩/换股池 → 再 advance。股池一次声明后粘住。"""
    print("[循序渐进取数] 取窗口 → 缩股池 → 推进")
    acc = _account("demo_prog")
    sid = _open(acc, level="1min", start_date="2026-06-03", end_date="2026-06-03",
                universe=["600519.SH", "000001.SZ"], fill_timing="next_bar")
    hist = _post(f"/sessions/{sid}/data", {
        "datasets": [{"dataset": "stk_mins", "symbols": "universe",
                      "fields": ["symbol", "trade_time", "close"], "level": "1min",
                      "window": {"count": 5}}]})
    rows = hist["results"].get("stk_mins", {}).get("rows", [])
    print(f"  取到股池最近 5 根: {len(rows)} 行（≤ 当前 sim_time）")
    _post(f"/sessions/{sid}/advance", {
        "set_universe": ["600519.SH"],  # 缩到 1 只，后续粘住
        "orders": [{"request_id": "p1", "symbol": "600519.SH", "side": 1, "quantity": 100, "exec_time": "10:00"}],
        "to": "2026-06-03T15:00:00"})
    _post(f"/sessions/{sid}/close", {})
    _report(sid)


# ───────────────────────────────────────────── 场景 5：A 特例（订单回放）
def scenario_replay() -> None:
    """A 特例：决策全已知 → 订单全预提交、一路 advance 到 end，从不调 /data。"""
    print("[A 特例·订单回放] 订单全预提交，一次推到底")
    acc = _account("demo_replay")
    sid = _open(acc, level="1min", start_date="2026-06-03", end_date="2026-06-04",
                universe=["600519.SH"], fill_timing="this_bar")
    _post(f"/sessions/{sid}/advance", {
        "orders": [
            {"request_id": "r1", "symbol": "600519.SH", "side": 1, "quantity": 100, "trade_date": "2026-06-03", "exec_time": "09:30"},
            {"request_id": "r2", "symbol": "600519.SH", "side": 2, "quantity": 100, "trade_date": "2026-06-04", "exec_time": "09:30"},
        ],
        "to": "2026-06-04T15:00:00"})
    _post(f"/sessions/{sid}/close", {})
    _report(sid)


SCENARIOS = {
    "daily": scenario_daily, "minute": scenario_minute, "scan": scenario_scan,
    "progressive": scenario_progressive, "replay": scenario_replay,
}


def main() -> None:
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    targets = list(SCENARIOS) if which == "all" else [which]
    for name in targets:
        fn = SCENARIOS.get(name)
        if not fn:
            print(f"未知场景: {name}；可选: {', '.join(SCENARIOS)} / all")
            continue
        try:
            fn()
        except httpx.HTTPError as e:
            print(f"  ✗ {name} 失败: {e}")
        print()


if __name__ == "__main__":
    main()

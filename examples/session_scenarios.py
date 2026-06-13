"""会话式回测 · 多场景示例（design/18）。

给使用者参考调试：每个场景演示**不同的交互流程**，对着真实 HTTP 接口走一遍。

前置：
  1. 起 data 网关（vortex_data）：默认 127.0.0.1:8765；建议设 `VORTEX_DATA_DASHBOARD_TOKEN`。
  2. 起 backtest 服务（vortex_backtest）：`vortex-backtest serve`，默认 127.0.0.1:8766；
     并让它能访问网关——设 `VORTEX_DATA_URL=http://127.0.0.1:8765`（不设则回退本地直读 parquet）。
  3. 跑本脚本：
        python examples/session_scenarios.py daily        # 日频选股
        python examples/session_scenarios.py minute        # 分钟择时
        python examples/session_scenarios.py scan          # 全市场扫描选股（算子下推）
        python examples/session_scenarios.py progressive   # 循序渐进取数（缩股池）
        python examples/session_scenarios.py replay        # A 特例：订单全预提交一次跑完
        # —— 银行股频繁买卖专题（高换手，喂看板换手率/仓位/分布图）——
        python examples/session_scenarios.py bank_rotate    # 日线轮动：多只银行股间高频轮换
        python examples/session_scenarios.py bank_pyramid   # 分钟分批：金字塔建仓 + 次日分批减仓
        python examples/session_scenarios.py bank_limit     # 限价单 + 撤单：limit 校验 / cancel
        python examples/session_scenarios.py bank_frenzy    # 满仓轮动狂点：买齐 10 只逐日轮换
        python examples/session_scenarios.py all

环境变量：
  VORTEX_BACKTEST_URL  默认 http://127.0.0.1:8766
  VORTEX_BACKTEST_TOKEN 若 backtest 配了写 token，则带上
"""
from __future__ import annotations

import os
import sys

import httpx

BASE = os.getenv("VORTEX_BACKTEST_URL", "http://127.0.0.1:8766").rstrip("/")
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
    try:  # 二期分布端点：打印换手率，没有也不影响
        d = _get(f"/sessions/{sid}/distributions")
        tm = d.get("turnover_mean")
        if tm is not None:
            print(f"  → 月均单边换手 {tm:.2%} | 调仓 {len(_get(f'/sessions/{sid}/rebalances'))} 次")
    except httpx.HTTPError:
        pass
    print(f"  → 看板: {BASE}/ui/#/session/{sid}")


# ───────────────────────────────────────────── 银行股专题：常量
# 10 只银行股（代码 + 中文名），价档从民生 3.5 元到招商 37 元，均有 82 交易日分钟数据。
BANKS = [
    ("600016.SH", "民生银行"), ("601988.SH", "中国银行"), ("601398.SH", "工商银行"),
    ("601288.SH", "农业银行"), ("600000.SH", "浦发银行"), ("601939.SH", "建设银行"),
    ("000001.SZ", "平安银行"), ("601166.SH", "兴业银行"), ("002142.SZ", "宁波银行"),
    ("600036.SH", "招商银行"),
]
BANK_CODES = [c for c, _ in BANKS]
# 窗口 2026-02-02~2026-06-09 内的真实交易日抽样（本地直读模式无 /data 取日历，故内置）。
ROTATE_DAYS = ["2026-02-09", "2026-02-24", "2026-03-03", "2026-03-10", "2026-03-17",
               "2026-03-24", "2026-03-31", "2026-04-08", "2026-04-15", "2026-04-22",
               "2026-04-29", "2026-05-11", "2026-05-18", "2026-05-25", "2026-06-01", "2026-06-08"]
FRENZY_DAYS = ["2026-02-04", "2026-02-05", "2026-02-06", "2026-02-09",
               "2026-02-10", "2026-02-11", "2026-02-12", "2026-02-13"]


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


# ═════════════════════════════ 银行股专题场景 ═════════════════════════════

def scenario_bank_rotate() -> None:
    """日线轮动：在 10 只银行股间高频轮换持仓，跨整个 82 日窗口。

    每个轮动日卖出当前 3 只持仓（已跨日解锁，满足 T+1）+ 买入轮换到的下 3 只，
    收盘语义、开盘附近成交。制造跨月高换手 → 看板换手率/仓位/月度热力图。
    """
    print("[银行股·日线轮动] 10 只银行股间高频轮换，跨全窗口")
    acc = _account("demo_bank_rotate", cash=20_000_000)
    sid = _open(acc, level="daily", start_date="2026-02-02", end_date="2026-06-09",
                universe=BANK_CODES, fill_timing="next_bar")
    qty, held, rid = 100_000, [], 0
    for i, day in enumerate(["2026-02-02"] + ROTATE_DAYS):
        picks = [BANK_CODES[(i * 3 + k) % len(BANK_CODES)] for k in range(3)]
        orders = []
        for sym in held:                                   # 卖出旧持仓（已解锁）
            rid += 1
            orders.append({"request_id": f"s{rid}", "symbol": sym, "side": 2, "quantity": qty,
                           "trade_date": day, "exec_time": "09:31"})
        for sym in picks:                                  # 买入轮换的新 3 只
            rid += 1
            orders.append({"request_id": f"b{rid}", "symbol": sym, "side": 1, "quantity": qty,
                           "trade_date": day, "exec_time": "09:35"})
        ctx = _post(f"/sessions/{sid}/advance",
                    {"request_id": f"rot{i}", "to": f"{day}T15:00:00", "orders": orders})
        held = list(picks)
        print(f"  {day}: 成交 {len(ctx['filled'])} / 拒 {len(ctx['rejected'])} → 持仓 {len(ctx['positions'])} 只")
    _post(f"/sessions/{sid}/advance", {"request_id": "rot_end", "to": "2026-06-09T15:00:00"})
    _post(f"/sessions/{sid}/close", {})
    _report(sid)


def scenario_bank_pyramid() -> None:
    """分钟分批建减仓：单只银行股 D1 金字塔建仓（多个 exec_time、数量递增），D2 分批减仓。

    展示分钟粒度 + this_bar 精确成交时点；尊重 T+1：D1 建的仓 D2 才卖。
    """
    print("[银行股·分钟分批建减仓] 平安银行金字塔建仓 + 次日分批减仓")
    sym = "000001.SZ"
    acc = _account("demo_bank_pyramid", cash=20_000_000)
    sid = _open(acc, level="1min", start_date="2026-02-03", end_date="2026-02-04",
                universe=[sym], fill_timing="this_bar")
    buys = [("09:35", 20_000), ("10:30", 30_000), ("11:20", 40_000), ("13:30", 50_000), ("14:45", 60_000)]
    orders = [{"request_id": f"pb{i}", "symbol": sym, "side": 1, "quantity": q,
               "trade_date": "2026-02-03", "exec_time": t} for i, (t, q) in enumerate(buys)]
    ctx = _post(f"/sessions/{sid}/advance",
                {"request_id": "pyr_d1", "to": "2026-02-03T15:00:00", "orders": orders})
    pos = {p["symbol"]: p["quantity"] for p in ctx["positions"]}
    print(f"  D1 金字塔建仓: 成交 {len(ctx['filled'])} 笔 → 持仓 {pos}")
    sells = [("09:40", 100_000), ("13:00", 100_000)]      # 分两批清掉 20 万股
    orders = [{"request_id": f"ps{i}", "symbol": sym, "side": 2, "quantity": q,
               "trade_date": "2026-02-04", "exec_time": t} for i, (t, q) in enumerate(sells)]
    ctx = _post(f"/sessions/{sid}/advance",
                {"request_id": "pyr_d2", "to": "2026-02-04T15:00:00", "orders": orders})
    print(f"  D2 分批减仓: 成交 {len(ctx['filled'])} / 拒 {len(ctx['rejected'])}")
    _post(f"/sessions/{sid}/close", {})
    _report(sid)


def scenario_bank_limit() -> None:
    """限价单 + 撤单：演示 limit_price 撮合校验与 cancel 撤未成交挂单。"""
    print("[银行股·限价单+撤单] 工商银行 limit 校验 / cancel")
    sym = "601398.SH"  # 工行 ~7 元
    acc = _account("demo_bank_limit", cash=20_000_000)
    sid = _open(acc, level="1min", start_date="2026-02-03", end_date="2026-02-04",
                universe=[sym], fill_timing="next_bar")
    # 1) 限价高于市价 → 成交
    ctx = _post(f"/sessions/{sid}/advance", {"request_id": "lim1", "to": "2026-02-03T10:00:00",
        "orders": [{"request_id": "lb1", "symbol": sym, "side": 1, "quantity": 50_000,
                    "trade_date": "2026-02-03", "exec_time": "09:35", "limit_price": 10.0}]})
    print(f"  限价 10.0 买入: 成交 {len(ctx['filled'])} / 拒 {len(ctx['rejected'])}")
    # 2) 限价远低于市价 → 撮合即拒（打印拒因）
    ctx = _post(f"/sessions/{sid}/advance", {"request_id": "lim2", "to": "2026-02-03T11:00:00",
        "orders": [{"request_id": "lb2", "symbol": sym, "side": 1, "quantity": 50_000,
                    "trade_date": "2026-02-03", "exec_time": "10:30", "limit_price": 1.00}]})
    reasons = [r.get("reason") for r in ctx["rejected"]]
    print(f"  限价 1.00 买入(过低): 成交 {len(ctx['filled'])} / 拒 {len(ctx['rejected'])} {reasons}")
    # 3) 提交 next_bar 停泊买单但不推进时钟 → 进 open_orders
    ctx = _post(f"/sessions/{sid}/advance", {"request_id": "lim3", "to": ctx["sim_time"],
        "orders": [{"request_id": "lb3", "symbol": sym, "side": 1, "quantity": 50_000}]})
    print(f"  提交停泊单(不推进): open_orders {len(ctx['open_orders'])}")
    # 4) cancel 撤掉停泊单
    ctx = _post(f"/sessions/{sid}/advance", {"request_id": "lim4", "to": ctx["sim_time"],
        "cancel": ["lb3"]})
    print(f"  撤单: cancelled {ctx.get('cancelled')} → open_orders {len(ctx['open_orders'])}")
    _post(f"/sessions/{sid}/advance", {"request_id": "lim_end", "to": "2026-02-04T15:00:00"})
    _post(f"/sessions/{sid}/close", {})
    _report(sid)


def scenario_bank_frenzy() -> None:
    """满仓轮动狂点：一次买齐 10 只银行股，之后逐日卖 3 只买回 3 只制造极高换手。"""
    print("[银行股·满仓轮动狂点] 买齐 10 只 + 逐日轮换")
    acc = _account("demo_bank_frenzy", cash=20_000_000)
    sid = _open(acc, level="daily", start_date="2026-02-02", end_date="2026-03-13",
                universe=BANK_CODES, fill_timing="next_bar")
    qty = 30_000
    orders = [{"request_id": f"f0_{i}", "symbol": c, "side": 1, "quantity": qty,
               "trade_date": "2026-02-03", "exec_time": "09:35"} for i, c in enumerate(BANK_CODES)]
    ctx = _post(f"/sessions/{sid}/advance",
                {"request_id": "frz0", "to": "2026-02-03T15:00:00", "orders": orders})
    print(f"  D0 满仓: 成交 {len(ctx['filled'])} → 持仓 {len(ctx['positions'])} 只")
    for i, day in enumerate(FRENZY_DAYS):
        swap = [BANK_CODES[(i * 3 + k) % len(BANK_CODES)] for k in range(3)]
        orders = []
        for k, c in enumerate(swap):                       # 09:35 卖出（昨日持仓已解锁）
            orders.append({"request_id": f"f{i}s{k}", "symbol": c, "side": 2, "quantity": qty,
                           "trade_date": day, "exec_time": "09:35"})
        for k, c in enumerate(swap):                       # 14:30 买回（次日才能再卖）
            orders.append({"request_id": f"f{i}b{k}", "symbol": c, "side": 1, "quantity": qty,
                           "trade_date": day, "exec_time": "14:30"})
        ctx = _post(f"/sessions/{sid}/advance",
                    {"request_id": f"frz{i + 1}", "to": f"{day}T15:00:00", "orders": orders})
        print(f"  {day}: 卖买各 3 → 成交 {len(ctx['filled'])} / 拒 {len(ctx['rejected'])}")
    _post(f"/sessions/{sid}/advance", {"request_id": "frz_end", "to": "2026-03-13T15:00:00"})
    _post(f"/sessions/{sid}/close", {})
    _report(sid)


SCENARIOS = {
    "daily": scenario_daily, "minute": scenario_minute, "scan": scenario_scan,
    "progressive": scenario_progressive, "replay": scenario_replay,
    "bank_rotate": scenario_bank_rotate, "bank_pyramid": scenario_bank_pyramid,
    "bank_limit": scenario_bank_limit, "bank_frenzy": scenario_bank_frenzy,
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

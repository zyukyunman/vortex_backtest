# 看板接真数据 + 分析报告层 · 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把看板从演示假数据接回真实会话回测，并新增调仓记录、日/周/时/分多粒度持仓、与基准对比的绩效指标（metrics/equity/positions/rebalances/benchmarks 五个只读端点 + 看板两页重写）。

**Architecture:** 纯增量三层——`analytics.py` 指标纯函数层（输入输出全为 list/dict，金标可测）+ `benchmark.py` 基准直读（workspace `index_daily`/`sw_daily`）+ `app.py` 五个只读 GET 端点（读时归约，open 会话也能实时出指标）；前端 `web/static/app.js` 整体重写为会话列表/详情两页，**删除 mock 兜底**。引擎/撮合/产物格式零改动。

**Tech Stack:** Python 3.12 venv、FastAPI、pandas（仅 benchmark 读 parquet）、纯 stdlib math（analytics）、原生 JS + vendored Chart.js（无构建链）。

**Spec:** `docs/superpowers/specs/2026-06-12-dashboard-analytics-design.md`

---

## 既有事实（执行者直接信任）

- 产物形状：`daily` 行 = `{strategy_id, trade_date("YYYY-MM-DD"), cash, market_value, total_value, daily_pnl, total_return, drawdown, positions[], trades[], rejections[]}`；`snapshots.jsonl` 行 = `{strategy_id, timestamp(ISO), frequency, cash, market_value, total_value, positions[], trades[], rejections[]}`；`positions[]` 元素 = `{strategy_id, symbol, quantity, available_quantity, cost_basis, last_price, market_value, unrealized_pnl, unrealized_pnl_ratio}`；`trades.jsonl` 行含 `{trade_date, symbol, side(1/2), quantity, amount, price, commission, stamp_tax, transfer_fee, realized_pnl, cash_after}`。
- 基准数据（2026-06-12 实测）：`index_daily` 列 `[symbol, date(str YYYYMMDD), close, …]` 无 name 列、4151 个代码；`sw_daily` 列 `[symbol, date, name, …, close, …]` 439 个申万指数；两者覆盖 20260202→20260610。
- `app.py` 内可复用：`_session_or_404` / `_session_dir` / `_read_jsonl` / `_session_summary`（返回 dict 含 `daily` 键）。
- 前端：`web/index.html`（27 行壳，保留）已引 `static/vendor/chart.umd.min.js`；`static/app.js`（742 行，整体替换）；`static/app.css`（63 行，保留+追加）。
- 真实冒烟环境：容器 vortex-backtest 在 8766（重建后才含新代码）；`~/vortex/state` 有已 close 会话 `6e64f3d6-86d2-45fb-89a2-fc51b42d2909`（demo-container，2 笔成交，82 交易日）。

## 文件结构

| 动作 | 路径 | 职责 |
|---|---|---|
| Create | `vortex_backtest/analytics.py` | 指标/切片/事件纯函数（口径单一真值源） |
| Create | `vortex_backtest/benchmark.py` | 基准序列直读 + 目录 |
| Modify | `vortex_backtest/app.py` | 5 个只读 GET 端点 |
| Create | `tests/test_analytics.py` | 金标单测 |
| Create | `tests/test_benchmark.py` | fixture parquet 单测 |
| Create | `tests/test_report_api.py` | 端点 API 测试（手工构造 state+JSONL） |
| Rewrite | `vortex_backtest/web/static/app.js` | 两页 SPA，无 mock |
| Modify | `vortex_backtest/web/index.html` | 仅 `?v=6` 缓存戳 |
| Modify | `vortex_backtest/web/static/app.css` | 追加错误横幅/表格小样式 |
| Modify | `docs/usage-and-api.md` `README.md` | 端点清单补 5 行/能力补 1 行 |

---

### Task 1: analytics 基础指标（perf_stats / equity_curve）— TDD

**Files:** Create `vortex_backtest/analytics.py`、Create `tests/test_analytics.py`

- [ ] **Step 1.1: 写失败测试**

```python
"""analytics 金标：手工构造已知序列，断言指标到 1e-9。"""
import math

import pytest

from vortex_backtest import analytics


def s(pairs):  # 简写：[(date, value)] -> series dict
    return {d: v for d, v in pairs}


def test_perf_stats_constant_growth():
    # 恒定日收益 0.1%：TR=(1.001)^4-1；std=0 → sharpe None、vol=0；无回撤
    series = s([("2026-01-05", 100.0), ("2026-01-06", 100.1), ("2026-01-07", 100.2001),
                ("2026-01-08", 100.3003001), ("2026-01-09", 100.40060040)])
    st = analytics.perf_stats(series)
    assert st["total_return"] == pytest.approx(1.001 ** 4 - 1, rel=1e-9)
    assert st["annual_return"] == pytest.approx((1.001 ** 4) ** (252 / 4) - 1, rel=1e-9)
    assert st["sharpe"] is None and st["volatility"] == pytest.approx(0.0)
    assert st["max_drawdown"] == pytest.approx(0.0)
    assert st["win_days_ratio"] == pytest.approx(1.0)
    assert st["n_days"] == 5


def test_perf_stats_known_drawdown():
    series = s([("2026-01-05", 100.0), ("2026-01-06", 110.0), ("2026-01-07", 99.0), ("2026-01-08", 121.0)])
    st = analytics.perf_stats(series)
    assert st["max_drawdown"] == pytest.approx(99.0 / 110.0 - 1, rel=1e-12)  # -0.1
    assert st["total_return"] == pytest.approx(0.21, rel=1e-12)


def test_perf_stats_degenerate():
    assert analytics.perf_stats({})["total_return"] == 0.0
    one = analytics.perf_stats({"2026-01-05": 100.0})
    assert one["annual_return"] is None and one["n_days"] == 1


def test_equity_curve_rebased_and_drawdown():
    series = s([("2026-01-05", 100.0), ("2026-01-06", 110.0), ("2026-01-07", 99.0)])
    bench = s([("2026-01-05", 4000.0), ("2026-01-07", 4400.0)])  # 06 缺数 → carry
    eq = analytics.equity_curve(series, bench)
    assert eq["dates"] == ["2026-01-05", "2026-01-06", "2026-01-07"]
    assert eq["strategy"] == [1.0, 1.1, 0.99]
    assert eq["benchmark"] == [1.0, 1.0, 1.1]  # 缺数日 forward-fill
    assert eq["drawdown"][2] == pytest.approx(0.99 / 1.1 - 1)
    assert analytics.equity_curve(series, None)["benchmark"] is None
```

- [ ] **Step 1.2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_analytics.py -q`
Expected: FAIL（`No module named 'vortex_backtest.analytics'`）

- [ ] **Step 1.3: 实现**

```python
"""分析/报告纯函数层（spec 2026-06-12 §3/§5）。

输入输出全为 python 基本类型，不读文件不碰网络——指标口径的单一真值源，金标可测。
序列约定：``series = {"YYYY-MM-DD": value}``（日频净值/收盘）。
"""
from __future__ import annotations

import math
from datetime import date
from typing import Any, Mapping

TRADING_DAYS = 252
LOW_CONFIDENCE_DAYS = 60


def _sorted_items(series: Mapping[str, float]) -> tuple[list[str], list[float]]:
    dates = sorted(series)
    return dates, [float(series[d]) for d in dates]


def _returns(values: list[float]) -> list[float]:
    return [values[i] / values[i - 1] - 1 for i in range(1, len(values)) if values[i - 1]]


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _std(xs: list[float]) -> float:  # 样本标准差（ddof=1）
    n = len(xs)
    if n < 2:
        return 0.0
    m = _mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1))


def _max_drawdown(values: list[float]) -> float:
    hw, mdd = float("-inf"), 0.0
    for v in values:
        hw = max(hw, v)
        if hw > 0:
            mdd = min(mdd, v / hw - 1)
    return mdd


def perf_stats(series: Mapping[str, float], *, rf: float = 0.0) -> dict[str, Any]:
    """绝对类指标包。不足 2 个点 → 比率类置 None（不伪装）。"""
    _, values = _sorted_items(series)
    if len(values) < 2:
        return {"total_return": 0.0, "annual_return": None, "sharpe": None,
                "volatility": None, "max_drawdown": 0.0, "win_days_ratio": None,
                "n_days": len(values)}
    rets = _returns(values)
    n = len(rets)
    tr = values[-1] / values[0] - 1 if values[0] else 0.0
    std = _std(rets)
    return {
        "total_return": tr,
        "annual_return": (1 + tr) ** (TRADING_DAYS / n) - 1 if n else None,
        "sharpe": ((_mean(rets) - rf / TRADING_DAYS) / std) * math.sqrt(TRADING_DAYS) if std > 0 else None,
        "volatility": std * math.sqrt(TRADING_DAYS),
        "max_drawdown": _max_drawdown(values),
        "win_days_ratio": sum(1 for r in rets if r > 0) / n if n else None,
        "n_days": len(values),
    }


def equity_curve(series: Mapping[str, float], benchmark: Mapping[str, float] | None = None) -> dict[str, Any]:
    """起点 1.0 的对齐净值曲线 + 逐日回撤。基准对齐到策略日期轴，缺数日 forward-fill。"""
    dates, values = _sorted_items(series)
    if not values:
        return {"dates": [], "strategy": [], "benchmark": None, "drawdown": []}
    base = values[0] or 1.0
    strat = [round(v / base, 6) for v in values]
    hw, dd = float("-inf"), []
    for v in strat:
        hw = max(hw, v)
        dd.append(round(v / hw - 1, 6) if hw > 0 else 0.0)
    bench = None
    if benchmark:
        bbase, carry, bench = None, None, []
        for d in dates:
            if d in benchmark:
                carry = float(benchmark[d])
                if bbase is None:
                    bbase = carry or 1.0
            bench.append(round(carry / bbase, 6) if carry is not None and bbase else None)
    return {"dates": dates, "strategy": strat, "benchmark": bench, "drawdown": dd}
```

- [ ] **Step 1.4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_analytics.py -q`
Expected: 4 passed

- [ ] **Step 1.5: 提交**

```bash
git add vortex_backtest/analytics.py tests/test_analytics.py
git commit -m "feat(backtest): analytics 基础指标层（perf_stats/equity_curve，金标测试）"
```

---

### Task 2: analytics 基准相对指标 + 年月切片 — TDD

**Files:** Modify `vortex_backtest/analytics.py`（追加）、Modify `tests/test_analytics.py`（追加）

- [ ] **Step 2.1: 追加失败测试**

```python
def test_relative_stats_beta2_alpha0():
    # 策略日收益恒为基准 2 倍 → beta=2、alpha=0、TE=std(rb)*sqrt(252)
    bench_vals, strat_vals = [100.0], [100.0]
    rb_seq = [0.01, -0.02, 0.015, 0.005]
    for r in rb_seq:
        bench_vals.append(bench_vals[-1] * (1 + r))
        strat_vals.append(strat_vals[-1] * (1 + 2 * r))
    dates = ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08", "2026-01-09"]
    strat = dict(zip(dates, strat_vals))
    bench = dict(zip(dates, bench_vals))
    rel = analytics.relative_stats(strat, bench)
    assert rel["beta"] == pytest.approx(2.0, rel=1e-9)
    assert rel["alpha"] == pytest.approx(0.0, abs=1e-9)
    rs = [2 * r for r in rb_seq]
    diff = [a - b for a, b in zip(rs, rb_seq)]
    m = sum(diff) / len(diff)
    te = math.sqrt(sum((x - m) ** 2 for x in diff) / (len(diff) - 1)) * math.sqrt(252)
    assert rel["tracking_error"] == pytest.approx(te, rel=1e-9)
    assert rel["excess_return"] == pytest.approx(
        (strat_vals[-1] / strat_vals[0] - 1) - (bench_vals[-1] / bench_vals[0] - 1), rel=1e-9)


def test_relative_stats_insufficient_overlap():
    rel = analytics.relative_stats({"2026-01-05": 1.0}, {"2026-01-06": 1.0})
    assert rel["beta"] is None and rel["information_ratio"] is None


def test_period_stats_monthly_split():
    # 1 月两天 +1%/日，2 月一天 -2%：月收益分别 ≈2.01% 与 -2%
    strat = s([("2026-01-29", 100.0), ("2026-01-30", 101.0), ("2026-02-02", 102.01), ("2026-02-03", 99.9698)])
    rows = analytics.period_stats(strat, None, freq="M")
    assert [r["period"] for r in rows] == ["2026-01", "2026-02"]
    assert rows[0]["strategy_return"] == pytest.approx(0.01, rel=1e-9)        # 01-30 一笔日收益
    # 02 月：r(02-02)=1%、r(02-03)=-2% → (1.01*0.98)-1
    assert rows[1]["strategy_return"] == pytest.approx(1.01 * 0.98 - 1, rel=1e-9)
    assert rows[1]["benchmark_return"] is None and rows[1]["max_drawdown"] <= 0
```

- [ ] **Step 2.2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_analytics.py -q`
Expected: 新增 3 个 FAIL（`relative_stats`/`period_stats` 未定义），原 4 个 PASS

- [ ] **Step 2.3: 实现（追加到 analytics.py）**

```python
def relative_stats(strategy: Mapping[str, float], benchmark: Mapping[str, float],
                   *, rf: float = 0.0) -> dict[str, Any]:
    """相对类指标：对齐两序列共同日期后做日收益 OLS/超额。重叠 < 3 点 → 全 None。"""
    common = sorted(set(strategy) & set(benchmark))
    none = {"excess_return": None, "information_ratio": None, "beta": None,
            "alpha": None, "tracking_error": None}
    if len(common) < 3:
        return none
    sv = [float(strategy[d]) for d in common]
    bv = [float(benchmark[d]) for d in common]
    rs, rb = _returns(sv), _returns(bv)
    if len(rs) != len(rb) or len(rs) < 2:
        return none
    n = len(rs)
    ms, mb = _mean(rs), _mean(rb)
    var_b = sum((b - mb) ** 2 for b in rb) / (n - 1)
    cov = sum((a - ms) * (b - mb) for a, b in zip(rs, rb)) / (n - 1)
    beta = cov / var_b if var_b > 0 else None
    alpha = ((ms - rf / TRADING_DAYS) - beta * (mb - rf / TRADING_DAYS)) * TRADING_DAYS if beta is not None else None
    diff = [a - b for a, b in zip(rs, rb)]
    te = _std(diff) * math.sqrt(TRADING_DAYS)
    return {
        "excess_return": (sv[-1] / sv[0] - 1) - (bv[-1] / bv[0] - 1) if sv[0] and bv[0] else None,
        "information_ratio": (_mean(diff) * TRADING_DAYS) / te if te > 0 else None,
        "beta": beta,
        "alpha": alpha,
        "tracking_error": te,
    }


def _period_returns(series: Mapping[str, float], keylen: int) -> dict[str, list[float]]:
    """日收益按期间分桶：r_i 归属其结束日所在期间（key = date[:keylen]）。"""
    dates, values = _sorted_items(series)
    out: dict[str, list[float]] = {}
    for i in range(1, len(values)):
        if values[i - 1]:
            out.setdefault(dates[i][:keylen], []).append(values[i] / values[i - 1] - 1)
    return out


def period_stats(strategy: Mapping[str, float], benchmark: Mapping[str, float] | None,
                 *, freq: str = "Y", rf: float = 0.0) -> list[dict[str, Any]]:
    """年度(freq='Y')/月度(freq='M')统计：组收益=组内日收益连乘；回撤/波动/夏普按组内序列。"""
    keylen = 4 if freq == "Y" else 7
    sbuckets = _period_returns(strategy, keylen)
    bbuckets = _period_returns(benchmark, keylen) if benchmark else {}
    dates, values = _sorted_items(strategy)
    rows: list[dict[str, Any]] = []
    for period in sorted(sbuckets):
        rets = sbuckets[period]
        cum = 1.0
        for r in rets:
            cum *= 1 + r
        sub_vals = [values[i] for i in range(len(values)) if dates[i][:keylen] == period]
        std = _std(rets)
        brets = bbuckets.get(period)
        bret = None
        if brets:
            bcum = 1.0
            for r in brets:
                bcum *= 1 + r
            bret = bcum - 1
        rows.append({
            "period": period,
            "strategy_return": cum - 1,
            "benchmark_return": bret,
            "excess": (cum - 1 - bret) if bret is not None else None,
            "max_drawdown": _max_drawdown(sub_vals),
            "volatility": std * math.sqrt(TRADING_DAYS) if len(rets) >= 2 else None,
            "sharpe": ((_mean(rets) - rf / TRADING_DAYS) / std) * math.sqrt(TRADING_DAYS) if std > 0 else None,
        })
    return rows
```

- [ ] **Step 2.4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_analytics.py -q`
Expected: 7 passed

- [ ] **Step 2.5: 提交**

```bash
git add vortex_backtest/analytics.py tests/test_analytics.py
git commit -m "feat(backtest): analytics 相对指标(beta/alpha/IR/TE) + 年度月度切片"
```

---

### Task 3: analytics 持仓粒度切片（weight/daily/weekly/hourly/minute）— TDD

**Files:** Modify `vortex_backtest/analytics.py`（追加）、Modify `tests/test_analytics.py`（追加）

- [ ] **Step 3.1: 追加失败测试**

```python
def _mk_pos(symbol, qty, mv):
    return {"strategy_id": "t", "symbol": symbol, "quantity": qty, "available_quantity": qty,
            "cost_basis": 10.0, "last_price": mv / qty if qty else 0, "market_value": mv,
            "unrealized_pnl": 0.0, "unrealized_pnl_ratio": 0.0}


def _mk_daily(trade_date, cash, mv, positions):
    return {"strategy_id": "t", "trade_date": trade_date, "cash": cash, "market_value": mv,
            "total_value": cash + mv, "daily_pnl": 0, "total_return": 0, "drawdown": 0,
            "positions": positions, "trades": [], "rejections": []}


def _mk_snap(ts, cash, mv, positions):
    return {"strategy_id": "t", "timestamp": ts, "frequency": "1min", "cash": cash,
            "market_value": mv, "total_value": cash + mv, "positions": positions,
            "trades": [], "rejections": []}


def test_daily_views_weight():
    rows = [_mk_daily("2026-02-03", 900.0, 100.0, [_mk_pos("000001.SZ", 10, 100.0)])]
    views = analytics.daily_views(rows)
    assert views[0]["timestamp"] == "2026-02-03"
    assert views[0]["positions"][0]["weight"] == pytest.approx(0.1)


def test_weekly_views_last_trading_day_of_week():
    # 02-03(周二)/02-06(周五) 同属 2026-W06；02-09(周一) 属 W07 → 每周留最后一行
    rows = [_mk_daily("2026-02-03", 1000, 0, []), _mk_daily("2026-02-06", 1100, 0, []),
            _mk_daily("2026-02-09", 1200, 0, [])]
    views = analytics.weekly_views(rows)
    assert [(v["week"], v["timestamp"]) for v in views] == [("2026-W06", "2026-02-06"), ("2026-W07", "2026-02-09")]


def test_hourly_views_buckets():
    snaps = [_mk_snap("2026-02-03T09:31:00", 1, 0, []), _mk_snap("2026-02-03T10:30:00", 2, 0, []),
             _mk_snap("2026-02-03T11:29:00", 3, 0, []), _mk_snap("2026-02-03T14:55:00", 4, 0, []),
             _mk_snap("2026-02-03T15:00:00", 5, 0, [])]
    views = analytics.hourly_views(snaps)
    # (≤10:30]=cash2、(10:30,11:30]=cash3、(14:00,15:00] 桶内最后一根=15:00 的 cash5
    assert [(v["timestamp"], v["cash"]) for v in views] == [
        ("2026-02-03T10:30:00", 2), ("2026-02-03T11:30:00", 3), ("2026-02-03T15:00:00", 5)]


def test_minute_views_passthrough():
    snaps = [_mk_snap("2026-02-03T09:31:00", 990.0, 10.0, [_mk_pos("000001.SZ", 1, 10.0)])]
    v = analytics.minute_views(snaps)
    assert v[0]["timestamp"] == "2026-02-03T09:31:00" and v[0]["positions"][0]["weight"] == pytest.approx(0.01)
```

- [ ] **Step 3.2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_analytics.py -q`
Expected: 新增 4 个 FAIL，已有 7 个 PASS

- [ ] **Step 3.3: 实现（追加到 analytics.py）**

```python
def _snapshot_view(row: Mapping[str, Any], *, timestamp: str) -> dict[str, Any]:
    """统一持仓快照视图：补每标的 weight（市值/总资产）。"""
    total = float(row.get("total_value") or 0.0)
    positions = []
    for p in row.get("positions") or []:
        q = dict(p)
        q["weight"] = round(float(p["market_value"]) / total, 6) if total else None
        positions.append(q)
    return {"timestamp": timestamp, "cash": row.get("cash"),
            "market_value": row.get("market_value"), "total_value": row.get("total_value"),
            "positions": positions}


def daily_views(daily_rows: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [_snapshot_view(r, timestamp=str(r["trade_date"])) for r in daily_rows]


def weekly_views(daily_rows: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """每 ISO 周最后一个交易日的 EOD 行（输入按 trade_date 升序）。"""
    by_week: dict[str, Mapping[str, Any]] = {}
    for r in daily_rows:
        d = date.fromisoformat(str(r["trade_date"]))
        iso = d.isocalendar()
        by_week[f"{iso.year}-W{iso.week:02d}"] = r  # 升序输入 → 留每周最后一行
    return [dict(_snapshot_view(r, timestamp=str(r["trade_date"])), week=wk)
            for wk, r in sorted(by_week.items())]


_HOUR_BUCKETS = ("10:30:00", "11:30:00", "14:00:00", "15:00:00")


def hourly_views(snapshot_rows: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """每 (交易日, 小时档) 取该档窗口内最后一根 bar：(≤10:30]、(10:30,11:30]、(13:00,14:00]、(14:00,15:00]。"""
    out: dict[tuple[str, str], Mapping[str, Any]] = {}
    for r in snapshot_rows:  # 升序
        ts = str(r["timestamp"])
        d, t = ts[:10], ts[11:19]
        for i, b in enumerate(_HOUR_BUCKETS):
            lo = _HOUR_BUCKETS[i - 1] if i else "00:00:00"
            if lo < t <= b:
                out[(d, b)] = r
                break
    return [_snapshot_view(r, timestamp=f"{d}T{b}") for (d, b), r in sorted(out.items())]


def minute_views(snapshot_rows: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [_snapshot_view(r, timestamp=str(r["timestamp"])) for r in snapshot_rows]
```

- [ ] **Step 3.4: 跑测试确认通过** — Run: `.venv/bin/python -m pytest tests/test_analytics.py -q`，Expected: 11 passed

- [ ] **Step 3.5: 提交**

```bash
git add vortex_backtest/analytics.py tests/test_analytics.py
git commit -m "feat(backtest): analytics 持仓粒度切片(日/周/时/分 + weight)"
```

---

### Task 4: analytics 调仓事件（rebalance_events）— TDD

**Files:** Modify `vortex_backtest/analytics.py`（追加）、Modify `tests/test_analytics.py`（追加）

- [ ] **Step 4.1: 追加失败测试**

```python
def test_rebalance_events_diff_and_aggregation():
    daily = [
        _mk_daily("2026-02-02", 1000.0, 0.0, []),
        _mk_daily("2026-02-03", 890.0, 110.0, [_mk_pos("000001.SZ", 10, 110.0)]),
        _mk_daily("2026-02-04", 1002.0, 0.0, []),
    ]
    trades = [
        {"trade_date": "2026-02-03", "symbol": "000001.SZ", "side": 1, "quantity": 6,
         "amount": 60.0, "commission": 5.0, "stamp_tax": 0.0, "transfer_fee": 0.01, "realized_pnl": 0.0},
        {"trade_date": "2026-02-03", "symbol": "000001.SZ", "side": 1, "quantity": 4,
         "amount": 44.0, "commission": 5.0, "stamp_tax": 0.0, "transfer_fee": 0.01, "realized_pnl": 0.0},
        {"trade_date": "2026-02-04", "symbol": "000001.SZ", "side": 2, "quantity": 10,
         "amount": 112.0, "commission": 5.0, "stamp_tax": 0.056, "transfer_fee": 0.01, "realized_pnl": 2.0},
    ]
    events = analytics.rebalance_events(trades, daily)
    assert [e["trade_date"] for e in events] == ["2026-02-03", "2026-02-04"]
    buy_day = events[0]
    assert buy_day["n_trades"] == 2 and buy_day["sells"] == []
    assert buy_day["buys"] == [{"symbol": "000001.SZ", "quantity": 10, "amount": 104.0,
                                "avg_price": pytest.approx(10.4)}]
    assert buy_day["fees_total"] == pytest.approx(10.02)
    assert buy_day["position_diff"] == [{"symbol": "000001.SZ", "qty_before": 0, "qty_after": 10,
                                         "weight_before": 0.0, "weight_after": pytest.approx(0.11)}]
    assert buy_day["cash_after"] == 890.0 and buy_day["total_value_after"] == 1000.0
    sell_day = events[1]
    assert sell_day["position_diff"][0]["qty_after"] == 0
    assert sell_day["realized_pnl_total"] == pytest.approx(2.0)
```

- [ ] **Step 4.2: 确认失败** — Run: `.venv/bin/python -m pytest tests/test_analytics.py -q`，Expected: 1 FAIL / 11 PASS

- [ ] **Step 4.3: 实现（追加到 analytics.py）**

```python
def rebalance_events(trades: list[Mapping[str, Any]],
                     daily_rows: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """按 trade_date 聚合成交为调仓事件，附调仓前后持仓 diff（qty/weight）。

    before = 前一交易日 EOD（日历轴上首日 before=空仓）；after = 当日 EOD。
    """
    axis = [str(r["trade_date"]) for r in daily_rows]
    by_date = {str(r["trade_date"]): r for r in daily_rows}

    def eod(d: str | None) -> dict[str, tuple[int, float]]:
        row = by_date.get(d) if d else None
        if not row:
            return {}
        total = float(row.get("total_value") or 0.0)
        return {str(p["symbol"]): (int(p["quantity"]),
                                   float(p["market_value"]) / total if total else 0.0)
                for p in row.get("positions") or []}

    by_day: dict[str, list[Mapping[str, Any]]] = {}
    for t in trades:
        by_day.setdefault(str(t["trade_date"]), []).append(t)

    events: list[dict[str, Any]] = []
    for day in sorted(by_day):
        rows = by_day[day]

        def side_agg(side: int) -> list[dict[str, Any]]:
            agg: dict[str, dict[str, Any]] = {}
            for t in rows:
                if int(t["side"]) != side:
                    continue
                a = agg.setdefault(str(t["symbol"]), {"symbol": str(t["symbol"]), "quantity": 0, "amount": 0.0})
                a["quantity"] += int(t["quantity"])
                a["amount"] += float(t["amount"])
            return [dict(a, avg_price=round(a["amount"] / a["quantity"], 4) if a["quantity"] else None)
                    for _, a in sorted(agg.items())]

        idx = axis.index(day) if day in axis else -1
        before = eod(axis[idx - 1]) if idx > 0 else {}
        after = eod(day)
        touched = {str(t["symbol"]) for t in rows}
        touched |= {s for s in set(before) | set(after)
                    if before.get(s, (0, 0.0))[0] != after.get(s, (0, 0.0))[0]}
        diff = [{"symbol": s,
                 "qty_before": before.get(s, (0, 0.0))[0], "qty_after": after.get(s, (0, 0.0))[0],
                 "weight_before": round(before.get(s, (0, 0.0))[1], 6),
                 "weight_after": round(after.get(s, (0, 0.0))[1], 6)}
                for s in sorted(touched)]
        day_row = by_date.get(day) or {}
        events.append({
            "trade_date": day, "n_trades": len(rows),
            "buys": side_agg(1), "sells": side_agg(2),
            "fees_total": round(sum(float(t.get("commission", 0)) + float(t.get("stamp_tax", 0))
                                    + float(t.get("transfer_fee", 0)) for t in rows), 4),
            "realized_pnl_total": round(sum(float(t.get("realized_pnl", 0)) for t in rows), 4),
            "position_diff": diff,
            "cash_after": day_row.get("cash"), "total_value_after": day_row.get("total_value"),
        })
    return events
```

- [ ] **Step 4.4: 确认通过** — Run: `.venv/bin/python -m pytest tests/test_analytics.py -q`，Expected: 12 passed

- [ ] **Step 4.5: 提交**

```bash
git add vortex_backtest/analytics.py tests/test_analytics.py
git commit -m "feat(backtest): analytics 调仓事件聚合(买卖汇总+前后持仓diff)"
```

---

### Task 5: benchmark.py 基准直读 — TDD

**Files:** Create `vortex_backtest/benchmark.py`、Create `tests/test_benchmark.py`

- [ ] **Step 5.1: 写失败测试**

```python
"""benchmark：fixture parquet 验证代码裁剪/窗口/名称/缺数。"""
import pandas as pd
import pytest

from vortex_backtest import benchmark


@pytest.fixture
def ws(tmp_path):
    idx = tmp_path / "data" / "index_daily" / "date=20260203"
    idx.mkdir(parents=True)
    pd.DataFrame({
        "symbol": ["000300.SH", "000300.SH", "000905.SH"],
        "date": ["20260203", "20260204", "20260203"],
        "close": [4100.0, 4150.0, 6000.0],
    }).to_parquet(idx / "data.parquet")
    sw = tmp_path / "data" / "sw_daily" / "date=20260203"
    sw.mkdir(parents=True)
    pd.DataFrame({
        "symbol": ["801120.SI"], "date": ["20260203"], "name": ["食品饮料"], "close": [8000.0],
    }).to_parquet(sw / "data.parquet")
    return tmp_path


def test_load_series_window_and_name(ws):
    series, name = benchmark.load_series("000300.SH", 20260203, 20260203, workspace=ws)
    assert series == {"2026-02-03": 4100.0}        # 窗口裁掉 0204
    assert name == "沪深300"                        # 常用指数名映射
    sw_series, sw_name = benchmark.load_series("801120.SI", 20260101, 20261231, workspace=ws)
    assert sw_series == {"2026-02-03": 8000.0} and sw_name == "食品饮料"   # sw_daily 带 name 列


def test_load_series_missing_code(ws):
    series, name = benchmark.load_series("999999.XX", 20260101, 20261231, workspace=ws)
    assert series == {} and name == "999999.XX"


def test_list_benchmarks(ws):
    items = benchmark.list_benchmarks(workspace=ws)
    codes = {i["code"]: i for i in items}
    assert codes["000300.SH"]["name"] == "沪深300" and codes["000300.SH"]["source"] == "index_daily"
    assert codes["801120.SI"]["name"] == "食品饮料" and codes["801120.SI"]["source"] == "sw_daily"
    assert "999999.XX" not in codes
```

- [ ] **Step 5.2: 确认失败** — Run: `.venv/bin/python -m pytest tests/test_benchmark.py -q`，Expected: FAIL（模块不存在）

- [ ] **Step 5.3: 实现**

```python
"""基准序列直读（spec 2026-06-12 §3/§4.5）：workspace `index_daily`/`sw_daily` 收盘序列与目录。

与 data_adapter 同款本地直读模式（pyarrow via pandas），只读不写。
"""
from __future__ import annotations

import os
from pathlib import Path

import pandas as pd

from .data_adapter import DEFAULT_WORKSPACE

_SOURCES = ("index_daily", "sw_daily")
# index_daily 无名称列 → 常用指数静态名映射（找不到回代码本身）
_COMMON_INDEX_NAMES = {
    "000300.SH": "沪深300", "000001.SH": "上证指数", "000905.SH": "中证500",
    "000016.SH": "上证50", "000852.SH": "中证1000", "399001.SZ": "深证成指",
    "399006.SZ": "创业板指",
}


def _workspace(workspace: Path | None) -> Path:
    return Path(workspace) if workspace else Path(os.getenv("VORTEX_WORKSPACE", str(DEFAULT_WORKSPACE)))


def _read(dataset: str, workspace: Path | None) -> pd.DataFrame:
    root = _workspace(workspace) / "data" / dataset
    files = sorted(root.rglob("*.parquet")) if root.exists() else []
    if not files:
        return pd.DataFrame()
    return pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)


def load_series(code: str, start_key: int, end_key: int, *,
                workspace: Path | None = None) -> tuple[dict[str, float], str]:
    """→ ({iso_date: close}, 名称)。两源依次找；都无该代码 → ({}, 静态名或代码)。"""
    for ds in _SOURCES:
        df = _read(ds, workspace)
        if df.empty or not {"symbol", "date", "close"}.issubset(df.columns):
            continue
        df = df[df["symbol"] == code].copy()
        if df.empty:
            continue
        df["_d"] = df["date"].astype(str).str[:8].astype(int)
        df = df[(df["_d"] >= start_key) & (df["_d"] <= end_key)].sort_values("_d")
        series = {f"{str(k)[:4]}-{str(k)[4:6]}-{str(k)[6:8]}": float(c)
                  for k, c in zip(df["_d"], df["close"])}
        name = (str(df["name"].iloc[0]) if "name" in df.columns and len(df)
                else _COMMON_INDEX_NAMES.get(code, code))
        return series, name
    return {}, _COMMON_INDEX_NAMES.get(code, code)


def list_benchmarks(*, workspace: Path | None = None) -> list[dict[str, str]]:
    """基准目录：index_daily 中存在的常用指数（静态名）+ sw_daily 全量（自带名称列）。"""
    out: list[dict[str, str]] = []
    idx = _read("index_daily", workspace)
    have = set(idx["symbol"].unique()) if not idx.empty and "symbol" in idx.columns else set()
    for code, name in _COMMON_INDEX_NAMES.items():
        if code in have:
            out.append({"code": code, "name": name, "source": "index_daily"})
    sw = _read("sw_daily", workspace)
    if not sw.empty and "symbol" in sw.columns:
        names = sw.groupby("symbol")["name"].first().to_dict() if "name" in sw.columns else {}
        for code in sorted(sw["symbol"].astype(str).unique()):
            out.append({"code": code, "name": str(names.get(code, code)), "source": "sw_daily"})
    return out
```

- [ ] **Step 5.4: 确认通过** — Run: `.venv/bin/python -m pytest tests/test_benchmark.py -q`，Expected: 3 passed

- [ ] **Step 5.5: 提交**

```bash
git add vortex_backtest/benchmark.py tests/test_benchmark.py
git commit -m "feat(backtest): benchmark 基准序列直读(index_daily/sw_daily)+目录"
```

---

### Task 6: app.py 五个只读端点 — TDD

**Files:** Modify `vortex_backtest/app.py`（在 `session_data` 端点之后、`# 托管只读看板` 注释之前插入）、Create `tests/test_report_api.py`

- [ ] **Step 6.1: 写失败测试（完整文件）**

```python
"""分析/报告端点 API 测试：手工构造 state（store 行 + JSONL 产物）+ fixture 基准 workspace。"""
import json

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from vortex_backtest.app import create_app
from vortex_backtest.models import AccountCreate
from vortex_backtest.store import DataStore

SID = "rpt-test-session"


@pytest.fixture
def client(tmp_path, monkeypatch):
    # fixture 基准 workspace（同 test_benchmark 形状）
    idx = tmp_path / "ws" / "data" / "index_daily" / "date=20260203"
    idx.mkdir(parents=True)
    pd.DataFrame({"symbol": ["000300.SH"] * 3,
                  "date": ["20260203", "20260204", "20260205"],
                  "close": [4000.0, 4040.0, 4000.4]}).to_parquet(idx / "data.parquet")
    monkeypatch.setenv("VORTEX_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.delenv("VORTEX_DATA_URL", raising=False)
    monkeypatch.setenv("VORTEX_BACKTEST_HOST", "127.0.0.1")

    state = tmp_path / "state"
    store = DataStore(state)
    store.create_account(AccountCreate(account_id="acct", initial_cash=1000.0))
    store.create_session(session_id=SID, account_id="acct", level="1min",
                         start_date="2026-02-03", end_date="2026-02-05", sim_time=None,
                         initial_cash=1000.0, universe=["000001.SZ"], config={"strategy_id": "t"})

    def pos(qty, mv):
        return [{"strategy_id": "t", "symbol": "000001.SZ", "quantity": qty,
                 "available_quantity": qty, "cost_basis": 10.0,
                 "last_price": mv / qty if qty else 0.0, "market_value": mv,
                 "unrealized_pnl": 0.0, "unrealized_pnl_ratio": 0.0}] if qty else []

    sdir = state / "reports" / "sessions" / SID
    sdir.mkdir(parents=True)
    snaps = [
        {"strategy_id": "t", "timestamp": "2026-02-03T09:31:00", "frequency": "1min",
         "cash": 900.0, "market_value": 100.0, "total_value": 1000.0,
         "positions": pos(10, 100.0), "trades": [], "rejections": []},
        {"strategy_id": "t", "timestamp": "2026-02-03T15:00:00", "frequency": "1min",
         "cash": 900.0, "market_value": 110.0, "total_value": 1010.0,
         "positions": pos(10, 110.0), "trades": [], "rejections": []},
        {"strategy_id": "t", "timestamp": "2026-02-04T15:00:00", "frequency": "1min",
         "cash": 1012.0, "market_value": 0.0, "total_value": 1012.0,
         "positions": [], "trades": [], "rejections": []},
    ]
    trades = [
        {"strategy_id": "t", "trade_id": "t-1", "request_id": "b1", "trade_date": "2026-02-03",
         "symbol": "000001.SZ", "side": 1, "side_name": "BUY", "requested_quantity": 10,
         "quantity": 10, "price": 10.0, "amount": 100.0, "commission": 5.0,
         "stamp_tax": 0.0, "transfer_fee": 0.001, "realized_pnl": 0.0, "cash_after": 900.0},
        {"strategy_id": "t", "trade_id": "t-2", "request_id": "s1", "trade_date": "2026-02-04",
         "symbol": "000001.SZ", "side": 2, "side_name": "SELL", "requested_quantity": 10,
         "quantity": 10, "price": 11.2, "amount": 112.0, "commission": 5.0,
         "stamp_tax": 0.056, "transfer_fee": 0.001, "realized_pnl": 2.0, "cash_after": 1012.0},
    ]
    with open(sdir / "snapshots.jsonl", "w") as fh:
        for r in snaps:
            fh.write(json.dumps(r) + "\n")
    with open(sdir / "trades.jsonl", "w") as fh:
        for r in trades:
            fh.write(json.dumps(r) + "\n")
    with open(sdir / "calendar.jsonl", "w") as fh:
        for d in (20260203, 20260204, 20260205):
            fh.write(json.dumps({"d": d}) + "\n")
    return TestClient(create_app(state_dir=state))


def test_metrics_shape_and_benchmark(client):
    r = client.get(f"/sessions/{SID}/metrics?benchmark=000300.SH")
    assert r.status_code == 200
    m = r.json()
    assert m["benchmark_name"] == "沪深300" and m["low_confidence"] is True
    assert m["strategy"]["n_days"] == 3 and m["strategy"]["total_return"] == pytest.approx(0.012)
    assert m["benchmark_stats"]["total_return"] == pytest.approx(4000.4 / 4000.0 - 1, rel=1e-9)
    assert m["relative"]["beta"] is not None
    assert m["annual"][0]["period"] == "2026" and m["monthly"][0]["period"] == "2026-02"


def test_metrics_benchmark_missing_degrades(client):
    m = client.get(f"/sessions/{SID}/metrics?benchmark=NOPE.XX").json()
    assert m["benchmark_stats"] is None and m["relative"] is None
    assert m["error"] == "benchmark_data_missing"
    assert m["strategy"]["total_return"] == pytest.approx(0.012)   # 绝对类照常


def test_equity_curve(client):
    eq = client.get(f"/sessions/{SID}/equity?benchmark=000300.SH").json()
    assert eq["dates"] == ["2026-02-03", "2026-02-04", "2026-02-05"]
    assert eq["strategy"][0] == 1.0 and eq["benchmark"][0] == 1.0
    assert len(eq["drawdown"]) == 3


def test_positions_granularities(client):
    daily = client.get(f"/sessions/{SID}/positions?granularity=daily").json()
    assert [d["timestamp"] for d in daily] == ["2026-02-03", "2026-02-04", "2026-02-05"]
    assert daily[0]["positions"][0]["weight"] == pytest.approx(110.0 / 1010.0)  # EOD
    weekly = client.get(f"/sessions/{SID}/positions?granularity=weekly").json()
    assert len(weekly) == 1 and weekly[0]["week"].startswith("2026-W")
    hourly = client.get(f"/sessions/{SID}/positions?granularity=hourly").json()
    assert [h["timestamp"] for h in hourly] == [
        "2026-02-03T10:30:00", "2026-02-03T15:00:00", "2026-02-04T15:00:00"]
    assert client.get(f"/sessions/{SID}/positions?granularity=minute").status_code == 422
    minute = client.get(f"/sessions/{SID}/positions?granularity=minute&date=2026-02-03").json()
    assert len(minute) == 2 and minute[0]["timestamp"] == "2026-02-03T09:31:00"
    assert client.get(f"/sessions/{SID}/positions?granularity=nope").status_code == 422


def test_rebalances(client):
    ev = client.get(f"/sessions/{SID}/rebalances").json()
    assert [e["trade_date"] for e in ev] == ["2026-02-03", "2026-02-04"]
    assert ev[0]["buys"][0]["avg_price"] == pytest.approx(10.0)
    assert ev[0]["position_diff"][0]["qty_before"] == 0
    assert ev[0]["position_diff"][0]["qty_after"] == 10
    assert ev[1]["position_diff"][0]["qty_after"] == 0


def test_benchmarks_catalog(client):
    items = client.get("/benchmarks").json()
    assert {"code": "000300.SH", "name": "沪深300", "source": "index_daily"} in items
```

- [ ] **Step 6.2: 确认失败** — Run: `.venv/bin/python -m pytest tests/test_report_api.py -q`，Expected: 404 类 FAIL（端点不存在）

- [ ] **Step 6.3: 实现（app.py 插入；位置：`session_data` 端点定义之后、`# 托管只读看板` 注释之前）**

```python
    # ------------------------------------------------------------------
    # 分析/报告层（spec 2026-06-12：metrics/equity/positions/rebalances/benchmarks）
    # ------------------------------------------------------------------
    from . import analytics
    from .benchmark import list_benchmarks as _list_benchmarks
    from .benchmark import load_series as _load_benchmark

    def _daily_rows(data_store: DataStore, session_id: str) -> list[dict]:
        return _session_summary(data_store, session_id).get("daily", [])

    def _strategy_series(daily_rows: list[dict]) -> dict[str, float]:
        return {str(r["trade_date"]): float(r["total_value"]) for r in daily_rows}

    def _benchmark_for(row: dict, code: str) -> tuple[dict[str, float], str]:
        start = int(str(row["start_date"]).replace("-", "")) if row.get("start_date") else 0
        end = int(str(row["end_date"]).replace("-", "")) if row.get("end_date") else 99999999
        return _load_benchmark(code, start, end)

    @app.get("/sessions/{session_id}/metrics")
    def session_metrics(
        session_id: str,
        benchmark: str = Query(default="000300.SH"),
        rf: float = Query(default=0.0, ge=0.0, le=1.0),
        data_store: DataStore = Depends(get_store),
    ) -> dict:
        row = _session_or_404(data_store, session_id)
        strat = _strategy_series(_daily_rows(data_store, session_id))
        bench, bench_name = _benchmark_for(row, benchmark)
        out = {
            "benchmark": benchmark,
            "benchmark_name": bench_name,
            "low_confidence": len(strat) < analytics.LOW_CONFIDENCE_DAYS,
            "strategy": analytics.perf_stats(strat, rf=rf),
            "benchmark_stats": analytics.perf_stats(bench, rf=rf) if bench else None,
            "relative": analytics.relative_stats(strat, bench, rf=rf) if bench else None,
            "annual": analytics.period_stats(strat, bench or None, freq="Y", rf=rf),
            "monthly": analytics.period_stats(strat, bench or None, freq="M", rf=rf),
        }
        if not bench:
            out["error"] = "benchmark_data_missing"
        return out

    @app.get("/sessions/{session_id}/equity")
    def session_equity(
        session_id: str,
        benchmark: str = Query(default="000300.SH"),
        data_store: DataStore = Depends(get_store),
    ) -> dict:
        row = _session_or_404(data_store, session_id)
        strat = _strategy_series(_daily_rows(data_store, session_id))
        bench, _ = _benchmark_for(row, benchmark)
        return analytics.equity_curve(strat, bench or None)

    @app.get("/sessions/{session_id}/positions")
    def session_positions(
        session_id: str,
        granularity: str = Query(default="daily"),
        date: str | None = Query(default=None),
        week: str | None = Query(default=None),
        limit: int = Query(default=500, ge=1, le=5000),
        offset: int = Query(default=0, ge=0),
        data_store: DataStore = Depends(get_store),
    ) -> list[dict]:
        _session_or_404(data_store, session_id)
        if granularity not in ("daily", "weekly", "hourly", "minute"):
            raise HTTPException(status_code=422, detail={"error": "invalid_granularity"})
        if granularity in ("daily", "weekly"):
            rows = _daily_rows(data_store, session_id)
            views = analytics.daily_views(rows) if granularity == "daily" else analytics.weekly_views(rows)
            if week:
                views = [v for v in views if v.get("week") == week]
        else:
            if granularity == "minute" and not date:
                raise HTTPException(status_code=422, detail={"error": "date_required_for_minute"})
            snaps = _read_jsonl(_session_dir(data_store, session_id) / "snapshots.jsonl")
            if date:
                snaps = [s for s in snaps if str(s["timestamp"])[:10] == date]
            views = (analytics.minute_views(snaps) if granularity == "minute"
                     else analytics.hourly_views(snaps))
        if date and granularity in ("daily", "weekly"):
            views = [v for v in views if str(v["timestamp"])[:10] == date]
        return views[offset:offset + limit]

    @app.get("/sessions/{session_id}/rebalances")
    def session_rebalances(
        session_id: str, data_store: DataStore = Depends(get_store)
    ) -> list[dict]:
        _session_or_404(data_store, session_id)
        trades = _read_jsonl(_session_dir(data_store, session_id) / "trades.jsonl")
        return analytics.rebalance_events(trades, _daily_rows(data_store, session_id))

    @app.get("/benchmarks")
    def benchmarks() -> list[dict]:
        return _list_benchmarks()
```

- [ ] **Step 6.4: 确认通过 + 全量回归**

Run: `.venv/bin/python -m pytest tests/test_report_api.py -q && .venv/bin/python -m pytest -q`
Expected: 报告 API 6 passed；全量无回归（145+新增 passed）

- [ ] **Step 6.5: 提交**

```bash
git add vortex_backtest/app.py tests/test_report_api.py
git commit -m "feat(backtest): 5 个分析/报告只读端点(metrics/equity/positions/rebalances/benchmarks)"
```

---

### Task 7: 看板前端重写（两页，无 mock）

**Files:**
- Rewrite: `vortex_backtest/web/static/app.js`（整文件替换）
- Modify: `vortex_backtest/web/index.html`（仅 `app.js?v=5` → `?v=6`）
- Modify: `vortex_backtest/web/static/app.css`（文件末尾追加）

- [ ] **Step 7.1: app.css 末尾追加**

```css
/* ---- 报告看板（2026-06-12 重写）---- */
.error-banner{background:#fde8e8;color:#b91c1c;border:1px solid #f5c2c2;border-radius:8px;padding:12px 16px;margin:16px 0;font-size:14px}
.kpi-table td,.kpi-table th{text-align:right;padding:6px 12px}
.kpi-table td:first-child,.kpi-table th:first-child{text-align:left}
.section{margin:22px 0}
.section h2{font-size:15px;margin:0 0 10px}
.gran-switch button{margin-right:6px;padding:4px 10px;border:1px solid #d0d7de;border-radius:6px;background:#fff;cursor:pointer}
.gran-switch button.active{background:#0969da;color:#fff;border-color:#0969da}
.profit{color:#cf222e}.loss{color:#1a7f37}
details.rebalance{border:1px solid #d0d7de;border-radius:8px;padding:8px 12px;margin:8px 0}
details.rebalance summary{cursor:pointer;font-weight:600}
```

- [ ] **Step 7.2: app.js 整文件替换**

```javascript
(function () {
  'use strict';
  // 会话看板 v6（2026-06-12 重写）：只消费真实 sessions/分析端点，接口失败显式报错，无 mock。
  var app = document.getElementById('app');
  var crumbs = document.getElementById('crumbs');
  var charts = [];
  var state = { benchmark: '000300.SH', gran: 'daily', minuteDate: '', benchmarks: [] };

  function get(path) {
    return fetch(path).then(function (r) {
      if (!r.ok) { throw new Error('HTTP ' + r.status + ' ' + path); }
      return r.json();
    });
  }
  function fail(err) {
    app.innerHTML = '<div class="error-banner">后端不可达或数据缺失：' + esc(String(err && err.message || err)) +
      '<br>请确认服务在运行、会话存在；本看板不再展示演示数据。</div>';
  }
  function esc(s) { return String(s).replace(/[&<>]/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c]; }); }
  function pct(x) { return x == null ? '—' : ((x >= 0 ? '+' : '') + (x * 100).toFixed(2) + '%'); }
  function num(x, d) { return x == null ? '—' : Number(x).toFixed(d == null ? 2 : d); }
  function money(x) { return x == null ? '—' : Number(x).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 }); }
  function cls(x) { return x == null ? '' : (x >= 0 ? 'profit' : 'loss'); }
  function destroyCharts() { charts.forEach(function (c) { c.destroy(); }); charts = []; }

  window.addEventListener('hashchange', route);
  route();

  function route() {
    destroyCharts();
    var m = location.hash.match(/^#\/session\/([\w-]+)/);
    if (m) { renderDetail(m[1]); } else { renderList(); }
  }

  // ---------------- 列表页 ----------------
  function renderList() {
    crumbs.innerHTML = '会话列表';
    app.innerHTML = '<div class="section"><h2>回测会话</h2><div id="list">加载中…</div></div>';
    get('/sessions').then(function (rows) {
      if (!rows.length) {
        document.getElementById('list').innerHTML = '<p>暂无会话。用 scripts/backtest_roundtrip.sh 或 POST /sessions 跑一笔回测。</p>';
        return;
      }
      return Promise.all(rows.map(function (r) {
        return get('/sessions/' + r.session_id + '/summary').then(function (s) { return { row: r, sum: s }; });
      })).then(function (items) {
        var html = '<table class="kpi-table"><thead><tr><th>会话</th><th>账户</th><th>状态</th><th>区间</th>' +
          '<th>总收益</th><th>最大回撤</th><th>总资产</th><th>更新时间</th></tr></thead><tbody>';
        items.forEach(function (it) {
          var r = it.row, s = it.sum;
          html += '<tr><td><a href="#/session/' + r.session_id + '">' + esc(r.session_id.slice(0, 8)) + '…</a></td>' +
            '<td>' + esc(r.account_id) + '</td><td>' + esc(r.status) + '</td>' +
            '<td>' + esc(r.start_date || '') + ' ~ ' + esc(r.end_date || '') + '</td>' +
            '<td class="' + cls(s.total_return) + '">' + pct(s.total_return) + '</td>' +
            '<td>' + pct(s.max_drawdown) + '</td><td>' + money(s.total_value) + '</td>' +
            '<td>' + esc(String(r.updated_at || '').slice(0, 16).replace('T', ' ')) + '</td></tr>';
        });
        document.getElementById('list').innerHTML = html + '</tbody></table>';
      });
    }).catch(fail);
  }

  // ---------------- 详情页 ----------------
  function renderDetail(sid) {
    crumbs.innerHTML = '<a href="#/">会话列表</a> / ' + esc(sid.slice(0, 8)) + '…';
    app.innerHTML = '<div class="section">加载中…</div>';
    var benchQ = '?benchmark=' + encodeURIComponent(state.benchmark);
    var posQ = '?granularity=' + state.gran +
      (state.gran === 'minute' ? '&date=' + encodeURIComponent(state.minuteDate) : '');
    Promise.all([
      get('/sessions/' + sid),
      state.benchmarks.length ? Promise.resolve(state.benchmarks) : get('/benchmarks'),
      get('/sessions/' + sid + '/metrics' + benchQ),
      get('/sessions/' + sid + '/equity' + benchQ),
      get('/sessions/' + sid + '/rebalances'),
      (state.gran === 'minute' && !state.minuteDate)
        ? Promise.resolve(null) : get('/sessions/' + sid + '/positions' + posQ),
    ]).then(function (res) {
      state.benchmarks = res[1];
      draw(sid, res[0], res[2], res[3], res[4], res[5]);
    }).catch(fail);
  }

  function metricRow(label, st, bm, rel, key, fmt) {
    fmt = fmt || pct;
    return '<tr><td>' + label + '</td>' +
      '<td class="' + cls(st && st[key]) + '">' + fmt(st && st[key]) + '</td>' +
      '<td>' + fmt(bm && bm[key]) + '</td>' +
      '<td>' + (rel == null ? '—' : fmt(rel)) + '</td></tr>';
  }

  function draw(sid, ses, m, eq, rebalances, positions) {
    var st = m.strategy, bm = m.benchmark_stats, rel = m.relative || {};
    var benchSel = '<select id="bench-sel">' + state.benchmarks.map(function (b) {
      return '<option value="' + esc(b.code) + '"' + (b.code === state.benchmark ? ' selected' : '') + '>' +
        esc(b.name) + ' (' + esc(b.code) + ')</option>';
    }).join('') + '</select>';
    var lowConf = m.low_confidence ? ' <span style="color:#9a6700">⚠︎ 样本&lt;60交易日，风险指标仅供参考</span>' : '';
    var html =
      '<div class="section"><h2>指标对比 · 基准 ' + benchSel + lowConf +
      (m.error ? ' <span style="color:#cf222e">（基准数据缺失，相对指标不可用）</span>' : '') + '</h2>' +
      '<table class="kpi-table"><thead><tr><th>指标</th><th>本策略</th><th>基准</th><th>相对</th></tr></thead><tbody>' +
      metricRow('总收益', st, bm, rel.excess_return, 'total_return') +
      metricRow('年化收益', st, bm, null, 'annual_return') +
      metricRow('夏普比率', st, bm, null, 'sharpe', function (x) { return num(x); }) +
      metricRow('最大回撤', st, bm, null, 'max_drawdown') +
      metricRow('收益波动率', st, bm, rel.tracking_error, 'volatility') +
      '<tr><td>信息比率 / Beta / Alpha</td><td colspan="3">' +
      num(rel.information_ratio) + ' / ' + num(rel.beta) + ' / ' + pct(rel.alpha) + '</td></tr>' +
      '</tbody></table></div>' +
      '<div class="section"><h2>净值曲线（起点 1.0，副轴回撤）</h2><canvas id="eq" height="90"></canvas></div>' +
      periodTable('年度收益统计', m.annual) + periodTable('月度收益统计', m.monthly) +
      positionsSection(positions) + rebalanceSection(rebalances) +
      '<div class="section"><h2>原始数据</h2><p>' +
      '<a href="/sessions/' + sid + '/trades" target="_blank">成交</a> · ' +
      '<a href="/sessions/' + sid + '/rejections" target="_blank">拒单</a> · ' +
      '<a href="/sessions/' + sid + '/summary" target="_blank">汇总 JSON</a></p></div>';
    app.innerHTML = html;

    document.getElementById('bench-sel').addEventListener('change', function (e) {
      state.benchmark = e.target.value; renderDetail(sid);
    });
    bindGranSwitch(sid);
    drawChart(eq);
  }

  function periodTable(title, rows) {
    if (!rows || !rows.length) { return ''; }
    var body = rows.map(function (r) {
      return '<tr><td>' + esc(r.period) + '</td>' +
        '<td class="' + cls(r.strategy_return) + '">' + pct(r.strategy_return) + '</td>' +
        '<td>' + pct(r.benchmark_return) + '</td><td>' + pct(r.excess) + '</td>' +
        '<td>' + pct(r.max_drawdown) + '</td><td>' + pct(r.volatility) + '</td>' +
        '<td>' + num(r.sharpe) + '</td></tr>';
    }).join('');
    return '<div class="section"><h2>' + title + '</h2><table class="kpi-table"><thead>' +
      '<tr><th>期间</th><th>策略收益</th><th>基准收益</th><th>超额</th><th>最大回撤</th><th>波动率</th><th>夏普</th></tr>' +
      '</thead><tbody>' + body + '</tbody></table></div>';
  }

  function positionsSection(rows) {
    var grans = [['daily', '日'], ['weekly', '周'], ['hourly', '时'], ['minute', '分']];
    var btns = grans.map(function (g) {
      return '<button data-g="' + g[0] + '" class="' + (state.gran === g[0] ? 'active' : '') + '">' + g[1] + '</button>';
    }).join('');
    var dateInput = state.gran === 'minute'
      ? ' <input id="minute-date" type="date" value="' + esc(state.minuteDate) + '"> （分钟粒度须选日期）' : '';
    var body = '';
    if (rows === null) {
      body = '<p>请选择日期后查看分钟级持仓。</p>';
    } else if (!rows.length) {
      body = '<p>该粒度暂无持仓快照。</p>';
    } else {
      body = rows.slice(-30).map(function (s) {     // 默认展示最近 30 个快照
        var ps = (s.positions || []).map(function (p) {
          return '<tr><td>' + esc(p.symbol) + '</td><td>' + p.quantity + '</td><td>' + num(p.last_price) +
            '</td><td>' + money(p.market_value) + '</td><td>' + pct(p.weight) +
            '</td><td class="' + cls(p.unrealized_pnl) + '">' + money(p.unrealized_pnl) + '</td></tr>';
        }).join('') || '<tr><td colspan="6">空仓</td></tr>';
        return '<details class="rebalance"><summary>' + esc(s.timestamp) + (s.week ? '（' + esc(s.week) + '）' : '') +
          ' · 总资产 ' + money(s.total_value) + ' · 现金 ' + money(s.cash) + '</summary>' +
          '<table class="kpi-table"><thead><tr><th>代码</th><th>数量</th><th>现价</th><th>市值</th><th>权重</th><th>浮盈</th></tr></thead>' +
          '<tbody>' + ps + '</tbody></table></details>';
      }).join('');
    }
    return '<div class="section"><h2>持仓快照 <span class="gran-switch">' + btns + '</span>' + dateInput +
      '</h2>' + body + '</div>';
  }

  function rebalanceSection(events) {
    if (!events || !events.length) {
      return '<div class="section"><h2>调仓记录</h2><p>无成交。</p></div>';
    }
    var body = events.map(function (e) {
      var legs = function (xs, label) {
        return (xs || []).map(function (x) {
          return label + ' ' + esc(x.symbol) + ' × ' + x.quantity + ' @ ' + num(x.avg_price, 4) + '（' + money(x.amount) + '）';
        }).join('<br>');
      };
      var diff = (e.position_diff || []).map(function (d) {
        return '<tr><td>' + esc(d.symbol) + '</td><td>' + d.qty_before + ' → ' + d.qty_after + '</td>' +
          '<td>' + pct(d.weight_before) + ' → ' + pct(d.weight_after) + '</td></tr>';
      }).join('');
      return '<details class="rebalance"><summary>' + esc(e.trade_date) + ' · ' + e.n_trades + ' 笔成交 · 费用 ' +
        money(e.fees_total) + ' · 已实现盈亏 <span class="' + cls(e.realized_pnl_total) + '">' +
        money(e.realized_pnl_total) + '</span></summary>' +
        '<p>' + [legs(e.buys, '买入'), legs(e.sells, '卖出')].filter(Boolean).join('<br>') + '</p>' +
        '<table class="kpi-table"><thead><tr><th>代码</th><th>数量变化</th><th>权重变化</th></tr></thead><tbody>' +
        diff + '</tbody></table>' +
        '<p>调仓后现金 ' + money(e.cash_after) + ' · 总资产 ' + money(e.total_value_after) + '</p></details>';
    }).join('');
    return '<div class="section"><h2>调仓记录（' + events.length + ' 次）</h2>' + body + '</div>';
  }

  function bindGranSwitch(sid) {
    Array.prototype.forEach.call(document.querySelectorAll('.gran-switch button'), function (btn) {
      btn.addEventListener('click', function () { state.gran = btn.dataset.g; renderDetail(sid); });
    });
    var di = document.getElementById('minute-date');
    if (di) { di.addEventListener('change', function (e) { state.minuteDate = e.target.value; renderDetail(sid); }); }
  }

  function drawChart(eq) {
    var canvas = document.getElementById('eq');
    if (!canvas || typeof Chart === 'undefined' || !eq.dates.length) { return; }
    var datasets = [
      { label: '策略', data: eq.strategy, borderColor: '#0969da', pointRadius: 0, borderWidth: 2, yAxisID: 'y' },
    ];
    if (eq.benchmark) {
      datasets.push({ label: '基准', data: eq.benchmark, borderColor: '#9a6700', pointRadius: 0, borderWidth: 1.5, yAxisID: 'y' });
    }
    datasets.push({ label: '回撤', data: eq.drawdown, borderColor: 'rgba(207,34,46,.6)',
      backgroundColor: 'rgba(207,34,46,.12)', fill: true, pointRadius: 0, borderWidth: 1, yAxisID: 'dd' });
    charts.push(new Chart(canvas.getContext('2d'), {
      type: 'line',
      data: { labels: eq.dates, datasets: datasets },
      options: {
        animation: false, interaction: { mode: 'index', intersect: false },
        scales: { y: { position: 'left' }, dd: { position: 'right', max: 0, grid: { display: false } } },
        plugins: { legend: { position: 'top' } },
      },
    }));
  }
})();
```

- [ ] **Step 7.3: index.html 缓存戳**

Edit：`<script src="static/app.js?v=5"></script>` → `<script src="static/app.js?v=6"></script>`

- [ ] **Step 7.4: 本地冒烟（venv 起服务 + 浏览器人工核对）**

```bash
export VORTEX_WORKSPACE=~/vortex/workspace VORTEX_STATE=~/vortex/state
.venv/bin/vortex-backtest serve --port 18770 &
sleep 2
curl -sS http://127.0.0.1:18770/ui/ | grep -c "app.js?v=6"     # 期望 1
curl -sS "http://127.0.0.1:18770/sessions" | head -c 200       # 期望真实会话 JSON
# 浏览器打开 http://127.0.0.1:18770/ui/ 人工核对：列表→详情→粒度切换→基准切换→调仓展开
kill %1
```
Expected: 页面渲染真实会话；故意停服务刷新 → 显式错误横幅（无假数据）。

- [ ] **Step 7.5: 提交**

```bash
git add vortex_backtest/web/static/app.js vortex_backtest/web/static/app.css vortex_backtest/web/index.html
git commit -m "feat(backtest): 看板重写为会话列表/详情两页(真数据,删 mock 兜底)"
```

---

### Task 8: 文档对齐 + 全量回归

**Files:** Modify `docs/usage-and-api.md`、Modify `README.md`

- [ ] **Step 8.1: usage-and-api.md §4 只读接口表追加 5 行（在 `/sessions/{id}/minutes` 行之后）**

```markdown
| GET | `/sessions/{id}/metrics?benchmark=&rf=` | 指标包：绝对/基准/相对(夏普/回撤/IR/Beta/Alpha) + 年度月度统计 |
| GET | `/sessions/{id}/equity?benchmark=` | 起点 1.0 对齐净值曲线 + 逐日回撤 |
| GET | `/sessions/{id}/positions?granularity=daily\|weekly\|hourly\|minute&date=` | 多粒度持仓快照（含权重；minute 须带 date） |
| GET | `/sessions/{id}/rebalances` | 调仓事件（按日聚合买卖 + 前后持仓 diff + 费用） |
| GET | `/benchmarks` | 可选基准目录（常用指数 + 申万行业） |
```

- [ ] **Step 8.2: README「当前能力」追加一行（在报告 GET 行之后）**

```markdown
- `GET /sessions/{id}/metrics|equity|positions|rebalances`、`GET /benchmarks` 分析报告层：
  基准对比指标（夏普/回撤/IR/Beta/Alpha）、年度月度统计、多粒度持仓（日/周/时/分）、调仓记录
```

- [ ] **Step 8.3: 全量回归 + 提交**

```bash
.venv/bin/python -m pytest -q && .venv/bin/python -m compileall -q vortex_backtest tests examples
git add docs/usage-and-api.md README.md
git commit -m "docs(backtest): 分析报告层端点入使用文档/README"
```
Expected: 全绿后提交。

---

### Task 9: 容器重建 + 真实数据验收

- [ ] **Step 9.1: 重建容器（镜像吃进新代码）**

Run: `vortex run up backtest 2>&1 | tail -4 && sleep 3 && curl -sS http://127.0.0.1:8766/health`
Expected: `Started` + `{"status":"ok"}`

- [ ] **Step 9.2: 对真实会话验证 5 个端点**

```bash
SID=6e64f3d6-86d2-45fb-89a2-fc51b42d2909
curl -sS "http://127.0.0.1:8766/sessions/$SID/metrics?benchmark=000300.SH" | .venv/bin/python -c '
import sys, json
m = json.load(sys.stdin)
assert m["strategy"]["n_days"] == 82 and m["benchmark_stats"] is not None
assert m["annual"] and m["monthly"], "年月统计为空"
print("METRICS-OK", {k: round(v, 4) if isinstance(v, float) else v
      for k, v in m["strategy"].items()})'
curl -sS "http://127.0.0.1:8766/sessions/$SID/equity?benchmark=000300.SH" | .venv/bin/python -c '
import sys, json
e = json.load(sys.stdin)
assert len(e["dates"]) == 82 and e["strategy"][0] == 1.0 and e["benchmark"][0] == 1.0
print("EQUITY-OK", len(e["dates"]), "天")'
curl -sS "http://127.0.0.1:8766/sessions/$SID/positions?granularity=weekly" | .venv/bin/python -c '
import sys, json
w = json.load(sys.stdin)
assert w and all("week" in r for r in w)
print("WEEKLY-OK", len(w), "周")'
curl -sS "http://127.0.0.1:8766/sessions/$SID/rebalances" | .venv/bin/python -c '
import sys, json
ev = json.load(sys.stdin)
assert [e["trade_date"] for e in ev] == ["2026-02-03", "2026-02-10"]
assert ev[0]["position_diff"][0]["qty_after"] == 1000
print("REBALANCES-OK", json.dumps(ev[0]["position_diff"], ensure_ascii=False))'
curl -sS "http://127.0.0.1:8766/benchmarks" | .venv/bin/python -c '
import sys, json
b = json.load(sys.stdin)
assert any(x["code"] == "000300.SH" for x in b) and any(x["source"] == "sw_daily" for x in b)
print("BENCHMARKS-OK", len(b), "个基准")'
```
Expected: 5 个 OK。

- [ ] **Step 9.3: 浏览器人工核对** — 打开 `http://127.0.0.1:8766/ui/`：列表页见 demo-container 会话 → 详情页指标表/曲线/年月统计/持仓粒度切换/调仓记录全部为真实数据；`low_confidence` 因 82≥60 不应出现。

- [ ] **Step 9.4: 报告回填 + 收尾提交**

`docs/superpowers/reports/2026-06-11-runnability-verification.md` §5a 第 1 条（看板对齐）标记完成；最后 `git log --oneline -8` 留档汇报。

---

## 自检记录（plan self-review）

- **Spec 覆盖**：§4.1-4.5 五端点→Task 6；§5 口径→Task 1/2（金标）；§4.3 四粒度+weight→Task 3+6；§4.4 调仓 diff→Task 4+6；§3 benchmark→Task 5；§6 看板两页/删 mock/基准下拉/粒度切换→Task 7；§7 错误处理（422/降级/分页/minute 强制 date）→Task 6 实现+测试；§8 测试策略→Task 1-6；文档→Task 8；真实验收→Task 9。无缺口。
- **占位符扫描**：所有代码步骤含完整代码；无 TBD/“类似 Task N”。
- **类型一致性**：`perf_stats/relative_stats/period_stats/equity_curve/daily_views/weekly_views/hourly_views/minute_views/rebalance_events/LOW_CONFIDENCE_DAYS` 在 Task 1-4 定义、Task 6 引用名一致；`load_series→(series, name)`、`list_benchmarks→[{code,name,source}]` 在 Task 5 定义、Task 6/7 使用一致；前端消费字段（`strategy.n_days`、`relative.beta`、`position_diff[].qty_before` 等）与端点输出一致。
- **已知取舍**：benchmark `_read` 每请求全量读数据集（index_daily 257k 行 <1s），MVP 接受，缓存留二期；前端持仓快照默认只渲染最近 30 个（分页防爆 DOM），全量走 API。

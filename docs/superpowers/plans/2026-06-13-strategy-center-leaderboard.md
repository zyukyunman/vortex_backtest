# 策略中心 / 排行榜 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在看板加一个跨会话聚合视角——按 `strategy_id` 把历次回测聚合成"策略"，出排行榜（n_runs / 最新一次 / 历史最优 / 收益排行）与多策略横向对比（净值叠加 + 指标并排）。

**Architecture:** 沿用一/二期已锁定的模式——`analytics.py` 纯函数（聚合逻辑，金标可测）+ `app.py` 只读 GET 端点（读时薄聚合，零物化）+ 静态 SPA 加页（hash 路由）。对比视图不加新端点，前端复用每个 run 的现有 `/equity`、`/metrics`。引擎/撮合/会话语义/JSONL 产物零改动。

**Tech Stack:** Python 3 / FastAPI / pytest（后端）；原生 JS + 已 vendor 的 Chart.js（前端，无构建链）；SQLite（sessions 表，strategy_id 在 config_json）。

**Spec:** `docs/superpowers/specs/2026-06-13-strategy-center-leaderboard-design.md`

---

## 文件结构

| 文件 | 动作 | 职责 |
|---|---|---|
| `vortex_backtest/analytics.py` | 修改 | 加 `strategy_rollup()` / `strategy_detail()` 及内部 helper（纯函数） |
| `tests/test_strategy_analytics.py` | 新建 | `strategy_rollup`/`strategy_detail` 金标单测 |
| `vortex_backtest/app.py` | 修改 | 加 `_strategy_records()` helper + `GET /strategies`、`GET /strategies/{id}` |
| `tests/test_strategy_api.py` | 新建 | 两端点的 TestClient API 测试 |
| `vortex_backtest/web/index.html` | 修改 | 顶部主导航（会话列表 ↔ 策略中心）+ bump app.js 版本 |
| `vortex_backtest/web/static/app.js` | 修改 | route 加 3 路由 + 排行榜/策略详情/对比三视图 + `drawMultiCurve` |
| `examples/session_scenarios.py` | 修改 | `_open` 默认带 strategy_id + 新增 `multi_run` 多次回测场景 |

---

## Task 1: analytics 聚合纯函数（金标 TDD）

**Files:**
- Modify: `vortex_backtest/analytics.py`（文件头 import 区 + 文件末尾追加函数）
- Test: `tests/test_strategy_analytics.py`（新建）

- [ ] **Step 1: 写失败测试**

新建 `tests/test_strategy_analytics.py`：

```python
"""策略中心聚合纯函数金标单测（strategy_rollup / strategy_detail）。"""
from vortex_backtest import analytics


def _rec(strategy_id, session_id, created_at, total_return, *, account_id="acct",
         end_date="2026-03-01", status="closed"):
    """构造一条 per-run 记录（调用方在 app.py 从会话行 + summary + perf_stats 抽好的形状）。"""
    return {
        "strategy_id": strategy_id, "session_id": session_id, "account_id": account_id,
        "start_date": "2026-02-01", "end_date": end_date, "status": status,
        "created_at": created_at, "updated_at": created_at,
        "total_return": total_return, "annual_return": None, "sharpe": None,
        "volatility": None, "max_drawdown": -0.05, "n_days": 5, "low_confidence": True,
    }


def test_rollup_empty():
    assert analytics.strategy_rollup([]) == []


def test_rollup_single_run_latest_eq_best():
    rows = analytics.strategy_rollup([_rec("a", "s1", "2026-01-01T00:00:00", 0.10)])
    assert len(rows) == 1
    r = rows[0]
    assert r["strategy_id"] == "a" and r["n_runs"] == 1
    assert r["latest"]["session_id"] == "s1"
    assert r["best"]["session_id"] == "s1" and r["best"]["total_return"] == 0.10


def test_rollup_latest_by_created_at_best_by_total_return():
    # 同策略两次：s_old 收益高但更早；s_new 收益低但最新 → latest=s_new、best=s_old
    recs = [_rec("a", "s_old", "2026-01-01T00:00:00", 0.20),
            _rec("a", "s_new", "2026-02-01T00:00:00", 0.01)]
    r = analytics.strategy_rollup(recs)[0]
    assert r["n_runs"] == 2
    assert r["latest"]["session_id"] == "s_new"
    assert r["best"]["session_id"] == "s_old" and r["best"]["total_return"] == 0.20


def test_rollup_sorted_by_latest_total_return_desc():
    recs = [_rec("low", "s1", "2026-01-01T00:00:00", 0.01),
            _rec("high", "s2", "2026-01-01T00:00:00", 0.50),
            _rec("mid", "s3", "2026-01-01T00:00:00", 0.10)]
    assert [r["strategy_id"] for r in analytics.strategy_rollup(recs)] == ["high", "mid", "low"]


def test_rollup_created_at_tiebreak_by_session_id():
    # created_at 相同 → latest 取 session_id 字典序较大者
    recs = [_rec("a", "s_aaa", "2026-01-01T00:00:00", 0.30),
            _rec("a", "s_zzz", "2026-01-01T00:00:00", 0.05)]
    assert analytics.strategy_rollup(recs)[0]["latest"]["session_id"] == "s_zzz"


def test_rollup_accounts_dedup_sorted_and_window():
    recs = [_rec("a", "s1", "2026-01-01T00:00:00", 0.1, account_id="b", end_date="2026-03-01"),
            _rec("a", "s2", "2026-02-01T00:00:00", 0.2, account_id="a", end_date="2026-02-01"),
            _rec("a", "s3", "2026-03-01T00:00:00", 0.0, account_id="a", end_date="2026-04-01")]
    r = analytics.strategy_rollup(recs)[0]
    assert r["accounts"] == ["a", "b"]
    assert r["first_run"] == "2026-02-01" and r["last_run"] == "2026-04-01"


def test_rollup_window_all_end_dates_missing():
    recs = [_rec("a", "s1", "2026-01-01T00:00:00", 0.1, end_date=None)]
    r = analytics.strategy_rollup(recs)[0]
    assert r["first_run"] is None and r["last_run"] is None


def test_detail_none_when_absent():
    assert analytics.strategy_detail("nope", [_rec("a", "s1", "2026-01-01T00:00:00", 0.1)]) is None


def test_detail_runs_sorted_by_created_at_asc():
    recs = [_rec("a", "s2", "2026-02-01T00:00:00", 0.01),
            _rec("a", "s1", "2026-01-01T00:00:00", 0.20),
            _rec("b", "s9", "2026-01-01T00:00:00", 0.9)]
    d = analytics.strategy_detail("a", recs)
    assert d["strategy_id"] == "a" and d["n_runs"] == 2
    assert [run["session_id"] for run in d["runs"]] == ["s1", "s2"]   # created_at 升序
    assert d["latest"]["session_id"] == "s2" and d["best"]["session_id"] == "s1"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_strategy_analytics.py -v`
Expected: FAIL — `AttributeError: module 'vortex_backtest.analytics' has no attribute 'strategy_rollup'`

- [ ] **Step 3: 实现纯函数**

在 `vortex_backtest/analytics.py` 文件头 import 区加 `defaultdict`：

```python
from collections import defaultdict
```

（即把第 8 行附近的 `from typing import Any, Mapping` 上方加一行 `from collections import defaultdict`。）

在 `analytics.py` **文件末尾**追加：

```python
# ──────────────────────────────────────────────────────────────────────────
# 策略中心 / 排行榜（spec 2026-06-13）：按 strategy_id 聚合 per-run 记录。
# 输入 record 由 app 层从会话行 + summary + perf_stats 抽好；本层不读文件不碰网络。
# ──────────────────────────────────────────────────────────────────────────

_RUN_FIELDS = ("session_id", "account_id", "start_date", "end_date", "status",
               "total_return", "annual_return", "sharpe", "volatility",
               "max_drawdown", "n_days", "low_confidence", "created_at", "updated_at")


def _run_view(rec: Mapping[str, Any]) -> dict[str, Any]:
    return {k: rec.get(k) for k in _RUN_FIELDS}


def _latest_run(group: list[Mapping[str, Any]]) -> Mapping[str, Any]:
    # 最新一次 = created_at 最大；tie-break session_id 字典序。
    return max(group, key=lambda r: (str(r["created_at"]), str(r["session_id"])))


def _best_run(group: list[Mapping[str, Any]]) -> Mapping[str, Any]:
    # 历史最优 = total_return 最大；tie-break created_at 较新、再 session_id。
    return max(group, key=lambda r: (float(r["total_return"]), str(r["created_at"]), str(r["session_id"])))


def _run_window(group: list[Mapping[str, Any]]) -> tuple[str | None, str | None]:
    # first/last_run = 各 run end_date 的 min/max；无 end_date 的 run 不计入，全缺则 (None, None)。
    ends = sorted(str(r["end_date"]) for r in group if r.get("end_date"))
    return (ends[0], ends[-1]) if ends else (None, None)


def _group_row(strategy_id: str, group: list[Mapping[str, Any]]) -> dict[str, Any]:
    latest, best = _latest_run(group), _best_run(group)
    first_run, last_run = _run_window(group)
    return {
        "strategy_id": strategy_id,
        "n_runs": len(group),
        "accounts": sorted({str(r["account_id"]) for r in group}),
        "first_run": first_run, "last_run": last_run,
        "latest": _run_view(latest),
        "best": {"session_id": best["session_id"], "total_return": best["total_return"]},
    }


def strategy_rollup(records: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """按 strategy_id 聚合 → 排行榜行，默认按 latest.total_return 降序（tie-break strategy_id 升序）。"""
    groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for r in records:
        groups[str(r["strategy_id"])].append(r)
    rows = [_group_row(sid, g) for sid, g in groups.items()]
    rows.sort(key=lambda row: (-float(row["latest"]["total_return"]), row["strategy_id"]))
    return rows


def strategy_detail(strategy_id: str, records: list[Mapping[str, Any]]) -> dict[str, Any] | None:
    """单策略详情：runs 按 created_at 升序 + latest/best。无任何会话 → None（端点转 404）。"""
    group = [r for r in records if str(r["strategy_id"]) == strategy_id]
    if not group:
        return None
    latest, best = _latest_run(group), _best_run(group)
    runs = sorted((_run_view(r) for r in group),
                  key=lambda r: (str(r["created_at"]), str(r["session_id"])))
    return {
        "strategy_id": strategy_id, "n_runs": len(group),
        "accounts": sorted({str(r["account_id"]) for r in group}),
        "runs": runs, "latest": _run_view(latest),
        "best": {"session_id": best["session_id"], "total_return": best["total_return"]},
    }
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_strategy_analytics.py -v`
Expected: PASS（10 passed）

- [ ] **Step 5: 提交**

```bash
git add vortex_backtest/analytics.py tests/test_strategy_analytics.py
git commit -m "feat(analytics): strategy_rollup/strategy_detail——按 strategy_id 聚合 per-run 记录(金标)"
```

---

## Task 2: app.py 两个只读端点（API TDD）

**Files:**
- Modify: `vortex_backtest/app.py`（在 `/benchmarks` 端点之后、静态 mount 之前插入 helper + 两端点）
- Test: `tests/test_strategy_api.py`（新建）

- [ ] **Step 1: 写失败测试**

新建 `tests/test_strategy_api.py`：

```python
"""策略中心端点 API 测试：直接写 summary.json（closed 会话走缓存路径），控制 created_at/strategy_id/收益。"""
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from vortex_backtest.app import create_app
from vortex_backtest.models import AccountCreate
from vortex_backtest.store import DataStore


def _seed(state, store, *, session_id, strategy_id, account_id, last_value,
          created_at, start="2026-02-02", end="2026-02-06"):
    """建账户(幂等) + 会话行 + closed + 写 summary.json。
    total_return 由 perf_stats(日序列) 算：last_value / initial_cash(1000) - 1。"""
    try:
        store.create_account(AccountCreate(account_id=account_id, initial_cash=1000.0))
    except sqlite3.IntegrityError:
        pass
    store.create_session(session_id=session_id, account_id=account_id, level="daily",
                         start_date=start, end_date=end, sim_time=None, initial_cash=1000.0,
                         universe=["000001.SZ"], config={"strategy_id": strategy_id})
    with store.connect() as c:   # 显式控制 created_at 以断言 latest 口径
        c.execute("UPDATE sessions SET created_at=? WHERE session_id=?", (created_at, session_id))
    daily = [{"trade_date": start, "cash": 1000.0, "market_value": 0.0,
              "total_value": 1000.0, "positions": []},
             {"trade_date": end, "cash": last_value, "market_value": 0.0,
              "total_value": last_value, "positions": []}]
    summary = {"strategy_id": strategy_id, "initial_cash": 1000.0, "cash": last_value,
               "market_value": 0.0, "total_value": last_value,
               "total_return": last_value / 1000.0 - 1, "max_drawdown": 0.0,
               "realized_pnl": 0.0, "positions": [], "daily": daily}
    sdir = state / "reports" / "sessions" / session_id
    sdir.mkdir(parents=True)
    (sdir / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    store.update_session(session_id, status="closed")


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("VORTEX_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.delenv("VORTEX_DATA_URL", raising=False)
    monkeypatch.setenv("VORTEX_BACKTEST_HOST", "127.0.0.1")
    state = tmp_path / "state"
    store = DataStore(state)
    # 策略 alpha 两次：a_old 收益高(0.20)但早；a_new 收益低(0.01)但最新 → latest=a_new、best=a_old
    _seed(state, store, session_id="a_old", strategy_id="alpha", account_id="acc_a",
          last_value=1200.0, created_at="2026-01-01T00:00:00+00:00")
    _seed(state, store, session_id="a_new", strategy_id="alpha", account_id="acc_a",
          last_value=1010.0, created_at="2026-02-01T00:00:00+00:00")
    # 策略 beta 一次：收益 0.05
    _seed(state, store, session_id="b1", strategy_id="beta", account_id="acc_b",
          last_value=1050.0, created_at="2026-01-15T00:00:00+00:00")
    return TestClient(create_app(state_dir=state))


def test_strategies_leaderboard(client):
    rows = client.get("/strategies").json()
    # 默认按 latest.total_return 降序：beta(latest 0.05) 在 alpha(latest a_new 0.01) 前
    assert [r["strategy_id"] for r in rows] == ["beta", "alpha"]
    alpha = next(r for r in rows if r["strategy_id"] == "alpha")
    assert alpha["n_runs"] == 2 and alpha["accounts"] == ["acc_a"]
    assert alpha["latest"]["session_id"] == "a_new"
    assert alpha["latest"]["total_return"] == pytest.approx(0.01)
    assert alpha["best"]["session_id"] == "a_old"
    assert alpha["best"]["total_return"] == pytest.approx(0.20)
    assert alpha["latest"]["low_confidence"] is True   # n_days=2 < 60


def test_strategy_detail_runs_sorted(client):
    d = client.get("/strategies/alpha").json()
    assert d["n_runs"] == 2
    assert [run["session_id"] for run in d["runs"]] == ["a_old", "a_new"]   # created_at 升序
    assert d["latest"]["session_id"] == "a_new" and d["best"]["session_id"] == "a_old"


def test_strategy_detail_404(client):
    assert client.get("/strategies/nope").status_code == 404


def test_strategies_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("VORTEX_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.delenv("VORTEX_DATA_URL", raising=False)
    monkeypatch.setenv("VORTEX_BACKTEST_HOST", "127.0.0.1")
    state = tmp_path / "state"
    DataStore(state)
    c = TestClient(create_app(state_dir=state))
    assert c.get("/strategies").json() == []
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_strategy_api.py -v`
Expected: FAIL — `/strategies` 返回 404（端点未注册）

- [ ] **Step 3: 实现 helper + 端点**

在 `vortex_backtest/app.py` 的 `@app.get("/benchmarks")` 块（约 547-549 行）**之后**、`# 托管只读看板` 注释**之前**，插入：

```python
    def _strategy_records(data_store: DataStore) -> list[dict]:
        """遍历所有会话 → 每会话一条 per-run 记录（行身份 + summary 指标 + perf_stats 派生）。

        供 strategy_rollup/strategy_detail 聚合。读时计算：已 close 命中 summary.json 缓存，
        open 即时归约（与 _session_summary 现有行为一致）。n_days = 实际交易日数 = len(daily)
        （不含基线锚点）；low_confidence 同 /metrics 口径。
        """
        records = []
        for row in data_store.list_sessions():
            cfg = json.loads(row.get("config_json") or "{}")
            daily = _daily_rows(data_store, row["session_id"])
            stats = analytics.perf_stats(_strategy_series(row, daily))
            records.append({
                "strategy_id": str(cfg.get("strategy_id") or "session"),
                "session_id": row["session_id"], "account_id": row["account_id"],
                "start_date": row.get("start_date"), "end_date": row.get("end_date"),
                "status": row["status"],
                "created_at": row["created_at"], "updated_at": row["updated_at"],
                "total_return": stats["total_return"], "annual_return": stats["annual_return"],
                "sharpe": stats["sharpe"], "volatility": stats["volatility"],
                "max_drawdown": stats["max_drawdown"],
                "n_days": len(daily), "low_confidence": len(daily) < analytics.LOW_CONFIDENCE_DAYS,
            })
        return records

    @app.get("/strategies")
    def strategies(data_store: DataStore = Depends(get_store)) -> list[dict]:
        return analytics.strategy_rollup(_strategy_records(data_store))

    @app.get("/strategies/{strategy_id}")
    def strategy_detail(
        strategy_id: str, data_store: DataStore = Depends(get_store)
    ) -> dict:
        detail = analytics.strategy_detail(strategy_id, _strategy_records(data_store))
        if detail is None:
            raise HTTPException(status_code=404, detail={"error": "strategy_not_found"})
        return detail
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_strategy_api.py -v`
Expected: PASS（4 passed）

- [ ] **Step 5: 跑全量后端测试确认零回归**

Run: `.venv/bin/python -m pytest -q`
Expected: 之前的全部 passed + 新增 14 个（10 analytics + 4 api）均 passed，无 failed。

- [ ] **Step 6: 提交**

```bash
git add vortex_backtest/app.py tests/test_strategy_api.py
git commit -m "feat(app): 只读端点 /strategies + /strategies/{id}——读时薄聚合排行榜/策略详情"
```

---

## Task 3: 前端——主导航 + 排行榜页

**Files:**
- Modify: `vortex_backtest/web/index.html`（加主导航 + bump 版本）
- Modify: `vortex_backtest/web/static/app.js`（state 字段 + 模块变量 + route + renderStrategies/renderLeaderboard）

> 前端无测试框架（spec §9），本任务用人工冒烟验证；代码须贴合现有 app.js 惯例（vanilla、字符串拼 HTML、`get`/`esc`/`pct`/`cls` helper、`kpi-table` class）。

- [ ] **Step 1: index.html 加主导航 + bump 版本**

把 `vortex_backtest/web/index.html` 的 header 块（13-22 行）改为在 brand 后加主导航；并把脚本版本 `?v=9` 改为 `?v=10`：

```html
  <header class="topbar">
    <div class="brand">vortex_backtest</div>
    <nav class="docnav" style="display:flex;gap:14px;margin:0 14px" aria-label="主导航">
      <a href="#/" style="color:#0969da;text-decoration:none;font-size:13px">会话列表</a>
      <a href="#/strategies" style="color:#0969da;text-decoration:none;font-size:13px">策略中心</a>
    </nav>
    <nav id="crumbs" class="crumbs" aria-label="面包屑"></nav>
    <div class="spacer"></div>
    <nav class="docnav" style="display:flex;gap:14px;margin-right:14px" aria-label="文档">
      <a href="/guide" style="color:#0969da;text-decoration:none;font-size:13px">📖 文档</a>
      <a href="/docs" style="color:#0969da;text-decoration:none;font-size:13px">🔌 API</a>
    </nav>
    <div id="toolbar" class="toolbar"></div>
  </header>
```

并把第 25 行 `<script src="static/app.js?v=9"></script>` 改为 `<script src="static/app.js?v=10"></script>`。

- [ ] **Step 2: app.js — state 字段 + 模块变量**

把 `app.js` 第 7-8 行：

```javascript
  var state = { benchmark: '000300.SH', gran: 'daily', minuteDate: '', benchmarks: [], account: '', tab: 'equity' };
  var detail = null;   // 详情页数据缓存 {sid, m, eq, dist}：切页签零请求
```

改为：

```javascript
  var state = { benchmark: '000300.SH', gran: 'daily', minuteDate: '', benchmarks: [], account: '', tab: 'equity',
                strat_sort: 'total_return', cmp_axis: 'calendar' };
  var detail = null;   // 详情页数据缓存 {sid, m, eq, dist}：切页签零请求
  var cmpSel = {};     // 排行榜对比篮：{strategy_id: true}
  var cmpItems = [];   // 叠加图曲线缓存 [{label, dates, values}]：切轴重画零请求
```

- [ ] **Step 3: app.js — route 加 3 路由**

把 `route()`（约 61-65 行）：

```javascript
  function route() {
    destroyCharts();
    var m = location.hash.match(/^#\/session\/([\w-]+)/);
    if (m) { renderDetail(m[1]); } else { renderList(); }
  }
```

改为：

```javascript
  function route() {
    destroyCharts();
    var h = location.hash, m;
    if ((m = h.match(/^#\/strategy\/(.+)$/))) { renderStrategyDetail(decodeURIComponent(m[1])); }
    else if ((m = h.match(/^#\/compare\/(.+)$/))) { renderCompare(m[1].split(',').map(decodeURIComponent)); }
    else if (h.indexOf('#/strategies') === 0) { renderStrategies(); }
    else if ((m = h.match(/^#\/session\/([\w-]+)/))) { renderDetail(m[1]); }
    else { renderList(); }
  }
```

- [ ] **Step 4: app.js — 排行榜视图**

在 `drawChart` 函数之后、文件末尾的 `})();` **之前**，追加排行榜代码：

```javascript
  // ════════════════════ 策略中心：排行榜（spec 2026-06-13）════════════════════
  // 代表行指标列：[字段, 表头]
  var STRAT_COLS = [['total_return', '总收益'], ['annual_return', '年化'],
                    ['sharpe', '夏普'], ['max_drawdown', '最大回撤']];

  function fmtStratCell(key, v) {
    var disp = key === 'sharpe' ? num(v) : pct(v);
    var c = (key === 'total_return' || key === 'annual_return') ? cls(v) : '';
    return '<td class="' + c + '">' + disp + '</td>';
  }

  function sortStrategies(rows) {
    var key = state.strat_sort;
    return rows.slice().sort(function (a, b) {
      var av = a.latest[key], bv = b.latest[key];
      av = av == null ? -Infinity : av; bv = bv == null ? -Infinity : bv;
      return bv - av;   // 一律降序（收益/夏普越大越好；回撤为负数，越大=越浅亦合理）
    });
  }

  function renderStrategies() {
    crumbs.innerHTML = '策略中心';
    app.innerHTML = '<div class="section"><h2>策略排行榜</h2><div id="lb">加载中…</div></div>';
    get('/strategies').then(function (rows) {
      if (!rows.length) {
        document.getElementById('lb').innerHTML = '<p>暂无策略。跑几笔带 strategy_id 的回测后再来。</p>';
        return;
      }
      renderLeaderboard(rows);
    }).catch(fail);
  }

  function renderLeaderboard(rows) {
    var sorted = sortStrategies(rows);
    var heads = STRAT_COLS.map(function (c) {
      return '<th class="sortable" data-k="' + c[0] + '" style="cursor:pointer">' + c[1] +
        (state.strat_sort === c[0] ? ' ▾' : '') + '</th>';
    }).join('');
    var body = sorted.map(function (r) {
      var checked = cmpSel[r.strategy_id] ? ' checked' : '';
      return '<tr>' +
        '<td><input type="checkbox" class="cmp-cb" data-sid="' + esc(r.strategy_id) + '"' + checked + '></td>' +
        '<td><a href="#/strategy/' + encodeURIComponent(r.strategy_id) + '">' + esc(r.strategy_id) + '</a></td>' +
        '<td>' + r.n_runs + '</td>' +
        '<td>' + esc(r.accounts.join(', ')) + '</td>' +
        STRAT_COLS.map(function (c) { return fmtStratCell(c[0], r.latest[c[0]]); }).join('') +
        '<td class="' + cls(r.best.total_return) + '">' + pct(r.best.total_return) + '</td>' +
        '<td>' + esc(r.first_run || '') + ' ~ ' + esc(r.last_run || '') + '</td></tr>';
    }).join('');
    document.getElementById('lb').innerHTML =
      '<div id="cmp-bar" class="muted" style="margin-bottom:8px"></div>' +
      '<table class="kpi-table"><thead><tr><th></th><th>策略</th><th>回测数</th><th>账户</th>' +
      heads + '<th>历史最优</th><th>区间</th></tr></thead><tbody>' + body + '</tbody></table>';
    Array.prototype.forEach.call(document.querySelectorAll('th.sortable'), function (th) {
      th.addEventListener('click', function () { state.strat_sort = th.dataset.k; renderLeaderboard(rows); });
    });
    Array.prototype.forEach.call(document.querySelectorAll('.cmp-cb'), function (cb) {
      cb.addEventListener('change', function () {
        if (cb.checked) { cmpSel[cb.dataset.sid] = true; } else { delete cmpSel[cb.dataset.sid]; }
        updateCmpBar();
      });
    });
    updateCmpBar();
  }

  function updateCmpBar() {
    var bar = document.getElementById('cmp-bar');
    if (!bar) { return; }
    var ids = Object.keys(cmpSel);
    if (ids.length < 2) { bar.innerHTML = '勾选 ≥2 个策略以横向对比。'; return; }
    bar.innerHTML = '已选 ' + ids.length + ' 个：' + esc(ids.join(', ')) +
      ' <a href="#/compare/' + ids.map(encodeURIComponent).join(',') + '">对比选中 →</a>';
  }
```

- [ ] **Step 5: 起服务人工冒烟（排行榜）**

Run（本地 venv 起服务，指向真实 state；端口 8766 若被容器占用则换 8799）：
```bash
VORTEX_WORKSPACE=~/vortex/workspace VORTEX_STATE=~/vortex/state \
  .venv/bin/vortex-backtest serve --port 8799
```
浏览器开 `http://127.0.0.1:8799/ui/#/strategies`，核对：
- 顶部主导航有「会话列表 / 策略中心」，可互切；
- 排行榜表出现，列 = 复选框/策略/回测数/账户/总收益/年化/夏普/最大回撤/历史最优/区间；
- 点表头「夏普」「总收益」等列，排序变化（▾ 跟随）；
- 勾选 ≥2 行，顶部出现「对比选中 →」链接（先不点，Task 4 实现目标页）；
- 点某策略名进 `#/strategy/<id>`（此时报错正常——Task 4 才实现，确认 route 命中即可）。

预期：排行榜正常渲染、排序可用、对比篮计数正确。完成后 Ctrl-C 停服务。

- [ ] **Step 6: 提交**

```bash
git add vortex_backtest/web/index.html vortex_backtest/web/static/app.js
git commit -m "feat(web): 策略中心主导航 + 排行榜页(可排序/对比篮)"
```

---

## Task 4: 前端——策略详情页 + 对比视图 + 叠加图

**Files:**
- Modify: `vortex_backtest/web/static/app.js`（追加 `drawMultiCurve`/`bindAxisSwitch`/`renderStrategyDetail`/`renderCompare`）
- Modify: `vortex_backtest/web/index.html`（bump 版本 `?v=10` → `?v=11`）

- [ ] **Step 1: app.js — 叠加图 + 轴切换 helper**

在 Task 3 追加的排行榜代码之后（仍在文件末尾 `})();` 之前），追加：

```javascript
  // ──────────── 多曲线叠加（日历轴 / 相对日轴），策略详情与对比共用 ────────────
  var CURVE_PALETTE = ['#0969da', '#9a6700', '#1a7f37', '#cf222e', '#8250df', '#bf3989', '#0550ae', '#e16f24'];

  function drawMultiCurve(canvasId, series, axis) {
    var canvas = document.getElementById(canvasId);
    if (!canvas || typeof Chart === 'undefined' || !series.length) { return; }
    var labels, datasets;
    if (axis === 'relative') {
      // 对齐起点：x = 第 N 个交易日；各曲线按自身下标对齐（短曲线点少，自然在前段对齐）
      var maxLen = series.reduce(function (m, s) { return Math.max(m, s.values.length); }, 0);
      labels = []; for (var i = 0; i < maxLen; i++) { labels.push('D' + i); }
      datasets = series.map(function (s, k) {
        return { label: s.label, data: s.values, borderColor: CURVE_PALETTE[k % CURVE_PALETTE.length],
          pointRadius: 0, borderWidth: 1.6 };
      });
    } else {
      // 日历轴：并所有日期为统一 x 轴，各曲线缺日填 null（spanGaps 连线）
      var seen = {};
      series.forEach(function (s) { s.dates.forEach(function (d) { seen[d] = 1; }); });
      labels = Object.keys(seen).sort();
      datasets = series.map(function (s, k) {
        var byDate = {}; s.dates.forEach(function (d, i) { byDate[d] = s.values[i]; });
        return { label: s.label, spanGaps: true,
          data: labels.map(function (d) { return d in byDate ? byDate[d] : null; }),
          borderColor: CURVE_PALETTE[k % CURVE_PALETTE.length], pointRadius: 0, borderWidth: 1.6 };
      });
    }
    charts.push(new Chart(canvas.getContext('2d'), {
      type: 'line', data: { labels: labels, datasets: datasets },
      options: { animation: false, interaction: { mode: 'index', intersect: false },
        plugins: { legend: { position: 'top' } } },
    }));
  }

  function axisSwitchHtml() {
    return ['calendar', 'relative'].map(function (a) {
      return '<button data-axis="' + a + '" class="' + (state.cmp_axis === a ? 'active' : '') + '">' +
        (a === 'calendar' ? '日历轴' : '对齐起点') + '</button>';
    }).join('');
  }

  function bindAxisSwitch(canvasId) {
    // cmpItems 为当前页缓存的曲线；切轴只销毁重画 canvas，不重取数据
    Array.prototype.forEach.call(document.querySelectorAll('#axis-sw button'), function (b) {
      b.addEventListener('click', function () {
        state.cmp_axis = b.dataset.axis;
        destroyCharts();
        Array.prototype.forEach.call(document.querySelectorAll('#axis-sw button'), function (x) {
          x.className = x.dataset.axis === state.cmp_axis ? 'active' : '';
        });
        drawMultiCurve(canvasId, cmpItems, state.cmp_axis);
      });
    });
  }
```

- [ ] **Step 2: app.js — 策略详情页**

接着追加：

```javascript
  // ──────────── 策略详情页 #/strategy/<id> ────────────
  function renderStrategyDetail(stratId) {
    destroyCharts();
    crumbs.innerHTML = '<a href="#/strategies">策略中心</a> / ' + esc(stratId);
    app.innerHTML = '<div class="section">加载中…</div>';
    get('/strategies/' + encodeURIComponent(stratId)).then(function (d) {
      return Promise.all(d.runs.map(function (run) {
        return get('/sessions/' + run.session_id + '/equity').then(function (eq) {
          return { run: run, eq: eq };
        });
      })).then(function (curves) { drawStrategyDetail(d, curves); });
    }).catch(fail);
  }

  function drawStrategyDetail(d, curves) {
    var runRows = d.runs.map(function (r) {
      return '<tr><td><a href="#/session/' + esc(r.session_id) + '">' + esc(r.session_id.slice(0, 8)) + '…</a></td>' +
        '<td>' + esc(r.start_date || '') + ' ~ ' + esc(r.end_date || '') + '</td>' +
        '<td>' + esc(r.status) + '</td>' +
        '<td class="' + cls(r.total_return) + '">' + pct(r.total_return) + '</td>' +
        '<td>' + pct(r.annual_return) + '</td><td>' + num(r.sharpe) + '</td>' +
        '<td>' + pct(r.max_drawdown) + '</td>' +
        '<td>' + esc(String(r.created_at || '').slice(0, 16).replace('T', ' ')) + '</td></tr>';
    }).join('');
    app.innerHTML =
      '<div class="section"><h2>' + esc(d.strategy_id) + '</h2><p class="muted">回测数 ' + d.n_runs +
      ' · 账户 ' + esc(d.accounts.join(', ')) +
      ' · 最新一次 <a href="#/session/' + esc(d.latest.session_id) + '">' + esc(d.latest.session_id.slice(0, 8)) +
      '…</a>（' + pct(d.latest.total_return) + '）· 历史最优 <a href="#/session/' + esc(d.best.session_id) + '">' +
      esc(d.best.session_id.slice(0, 8)) + '…</a>（' + pct(d.best.total_return) + '）</p></div>' +
      '<div class="section"><h2>历次回测</h2><table class="kpi-table"><thead><tr><th>会话</th><th>区间</th>' +
      '<th>状态</th><th>总收益</th><th>年化</th><th>夏普</th><th>最大回撤</th><th>发起时间</th></tr></thead><tbody>' +
      runRows + '</tbody></table></div>' +
      '<div class="section"><h2>净值叠加 <span class="gran-switch" id="axis-sw">' + axisSwitchHtml() + '</span></h2>' +
      '<canvas id="sd-c" height="90"></canvas></div>';
    cmpItems = curves.map(function (c) {
      return { label: c.run.session_id.slice(0, 8), dates: c.eq.dates, values: c.eq.strategy };
    });
    drawMultiCurve('sd-c', cmpItems, state.cmp_axis);
    bindAxisSwitch('sd-c');
  }
```

- [ ] **Step 3: app.js — 对比视图**

接着追加：

```javascript
  // ──────────── 对比视图 #/compare/<stratId,stratId,...> ────────────
  function renderCompare(stratIds) {
    destroyCharts();
    crumbs.innerHTML = '<a href="#/strategies">策略中心</a> / 对比';
    app.innerHTML = '<div class="section">加载中…</div>';
    get('/strategies').then(function (rows) {
      var byId = {}; rows.forEach(function (r) { byId[r.strategy_id] = r; });
      var picks = stratIds.filter(function (id) { return byId[id]; });
      if (picks.length < 2) {
        app.innerHTML = '<div class="error-banner">对比需要 ≥2 个有效策略。</div>'; return;
      }
      return Promise.all(picks.map(function (id) {
        var sess = byId[id].latest.session_id;   // 各策略取最新一次回测做代表
        return Promise.all([
          get('/sessions/' + sess + '/equity'),
          get('/sessions/' + sess + '/metrics'),
        ]).then(function (res) { return { id: id, eq: res[0], m: res[1] }; });
      })).then(drawCompare);
    }).catch(fail);
  }

  var CMP_METRICS = [['总收益', 'total_return'], ['年化', 'annual_return'], ['夏普', 'sharpe'],
                     ['最大回撤', 'max_drawdown'], ['波动率', 'volatility']];

  function drawCompare(items) {
    var head = '<tr><th>指标</th>' + items.map(function (it) { return '<th>' + esc(it.id) + '</th>'; }).join('') + '</tr>';
    var mbody = CMP_METRICS.map(function (mk) {
      return '<tr><td>' + mk[0] + '</td>' + items.map(function (it) {
        var v = it.m.strategy[mk[1]];
        var disp = mk[1] === 'sharpe' ? num(v) : pct(v);
        var c = (mk[1] === 'total_return' || mk[1] === 'annual_return') ? cls(v) : '';
        return '<td class="' + c + '">' + disp + '</td>';
      }).join('') + '</tr>';
    }).join('');
    app.innerHTML =
      '<div class="section"><h2>多策略净值叠加 <span class="gran-switch" id="axis-sw">' + axisSwitchHtml() + '</span></h2>' +
      '<canvas id="cmp-c" height="90"></canvas></div>' +
      '<div class="section"><h2>指标并排</h2><table class="kpi-table"><thead>' + head +
      '</thead><tbody>' + mbody + '</tbody></table></div>';
    cmpItems = items.map(function (it) { return { label: it.id, dates: it.eq.dates, values: it.eq.strategy }; });
    drawMultiCurve('cmp-c', cmpItems, state.cmp_axis);
    bindAxisSwitch('cmp-c');
  }
```

- [ ] **Step 4: index.html bump 版本**

把第 25 行 `<script src="static/app.js?v=10"></script>` 改为 `<script src="static/app.js?v=11"></script>`。

- [ ] **Step 5: 起服务人工冒烟（详情 + 对比）**

Run:
```bash
VORTEX_WORKSPACE=~/vortex/workspace VORTEX_STATE=~/vortex/state \
  .venv/bin/vortex-backtest serve --port 8799
```
浏览器核对：
- `#/strategies` → 点某策略名进 `#/strategy/<id>`：头部出现「回测数/最新一次/历史最优」（链接可点进单会话），「历次回测」表逐 run 列出，「净值叠加」图渲染；点「日历轴/对齐起点」切换，曲线 x 轴随之变化；
- 回 `#/strategies` 勾选 ≥2 策略 → 点「对比选中 →」进 `#/compare/...`：出现叠加净值图（图例为各策略名）+ 指标并排表（行=总收益/年化/夏普/最大回撤/波动率，列=各策略）；轴切换可用；
- run 表里点会话短码 → 进既有 `#/session/<id>` 详情正常。

预期：详情/对比两页正常，叠加图与轴切换可用，跨页链接通。完成后 Ctrl-C 停。

- [ ] **Step 6: 提交**

```bash
git add vortex_backtest/web/index.html vortex_backtest/web/static/app.js
git commit -m "feat(web): 策略详情页 + 多策略对比视图(净值叠加 日历/相对日轴 + 指标并排)"
```

---

## Task 5: examples 重播种代码（每场景带 strategy_id + 多次回测场景）

**Files:**
- Modify: `examples/session_scenarios.py`

- [ ] **Step 1: `_open` 默认携带 strategy_id**

把 `examples/session_scenarios.py` 的 `_open`（62-63 行）：

```python
def _open(account: str, **kw) -> str:
    return _post("/sessions", {"account_id": account, **kw})["session_id"]
```

改为：

```python
def _open(account: str, *, strategy_id: str | None = None, **kw) -> str:
    # 不显式给 strategy_id 时默认用账户名——避免历史默认值 "session" 把互不相关场景错塞一组
    return _post("/sessions", {"account_id": account,
                               "strategy_id": strategy_id or account, **kw})["session_id"]
```

> 这样每个既有场景自动获得 `strategy_id = 账户名`（如 `demo_bank_rotate`），零逐场景改动。

- [ ] **Step 2: 新增多次回测场景**

在 `scenario_bank_frenzy` 函数之后、`SCENARIOS = {` 之前，加：

```python
def scenario_multi_run() -> None:
    """同一策略多次回测：strategy_id='bank_momentum' 在 3 个不同区间各跑一次 close。

    制造 n_runs=3，让排行榜的『最新一次 / 历史最优』有区分（同一账户、买入持有招商银行至区间末）。
    区间起止均取自 ROTATE_DAYS（已确认的真实交易日），避免落在无数据日。
    """
    print("[多次回测] bank_momentum 跨 3 区间各跑一次（n_runs=3）")
    sym = "600036.SH"  # 招商银行
    acc = _account("demo_momentum", cash=20_000_000)
    windows = [("2026-02-09", "2026-03-10"), ("2026-03-17", "2026-04-15"), ("2026-04-22", "2026-05-18")]
    for i, (start, end) in enumerate(windows):
        sid = _open(acc, strategy_id="bank_momentum", level="daily",
                    start_date=start, end_date=end, universe=[sym], fill_timing="next_bar")
        _post(f"/sessions/{sid}/advance", {
            "request_id": f"mom{i}_buy", "to": f"{start}T15:00:00",
            "orders": [{"request_id": f"mb{i}", "symbol": sym, "side": 1, "quantity": 200_000,
                        "trade_date": start, "exec_time": "09:35"}]})
        _post(f"/sessions/{sid}/advance", {"request_id": f"mom{i}_end", "to": f"{end}T15:00:00"})
        _post(f"/sessions/{sid}/close", {})
        _report(sid)
```

- [ ] **Step 3: 注册到 SCENARIOS**

把 `SCENARIOS` 字典（317-322 行）末尾的 bank 行改为追加 `multi_run`：

```python
SCENARIOS = {
    "daily": scenario_daily, "minute": scenario_minute, "scan": scenario_scan,
    "progressive": scenario_progressive, "replay": scenario_replay,
    "bank_rotate": scenario_bank_rotate, "bank_pyramid": scenario_bank_pyramid,
    "bank_limit": scenario_bank_limit, "bank_frenzy": scenario_bank_frenzy,
    "multi_run": scenario_multi_run,
}
```

并在文件顶部 docstring 的场景清单里加一行（约第 19 行 `bank_frenzy` 之后）：

```python
        python examples/session_scenarios.py multi_run     # 同策略跨 3 区间多次回测（喂排行榜 n_runs>1）
```

- [ ] **Step 4: 语法自检**

Run: `.venv/bin/python -c "import ast; ast.parse(open('examples/session_scenarios.py').read()); print('ok')"`
Expected: `ok`

- [ ] **Step 5: 提交**

```bash
git add examples/session_scenarios.py
git commit -m "feat(examples): 每场景带 strategy_id + 新增 multi_run 多次回测(n_runs>1 喂排行榜)"
```

---

## Task 6: 运营重播种 + 线上冒烟（重生 demo state）

> 本任务对运行中的容器服务操作，重置 demo state 后重放 examples，让线上看板的排行榜语义正确可演示。**非代码任务**，无新建文件；引擎/产物零改动。state 全是 demo 数据，整库重置安全。
>
> 前提：容器服务以 `vortex run up backtest` 跑在 `127.0.0.1:8766`，写 token 在 backtest 仓 `.env` 的 `VORTEX_BACKTEST_TOKEN`。

- [ ] **Step 1: 停容器服务**

Run: `vortex run down backtest`
（若该命令不可用，用 `docker ps` 找到 vortex-backtest 容器后 `docker stop <id>`。）
Expected: 容器停止，8766 不再监听。

- [ ] **Step 2: 清旧 demo state（整库重置）**

Run:
```bash
rm -rf ~/vortex/state/reports/sessions/* ~/vortex/state/vortex_backtest.sqlite3*
```
Expected: 旧会话产物与 SQLite（含遗留 `strategy_id="session"` 脏会话）清空；账户由 examples 重建。

- [ ] **Step 3: 重起容器服务**

Run: `vortex run up backtest`
Expected: 服务回到 `127.0.0.1:8766`，`curl -s 127.0.0.1:8766/health` 返回 `{"status":"ok"}`，`curl -s 127.0.0.1:8766/sessions` 返回 `[]`。

- [ ] **Step 4: 重放全部场景（带写 token）**

Run:
```bash
export VORTEX_BACKTEST_TOKEN=$(grep -E '^VORTEX_BACKTEST_TOKEN=' .env | cut -d= -f2-)
.venv/bin/python examples/session_scenarios.py all
```
Expected: 各场景打印「成交/收益/看板链接」，无 `✗ 失败`；`multi_run` 打印 3 行 summary。

- [ ] **Step 5: 验证排行榜（n_runs>1 真实存在）**

Run:
```bash
curl -s 127.0.0.1:8766/strategies | .venv/bin/python -c "import sys,json; \
d=json.load(sys.stdin); \
print('strategies:', [(r['strategy_id'], r['n_runs']) for r in d]); \
mr=[r for r in d if r['strategy_id']=='bank_momentum'][0]; \
assert mr['n_runs']==3, mr; \
print('bank_momentum latest=%s best=%s latest!=best:%s' % (mr['latest']['session_id'][:8], mr['best']['session_id'][:8], mr['latest']['session_id']!=mr['best']['session_id']))"
```
Expected: 打印各策略 (id, n_runs)；`bank_momentum` 的 `n_runs==3`（assert 通过）；并打印 latest/best 短码及是否不同（供眼检——多区间收益通常不同，latest≠best）。

- [ ] **Step 6: 线上看板眼检**

浏览器开 `http://127.0.0.1:8766/ui/#/strategies`，确认：排行榜列出 demo_bank_* / bank_momentum / demo-container 等策略；`bank_momentum` 回测数=3，点进详情有 3 行 run + 叠加图；勾两个策略可进对比视图。

> 无需 commit（纯运营产物，state 不入库）。完成即本计划全部落地。

---

## Self-Review（写计划后自检，已修订）

**Spec 覆盖核对（逐节）：**
- spec §2 聚合键=strategy_id → Task 5 Step 1 `_open` 默认 strategy_id；§2 代表行=最新一次/按总收益排序 → Task 1 `_latest_run` + `strategy_rollup` 排序 + Task 3 `sortStrategies`；§2 叠加 x 轴日历+相对日切换 → Task 4 `drawMultiCurve`/`axisSwitchHtml`；§2 对比不加新端点 → Task 4 `renderCompare` 复用 `/equity`+`/metrics`；§2 读时薄聚合 → Task 2 `_strategy_records`。
- spec §5.1/5.2 端点契约 → Task 2 端点 + Task 1 `_group_row`/`strategy_detail` 字段；§5.3 对比复用 → Task 4。
- spec §6 口径（latest/best/perf_stats 来源/low_confidence/n_days=len(daily)/first_last_run）→ Task 1 helper + Task 2 `_strategy_records` 注释。
- spec §7 看板 IA（主导航/排行榜/详情/对比）→ Task 3+4。
- spec §8 重播种（每场景 strategy_id + 多次回测 + 清旧 state）→ Task 5 + Task 6。
- spec §9 测试（rollup 金标含 latest≠best/空/单 run/accounts；端点排序/404/空；重播种 n_runs>1）→ Task 1（10 例含 latest≠best、tiebreak、window 边界）+ Task 2（排序/详情/404/空）+ Task 6 Step 5（live n_runs==3）。

**占位符扫描：** 无 TBD/TODO；每个 code step 给完整代码；命令带预期输出。

**类型/命名一致性：** `strategy_rollup`/`strategy_detail`/`_run_view`/`_latest_run`/`_best_run`/`_run_window`/`_group_row`（Task1）与 Task2 调用一致；前端 `drawMultiCurve`/`bindAxisSwitch`/`axisSwitchHtml`/`cmpItems`/`cmpSel`/`state.strat_sort`/`state.cmp_axis` 跨 Task3/4 一致；记录字段 `_RUN_FIELDS` 与端点契约 §5.1/5.2 及测试断言字段一致。

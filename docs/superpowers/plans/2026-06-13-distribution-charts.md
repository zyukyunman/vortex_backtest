# 看板二期：分布图表 + guide.html 重写 · 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 看板详情页新增五个分布图表页签（收益分布/回撤分布/换手率/仓位/月度热力），后端一个聚合端点供数；guide.html 静态文档站对齐 sessions 现实。

**Architecture:** 沿一期三层：analytics.py 追加 4 个金标可测纯函数 → app.py 追加 `GET /sessions/{id}/distributions` 聚合端点（读时归约）→ 前端详情页曲线区改六页签（进页一次取齐、切页签零请求、缓存于闭包）。月度热力消费既有 metrics.monthly 纯前端实现。引擎/产物零改动。

**Tech Stack:** Python stdlib math（analytics）、FastAPI、原生 JS + vendored Chart.js、纯静态 HTML（guide）。

**Spec:** `docs/superpowers/specs/2026-06-13-distribution-charts-design.md`

---

## 既有事实（执行者直接信任）

- `analytics.py`（~290 行）已有 `_sorted_items/_returns/_mean/_std/_max_drawdown/_STD_EPS` 等私有件与 `TRADING_DAYS=252`；序列约定 `{"YYYY-MM-DD": value}`。
- `app.py` 报告端点段已有 `_daily_rows(data_store, sid)`、`_strategy_series(row, daily)`（含 initial_cash 基线点）、`_session_or_404/_session_dir/_read_jsonl`；端点段尾部是 `@app.get("/benchmarks")`。
- `tests/test_report_api.py` 有 fixture `client`（SID="rpt-test-session"，daily 轴 02-03→02-05、tv 1010/1012/1012、initial_cash=1000、trades 买 100 卖 112、基线点 02-02=1000）。
- 前端 `web/static/app.js` 252 行（上方 Read 过的终版）；`app.css` 有 `.gran-switch`/`.kpi-table`/`--profit/--loss` 变量；Chart.js vendor 于 `static/vendor/chart.umd.min.js`。
- 回归基线 **176 passed, 8 skipped**。真实验收会话 `6e64f3d6-86d2-45fb-89a2-fc51b42d2909`（82 交易日，2 笔成交）。

## 文件结构

| 动作 | 路径 | 职责 |
|---|---|---|
| Modify | `vortex_backtest/analytics.py` | +4 纯函数：return_histogram / drawdown_episodes / monthly_turnover / exposure_series |
| Modify | `tests/test_analytics.py` | +金标测试 |
| Modify | `vortex_backtest/app.py` | +1 端点 `GET /sessions/{id}/distributions` |
| Modify | `tests/test_report_api.py` | +端点测试 |
| Modify | `vortex_backtest/web/static/app.js` | 详情页六页签 + 5 个渲染函数 |
| Rewrite | `vortex_backtest/web/guide.html` | 静态文档站对齐 sessions |
| Modify | `docs/usage-and-api.md` | 只读接口表 +1 行 |

---

### Task 1: analytics 收益直方图 + 回撤事件 — TDD

**Files:** Modify `vortex_backtest/analytics.py`（末尾追加）、Modify `tests/test_analytics.py`（末尾追加）

- [ ] **Step 1.1: 追加失败测试**

```python
def test_return_histogram_buckets():
    # 日收益 +1% / -0.4% / +0.2% / 0% → 桶 (0.005,0.01] (−0.005,0]×2 (0,0.005]
    series = s([("2026-01-05", 100.0), ("2026-01-06", 101.0), ("2026-01-07", 100.596),
                ("2026-01-08", 100.797192), ("2026-01-09", 100.797192)])
    h = analytics.return_histogram(series)
    assert h["bucket_width"] == 0.005
    by_lo = {b["lo"]: b for b in h["buckets"]}
    assert by_lo[0.005]["hi"] == 0.01 and by_lo[0.005]["count"] == 1      # +1%
    assert by_lo[-0.005]["count"] == 2                                     # -0.4% 与 0%（lo<r≤hi）
    assert by_lo[0.0]["count"] == 1                                        # +0.2%
    assert analytics.return_histogram({})["buckets"] == []


def test_drawdown_episodes_golden():
    # 100→110→99→105→121→115→118：事件1 峰110谷99已收复；事件2 峰121谷115进行中
    series = s([("2026-01-05", 100.0), ("2026-01-06", 110.0), ("2026-01-07", 99.0),
                ("2026-01-08", 105.0), ("2026-01-09", 121.0), ("2026-01-12", 115.0),
                ("2026-01-13", 118.0)])
    eps = analytics.drawdown_episodes(series)
    assert len(eps) == 2
    e1, e2 = eps                                   # 按深度降序：-10% 在前
    assert e1["peak_date"] == "2026-01-06" and e1["trough_date"] == "2026-01-07"
    assert e1["depth"] == pytest.approx(99.0 / 110.0 - 1)
    assert e1["drawdown_days"] == 1 and e1["recovery_days"] == 2 and e1["recovered"] is True
    assert e2["peak_date"] == "2026-01-09" and e2["trough_date"] == "2026-01-12"
    assert e2["recovered"] is False and e2["recovery_days"] is None
    assert analytics.drawdown_episodes({"2026-01-05": 100.0}) == []
```

- [ ] **Step 1.2: 确认失败** — Run: `.venv/bin/python -m pytest tests/test_analytics.py -q`，Expected: 新增 2 FAIL（函数未定义）、原 17 PASS

- [ ] **Step 1.3: 实现（analytics.py 末尾追加）**

```python
def return_histogram(series: Mapping[str, float], *, bucket: float = 0.005) -> dict[str, Any]:
    """日收益直方图：桶 (idx*b, (idx+1)*b]（lo < r ≤ hi，边界含 1e-9 浮点容差），仅回非空桶、按 lo 升序。"""
    _, values = _sorted_items(series)
    counts: dict[int, int] = {}
    for r in _returns(values):
        idx = math.ceil(r / bucket - 1e-9) - 1  # 拍板：边界 1e-9 容差，见执行记录
        counts[idx] = counts.get(idx, 0) + 1
    buckets = [{"lo": round(i * bucket, 6), "hi": round((i + 1) * bucket, 6), "count": c}
               for i, c in sorted(counts.items())]
    return {"bucket_width": bucket, "buckets": buckets}


def drawdown_episodes(series: Mapping[str, float], *, top_n: int = 10) -> list[dict[str, Any]]:
    """回撤事件（峰→谷→收复）：按深度降序取 Top-N；末段未收复 → recovered=False。

    天数按交易日（序列索引差）计，不算自然日。
    """
    dates, values = _sorted_items(series)
    if len(values) < 2:
        return []

    def episode(peak_i: int, trough_i: int, recover_i: int | None) -> dict[str, Any]:
        return {
            "peak_date": dates[peak_i], "trough_date": dates[trough_i],
            "depth": round(values[trough_i] / values[peak_i] - 1, 6) if values[peak_i] else 0.0,
            "drawdown_days": trough_i - peak_i,
            "recovery_days": (recover_i - trough_i) if recover_i is not None else None,
            "recovered": recover_i is not None,
        }

    episodes: list[dict[str, Any]] = []
    peak_i, trough_i, in_dd = 0, 0, False
    for i in range(1, len(values)):
        if values[i] >= values[peak_i]:
            if in_dd:
                episodes.append(episode(peak_i, trough_i, i))
                in_dd = False
            peak_i = i
        else:
            if not in_dd:
                in_dd, trough_i = True, i
            elif values[i] < values[trough_i]:
                trough_i = i
    if in_dd:
        episodes.append(episode(peak_i, trough_i, None))
    episodes.sort(key=lambda e: e["depth"])
    return episodes[:top_n]
```

- [ ] **Step 1.4: 确认通过** — Run: `.venv/bin/python -m pytest tests/test_analytics.py -q`，Expected: 19 passed

- [ ] **Step 1.5: 提交**

```bash
git add vortex_backtest/analytics.py tests/test_analytics.py
git commit -m "feat(backtest): analytics 收益直方图+回撤事件提取(金标)"
```

---

### Task 2: analytics 月度换手率 + 仓位序列 — TDD

**Files:** Modify `vortex_backtest/analytics.py`（末尾追加）、Modify `tests/test_analytics.py`（末尾追加）

- [ ] **Step 2.1: 追加失败测试**

```python
def test_monthly_turnover_golden():
    daily = [_mk_daily("2026-02-03", 700.0, 300.0, []), _mk_daily("2026-02-04", 1010.0, 0.0, []),
             _mk_daily("2026-03-02", 1010.0, 0.0, [])]
    trades = [
        {"trade_date": "2026-02-03", "symbol": "000001.SZ", "side": 1, "quantity": 30,
         "amount": 300.0, "commission": 5.0, "stamp_tax": 0.0, "transfer_fee": 0.0, "realized_pnl": 0.0},
        {"trade_date": "2026-02-04", "symbol": "000001.SZ", "side": 2, "quantity": 10,
         "amount": 100.0, "commission": 5.0, "stamp_tax": 0.05, "transfer_fee": 0.0, "realized_pnl": 1.0},
    ]
    out = analytics.monthly_turnover(trades, daily)
    feb, mar = out["monthly"]
    assert feb["month"] == "2026-02"
    assert feb["turnover"] == pytest.approx(min(300.0, 100.0) / ((1000.0 + 1010.0) / 2))  # 单边/月日均资产
    assert feb["buy_amount"] == 300.0 and feb["sell_amount"] == 100.0
    assert mar["month"] == "2026-03" and mar["turnover"] == 0.0                            # 无成交月=0
    assert out["mean"] == pytest.approx((feb["turnover"] + 0.0) / 2)
    assert analytics.monthly_turnover([], daily) == {"monthly": [], "mean": None}          # 全程无成交→空


def test_exposure_series():
    daily = [_mk_daily("2026-02-03", 900.0, 100.0, []), _mk_daily("2026-02-04", 1000.0, 0.0, [])]
    ex = analytics.exposure_series(daily)
    assert ex["dates"] == ["2026-02-03", "2026-02-04"]
    assert ex["ratio"][0] == pytest.approx(0.1) and ex["ratio"][1] == 0.0
    assert analytics.exposure_series([]) == {"dates": [], "ratio": []}
```

- [ ] **Step 2.2: 确认失败** — Run: `.venv/bin/python -m pytest tests/test_analytics.py -q`，Expected: 新增 2 FAIL、原 19 PASS

- [ ] **Step 2.3: 实现（analytics.py 末尾追加）**

```python
def monthly_turnover(trades: list[Mapping[str, Any]],
                     daily_rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    """月度单边换手率 = min(月买入额, 月卖出额) ÷ 月日均总资产；无成交月=0。

    全程零成交 → {"monthly": [], "mean": None}（契约：分布为空不伪装）。
    """
    if not trades:
        return {"monthly": [], "mean": None}
    amt: dict[str, dict[str, float]] = {}
    for t in trades:
        m = str(t["trade_date"])[:7]
        d = amt.setdefault(m, {"buy": 0.0, "sell": 0.0})
        d["buy" if int(t["side"]) == 1 else "sell"] += float(t["amount"])
    tv: dict[str, list[float]] = {}
    for r in daily_rows:
        tv.setdefault(str(r["trade_date"])[:7], []).append(float(r["total_value"]))
    rows: list[dict[str, Any]] = []
    vals: list[float] = []
    for m in sorted(tv):
        a = amt.get(m, {"buy": 0.0, "sell": 0.0})
        avg_tv = _mean(tv[m])
        turn = (min(a["buy"], a["sell"]) / avg_tv) if avg_tv else None
        rows.append({"month": m, "turnover": round(turn, 6) if turn is not None else None,
                     "buy_amount": round(a["buy"], 2), "sell_amount": round(a["sell"], 2),
                     "avg_total_value": round(avg_tv, 2)})
        if turn is not None:
            vals.append(turn)
    return {"monthly": rows, "mean": round(_mean(vals), 6) if vals else None}


def exposure_series(daily_rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    """仓位水平日序列：持仓市值 ÷ 总资产（0~1）；总资产为 0 → 0.0。"""
    dates, ratio = [], []
    for r in daily_rows:
        dates.append(str(r["trade_date"]))
        total = float(r.get("total_value") or 0.0)
        ratio.append(round(float(r.get("market_value") or 0.0) / total, 6) if total else 0.0)
    return {"dates": dates, "ratio": ratio}
```

注意金标数值核对：测试里 2 月日均资产 = (1000+1010)/2——`_mk_daily(d, cash, mv, …)` 的 total_value = cash+mv（700+300=1000、1010+0=1010）。

- [ ] **Step 2.4: 确认通过** — Run: `.venv/bin/python -m pytest tests/test_analytics.py -q`，Expected: 21 passed

- [ ] **Step 2.5: 提交**

```bash
git add vortex_backtest/analytics.py tests/test_analytics.py
git commit -m "feat(backtest): analytics 月度单边换手率+仓位水平序列(金标)"
```

---

### Task 3: distributions 聚合端点 — TDD

**Files:** Modify `vortex_backtest/app.py`（`@app.get("/benchmarks")` 端点之前插入）、Modify `tests/test_report_api.py`（末尾追加）

- [ ] **Step 3.1: 追加失败测试**

```python
def test_distributions_shape(client):
    d = client.get(f"/sessions/{SID}/distributions").json()
    # 收益直方图：基线序列日收益 +1% / +0.198% / 0% → 三个桶各 1
    h = d["return_histogram"]
    assert h["bucket_width"] == 0.005 and len(h["buckets"]) == 3
    assert sum(b["count"] for b in h["buckets"]) == 3
    # 回撤事件：fixture 序列单调不降 → 无回撤
    assert d["drawdown_episodes"] == []
    # 换手率：2 月 min(买100,卖112)/日均资产 (1010+1012+1012)/3
    mt = d["monthly_turnover"]
    assert len(mt) == 1 and mt[0]["month"] == "2026-02"
    assert mt[0]["turnover"] == pytest.approx(100.0 / ((1010.0 + 1012.0 + 1012.0) / 3))
    assert d["turnover_mean"] == pytest.approx(mt[0]["turnover"])
    # 仓位序列：02-03 EOD 110/1010，02-04 起空仓
    ex = d["exposure"]
    assert ex["dates"] == ["2026-02-03", "2026-02-04", "2026-02-05"]
    assert ex["ratio"][0] == pytest.approx(110.0 / 1010.0) and ex["ratio"][1] == 0.0


def test_distributions_404(client):
    assert client.get("/sessions/nope/distributions").status_code == 404
```

- [ ] **Step 3.2: 确认失败** — Run: `.venv/bin/python -m pytest tests/test_report_api.py -q`，Expected: 新增 2 FAIL（404 路由不存在）

- [ ] **Step 3.3: 实现（app.py，插在 `@app.get("/benchmarks")` 之前，工厂内缩进 4 格）**

```python
    @app.get("/sessions/{session_id}/distributions")
    def session_distributions(
        session_id: str, data_store: DataStore = Depends(get_store)
    ) -> dict:
        """分布图表聚合供数（spec 2026-06-13）：直方图/回撤事件/月度换手/仓位序列一次取齐。"""
        row = _session_or_404(data_store, session_id)
        daily = _daily_rows(data_store, session_id)
        strat = _strategy_series(row, daily)  # 含期初本金基线点，与 metrics 同口径
        trades = _read_jsonl(_session_dir(data_store, session_id) / "trades.jsonl")
        turnover = analytics.monthly_turnover(trades, daily)
        return {
            "return_histogram": analytics.return_histogram(strat),
            "drawdown_episodes": analytics.drawdown_episodes(strat),
            "monthly_turnover": turnover["monthly"],
            "turnover_mean": turnover["mean"],
            "exposure": analytics.exposure_series(daily),
        }
```

- [ ] **Step 3.4: 确认通过 + 全量回归** — Run: `.venv/bin/python -m pytest tests/test_report_api.py -q && .venv/bin/python -m pytest -q`，Expected: 报告 API 16 passed；全量 180 passed, 8 skipped

- [ ] **Step 3.5: 提交**

```bash
git add vortex_backtest/app.py tests/test_report_api.py
git commit -m "feat(backtest): GET /sessions/{id}/distributions 分布图表聚合端点"
```

---

### Task 4: 前端六页签

**Files:** Modify `vortex_backtest/web/static/app.js`、Modify `vortex_backtest/web/index.html`（`?v=6`→`?v=7`）

对照现文件（252 行）做如下精确改动：

- [ ] **Step 4.1: state 加 tab + 详情数据缓存**

第 7 行 state 加 `tab: 'equity'`：

```javascript
  var state = { benchmark: '000300.SH', gran: 'daily', minuteDate: '', benchmarks: [], account: '', tab: 'equity' };
  var detail = null;   // 详情页数据缓存 {sid, m, eq, dist}：切页签零请求
```

- [ ] **Step 4.2: renderDetail 的 Promise.all 增取 distributions**

在 `get('/sessions/' + sid + '/rebalances'),` 之后插入一行：

```javascript
      get('/sessions/' + sid + '/distributions'),
```

并把回调改为（注意 res 索引整体后移）：

```javascript
    ]).then(function (res) {
      state.benchmarks = res[1];
      detail = { sid: sid, m: res[2], eq: res[3], dist: res[5] };
      draw(sid, res[0], res[2], res[3], res[4], res[6]);
    }).catch(fail);
```

（positions 原 res[5] 变 res[6]；distributions 为 res[5]。核对 Promise.all 数组顺序：sessions/benchmarks/metrics/equity/rebalances/distributions/positions——把 distributions 放 rebalances 之后、positions 之前。）

- [ ] **Step 4.3: draw() 里曲线区换页签区**

把这一行：

```javascript
      '<div class="section"><h2>净值曲线（起点 1.0，副轴回撤）</h2><canvas id="eq" height="90"></canvas></div>' +
```

替换为：

```javascript
      '<div class="section"><h2>图表 <span class="gran-switch" id="chart-tabs">' + chartTabsHtml() + '</span></h2>' +
      '<div id="chart-area"></div></div>' +
```

并把 draw() 末尾的 `drawChart(eq);` 替换为：

```javascript
    bindChartTabs();
    renderChartTab();
```

- [ ] **Step 4.4: 追加页签函数（drawChart 函数之前插入；drawChart 本体不动）**

```javascript
  var TABS = [['equity', '净值曲线'], ['returns', '收益分布'], ['drawdowns', '回撤分布'],
              ['turnover', '换手率'], ['exposure', '仓位'], ['heatmap', '月度热力']];

  function chartTabsHtml() {
    return TABS.map(function (t) {
      return '<button data-tab="' + t[0] + '" class="' + (state.tab === t[0] ? 'active' : '') + '">' + t[1] + '</button>';
    }).join('');
  }

  function bindChartTabs() {
    Array.prototype.forEach.call(document.querySelectorAll('#chart-tabs button'), function (btn) {
      btn.addEventListener('click', function () {
        state.tab = btn.dataset.tab;
        Array.prototype.forEach.call(document.querySelectorAll('#chart-tabs button'), function (b) {
          b.className = b.dataset.tab === state.tab ? 'active' : '';
        });
        renderChartTab();   // 用 detail 缓存，零请求
      });
    });
  }

  function cssVar(n) { return getComputedStyle(document.documentElement).getPropertyValue(n).trim() || '#888'; }

  function renderChartTab() {
    var area = document.getElementById('chart-area');
    if (!area || !detail) { return; }
    destroyCharts();   // 页签互斥，全量销毁再画（详情页同时只有一个 chart）
    var fns = { equity: tabEquity, returns: tabReturns, drawdowns: tabDrawdowns,
                turnover: tabTurnover, exposure: tabExposure, heatmap: tabHeatmap };
    fns[state.tab](area);
  }

  function tabEquity(area) {
    area.innerHTML = '<canvas id="eq" height="90"></canvas>';
    drawChart(detail.eq);
  }

  function tabReturns(area) {
    var h = detail.dist.return_histogram;
    if (!h.buckets.length) { area.innerHTML = '<p>数据不足。</p>'; return; }
    area.innerHTML = '<canvas id="dist-c" height="90"></canvas>';
    charts.push(new Chart(document.getElementById('dist-c').getContext('2d'), {
      type: 'bar',
      data: {
        labels: h.buckets.map(function (b) { return (b.lo * 100).toFixed(1) + '~' + (b.hi * 100).toFixed(1) + '%'; }),
        datasets: [{ label: '天数', data: h.buckets.map(function (b) { return b.count; }),
          backgroundColor: h.buckets.map(function (b) { return b.hi <= 0 ? cssVar('--loss') : cssVar('--profit'); }) }],
      },
      options: { animation: false, plugins: { legend: { display: false } } },
    }));
  }

  function tabDrawdowns(area) {
    var eps = detail.dist.drawdown_episodes;
    if (!eps.length) { area.innerHTML = '<p>无回撤事件。</p>'; return; }
    var rows = eps.map(function (e) {
      return '<tr><td>' + esc(e.peak_date) + '</td><td>' + esc(e.trough_date) + '</td>' +
        '<td class="loss">' + pct(e.depth) + '</td><td>' + e.drawdown_days + '</td>' +
        '<td>' + (e.recovered ? e.recovery_days : '进行中') + '</td></tr>';
    }).join('');
    area.innerHTML = '<table class="kpi-table"><thead><tr><th>峰值日</th><th>谷底日</th><th>深度</th>' +
      '<th>回撤天数</th><th>恢复天数</th></tr></thead><tbody>' + rows + '</tbody></table>' +
      '<canvas id="dist-c" height="70"></canvas>';
    charts.push(new Chart(document.getElementById('dist-c').getContext('2d'), {
      type: 'bar',
      data: { labels: eps.map(function (e) { return e.peak_date; }),
        datasets: [{ label: '回撤深度%', data: eps.map(function (e) { return +(e.depth * 100).toFixed(2); }),
          backgroundColor: cssVar('--loss') }] },
      options: { animation: false, plugins: { legend: { display: false } } },
    }));
  }

  function tabTurnover(area) {
    var mt = detail.dist.monthly_turnover;
    if (!mt.length) { area.innerHTML = '<p>无成交。</p>'; return; }
    area.innerHTML = '<canvas id="dist-c" height="90"></canvas>' +
      '<p class="muted">月度单边换手率 = min(月买入额, 月卖出额) ÷ 月日均总资产；均值 ' +
      pct(detail.dist.turnover_mean) + '</p>';
    var mean = detail.dist.turnover_mean;
    charts.push(new Chart(document.getElementById('dist-c').getContext('2d'), {
      data: {
        labels: mt.map(function (r) { return r.month; }),
        datasets: [
          { type: 'bar', label: '换手率%', data: mt.map(function (r) { return r.turnover == null ? null : +(r.turnover * 100).toFixed(2); }),
            backgroundColor: '#0969da' },
          { type: 'line', label: '均值%', data: mt.map(function () { return mean == null ? null : +(mean * 100).toFixed(2); }),
            borderColor: '#9a6700', pointRadius: 0, borderWidth: 1.5, borderDash: [6, 4] },
        ],
      },
      options: { animation: false },
    }));
  }

  function tabExposure(area) {
    var ex = detail.dist.exposure;
    if (!ex.dates.length) { area.innerHTML = '<p>数据不足。</p>'; return; }
    area.innerHTML = '<canvas id="dist-c" height="90"></canvas>';
    charts.push(new Chart(document.getElementById('dist-c').getContext('2d'), {
      type: 'line',
      data: { labels: ex.dates,
        datasets: [{ label: '仓位%', data: ex.ratio.map(function (r) { return +(r * 100).toFixed(2); }),
          borderColor: '#0969da', backgroundColor: 'rgba(9,105,218,.15)', fill: true,
          pointRadius: 0, borderWidth: 1.5 }] },
      options: { animation: false, scales: { y: { min: 0, max: 100 } } },
    }));
  }

  function tabHeatmap(area) {
    var monthly = detail.m.monthly || [];
    if (!monthly.length) { area.innerHTML = '<p>数据不足。</p>'; return; }
    var byPeriod = {};
    var years = [];
    monthly.forEach(function (r) {
      byPeriod[r.period] = r.strategy_return;
      var y = r.period.slice(0, 4);
      if (years.indexOf(y) < 0) { years.push(y); }
    });
    var head = '<tr><th>年\\月</th>';
    for (var mm = 1; mm <= 12; mm++) { head += '<th>' + mm + '月</th>'; }
    head += '</tr>';
    var body = years.sort().map(function (y) {
      var tds = '';
      for (var i = 1; i <= 12; i++) {
        var key = y + '-' + (i < 10 ? '0' : '') + i;
        var v = byPeriod[key];
        if (v == null) { tds += '<td></td>'; continue; }
        // 透明度按 |收益| 线性映射，10% 封顶；红涨绿跌（A 股口径，与 --profit/--loss 一致）
        var a = Math.min(Math.abs(v) / 0.10, 1) * 0.85 + 0.1;
        var bg = v >= 0 ? 'rgba(207,34,46,' + a.toFixed(2) + ')' : 'rgba(26,127,55,' + a.toFixed(2) + ')';
        tds += '<td style="background:' + bg + ';color:#fff" title="' + esc(key) + ' ' + pct(v) + '">' + pct(v) + '</td>';
      }
      return '<tr><th>' + esc(y) + '</th>' + tds + '</tr>';
    }).join('');
    area.innerHTML = '<table class="kpi-table heatmap"><thead>' + head + '</thead><tbody>' + body + '</tbody></table>';
  }
```

- [ ] **Step 4.5: index.html 缓存戳** — `app.js?v=6` → `app.js?v=7`。

- [ ] **Step 4.6: 验证**

```bash
node --check vortex_backtest/web/static/app.js && echo JS-OK
cd /Users/zyukyunman/Documents/vortex/vortex_backtest
VORTEX_WORKSPACE=~/vortex/workspace VORTEX_STATE=~/vortex/state .venv/bin/vortex-backtest serve --port 18772 > /tmp/vbt-p2-smoke.log 2>&1 &
sleep 3
SID=6e64f3d6-86d2-45fb-89a2-fc51b42d2909
curl -sS "http://127.0.0.1:18772/sessions/$SID/distributions" | .venv/bin/python -m json.tool | head -30
curl -sS http://127.0.0.1:18772/ui/static/app.js | grep -c "chart-tabs"   # 期望 ≥2
lsof -ti tcp:18772 | xargs kill
```

- [ ] **Step 4.7: 提交**

```bash
git add vortex_backtest/web/static/app.js vortex_backtest/web/index.html
git commit -m "feat(backtest): 看板详情页六图表页签(收益/回撤/换手/仓位分布+月度热力,切签零请求)"
```

---

### Task 5: guide.html 整页重写

**Files:** Rewrite `vortex_backtest/web/guide.html`（整文件替换为以下内容）

- [ ] **Step 5.1: 整文件替换**

```html
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>vortex_backtest 指南</title>
<style>
  body{font:15px/1.7 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif;color:#1f2328;max-width:880px;margin:0 auto;padding:32px 20px}
  h1{font-size:24px}h2{font-size:18px;margin-top:32px;border-bottom:1px solid #d0d7de;padding-bottom:6px}
  code{background:#f6f8fa;padding:2px 6px;border-radius:6px;font-size:13px}
  pre{background:#f6f8fa;padding:14px;border-radius:8px;overflow-x:auto;font-size:13px}
  table{border-collapse:collapse;width:100%;font-size:14px}
  th,td{border:1px solid #d0d7de;padding:6px 10px;text-align:left}
  th{background:#f6f8fa}
  a{color:#0969da;text-decoration:none}
  .nav{margin:12px 0;font-size:14px}
</style>
</head>
<body>
<h1>vortex_backtest 指南</h1>
<p class="nav"><a href="/ui/">📊 看板</a> · <a href="/docs">🔌 Swagger（交互式 API）</a> · <a href="/redoc">ReDoc</a></p>

<h2>它是什么</h2>
<p>独立 HTTP <strong>会话式回测/账户回放</strong>服务（A 股分钟级）。交互模型：
<strong>建会话 → 按模拟时钟逐步 advance（提交委托+推进）→ close 出报告</strong>。
服务端控 <code>sim_time</code>、按 <code>as_of</code> 强制 point-in-time 取数（防未来函数）；
T+1 / 涨跌停 / 分板手数 / 费用由规则内核强制。</p>

<h2>七步开闭环</h2>
<pre>① POST /accounts                建账户 {account_id, initial_cash}
② POST /sessions                开会话 {account_id, level, start_date, end_date, universe}
③ POST /sessions/{id}/advance   买入+推进 {request_id, to, orders:[{symbol, side:1, quantity, …}]}
④ POST /sessions/{id}/advance   T+1 后卖出
⑤ POST /sessions/{id}/advance   {to:"end"} 推进到期末
⑥ POST /sessions/{id}/close     关闭出报告
⑦ GET  /sessions/{id}/summary   汇总（或一条命令: scripts/backtest_roundtrip.sh）</pre>

<h2>端点速查</h2>
<table>
<tr><th>类别</th><th>端点</th></tr>
<tr><td>写（须鉴权）</td><td><code>POST /accounts</code> · <code>POST /sessions</code> · <code>POST /sessions/{id}/advance|data|close</code></td></tr>
<tr><td>报告</td><td><code>GET /sessions/{id}/summary|daily|trades|rejections|minutes</code></td></tr>
<tr><td>分析</td><td><code>GET /sessions/{id}/metrics|equity|positions|rebalances|distributions</code> · <code>GET /benchmarks</code></td></tr>
<tr><td>其他</td><td><code>GET /health</code> · <code>GET /accounts</code> · <code>GET /symbols/{symbol}</code></td></tr>
</table>

<h2>两条取数路（口径不同）</h2>
<table>
<tr><th>路</th><th>触发</th><th>口径</th><th>分红</th></tr>
<tr><td><strong>data 网关</strong>（推荐）</td><td>配 <code>VORTEX_DATA_URL</code>+token</td><td>RAW 不复权真实价</td><td>除权日显式入账（真实账户）</td></tr>
<tr><td>本地直读（回退）</td><td>配 <code>VORTEX_WORKSPACE</code></td><td>qfq 前复权</td><td>不入账（已吸进价）</td></tr>
</table>
<p>两路总收益近似一致，现金流/估值数值不同，不混用对账。</p>

<h2>鉴权</h2>
<p>写接口 fail-closed：配了 <code>VORTEX_BACKTEST_TOKEN</code> 须带
<code>Authorization: Bearer</code> 或 <code>X-Auth-Token</code>；未配时仅本机回环放行。
容器内监听 0.0.0.0 属非回环——<strong>容器部署必须配 token</strong>（本仓 .env）。</p>

<h2>更多</h2>
<p>完整上手与契约：仓库 <code>docs/usage-and-api.md</code> · 部署 <code>docs/operations.md</code> ·
会话引擎设计 <code>design/18-session-backtest-engine.md</code>。</p>
</body>
</html>
```

- [ ] **Step 5.2: 验证 + 提交**

```bash
curl -sS http://127.0.0.1:8766/guide 2>/dev/null | grep -c "会话式" || echo "（容器未起则跳过，Task 6 一并验）"
git add vortex_backtest/web/guide.html
git commit -m "docs(backtest): guide.html 重写对齐 sessions 现实(替换旧 A 面内容)"
```

---

### Task 6: 文档行 + 全量回归 + 容器重建 + 真实验收

**Files:** Modify `docs/usage-and-api.md`

- [ ] **Step 6.1: usage-and-api.md §4 只读接口表 `/benchmarks` 行之前追加**

```markdown
| GET | `/sessions/{id}/distributions` | 分布图表供数：日收益直方图 / Top-10 回撤事件 / 月度单边换手率 / 仓位水平序列 |
```

- [ ] **Step 6.2: 全量回归** — Run: `.venv/bin/python -m pytest -q && .venv/bin/python -m compileall -q vortex_backtest tests examples && echo OK`，Expected: 180 passed, 8 skipped + OK

- [ ] **Step 6.3: 提交文档**

```bash
git add docs/usage-and-api.md
git commit -m "docs(backtest): distributions 端点入使用文档"
```

- [ ] **Step 6.4: 容器重建 + 真实数据验收**

```bash
vortex run up backtest 2>&1 | tail -3 && sleep 4
SID=6e64f3d6-86d2-45fb-89a2-fc51b42d2909
curl -sS "http://127.0.0.1:8766/sessions/$SID/distributions" | .venv/bin/python -c '
import sys, json
d = json.load(sys.stdin)
assert d["return_histogram"]["buckets"], "直方图空"
assert isinstance(d["drawdown_episodes"], list)
assert d["monthly_turnover"] and d["exposure"]["dates"], "换手/仓位空"
print("DISTRIBUTIONS-OK buckets=%d episodes=%d months=%d days=%d" % (
    len(d["return_histogram"]["buckets"]), len(d["drawdown_episodes"]),
    len(d["monthly_turnover"]), len(d["exposure"]["dates"])))'
curl -sS http://127.0.0.1:8766/ui/static/app.js | grep -c "chart-tabs"   # ≥2
curl -sS http://127.0.0.1:8766/guide | grep -c "会话式"                   # ≥1
```

- [ ] **Step 6.5: 浏览器人工核对** — 打开 `http://127.0.0.1:8766/ui/` 进详情页，六页签逐个点：净值/收益分布/回撤分布/换手率/仓位/月度热力均渲染真实数据；切页签无网络请求（DevTools Network 验证可选）。

- [ ] **Step 6.6: 收尾** — 一期 spec §9 backlog 中"分布类图表""guide.html"两项在 `docs/superpowers/specs/2026-06-12-dashboard-analytics-design.md` §9 标记完成（划线+注记完成日期与本 spec 文件名），提交：

```bash
git add docs/superpowers/specs/2026-06-12-dashboard-analytics-design.md
git commit -m "docs(backtest): 一期 backlog 标记分布图表/guide 已由二期完成"
git log --oneline -8
```

---

## 自检记录（plan self-review）

- **Spec 覆盖**：§2 五图口径→Task 1/2（四纯函数金标）+ Task 4（前端，热力图纯前端消费 metrics.monthly）；§4 端点契约（含退化：无成交→[]、<2 点→空数组、空仓→0、404）→Task 3；§5 看板交互（六页签/切签零请求/destroyCharts/进行中标记/热力着色）→Task 4；guide.html→Task 5；§6 测试策略→Task 1-3 金标+API、Task 4/6 冒烟；容器+真实会话验收→Task 6。无缺口。
- **占位符**：所有代码步骤含完整代码；无 TBD/“同上”。
- **类型一致性**：`return_histogram→{bucket_width,buckets[{lo,hi,count}]}`、`drawdown_episodes→[{peak_date,trough_date,depth,drawdown_days,recovery_days,recovered}]`、`monthly_turnover→{monthly[{month,turnover,buy_amount,sell_amount,avg_total_value}],mean}`、`exposure_series→{dates,ratio}` 在 Task 1/2 定义、Task 3 端点展开（monthly_turnover/turnover_mean 两键）、Task 4 前端消费（detail.dist.* 字段名逐一对应）一致。
- **已知取舍**：直方图桶含基线点首日收益（与 metrics 口径一致，刻意）；热力图月份补零格式 `y + '-' + (i<10?'0':'') + i` 与 period 键 "YYYY-MM" 对齐。

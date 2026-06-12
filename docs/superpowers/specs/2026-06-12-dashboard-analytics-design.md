# 2026-06-12 · 看板接真数据 + 分析报告层（调仓记录/多粒度持仓/基准对比指标）设计 spec

> 目标一句话：把看板从"演示假数据"接回真实会话回测，并补齐用户点名的三项能力——
> **每次调仓的记录、日/周/小时/分钟多粒度持仓快照、与基准对比的绩效指标表**（对标用户提供的
> 量化平台截图）。引擎/撮合**零改动**，纯增量：分析层 + 只读端点 + 看板前端重写。

## 1. 背景与动机

- 2026-06-08 引擎迁会话式（design/18）时，旧"作业面"HTTP 接口整体删除，**连带删除了**
  策略中心/排行榜聚合端点与 equity/metrics 指标计算。
- 看板前端（`web/index.html` + `static/app.js`）未同步改造：调旧接口全 404，
  静默回退内置 `genSeries()` 演示假数据——页面"看起来正常"实际零真实数据
  （2026-06-12 实测确认，见 reports/2026-06-11-runnability-verification.md §5a）。
- 用户需求（截图参照某量化平台策略回测页）：
  ① 每次调仓的记录；② 某周持仓信息；③ 每天甚至每小时的持仓信息更好；
  ④ 收益统计表（总收益/年化/夏普/最大回撤/波动率/信息比率/Beta/Alpha，与基准对比）+
  年度/月度收益统计。

## 2. 已确认的范围决策（用户拍板）

| 决策点 | 结论 |
|---|---|
| 一期范围 | **后端分析 API 全量 + 最小看板**（会话列表页 + 会话详情页） |
| 基准 | **默认 000300.SH（沪深300）**，`benchmark` 参数可指定 `index_daily`/`sw_daily` 中任意代码；看板出下拉选择器 |
| 策略中心/排行榜（按 strategy_id 聚合历次会话、横向比较） | **二期**恢复 |
| 分布类图表（回撤/换手率/仓位分布、月度热力） | **二期** |
| 引擎/撮合/会话语义 | **零改动**（数据已够：snapshots.jsonl 每 bar 含完整持仓明细、daily 含 EOD 持仓、trades.jsonl 逐笔成交） |

## 3. 架构（模块划分）

| 单元 | 职责 | 依赖 | 测试方式 |
|---|---|---|---|
| `vortex_backtest/analytics.py`（新） | **纯函数**：日净值序列 + 基准收盘序列 + 成交列表 → 指标包 / 年度月度统计 / 对齐净值曲线 / 调仓事件 / 持仓粒度切片 | 仅 pandas | 金标单测（手工构造已知序列断言指标值） |
| `vortex_backtest/benchmark.py`（新） | 直读 workspace `index_daily` / `sw_daily` parquet：`load_series(code, start, end)` 取收盘序列；`list_benchmarks()` 出代码目录 | 与 data_adapter 同款直读模式（pyarrow，按代码/日期裁剪） | 单测（fixture parquet） |
| `app.py` 新增 5 个只读 GET 端点 | 见 §4；复用现有 `_session_summary`/JSONL 读取惯例，读时归约（open 会话也能实时出指标） | analytics + benchmark | TestClient API 测试 |
| `web/index.html` + `static/app.js`（重写） | 两页：会话列表 / 会话详情；**删除全部 mock 兜底**（接口失败显式报错，不再播假数据） | 仅新端点 + 已 vendor 的 Chart.js | 冒烟（起服务人工核对 + 端点字段契约测试兜底） |

## 4. 新端点契约

基址同现有服务；全部只读 GET、无鉴权要求（与现有报告端点一致）。

### 4.1 `GET /sessions/{id}/metrics?benchmark=000300.SH&rf=0`

```jsonc
{
  "benchmark": "000300.SH", "benchmark_name": "沪深300",
  "low_confidence": true,            // 有效交易日 < 60 置真（口径沿用旧 A）
  "strategy":  {"total_return":…, "annual_return":…, "sharpe":…, "max_drawdown":…,
                "volatility":…, "win_days_ratio":…},
  "benchmark_stats": {同上结构},     // 基准缺数 → null + "benchmark_data_missing"
  "relative":  {"excess_return":…, "information_ratio":…, "beta":…, "alpha":…,
                "tracking_error":…},
  "annual":  [{"year":2026, "strategy_return":…, "benchmark_return":…, "excess":…,
               "max_drawdown":…, "benchmark_max_drawdown":…, "volatility":…, "sharpe":…}],
  "monthly": [{"month":"2026-02", 同 annual 字段}]
}
```

### 4.2 `GET /sessions/{id}/equity?benchmark=`

起点 1.0 的对齐序列（曲线数据源）：
`{"dates":[…], "strategy":[1.0,…], "benchmark":[1.0,…]|null, "drawdown":[0,…]}`。

### 4.3 `GET /sessions/{id}/positions?granularity=daily|weekly|hourly|minute&date=&week=&limit=&offset=`

持仓快照切片，行 = `{"timestamp", "positions":[{symbol, quantity, available_quantity,
cost_basis, last_price, market_value, unrealized_pnl, unrealized_pnl_ratio, weight}],
"cash", "market_value", "total_value"}`（`weight` = 单标的市值/总资产，分析层补算）：

- `daily`：daily 序列每日 EOD（停牌日 forward-fill，现有语义）；
- `weekly`：每自然周**最后一个交易日**的 EOD 行；
- `hourly`：`snapshots.jsonl` 中每小时**最后一根 bar** 的快照（10:30/11:30/14:00/15:00 档）；
- `minute`：`snapshots.jsonl` 原生逐 bar（必须配 `date=` 单日查询 + 分页，防大响应）。

### 4.4 `GET /sessions/{id}/rebalances`

调仓事件列表（按 `trade_date` 聚合当日全部成交）：

```jsonc
[{"trade_date":"2026-02-03", "n_trades":2,
  "buys":[{symbol, quantity, avg_price, amount}], "sells":[…],
  "fees_total":…, "realized_pnl_total":…,
  "position_diff":[{"symbol":…, "qty_before":0, "qty_after":1000,
                    "weight_before":0.0, "weight_after":0.011}],   // 调仓前后持仓对比
  "cash_after":…, "total_value_after":…}]
```

`position_diff` 由前一交易日 EOD 持仓 vs 当日 EOD 持仓求差（首日 before=空仓）。

### 4.5 `GET /benchmarks`

`[{"code":"000300.SH","name":"沪深300","source":"index_daily"},
  {"code":"801120.SI","name":"食品饮料(申万)","source":"sw_daily"},…]`
（名称取自数据列；缺名称列则回代码本身。）

## 5. 指标口径（集中定义在 analytics.py，单一真值）

- 收益率基于**日频 total_value 序列**（来源 `/daily`，已含停牌 forward-fill）。
- 年化因子 **252**；`annual_return = (1+total_return)^(252/n_days) - 1`。
- `sharpe = (mean(daily_ret) - rf/252) / std(daily_ret) * sqrt(252)`，rf 默认 0、查询参数可调。
- `volatility = std(daily_ret) * sqrt(252)`；`max_drawdown` 取日序列高水位回撤最小值。
- `beta/alpha`：策略-基准**日收益**OLS（alpha 年化）；`tracking_error = std(超额日收益)*sqrt(252)`；
  `information_ratio = 年化超额收益 / tracking_error`。
- 年度/月度切片：按自然年/月分组重算上述口径（组内 n_days 作年化基数）。
- **降级规则**：基准代码无数据 → `benchmark_stats`/`relative` 置 null + 错误提示字段，
  绝对类指标照常输出（不伪装、不 500）；`std==0` 等退化情形比率类指标置 null。
- 有效样本 < 60 交易日 → `low_confidence: true`（看板置灰提示，沿用旧约定）。

## 6. 看板一期（两页，沿用无构建链静态 SPA + vendored Chart.js）

- **会话列表页**：账户选择 → 会话表（session_id 短码 / 状态 open·closed / 区间 / 总收益 /
  最大回撤 / 更新时间），点行进详情。数据：`GET /sessions` + 各会话 `summary`。
- **会话详情页**：
  1. 顶部指标对比表（行=本策略/基准/超额，列=总收益/年化/夏普/最大回撤/波动率/信息比率/Beta/Alpha，
     对标截图）+ 基准下拉（`/benchmarks`）+ `low_confidence` 置灰提示；
  2. 净值曲线（策略 vs 基准，起点 1.0）+ 回撤副轴（`/equity`，Chart.js）；
  3. 年度收益统计表 + 月度收益统计表（`/metrics.annual/monthly`）;
  4. 持仓快照区：粒度切换 日/周/时/分（分钟粒度强制选日期）+ 持仓明细表（含 weight）（`/positions`）;
  5. 调仓记录表：每行一个调仓日，展开看买卖明细与前后持仓 diff（`/rebalances`）;
  6. 成交/拒单原始表（现有 `/trades` `/rejections`）。
- **删除 mock**：接口失败显式横幅报错（"后端不可达/数据缺失"），绝不静默播假数据。

## 7. 错误处理

- 会话不存在 → 404（现有惯例）；`granularity`/`benchmark` 参数非法 → 422 带提示。
- open 会话：读时归约出"当前累积态"指标（与现有 summary 行为一致）。
- 大响应防护：`minute` 粒度必须带 `date=`；所有列表端点 limit/offset 分页（上限同现有 5000）。
- 基准数据缺失：见 §5 降级规则；`/benchmarks` 在 workspace 无 index_daily 时返回空表。

## 8. 测试策略

- `analytics.py` 金标单测：构造已知日收益序列（恒定收益/对称波动/已知回归系数），断言
  夏普/年化/回撤/beta/alpha 到 1e-6；年月切片边界（跨年/单月）各一例。
- `benchmark.py`：fixture parquet 验证代码裁剪/窗口/缺数返回空。
- 端点 API 测试：TestClient + 真实形状 JSONL fixture（复用既有 session fixture 惯例），
  覆盖四粒度持仓、调仓 diff、基准降级、422 参数校验。
- 前端：不上前端测试框架；以端点契约测试兜底 + 起服务人工冒烟（开发自验流程写入计划）。

## 9. 二期 backlog（本 spec 不实现，记录以防丢）

- 分布类图表：回撤分布、换手率分布、仓位分布、月度收益热力图。
- 策略中心/排行榜：按 strategy_id 聚合历次会话、多策略对比（旧 design/13 概念在会话模型下重建）。
- `web/guide.html` 静态文档站内容同步更新（仍是旧 API 叙述）。
- 回测配置区（截图里的基准/交易成本选择联动重跑）——依赖"从看板发起回测"，需写接口鉴权联动，单独议。

## 10. 范围边界

- 引擎/撮合/会话语义/JSONL 产物格式零改动（只读消费）。
- 不动 vortex_data（基准数据走本地直读 workspace，与行情同款模式）。
- 不做用户/权限/多租户；看板仍是只读视图（写操作不进看板）。

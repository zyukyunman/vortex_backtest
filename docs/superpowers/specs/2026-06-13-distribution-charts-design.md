# 2026-06-13 · 看板二期：分布类图表 + guide.html 重写（设计 spec）

> 一期（spec 2026-06-12，已完成）给看板接通了真数据与指标/持仓/调仓能力；
> 本二期补**分布类图表**（对标用户提供的量化平台截图页签）+ 搭车重写过时的 `web/guide.html`。
> 策略中心/排行榜继续推迟（等多策略真实产生）。架构沿一期模式：analytics 纯函数扩展 +
> 一个只读聚合端点 + 看板详情页页签。引擎/产物格式零改动。

## 1. 范围（用户已拍板）

- ✅ 收益分布、回撤分布、换手率分布、仓位分布、月度收益热力图（五个图表页签）
- ✅ `web/guide.html` 静态文档站重写（旧 A 面内容 → sessions 现实）
- ❌ 策略中心/排行榜（推迟）；❌ 看板发起回测（单独议）

## 2. 图表口径定义（本 spec 核心）

| 图表 | 口径定义 | 数据源 | 形态 |
|---|---|---|---|
| **收益分布** | 日收益直方图：固定桶宽 0.5%，对称分桶（…-1%~-0.5%、-0.5%~0、0~0.5%…），正负着色 | 日净值序列（含期初本金基线，一期口径） | Chart.js 柱状图 |
| **回撤分布** | **回撤事件表**：从日净值序列提取 **Top-10 回撤事件**，每事件含：峰值日、谷底日、深度(%)、回撤天数(峰→谷)、恢复天数(谷→收复峰值；未收复标"进行中")。按深度降序 | 日净值序列 | 表格 + 深度柱状图 |
| **换手率分布** | **月度单边换手率** = `min(当月买入额, 当月卖出额) ÷ 当月日均总资产`；另给全期月均值参考线。无成交月份为 0 | trades.jsonl + daily | 按月柱状图 |
| **仓位分布** | **仓位水平时间序列** = `持仓市值 ÷ 总资产` 的日序列（0~100%） | daily | Chart.js 面积图 |
| **月度收益热力图** | 年(行)×月(列)网格，格值=当月收益，红涨绿跌按值深浅着色，空月留白 | 既有 `metrics.monthly`（**纯前端，零后端改动**） | HTML 表格着色（不引热力图库） |

> 口径备注：换手率取单边（min(买,卖)）避免买卖双计；回撤分布选事件表而非直方图——
> 信息量更大（深度+持续+恢复三维），亦是主流平台做法。

## 3. 架构（方案 A：聚合端点，已比选）

沿一期三层模式，全部增量：

| 单元 | 改动 |
|---|---|
| `vortex_backtest/analytics.py` | 追加 4 个纯函数（金标可测）：`return_histogram(series, bucket=0.005)`、`drawdown_episodes(series, top_n=10)`、`monthly_turnover(trades, daily_rows)`、`exposure_series(daily_rows)` |
| `vortex_backtest/app.py` | 追加 1 个只读端点 `GET /sessions/{id}/distributions`，一次返回四组数据；复用 `_daily_rows`/`_read_jsonl`，读时归约（open 会话可用） |
| `web/static/app.js` | 详情页"净值曲线"区扩展为**六页签**：净值曲线│收益分布│回撤分布│换手率│仓位│月度热力（沿用 gran-switch 按钮样式；月度热力消费既有 metrics.monthly，切页签不重复取数） |
| `web/guide.html` | 整页重写：系统概览 / 会话七步流程 / 端点速查表 / 双路口径说明 / 指向 `/docs` Swagger。内容与 README/usage-and-api 对齐，保持纯静态无依赖 |

已弃方案：B 纯前端自算（口径散落、无法金标测、API 消费者拿不到——与一期同理）；
C 四个细分端点（多三次往返无收益）。

## 4. 端点契约

### `GET /sessions/{id}/distributions`

```jsonc
{
  "return_histogram": {                       // 收益分布
    "bucket_width": 0.005,
    "buckets": [{"lo": -0.01, "hi": -0.005, "count": 3}, …]   // 仅非空桶，按 lo 升序
  },
  "drawdown_episodes": [                      // 回撤分布（Top-10，按深度降序）
    {"peak_date": "2026-02-10", "trough_date": "2026-03-05", "depth": -0.083,
     "drawdown_days": 16, "recovery_days": 22, "recovered": true}, …
  ],
  "monthly_turnover": [                       // 换手率分布
    {"month": "2026-02", "turnover": 0.35, "buy_amount": …, "sell_amount": …,
     "avg_total_value": …}, …
  ],
  "turnover_mean": 0.28,                      // 全期月均
  "exposure": {"dates": [...], "ratio": [0.0, 0.11, …]}   // 仓位序列（市值/总资产）
}
```

- 404：会话不存在（沿现有惯例）。
- 退化：无成交 → `monthly_turnover: []`、`turnover_mean: null`；序列 <2 点 → 直方图/回撤为空数组；
  全程空仓 → exposure.ratio 全 0。空数据不炸、不伪装。
- 天数口径：drawdown_days/recovery_days 按**交易日**计（日序列索引差），不算自然日。

## 5. 看板交互

- 页签行替换现"净值曲线"标题区；默认页签=净值曲线；切换销毁旧 Chart 实例（沿一期 destroyCharts 约定）。
- 回撤分布页签：上半事件表（峰值日/谷底日/深度/回撤天数/恢复天数），下半深度柱状图；未收复事件的恢复天数列直接显示"进行中"（与 §2 口径一致）。
- 月度热力：行=年、列=1-12 月，红涨绿跌（沿 `--profit/--loss` 变量），透明度按 |收益| 线性映射（上限 10%）；hover 显示具体值。
- distributions 与 metrics 在进详情页时一次并发取齐；页签切换零请求。

## 6. 测试策略

- analytics 金标：构造已知序列断言——双回撤事件（深度/天数/恢复精确值）、含未恢复事件；
  换手率手算月度值（含无成交月=0）；直方图桶边界（恰在桶边的值归属：`lo < r ≤ hi`）；exposure 含空仓日。
- 端点：复用一期 `tests/test_report_api.py` 的 fixture 会话，断言四组数据形状与退化路径。
- 前端：node --check + venv 起服务对真实会话目检六页签；回归基线 176 passed。

## 7. 范围边界

- 引擎/产物零改动；不动 vortex_data；不引新前端依赖（Chart.js + HTML 表格足够）。
- 策略中心/排行榜、看板发起回测仍在 backlog（一期 spec §9）。

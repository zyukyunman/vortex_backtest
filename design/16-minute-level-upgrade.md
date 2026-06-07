---
title: ADR — 升级为真·分钟级回测（订单分钟时戳 + 逐分钟净值，向后兼容日级）
created: 2026-06-07
status: Accepted（2026-06-07，与负责人确认）
deciders: 项目负责人 / 后端
depends_on: design/15-trader-completion-plan.md
---

# ADR: 真·分钟级回测升级

## Context

现状（design/15）：服务**读 1min 数据**、引擎**内部逐分钟推进**，但对外是**日级订单回放**——
订单只有 `trade_date` + `price_type(open/close)`，在当日**首/末分钟**成交；净值/报告为**日级**
（分钟级净值早先在 P4b 因 SQLite 膨胀被移除）。

负责人的策略**日频与盘中分钟择时都要能用**，因此要求升级为**真·分钟级**：能在**任意指定分钟**
下单/成交，并产出**逐分钟净值**。约束：

- **向后兼容**：现有日级订单（open/close）行为不变，现有脚本/测试不破。
- **数据无需改**：`stk_mins` 已是 1min；`vortex_data` 不动。
- **存储不回潮 P4b 的坑**：分钟净值**不得**塞进 SQLite 的 summary（会膨胀）。
- **A 股口径不变**：T+1、涨跌停、手数、费用、量能、滑点照旧（已逐分钟成立）。

## Decision

**把撮合从"按 open/close 在首/末分钟成交"泛化为"每个订单解析到一个目标分钟、在该分钟的 bar 成交"。**

1. **订单模型**：`OrderCreate` 增可选字段 `exec_time`（`HH:MM` 或 `HH:MM:SS`）。
   - 不填 → 日级：按 `price_type` 解析到当日**首分钟(open)**或**末分钟(close)**（=现行为，不变）。
   - 填了 → 分钟级：解析到 `trade_date + exec_time` **at-or-after** 的那根分钟 bar，在该分钟成交
     （成交价 = 该分钟 `close_qfq`；合法性仍对该分钟 **raw 价**判涨跌停/tick/限价）。
     当日该时刻之后无 bar → `no_market_data` 拒单。
2. **撮合循环**：每个订单先解析出 `(目标时间戳, 取价字段)`，按时间戳分组；分钟循环到该时间戳时成交。
   open/close 只是"解析到首/末分钟"的特例 → 与分钟级**同一条代码路径**。
3. **逐分钟净值**：引擎本就每分钟打快照。落 `minute_equity.csv` **artifact**（逐分钟 cash/市值/净值，
   按策略 + 组合），并加 `GET /backtests/{id}/minutes` 与 `/equity?granularity=minute` 读取它。
   **summary 仍为日级**（不进 SQLite，规避 P4b 膨胀）。
4. T+1、涨跌停、手数、费用、量能、滑点逻辑**不动**（逐分钟天然成立：同日买入不增 `sellable` → 当日不可卖）。

## Options Considered

### 方案 A：引擎内泛化"解析到目标分钟"+ 订单加 exec_time（**推荐**）
| 维度 | 评估 |
|---|---|
| 复杂度 | 低–中：复用现有逐分钟循环，把 open/close 特例并入统一解析 |
| 改动面 | 小：order 模型 1 字段 + 撮合解析 + 分钟 artifact/端点 |
| 向后兼容 | **高**：不填 exec_time 即旧行为 |
| 风险 | 低：金标准用例覆盖 |

**Pros:** 一条撮合路径同时支持日级与分钟级；存储可控；现有契约不破。
**Cons:** 限价单是"在该订单分钟判定可成交即成交"，**非**挂单跨分钟等待（GTC/挂单簿）。

### 方案 B：另起一个独立"分钟引擎"与日级引擎并存
**Pros:** 互不影响。**Cons:** 两条撮合路径、易漂移、又回到"双引擎"老问题（design/14 刚收敛掉）。否决。

### 方案 C：完整日内挂单簿（resting limit + 队列优先 + 跨分钟撮合 + 冲击）
**Pros:** 最贴近真实撮合。**Cons:** 复杂度高（订单生命周期跨分钟、当日失效、部分成交累积），
对"按分钟择时下单回放"是过度设计。**作为将来扩展**（design/16+ 续）。

## Trade-off Analysis

核心权衡是"**够用的分钟择时** vs **完整日内挂单簿**"。A 用最小改动把现有"逐分钟走、首/末分钟成交"
扩成"任意分钟成交 + 逐分钟净值"，正好覆盖"分钟择时策略回放"的诉求，且日级路径零回归；它**不**模拟
跨分钟挂单等待（方案 C），但那是另一类需求（GTC/做市），回放显式委托用不到。需要时再上 C。

## Consequences

- **变容易**：策略可在任意分钟下单、看逐分钟净值；日级与分钟级同一引擎、同一份 A 股规则。
- **变难/代价**：多一个 `minute_equity.csv` artifact（磁盘，非 SQLite）；`/minutes` 大区间返回体可能大
  （分页/采样后续可加）。
- **不变**：summary/daily 口径、对账脚本（按 date 聚合，分钟订单同样按其 trade_date 归集）。
- **需复查**：若将来要 resting-limit / 日内冲击成本 / 按分钟量能曲线，再开方案 C。

## Action Items

1. [ ] `models.OrderCreate` 加 `exec_time: str | None`（校验 `HH:MM[:SS]`）；`OrderOut`/`normalize_order_row` 透传。
2. [ ] `replay_engine`：构建每标的每日的分钟时间轴；把 order 解析为 `(目标 ts, 取价字段)`；撮合循环按 ts 成交；无目标→`no_market_data`。
3. [ ] 引擎写 `minute_equity.csv`（逐分钟，按策略+组合）；`write_reports` 收录；artifact 名 `minute_equity`。
4. [ ] `app`：`GET /backtests/{id}/minutes`（读 artifact）、`/equity?granularity=minute`；summary 不变。
5. [ ] 金标准用例：分钟级（指定分钟成交、不同分钟不同价、at-or-after 解析、日级 open/close 回归）。
6. [ ] 更新脚本/文档：`examples/quickstart.py`、`scripts/backtest_roundtrip.sh`、`docs/quickstart.md`、`usage-and-api`、`design/15`。
7. [ ] 真机分钟级 e2e + 全量 pytest 绿；提交 push。

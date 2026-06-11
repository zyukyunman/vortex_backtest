# 2026-06-11 · 当前版本可运行性验证 + 数据可用性核查 + 文档对齐（设计 spec）

> 目标一句话：**验证 vortex_backtest 当前版本（会话式引擎 + data PIT 网关）基于 vortex_data
> 已落盘数据是否"明确可运行"**；顺手修掉验证中确认的明确缺陷，并把入口文档对齐代码现实。
> 本 session 约束：仅 vortex_backtest 仓可写；vortex_data / vortex_common / workspace 只读。

## 背景与已确认事实（代码核查结论，2026-06-11）

### 契约层面：数据设计符合回测需求

- 两侧 design/17 ↔ design/18 对齐，2026-06-08 双侧落地（data D1–D4，backtest B1–B6 + N8）。
- data 侧 PIT 可见性闸门按数据集分类声明（`provider/visibility.py`）：
  分钟=`trade_time`(ts)；`stk_limit`/`adj_factor`/`suspend_d`/`stock_st`=当日 09:30 可见(daily_at)；
  `dividend`=`effective_from` 公告闸门(写时物化)；宏观/`ths_member` 缺可见戳 → fail-closed。
- 网关 `POST /api/v1/data`（`service/query.py::gateway_query`）：批量数据集、`window.count/range`、
  算子下推、`as_of` 必填强制。
- backtest 消费口径（N8 真实账户）：`gateway_adapter.load(price_mode="raw")` RAW 价撮合/估值 +
  `load_dividends` 除权日显式入账；qfq 前复权固定锚降级为金标 oracle。
- 回测依赖 6 数据集在可见性规格中全部有声明：`stk_mins` `stk_limit` `adj_factor`
  `suspend_d` `stock_st` `dividend`。

### 发现的问题

| # | 问题 | 严重度 | 处置 |
|---|------|--------|------|
| 1 | 文档与代码脱节：旧 A 面（`POST /backtests`、`POST /accounts/{id}/orders`、作业队列、worker）已删；`app.py` 只剩 accounts+sessions 端点、`store.py` 只剩 accounts+sessions 两张表。但 README / CLAUDE.md / `scripts/backtest_roundtrip.sh` / `examples/run_30_day_http_sample.py` / docs/ 仍教旧接口 | 高 | Phase 2 修 |
| 2 | 默认端口缺陷：`cli.py:39`、`app.py:454` 默认 `8767`（= registry.yml 中 vortex_qmt 实盘端口）；backtest 规范端口 `8766` | 中 | Phase 1 修 |
| 3 | 双路语义分叉：配 `VORTEX_DATA_URL` 走网关=RAW+除权入账（N8）；不配走本地直读=qfq 前复权不入分红（N5 遗留口径，作离线开发回退）。有意设计但文档未写明 | 低 | Phase 2 文档写明 |

### 已知风险/未验证项（Phase 0 解决）

- workspace 落盘实况未验：数据集齐全性、日期覆盖、symbol 规模。
- `dividend` 存量数据可能缺 `ex_date`/`effective_from` 列（design/18 N8 明言"存量需重抓方含该列"），
  缺则分红入账失效 → 网关路 fail-closed/降级行为需确认。
- 8765 数据服务网关连通性与 token 配置未验。
- 环境注记：session 内执行类工具曾被平台权限分类器故障阻塞；执行步骤以工具恢复为前提。

## 验收标准（用户已确认）

1. workspace 数据核查通过（6 依赖集覆盖窗口明确、结论成表）。
2. 全量 `pytest -q` 绿 + `compileall` 过。
3. **端到端真实数据回测跑通**（网关主路）：建账户 → 开会话 → advance → close → 报告，
   成交/持仓/NAV/费用形状与数值合理。
4. 入口文档/脚本与代码对齐（README、CLAUDE.md、scripts、examples、docs/）。

## 方案选择

- **A（选定）：验证优先、依赖序推进**——数据核查 → 缺陷修复 → 文档对齐 → 端到端验收。失败可干净归因。
- B：直接端到端冒烟倒逼问题——归因混乱，弃。
- C：只修端口+文档、只跑 pytest——不满足验收标准 3，弃。

## 设计

### Phase 0 · 数据实况核查（只读）

- 列 `~/vortex/workspace/data` 全部数据集目录。
- 用 pyarrow（仓内 .venv）查 6 依赖集：schema、日期覆盖 [min,max]、symbol 数量级、分区结构。
- 三个关键判定：
  - (a) `stk_mins` 与 `stk_limit` 覆盖窗口重叠度——网关路缺当日 stk_limit 的 bar 被丢弃；
  - (b) `dividend` 是否含 `ex_date` + `effective_from` 列——缺则分红入账失效；
  - (c) 分钟 by-date 镜像是否已生成——只影响全市场扫描性能，不影响正确性。
- 网关连通：`GET http://127.0.0.1:8765/api/health`；带 `as_of` 的最小 `POST /api/v1/data`
  （token 取本地 env / .env；找不到向用户索取）。
- 产出：数据可用性结论表（数据集 × 覆盖 × 缺口 × 影响）。
- **dividend 缺列不阻塞**：验收窗口避开除权事件日；"重抓 dividend"记为 vortex_data 侧行动项
  （本 session 不改该仓）。

### Phase 1 · 明确缺陷修复（代码，最小 diff）

- `cli.py` 与 `app.py` 默认端口 `8767` → `8766`（与 registry.yml/ADR-003 对齐）。
- 预留：Phase 0 若暴露适配层与落盘 schema 硬伤，一并修；目前未发现。

### Phase 2 · 文档/脚本对齐现实（仅 backtest 仓）

- `README.md`：A 面接口叙述全替换为 sessions 流程（建账户 → `POST /sessions` →
  `POST /sessions/{id}/advance`（提交委托+推进时钟）→ `POST /sessions/{id}/close` → 报告 GET）；
  数据要求表补 `dividend`；启动/端口口径核对；写明双路口径差异（网关=RAW+分红入账，直读=qfq 离线回退）。
- `CLAUDE.md`：模块图职责更新（store/models/app 现状）；"关键约定"删"回测异步 202+job_id"，
  改会话式描述。
- `scripts/backtest_roundtrip.sh`：改走 sessions API（开闭环：建账户→会话→advance→close→报告），
  作为 Phase 3 端到端验收工具复用。
- `examples/run_30_day_http_sample.py`：同步改 sessions API；工作量超出时标注废弃并指向 roundtrip 脚本。
- `docs/` 活文档（operations / usage-and-api）对齐；`design/NN-*.md` 为历史记录，不改。

### Phase 3 · 验收测试

- 全量 `pytest -q`（含金标 `test_golden_a_equals_b`、`test_golden_raw_vs_qfq`、对抗测试）+
  `python -m compileall -q vortex_backtest tests examples`。
- 端到端主路（网关）：`VORTEX_DATA_URL=http://127.0.0.1:8765` + `VORTEX_DATA_DASHBOARD_TOKEN`，
  服务起 8766；用 Phase 0 选定的真实 symbol/时间窗跑完整会话；校验：有成交、持仓/现金守恒、
  NAV 序列连续、费用为正且量级合理、拒单原因可解释。
- 回退路冒烟（本地直读）：不配 `VORTEX_DATA_URL` 跑一次最小会话，确认离线形态可用。
- 数据缺失必须显式失败（`minute_data_missing` 等），不伪装成功——作为检查点验证一次负路径。

## 错误处理

- Phase 0 发现阻塞性缺口（如 stk_mins 覆盖为空/与 stk_limit 完全不重叠）：停止后续阶段，
  报告缺口与 vortex_data 侧补数行动项，等用户决策。
- 网关不可达/无 token：端到端主路阻塞，先完成回退路冒烟并向用户索取 token。
- pytest 红：按 systematic-debugging 流程定位；修复属 backtest 仓则修，属跨仓则记录。

## 范围边界

- 不改 vortex_data / vortex_common（只读）；跨仓问题记录为行动项。
- 不做新功能、不做性能优化（by-date 镜像缺失只记录）；"优化"仅指本 spec 列明的缺陷修复与文档对齐。
- design/NN 历史文档不改写。

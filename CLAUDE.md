# CLAUDE.md · vortex_backtest 仓库约定

> 给协作者与 AI 的仓库总纲：定位、模块地图、配置分层、关键约定。
> 配置/端口以 vortex_common 的 [ADR-003](../vortex_common/docs/adr/ADR-003-unified-config-architecture.md) + [`config/registry.yml`](../vortex_common/config/registry.yml) 为单一真值源。

## 项目定位

vortex_backtest 是量化系统里的**回测/账户回放服务**：一个独立 HTTP 服务，接收
**账户 + 一批外部委托(订单) + 策略配置**，基于本地分钟行情按真实 A 股规则回放这些
委托，产出可对账的成交 / 拒单 / 持仓 / 日净值 / 汇总报告。

> 定位是**账户回放(replay)**，不是因子/信号研究：输入是**具体委托**(symbol/side/qty/日期)，
> 不是从数据现算的信号。这条边界决定了引擎选型(自研 replay，已去 Qlib)。

**刻意不做**：抓数据、训练、实盘交易、接 QMT。第一阶段只做 A 股现金账户、`1min` 分钟、
前复权 `qfq` 单一口径、多策略独立账户回放。

## 代码仓 vs 运行数据（重要）

- **代码仓**：本仓库。
- **行情 workspace（只读消费）**：vortex_data 抓取/导出的产物，本服务**只读消费**，自身不抓数据。
  容器内恒为 `/workspace`，由环境变量 `VORTEX_WORKSPACE` 指向(loader 自动接 `/data`)。
  数据单向流动：`vortex_data 抓取/导出 → workspace(parquet) → vortex_backtest 只读消费`。
- **状态目录（可写）**：账户 / 订单 / 作业 / 报告(SQLite + 报告产物)。容器内恒为 `/state`，
  由标准变量 `VORTEX_STATE` 指向（ADR-002）。
- **宿主机挂载路径**：由 `vortex run up backtest` 自动用 `~/vortex/{workspace,state}`，
  可用 `VORTEX_*_HOST_ROOT` 覆盖；不再手填挂载路径。

## 模块地图

| 文件 | 职责 |
|------|------|
| `vortex_backtest/cli.py` | 命令行入口 `vortex-backtest`(只剩 `serve` 起服务) |
| `vortex_backtest/app.py` | FastAPI 应用：REST/JSON 端点、写接口鉴权、异步作业队列、托管 /ui 与 /guide |
| `vortex_backtest/models.py` | 请求/响应/资源模型(account / order / job / report / strategy) |
| `vortex_backtest/store.py` | SQLite 持久层(账户 / 订单 / 作业 / 报告 / strategy_meta) |
| `vortex_backtest/data_adapter.py` | pyarrow 直读 workspace 分区 parquet，按 symbol×日期/列裁剪 |
| `vortex_backtest/market_rules.py` | A 股撮合口径：T+1 / 涨跌停 / 分板手数 / 费用 / tick |
| `vortex_backtest/replay_engine.py` | 自研 A 股分钟撮合引擎(撮合 / 账本 / 报告) |
| `vortex_backtest/session_engine.py` | 会话式回测引擎(sessions / data / advance / close，步进 + 崩溃恢复) |
| `vortex_backtest/gateway_adapter.py` | 接 vortex_data PIT 网关读行情 |
| `vortex_backtest/symbols.py` | 代码 ↔ 板块 / 手数 / 各市场代码 |
| `vortex_backtest/docs_site.py` · `web/` | 站内文档站(/guide) + 看板(/ui，index.html + static) |
| `tests/` | 撮合口径 / API / 会话引擎 / 对抗测试 |

## 配置分层（重要）

端口/变量/启动命令的权威来源是 vortex_common 的 `config/registry.yml`(单源) + ADR-003；
派生物由 `vortex cfg gen` 重生。本仓只消费，不再各自硬编码。

- **规范端口 `8766`**(内外一致，容器内==对外；规范见 registry.yml)。
- **workspace / state 标准变量**：`VORTEX_WORKSPACE`(只读行情) · `VORTEX_STATE`(可写状态)。
- **服务监听**：`VORTEX_BACKTEST_HOST` / `VORTEX_BACKTEST_PORT`；对外暴露 = 把
  `VORTEX_BACKTEST_BIND_ADDR` 设为 `0.0.0.0` 并用 `vortex run up backtest` 启动。
- **写接口鉴权**：`VORTEX_BACKTEST_TOKEN`(非回环必配)。

### 启动两形态

- **本地 venv = 开发/调试**：`pip install -e '.[dev]'`，`export VORTEX_WORKSPACE=…`，
  `.venv/bin/vortex-backtest serve --port 8766`。
- **容器 = 部署**：`vortex run up backtest`(单服务) / `vortex run deploy`(全栈)。
  应用镜像 `FROM vortex-base`(统一依赖底座)，只叠本仓代码。

## 关键约定

- **只读上游**：行情来自 vortex_data 导出的 workspace，回测进程**只读**；自身不抓数据。
- **回测异步**：`POST /backtests` 入队即回 `202 + job_id`，轮询 `GET /backtests/{job_id}`
  到终态(`completed/failed/cancelled/interrupted`)再取报告；崩溃重启自动重排残留作业。
- **写接口 fail-closed**：配了 token 须带 `Authorization: Bearer` / `X-Auth-Token`，否则 401；
  没配 token 时仅本机回环放行，绑非回环 host 写接口直接 403(避免裸暴露)。
- **数据预检**：缺关键表(`stk_mins`/`adj_factor`/`stk_limit`)→ 作业明确失败为 `*_data_missing`，
  不伪装成功。
- **全链路 qfq 前复权**：指标、买卖、成交、限价、持仓估值、净值都用前复权价；挂单合法性判 raw 价。
- **引擎为自研 replay**：已去 Qlib(ADR-1 rev.2)；旧值 `backtrader/qlib/rqalpha/ashare_replay`
  自动归一为 `replay`。

## 设计文档索引（design/）

> design/NN-*.md 为历史记录，配置/端口仍以 ADR-003 + registry.yml 为准。

01 代码审查 · 02 架构决策 · 03 产品化计划 · 04 看板 UI 规格 · 05 引擎需求 ·
06–07/11–12 qlib 探索与状态 · 08 容器策略 · 09 执行路线图 · **10 API 协议** ·
13 P5 看板设计 · 14 引擎选型复盘(去 Qlib) · 15 trader 完善 · 16 分钟级升级 ·
17 vortex_data 数据需求 · **18 会话式回测引擎** · 19 对抗测试。

---
title: 执行路线图（统一计划，逐项推进）
created: 2026-06-06
status: living
depends_on: design/03-productization-plan.md, design/02-architecture-decisions.md
---

# 执行路线图

把分散在 design/02、03、05–08 的计划合并成一张"按可执行性排序、逐项推进"的清单。已完成的勾掉，未完成的按"不阻塞优先"排队。任务列表（widget）与本表同步。

## 已完成 ✅

- 设计文档：评审 `01` / ADR `02` / 产品化 `03` / UI 规格 `04` / 引擎能力清单 `05` / Qlib spike 结论 `06` / 数据导出需求 `07` / 容器策略 `08`。
- 引擎方向定为 **Qlib + 薄规则层**（源码级 spike 通过，待真机印证）。
- 数据侧**已接受** Qlib 导出需求（`vortex_data/design/10`+`11`，规划其 P7 做 `export qlib`）。
- **阶段0** 基线提交 + 分支 `improve/phase-1`。
- **阶段1** 修两个 qfq 口径 bug（C1 tick 打复权价、C3 复权随窗口漂移）+ `limit_price` 用真实价 + adj≠1 测试，`pytest` **11/11 绿**。
- 环境与部署：Python 3.13 venv、打包声明修复；`Dockerfile`/`compose`/`.env`，**容器内服务已起、`/health` 正常**；`docs/operations.md` 操作指南。

## 待办（按顺序逐个做）

### P4 · 异步作业模型（ADR-3）（已做）✅
`POST /backtests` 改为入队返回 `202 + job_id`（不再请求线程内同步跑完）；后台 worker 执行；`GET /backtests/{job_id}` 轮询状态与进度；启动时把残留 `running` 标 `interrupted` 并重排；相同入参幂等去重。**引擎无关，不会被 Qlib 迁移作废。**

### P4b · 报告聚焦日级（已做）✅
负责人定调：**服务非可视化导向**，回测报告**到每日净值级别即可，不需要分钟级**。据此：summary 不再内嵌分钟快照、不写 `minute_equity.csv`、删除 `/minutes` 端点与模型 `minutes` 字段。顺带消除了"把分钟数据整坨塞进 SQLite"的臃肿（A3）。报告 = 日净值曲线 + 当日成交/拒单/持仓 + 摘要指标。

### P-CLI · 命令行 + 协议交互 ← 现在做
负责人定调：**先把命令行和协议（HTTP API）交互做扎实，之后做看板才有意义。** 为服务补 `vortex-backtest` 命令行：`serve`（起服务）+ `account/order/backtest/report/symbol` 子命令通过 HTTP 协议与服务交互（提交回测→轮询作业→取日级报告）。并把协议与异步作业生命周期文档化。

### P5 · 回测结果看板（推迟，非取消）
"服务不在于可视化"，但 CLI+协议扎实后再做看板**是有意义的**——因此**推迟**到 P-CLI 之后。`design/04` 规格保留备查；报告先以 JSON/CSV（日级）交付。

### P6 · 强化（核心已做）✅
- 写接口 token 鉴权（建账户/下单/提交回测）：配 `VORTEX_BACKTEST_TOKEN` 即强制；未配时仅回环放行、绑非回环 host 则 fail-closed 403。✅
- 错误脱敏：作业失败只回安全错误码；未知异常脱敏为 `internal_error`，完整堆栈只进服务端日志。✅
- 撮合参数可按回测覆盖（`BacktestCreate.execution`：费率/印花税/过户费/参与率/滑点）。✅
- 待选：基准 / 相对收益指标（可选，未做）。

### P2/P3 · Qlib 引擎迁移 + 数据访问（ADR-1/2）← 待 Linux + 数据侧 P7
等 `vortex_data` 的 Qlib 导出就绪后：删 backtrader、薄规则层接 Qlib `Exchange`、用 `FileStorage` 直接读盘、跑 `spike` 4 项验收 → 把 ADR-1 转 Accepted。

**进展（2026-06-06，见 design/11）**：数据访问已做 **C2 分区裁剪**（按 `symbol=` 只读所需标的）；用**真实数据（20260601–05）**经当前引擎端到端回测**验证通过**（T+1 / 科创手数 / qfq / 异步 / 日级报告）。**Qlib 本机装不上**（py3.13 + macOS arm64 无 wheel）→ Qlib 真机回测走**镜像（linux/amd64）**。当前自研引擎已可在真实数据上出可信日级回测。

**Qlib 真机印证通过（2026-06-06，见 design/12）**：在 `linux/amd64` 镜像里用 pyqlib 0.9.7 对 **vortex_data 真实导出的 qlib 数据**（`qlib_smoke`）跑通 `Exchange.deal_order`（手数取整、真单成交 @ 真实价、T+1 需我们规则层锁）。**数据链路 vortex_data `export qlib` → 镜像 → qlib Exchange 已打通**；镜像/脚本：`Dockerfile.qlib` + `scripts/build-qlib-image.sh`。**下一步**：写 Qlib 后端 `replay_engine`（薄规则层接 Exchange）复用现有作业/报告/CLI → ADR-1 转 Accepted。

## 顺序理由

- **先 CLI+协议，再看板**：负责人定调；看板推迟到 CLI+协议扎实后做（那时才有意义）。
- **P2/P3 放最后**：依赖数据侧 P7。但 **P4/P6/CLI 引擎无关**，现在就能做、且 Qlib 迁移后不浪费。
- 每项独立小步提交、`pytest` 必绿再进下一项。

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

### P6 · 强化
写接口 token 鉴权（建账户/下单/提交回测）+ 错误信息脱敏（M6）；费率/印花税/滑点/参与率可配置（M4）；基准/相对收益（可选）。

### P2/P3 · Qlib 引擎迁移 + 数据访问（ADR-1/2）← 待数据侧 P7
等 `vortex_data` 的 Qlib 导出就绪后：删 backtrader、薄规则层接 Qlib `Exchange`、用 `FileStorage` 直接读盘、跑 `spike` 4 项验收 → 把 ADR-1 转 Accepted。

## 顺序理由

- **先 CLI+协议，再看板**：负责人定调；看板推迟到 CLI+协议扎实后做（那时才有意义）。
- **P2/P3 放最后**：依赖数据侧 P7。但 **P4/P6/CLI 引擎无关**，现在就能做、且 Qlib 迁移后不浪费。
- 每项独立小步提交、`pytest` 必绿再进下一项。

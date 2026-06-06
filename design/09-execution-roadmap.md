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

### P4 · 异步作业模型（ADR-3）← 现在做
`POST /backtests` 改为入队返回 `202 + job_id`（不再请求线程内同步跑完）；后台 worker 执行；`GET /backtests/{job_id}` 轮询状态与进度；启动时把残留 `running` 标 `interrupted` 并重排；相同入参幂等去重。**引擎无关，不会被 Qlib 迁移作废。**

### P4b · 报告体积治理（A3）
分钟级产物落 Parquet/CSV 文件，DB 只存摘要指标 + artifact 路径；`/minutes` 分页。避免把巨型 JSON 塞 SQLite。

### P5 · 回测结果看板原型（design/04）
HTML 看板：回测列表、单次概览（净值/回撤曲线 + KPI）、成交、拒单原因分布、持仓、分钟钻取、多策略对比；轮询作业状态。连（异步化后的）API。

### P6 · 强化
写接口 token 鉴权（建账户/下单/提交回测）+ 错误信息脱敏（M6）；费率/印花税/滑点/参与率可配置（M4）；基准/相对收益（可选）。

### P2/P3 · Qlib 引擎迁移 + 数据访问（ADR-1/2）← 待数据侧 P7
等 `vortex_data` 的 Qlib 导出就绪后：删 backtrader、薄规则层接 Qlib `Exchange`、用 `FileStorage` 直接读盘、跑 `spike` 4 项验收 → 把 ADR-1 转 Accepted。

## 顺序理由

- **P4 先于 P5**：异步化改了 API 形状（`202`+轮询），先定下来，看板才不用返工；P4b 的"分钟产物落文件"也影响看板取数端点。
- **P2/P3 放最后**：依赖数据侧 P7。但 **P4/P5/P6 都引擎无关**，现在就能做、且 Qlib 迁移后不浪费。
- 每项独立小步提交、`pytest` 必绿再进下一项。

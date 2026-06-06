---
title: 真实数据回测验证 + Qlib 本地状态
created: 2026-06-06
status: findings
---

# 真实数据回测验证 + Qlib 本地状态

第一个交接版本在**真实数据**上的端到端验证记录。

## 1. 真实数据回测（通过 ✅）

- 数据：`vortex_data/workspace/data`，`stk_mins` 覆盖 **20260601–20260605（5 交易日、1min、数千标的）**，配齐 `adj_factor / stk_limit / suspend_d / stock_st / calendar / instruments`。
- 用**当前（自研）引擎**经 HTTP 服务 + CLI 跑通端到端真实数据回测（账户 100 万，batch `b1`）：

| 项 | 结果 |
|---|---|
| 成交 | `000001.SZ` 买 1000@10.99(0601)、卖 1000@10.99(0603)；`688169.SH` 买 200@107.98(0601) |
| T+1 | 0601 买、0603 卖 —— 通过，**0 拒单** |
| 科创手数 | `688169.SH` 200 股 —— 符合科创 200 起 |
| 日净值 | 5 天曲线，期末 **1,000,117.59**，收益 **+0.012%**，最大回撤 **-0.078%** |
| 耗时 | ~11s（分区裁剪后只读 2 个标的） |

**意义**：在真实 `adj_factor` 上印证了 phase-1 的 qfq 口径修复（C1/C3）与各 A 股规则（T+1、分板手数），并验证了异步作业 + 日级报告链路。

## 2. C2 修复：分区裁剪

`data_adapter` 按 `symbol=` 分区目录只读所需标的（`stk_mins` 5525 文件 → 命中的 2 个；`adj_factor`/`stock_st` 同理）。不破坏 C3（`adj_factor` 仍取该标的全历史最新作 qfq 基准）。单测：`test_read_optional_prunes_by_symbol_partition`。
- 剩余可优化：`stk_limit`/`suspend_d` 按 `date` 分区，目前全读（数百小文件）；后续可按 date 裁剪。

## 3. Qlib 本地状态（阻塞，走 Linux）

- `pip install pyqlib` 在本机**失败**：Python 3.13 + macOS arm64 无匹配 wheel（`No matching distribution found for pyqlib`）。
- 结论：**Qlib 的真机回测应在 Linux 跑**（manylinux wheel 可用），正好契合 Linux 服务器迁移；本机若强行试需 Python ≤3.12 且 arm64 wheel/源码编译，不稳。
- 所以 **P2/P3 的 Qlib 引擎迁移仍按计划**：放到 Linux 环境 + `vortex_data` 的 Qlib 导出（其 P7）就绪后做；在此之前**当前自研引擎已能在真实数据上产出可信的日级回测**。

## 4. 下一步

- 在 Linux（服务器/容器，manylinux）装 `pyqlib`，跑 `design/06` 的 4 项 spike 验收 → 把 ADR-1（引擎选型）转 Accepted。
- `vortex_data` 完成 Qlib 导出后，`vortex_backtest` 接 Qlib `FileStorage` + 薄规则层。
- 可选优化：`stk_limit` 按 date 裁剪；更大区间的取数内存（分钟快照已不入库/报告，主要成本在取数）。

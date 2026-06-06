---
title: Qlib 真机回测验证（镜像内 · 真实数据）
created: 2026-06-06
status: findings
depends_on: design/06-qlib-spike-findings.md
---

# Qlib 真机回测验证（镜像内 · 真实数据）

## 结果（通过 ✅）

在 Docker 镜像里（`linux/amd64`，pyqlib 0.9.7）对 **vortex_data 真实导出的 qlib 数据**（`vortex_data/workspace/qlib_smoke`：`SH600000` / `SZ000001`，day 频，2026-01..06）跑通 spike：

- `qlib.init(provider_uri=/qlib, region=cn)` 成功读到导出的 FileStorage；
- **手数取整 150 → 100**（PASS）；
- **`Exchange.deal_order` 真单成交**：买 `SH600000` 1000 股 @ **11.82**（2026-01-05 真实收盘），费用 = `min_cost` 5.0；
- **同日卖出被 Qlib 放行** → 真机印证「**T+1 必须我们规则层冻结**」（design/06 的源码结论）；
- 区间内无涨停日（涨停拒单机制已源码确认）；NAV = 999,985.54。

→ **design/06 的源码级结论得到「真机 + 真实数据」印证**：Qlib 能承载 A 股**外部订单回放**；T+1 / 科创手数 / 分项费用留一层薄规则层即可。**ADR-1（引擎选 Qlib）可据此从 Proposed 推进。**

## 数据链路（已打通）

```
vortex_data: `vortex-data export qlib`（其 P7，已实现，自包含 .bin，无需 pyqlib）
   → Qlib FileStorage：calendars/ + instruments/ + features/<code小写>/<field>.day.bin
vortex_backtest: vortex-backtest-qlib 镜像（含 pyqlib）
   → qlib.init 读盘 → Exchange/Order/Position 回放订单
```

## 关键坑：为什么必须 amd64 镜像

- **pyqlib 只有 x86_64(manylinux) wheel，没有 linux-arm64**；Apple Silicon 默认 arm64 容器（和本机 py3.13）都装不上（`No matching distribution found for pyqlib`）。
- 解法：镜像按 **`linux/amd64`** 构建（Docker Desktop Rosetta 模拟；x86_64 Linux 服务器上原生、更快）。
- Docker Hub 偶发超时：底座用**本地 tag**（`vbtqlib-base:amd64` = 拉过的 `python:3.12-slim`），让 BuildKit 用本地镜像、不去 Hub 校验（同 vortex_data `scripts/build-image.sh` 的离线底座思路）。
- 一键：`scripts/build-qlib-image.sh [run <qlib数据目录>]`（`Dockerfile.qlib` 精简，只装 qlib + spike）。

## 待办：把引擎真正切到 Qlib

当前是 **spike**（离散验证 `deal_order` 机制），还不是完整回放引擎。下一步：

1. 在 vortex_backtest 写 **Qlib 后端的 `replay_engine`**：薄规则层接 Qlib `Exchange` —— T+1 冻结、科创 200+1/北交所手数、分项费用拆解、**以数据为准的涨跌停**（用导出的 `limit_up/limit_down` 构造 `limit_buy/limit_sell`）；复用现有**异步作业 / 日级报告 / CLI** 框架，镜像内对 qlib 数据出日级报告。
2. ADR-1 转 **Accepted**（真机印证已具备）。
3. 分钟级：vortex_data 导出 1min qlib 数据后同法（注意分钟会话网格 + 日字段广播）。

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

## Qlib 后端引擎跑通（2026-06-06，真实数据，镜像内）

spike 之后，已在 vortex_backtest 落地 **`QlibReplayEngine`**（`vortex_backtest/qlib_engine.py`）——薄规则层直接读 Qlib FileStorage 数据 + 复用 `market_rules.AShareRuleEngine`（T+1 冻结、分板手数、以数据为准的涨跌停、分项费用/滑点）+ 复用现有**异步作业 / 日级报告 / CLI** 框架。引擎按 `EngineName.qlib` 选用（`worker.engine_for`），pyqlib 在 `qlib.init` 处**惰性 import**，故本机无 qlib 也能跑 `pytest` 16/16。

在 `vortex-backtest-qlib` amd64 镜像内，挂载仓库源码 + `qlib_smoke`（SH600000/SZ000001，2026-01-05…06-05，100 个交易日）跑 `spike/qlib_engine_demo.py`：

```
docker run --rm --platform linux/amd64 \
  -v <repo>:/work -w /work -e PYTHONPATH=/work \
  -v <qlib_smoke>:/qlib:ro -e VORTEX_QLIB_PROVIDER_URI=/qlib \
  vortex-backtest-qlib \
  python spike/qlib_engine_demo.py --symbols 000001.SZ,600000.SH --start 2026-01-05 --end 2026-06-05
```

结果（**完整日级回测报告**）：

- `status: completed`，`#trades 2`、`#rej 0`、`#daily 100`；
- 2 笔买入按真实收盘成交：`000001.SZ` 1000 @ **11.50**、`600000.SH` 1000 @ **11.82**；
- `total_value 996,989.77`、`total_return -0.30%`、`max_drawdown -0.41%`；`daily_equity.csv` 落盘。
- **关键修复**：qlib 以 float32 存价（如 `11.8199996…`），直接喂 `is_tick_aligned`（0.01 网格）会全量 `invalid_price_tick` 拒单 → `qlib_engine` 建 bar 时把 OHLC/涨跌停 `round(…, 2)` 回真实价，tick 校验通过。

→ **至此 design/06 源码结论 + 真机 spike + 完整引擎日级报告三重印证齐备**：Qlib 数据层 + 薄 A 股规则层产出与自研引擎同款日级报告。

## 收尾

1. ✅ Qlib 后端 `replay_engine` 已写并跑通（本节）。
2. ✅ **ADR-1 转 Accepted**（`design/02` 已更新）。
3. ✅ **分钟级也已打通**（下一节）。

## 分钟级 Qlib 回测（2026-06-06，真实分钟数据，镜像内）

vortex_data 导出器原生支持 `--freq 1min`（源 `stk_mins`，日级字段广播到每分钟，落 `<field>.1min.bin`），**数据侧无需改**。导出 2 标的 5 个交易日：

```
vortex-data export qlib --freq 1min --symbols 000001.SZ,600000.SH \
  --start 20260601 --end 20260605 --out workspace/qlib_smoke_1min
# → calendar_size 1205 (5 日 × 241 分钟)，features 24 (2 标的 × 12 字段)
```

`QlibReplayEngine` 加分钟支持：`frequency=1min` 时 `D.features(freq="1min")` 读分钟 bar，再 `_aggregate_minute_to_daily` 归约为**当日会话 bar**（open=首分钟 / close=末分钟 / high·low=日内极值 / volume=日内累加；factor/涨跌停/paused 整日常数取末值），其后与日频走**完全相同**的建 bar / 回放 / 日级报告路径——因为订单是日级（trade_date + open/close）。

镜像内对 `qlib_smoke_1min` 跑 `--freq 1min`：

```
status: completed  #trades 2  #rej 0  #daily 5
T 2026-06-01 000001.SZ BUY 1000 @ 10.99；600000.SH BUY 1000 @ 9.32
total_value 999,999.80  return -2e-7  maxDD -0.00038
```

**关键修复**：分钟频必须把 `end_time` 顶到当日 `23:59:59`，否则 qlib 把裸日期 `2026-06-05` 当 `00:00:00`，会丢掉**最后一个交易日**的全部盘中分钟（首跑只出 4 日，修后 5 日）。新增纯 pandas 单测覆盖归约口径 + float32 tick 回正（`tests/test_qlib_engine.py`，`pytest` 19/19）。

> 说明：当前订单模型是日级（无盘中时间戳），故分钟数据用于**忠实定位会话开/收价 + 日内量能累加**；未来若要盘中择时下单（按分钟时间戳撮合），扩订单模型后让回放走分钟时间轴即可——数据与读取层已就绪。

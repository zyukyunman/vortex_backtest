---
title: 给 vortex_data 的需求 —— Qlib 兼容数据落盘导出
created: 2026-06-06
status: requirements
audience: vortex_data 开发
origin: vortex_backtest（消费方）
depends_on: design/05-backtest-engine-requirements.md, design/06-qlib-spike-findings.md
---

# 需求：vortex_data 增加 Qlib 兼容数据导出

> 本文是 **vortex_backtest 提给 vortex_data 的需求**。背景：vortex_backtest 引擎方向定为 Qlib（见 `design/05/06`），按负责人意见**直接读硬盘、不走查询服务**。因此需要 vortex_data 把数据**额外导出成一份 Qlib `FileStorage` 目录**，让回测引擎用 `qlib.init(provider_uri=...)` 直接读。建议把本文复制进 `vortex_data/design/`（接其 04–07 之后，如 `08-qlib-export.md`）作为实现条目。

## 1. 目标与边界

**目标**：vortex_data 在现有 Parquet 落盘之外，提供命令把指定 universe/区间/频率的数据导出为 **Qlib 标准 FileStorage 目录**（`calendars/` + `instruments/` + `features/*.bin`），供 vortex_backtest 直接消费。

**边界**：
- 只做**导出/落盘**，不碰策略、回测、撮合（与 vortex_data 既有红线一致）。
- 不要求重写数据管线；这是在已有 `stk_mins/adj_factor/stk_limit/suspend_d/instruments/calendar` 之上加一个导出器。
- `vortex_data/design/04` 已把 "Qlib view export" 列为 v1.1、且已有 `qlib_view.py` 桩——本需求就是把它落实并明确契约。

**好消息（降低工作量）**：不需要自己实现 Qlib 的二进制 `.bin` 格式。Qlib 自带 `scripts/dump_bin.py`，**直接支持 parquet 输入**（`read_as_df` 认 `.parquet`）并支持 `dump_all` / `dump_update`（增量）/ `dump_fix`。所以本需求的核心是：**把数据整理成 dumper 期望的"每标的一张规范表 + 正确字段口径"，再调 dumper。**

## 2. 产物：Qlib FileStorage 目录布局

```
<provider_uri>/
  calendars/
    day.txt            # 交易日，每行 YYYY-MM-DD
    1min.txt           # 分钟时间戳，每行 "YYYY-MM-DD HH:MM:SS"（A股 240 分钟/日会话网格）
  instruments/
    all.txt            # 每行: SYMBOL<TAB>start_datetime<TAB>end_datetime（SYMBOL 大写）
  features/
    sh600000/          # 实例目录小写
      open.day.bin  high.day.bin  low.day.bin  close.day.bin  volume.day.bin
      factor.day.bin  change.day.bin  vwap.day.bin
      limit_up.day.bin  limit_down.day.bin
      open.1min.bin ... close.1min.bin volume.1min.bin factor.1min.bin ...
    sz000001/ ...
```

`.bin` 格式由 dumper 负责：`np.hstack([首行在日历中的下标, 值序列]).astype("<f")`，值按日历重索引（缺失→NaN）。**你不用手写它**，交给 `dump_bin.py`。

## 3. 字段契约（dumper 输入列 → Qlib `$字段`）

每标的一张表（CSV 或 **Parquet**），含 `symbol`、`date` 两个键列 + 下列特征列。Qlib 读取时自动给列名前面加 `$`（即列 `close` → 引擎里 `$close`）。

| 输入列 | 含义 | 口径要求（**MUST**） | Qlib 用途 |
|---|---|---|---|
| `symbol` | Qlib 代码 | 见 §4 符号映射（`SH600000`） | 实例标识 |
| `date` | 时间戳 | day=`YYYY-MM-DD`；1min=`YYYY-MM-DD HH:MM:SS` | 对齐日历 |
| `open/high/low/close` | **原始**未复权 OHLC | **raw 价**，不要预复权 | 估值/成交价 |
| `volume` | 成交量(股) | | 量能上限/PA |
| `factor` | 复权因子 | **`= adj_factor / 该标的全历史最新 adj_factor`，归一化到最新=1**（见 §5） | 手数取整、复权重建 |
| `change` | 日涨跌幅 | `close/prev_close - 1`（日级；1min 当日广播同值） | 涨跌停(float 路径)/展示 |
| `limit_up`/`limit_down` | **原始**涨跌停价 | 来自 `stk_limit`，raw 价（见 §6） | 精确涨跌停判定 |
| `vwap` | 均价(可选) | `amount/volume` | deal_price=vwap 选项 |

> 关键：`close` 必须是 **raw**、复权交给 `factor`——这正是结构性消灭 vortex_backtest 两个 qfq bug（C1 tick 打在复权价上、C3 复权随窗口漂移）的前提。详见 `design/06 §5`。

## 4. 符号映射（MUST）

vortex/Tushare 形如 `600000.SH`，Qlib CN 形如 `SH600000`。导出需做：

```
600000.SH → SH600000
000001.SZ → SZ000001
688981.SH → SH688981   (科创板)
430047.BJ → BJ430047   (北交所)
```

`instruments/all.txt` 里 SYMBOL 大写；`features/` 下实例目录小写（`sh600000`）。dumper 的 `code_to_fname`/`fname_to_code` 会处理大小写，你只需保证 `symbol` 列是 Qlib 代码。

## 5. 复权因子归一化（MUST，最易错）

- Qlib 约定：`close` 为 raw，`factor` 用于把价格还原成复权序列，且 **归一化到最新交易日 factor=1**。
- Tushare `adj_factor` 是后复权累计因子（随时间增大）。导出 `factor = adj_factor / 该标的全历史最新 adj_factor`。
- **"全历史最新"而不是"窗口内最新"**——这条直接修掉 vortex_backtest 的 C3（复权随回测窗口漂移）。
- 含义：发生新除权除息时，该标的历史所有 `factor` 会整体重缩放 → 需对受影响标的**重导 `factor`**（见 §8 增量）。每次导出记录 `as_of`，回测固定快照保证可复现。

验证：任取一标的，`close*factor` 应与已知前复权(qfq)序列在容差内一致（最新日 `close*factor==close`）。

## 6. 涨跌停与停牌编码（需确认，给出推荐）

**涨跌停**：A 股分板限幅不同（主板±10%、ST±5%、科创/创业±20%、北交所±30%、新股首日无限制），且应**以数据为准**。推荐：直接导出 `limit_up`/`limit_down`（raw 价，来自 `stk_limit`），由 vortex_backtest 侧据此构造 Qlib 的 `limit_buy`/`limit_sell` 标记或限价表达式（引擎已读到这两列即可）。这样不依赖单一 float 阈值，分板统一。

**停牌**：Qlib 约定 `$close` 为 NaN 即视为停牌/不可交易。推荐：停牌交易日该标的 `close` 置 NaN（dumper 重索引到日历也会把缺失日补 NaN）。持仓在停牌日按最后可得价估值（引擎侧处理）。
> 决策点：是"`close` 置 NaN"还是"另给 `suspended` 标记列+保留最后价"。推荐前者（贴 Qlib 原生），但若你们更想保留停牌日的最后价用于展示，可两者都给。

## 7. 频率与"日字段广播到分钟"

- 主用 `1min`（回测主频）；同时导出 `day`（日级 NAV/因子/涨跌停参照）。
- Qlib 的 `Exchange` 按其 freq 读取**所有**字段。所以做 1min 回测时，`factor/change/limit_up/limit_down` 这些**日级常量**字段需**广播到当日每个分钟 bar**（同日同值）。
- 影响：分钟 `.bin` 体积显著放大。请配合 vortex_data 现有**磁盘保护**：导出按 (universe, 区间) 限定范围、给存储估算、低于阈值阻断（复用现有 disk guard 逻辑）。

## 8. 增量、快照与 as_of

- 用 dumper 的 `dump_update` 做增量追加新交易日；`dump_fix` 修实例表。
- 新除权除息 → 受影响标的 `factor` 需整体重缩放重导（`dump_all` 该标的或单独重写 `factor.*.bin`）。
- 每次导出登记为一个**快照**（复用 vortex_data 既有 `snapshot_descriptors`/manifest），带 `as_of`；vortex_backtest 的一次回测**固定**某快照以保证可复现。

## 9. CLI（建议，沿用 vortex_data 风格）

```
vortex-data export qlib \
  --freq 1min \
  --universe all_active \
  --start 20240101 --end 20240630 \
  --out /data/qlib_cn_1min \
  [--update]            # 增量
```

内部流程：从既有 Parquet 取数 → 规范成"每标的一张 parquet（含 §3 字段、§4 符号、§5 因子、§6 编码）" → 调 Qlib dumper（`DumpDataAll`/`DumpDataUpdate`，`file_suffix=".parquet"`, `symbol_field_name="symbol"`, `date_field_name="date"`, `freq=...`）→ 落到 `--out`。

依赖：dumper 需 `from qlib.utils import fname_to_code, code_to_fname`。二选一：把 `pyqlib` 作为**导出可选依赖**（`pip install pyqlib`）；或**vendoring** `dump_bin.py` + 这两个小工具函数，避免给数据服务引入整套 qlib。推荐前者（可选 extra）。

## 10. 验收清单

- [ ] `qlib.init(provider_uri=<out>, region="cn")` 成功。
- [ ] `D.calendar(freq="1min")` 返回 A 股会话分钟网格；`D.instruments("all")` 覆盖目标 universe。
- [ ] `D.features([codes], ["$open","$high","$low","$close","$volume","$factor","$change","$limit_up","$limit_down"], freq="1min")` 返回按日历对齐的数据：`close` 为 raw、`factor` 最新=1、停牌日 `close` 为 NaN。
- [ ] `close*factor` 与已知 qfq 参照在容差内一致。
- [ ] `Exchange(freq="1min", codes=[...], deal_price="$close", trade_unit=100)` 能构造；vortex_backtest 的 `spike/qlib_replay_spike.py` 跑出 green。
- [ ] 导出受磁盘保护约束、带 `as_of`/快照、可增量。

## 11. 待确认决策（给数据开发）

1. **落盘方式**：用 Qlib `dump_bin` 产出 `.bin`（推荐，省事）vs 自研 Qlib Parquet 存储后端直接读现有 parquet（免数据二次落盘、省磁盘，但要实现 Qlib `Storage`/`Provider` 后端、工作量大）。建议先 `.bin`，磁盘成为瓶颈再评估后端方案。
2. **停牌编码**：`close=NaN` vs 另给 `suspended` 标记列（§6）。
3. **日字段广播到 1min** vs 只在 day 频导出由引擎 join（§7）。建议广播，换引擎侧简单。
4. **范围策略**：按回测 universe/区间**按需导出** vs 维护一个滚动的"活跃 universe" .bin 库。
5. **依赖**：`pyqlib` 可选依赖 vs vendoring `dump_bin.py`（§9）。
6. **新股首日/ST 限幅**：是否在 `limit_up/down` 数据里已正确反映（应来自 `stk_limit`，以数据为准）。

## 参考 / Sources

- Qlib dumper（确认支持 parquet 输入、`.bin` 格式、calendars/instruments/features 布局、`dump_all/update/fix`）：[scripts/dump_bin.py](https://github.com/microsoft/qlib/blob/main/scripts/dump_bin.py)
- Qlib 交易所对字段的使用（`$close/$factor/$change/$volume`、trade_unit、limit）：[qlib/backtest/exchange.py](https://github.com/microsoft/qlib/blob/main/qlib/backtest/exchange.py)（另见 `design/06`）

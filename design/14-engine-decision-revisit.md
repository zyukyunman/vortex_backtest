---
title: 回测引擎选型复审（ADR-1 rev.2）——回放自研 / Qlib 归研究线
created: 2026-06-07
status: Accepted（2026-06-07，与负责人讨论确认）
revises: design/02-architecture-decisions.md（ADR-1）、design/05-backtest-engine-requirements.md、design/06-qlib-spike-findings.md（§6 结论）
depends_on: design/06-qlib-spike-findings.md
deciders: 项目负责人 / 后端
---

# 回测引擎选型复审（ADR-1 rev.2）

## 0. 一句话结论

**账户/订单回放（含分钟级）用自研引擎 + 直读 parquet，回放关键路径上彻底去掉 Qlib 与 `.bin` 导出；Qlib 不报废，但降级为"将来真做因子/信号研究时才引入的独立研究线"。** 这并不是推翻 `design/06`，而是用它没掌握的两条新证据（实现现实 + 运维现实）做了一次诚实复审——结论恰好回到 `ADR-1` 初稿 line 62 早就写下的触发条件:**"纯订单回放→自研；服务端做信号→组合→才上 Qlib"**。

> 决策钟摆轨迹（便于后人理解，不要再来回摇）：
> `ADR-1` 初稿 = 方案 C（薄自研核心） → `design/05`+`06` = 全面锁定 Qlib（基于源码级核对 + "两仓同栈"诉求） → **本文 = 回放自研 + Qlib 仅留研究线**。本文是当前生效结论，取代 `design/06` §6 的"锁定 Qlib 作引擎"。

---

> **⚠ 更新（2026-06-07，同日定稿后）：Qlib 直接删除，不留对照链。** 进一步讨论后，负责人决定**现在就从仓库删掉 Qlib**（不保留为 CI 对照组）。曾考虑"保留 Qlib 作永久差分对照组"，**已否决**——① 若 Qlib 链与自研共用 `market_rules`/`replay_core`，差分只验数据层、对撮合/账本/规则是瞎的（两条链同样的 bug 一起错）；② 要让 Qlib 独立须用它自己的 `Exchange.deal_order`+`Position`（从没实现的那版，且语义对不齐无法等值比）；③ Qlib 本就不做 A 股规则（T+1/分板手数…），对最该验的部分当不了裁判；④ "data 服务也支持 Qlib"会把刚甩掉的 `.bin` 导出包袱重新引入。**正确性验证改用更权威的口径**：手算金标准用例（覆盖 A 股规则）+ **真实券商对账单回放**（账户回放的 ground truth，负责人将提供）做一次性对照。**落地基线修正**：当前 `main` 仍保留自研+parquet 引擎（`backtrader_adapter.py` 手写撮合 + `data_adapter.py` 直读 parquet，且本就是默认引擎），故删 Qlib 是"剥离"而非"重建"——§6 的"写 parquet 加载器"在以 `main` 为基线时**无需新写**，只需删掉 `qlib_engine.py` 与相关 scaffolding。

## 1. 为什么复审（`design/06` 之后出现的新证据）

`design/06`（2026-06-06）读 Qlib `main` 源码逐项核对，结论"Qlib 能干净承载 A 股订单回放"，**在源码层面是对的**。但它明确把两件事留作"⏳待真机/待落地"，而正是这两件事改变了取舍。

### 1.1 实现现实：所谓"纯 qlib"引擎，从未用过 Qlib 的回测引擎

把计划（`design/06` §3 用 `Exchange.deal_order` 当撮合原语）和**真正落下来的代码**对照：

| 计划（design/06） | 实际（`vortex_backtest/qlib_engine.py`） |
|---|---|
| `qlib.init` → 数据 | ✅ `D.features(...)`（`qlib_engine.py:138`） |
| `Exchange.deal_order(order, position)` 撮合 | ❌ 改用自研 `market_rules.validate_order`（`:260`）+ `replay_core.execute_order`（`:286`） |
| `Position` 账本 / NAV | ❌ 自研 `replay_core.Position` + `aggregate_summaries` |

**Qlib 的回测引擎（`Exchange`/`Position`/`deal_order`）一次都没进调用链。** git `73c1e0f`（"纯 qlib——拆 replay_core、删 backtrader 引擎"）删掉的是 backtrader，但撮合/记账逻辑被原样抽进 `replay_core.py`（其文件头自述"纯 Python，不依赖 pandas / backtrader / qlib"）。

→ **两次实现（backtrader 期、replay_core 期）都自然地自研了撮合。** 这就是"代码走偏"的真相，也是实践给的答案：把"显式委托 + 我们自己的拒单口径/报告契约"塞进 Qlib 的 `Exchange`（它对现金不足是裁剪成**部分成交**，而我们要**拒单**；reason code 也是它一套）摩擦足够大，于是自然绕开。`design/06` 视为 Qlib 最大价值的"高易错基础设施"，在落地中并未被采用。

### 1.2 运维现实：Qlib 数据层在小服务器上是净负担

回放只把 Qlib 当数据读取器，却要为此付一份重导出：

- `cn_1min` 导出 = **519 MB**，是已有 `stk_mins`（**829 MB** parquet）的二次拷贝，且被打散成 **5525 标的 × 12 字段 ≈ 6.6 万个 `.bin` 小文件**（inode 压力、备份/同步/镜像构建慢、增量难——已为此另开 `vortex_data/design/14` 增量导出）。
- `dump_bin` 需要把分钟价宽表 pivot 进内存 → **内存峰值正是小服务器扛不住的根因**。
- `pyqlib` 装不上本机 macOS arm64 → 被迫维护 amd64-only 容器（`Dockerfile.qlib`），本机连单测都跑不全（引擎端到端走 `importorskip(qlib)`，只能进容器）。

### 1.3 约束变化

负责人明确**目标是在小服务器（云主机）上跑通**。这把"导出的内存/磁盘成本"从工程细节升级为硬约束。

---

## 2. 决策

### 现在（账户/订单回放，分钟级）—— 自研引擎 + 直读 parquet

- **引擎**：保留并加固现有 `market_rules.py`（A 股规则）+ `replay_core.py`（撮合/账本/报告）。它们已是引擎无关的纯 Python 资产。
- **数据**：用 `pyarrow`/DuckDB 直读 `vortex_data` 的 parquet，按 symbol/date 做分区裁剪 + 列裁剪，**按需读取**（一次回放只碰订单涉及的票×窗口）。复用 `vortex_data/data/export/qlib_export.py` 已有的 join 口径产出同一 `{(symbol, yyyymmdd): bar}` 契约。
- **去耦**：从回放关键路径移除 `qlib.init`/`D.features`、`VORTEX_QLIB_PROVIDER_URI`、`Dockerfile.qlib`、amd64-only 约束与 `.bin` 导出依赖。
- **通用件借库**：绩效/风险指标用 `empyrical-reloaded` / `quantstats`（不自己算），与 `ADR-1` 一致。

### 将来（因子/信号研究 + ML）—— Qlib 作为独立研究线

当且仅当出现**真实的信号→组合研究需求**时，再把 Qlib 当独立工具引入：它的主轴（signal→strategy→executor）、`NestedExecutor` 日内执行模拟、`impact_cost` 冲击成本建模到那时才真正值钱；只对**研究 universe** 做 qlib 导出，在大机器上跑，**不与回放服务耦合**。

---

## 3. 关键论证：Qlib 回测引擎对"显式订单回放"无不可替代价值

### 3.1 能力对照（浓缩自 `design/06` §2 的源码逐项核对）

| 能力 | Qlib 提供 | 我们已有 | 结论 |
|---|---|---|---|
| 涨跌停拦截 / 量能裁剪 / 费用 / 复权 / 停牌 / 可交易性 | ✅ `Exchange` | ✅ `market_rules.validate_order`（已实现并测试） | **平手**——不是 Qlib 独有 |
| T+1 锁仓 | ❌ 不强制 | ✅ `sellable_quantity` | 不管用不用 Qlib **都得自写** |
| 分板手数（科创 200+1 / 北交所） | ❌ 单一 `trade_unit` | ✅ `_valid_lot` / `round_down_*_lot` | 同上 |
| 印花税/过户费分项、拒单 reason code、分钟/日净值报告契约、现金分红 | ❌ | ✅ / 待定 | 同上 |
| 现金不足 | 裁剪部分成交 | **拒单**（我们的产品语义） | 用 Qlib 反而要改语义或与之较劲 |

**净结论**：Qlib 在回放里能做的，`market_rules.py` 都已做；Qlib 不做的，无论选型都得自写——而我们已经写好。

### 3.2 Qlib 三大"名义优势"在本场景失效或已中和

1. **防未来函数框架保证**（`design/05` 列为最高易错、Qlib 最大价值之一）：对**外生的、已知的订单回放**基本不适用——订单不是从数据现算的信号，没有未来泄漏面。
2. **复权口径做对、结构性灭 C1/C3**：这是"数据约定（raw + factor）"红利，不是"引擎"红利。`market_rules.py` 已采用"挂单合法性判 raw 价、撮合/估值用 qfq"，C1/C3 在自研路径里**已修**。
3. **`NumpyQuote` 性能**：真实，但 (a) 绑死 `.bin` 格式（=要甩的导出包袱），(b) 回放只碰订单涉及的票，数据量本就小。Qlib 的性能工程面向**全市场因子回测**，不是有界回放。

### 3.3 执行模拟：Qlib 真正的强项，但不在回放射程内

`NestedExecutor` + `impact_cost`（把母单按 TWAP/VWAP 拆到分钟级子单、建模冲击成本）确实是自研要下功夫、Qlib 现成的能力。但它只解决**"仓位意图 → 模拟如何执行"**；而回放的输入是**具体委托**（`replay_core` 的 order 带 symbol/side/quantity/trade_date——成交怎么发生已给定）。回放的执行真实度用 `slippage_bps` 假设已足够；将来若真需要，可在滑点函数里加"跟成交量挂钩的冲击项"近似（`impact_cost` 本质也是一个公式）。

---

## 4. 边界线（明确"现在 / 将来"分工，避免再次走偏）

| 维度 | 回放服务（现在） | 研究线（将来，触发后） |
|---|---|---|
| 引擎 | 自研 `replay_core` + `market_rules` | Qlib（signal→组合，`NestedExecutor`） |
| 输入 | 账户 + **具体委托** + 策略配置 | 预测分数 / 信号 |
| 数据 | 直读 parquet（分区/列裁剪，按需） | qlib `.bin` 导出（仅研究 universe） |
| 执行真实度 | `slippage_bps` | `impact_cost` / TWAP / VWAP |
| 绩效指标 | `empyrical-reloaded` / `quantstats` | Qlib report / 同上 |
| 运行环境 | 小服务器 / 本机均可 | 大机器 |
| 触发条件 | 现状 | 出现真实因子/信号研究需求时 |

---

## 5. 影响（Consequences）

- **变容易**：回放可在小服务器/本机跑（无导出、无 6.6 万小文件、内存随单次回放规模）；去 Qlib 依赖后本机即可跑全套单测，CI 简化；`vortex_data` 可退役整套 qlib 导出子系统（**确认无其他消费者后**）；技术栈对新手可读、可调、可控。
- **变难 / 代价**：撮合与口径正确性自负——靠现有测试（git `4008b37`：`.venv` 39 passed / 1 skip / 1 xfail）兜底，并需修两个 xfail 钉住的 known bug；绩效指标需改接 `empyrical`/`quantstats`；将来研究线要用时再建导出。
- **需确认**：`vortex_data` 的 qlib 导出是否**仅**被 backtest 消费？是 → 可一并下线；否 → 导出留给其它消费者，但回放侧仍按本文去耦。

---

## 6. 落地步骤（轻量迁移，非重写）

1. **写 parquet 数据加载器**，替换 `qlib_engine.py` 的数据头（`D.features` + `_aggregate_minute_to_daily` + `_bars_by_symbol_date`）。复用 `vortex_data/data/export/qlib_export.py` 的 join 口径:`stk_mins`(OHLCV) + `adj_factor`→factor + `stk_limit`→涨跌停 + `suspend_d`→停牌 + `bars`→change，产出同一 `{(symbol, yyyymmdd): bar}` 契约（下游 `replay_core` 一行不改）。
2. **A/B 对拍**:保留现 Qlib 引擎为 golden reference，同 case 跑到数值一致，再切默认。
3. **切换默认引擎 + 去 Qlib 运维负担**:移除 `VORTEX_QLIB_PROVIDER_URI`、`Dockerfile.qlib`、amd64-only 约束。
4. **下线导出（确认无其他消费者后）**:`vortex_data` 的 `qlib_export` / `qlib_auto_export` / 增量导出（`vortex_data/design/14`）。
5. **修两个 known bug**（与引擎无关，独立推进）:① 滑点击穿现金校验（容器 xfail）；② 多策略日级聚合缺口失真（核心 xfail）。
6. **绩效指标接库**:`empyrical-reloaded` / `quantstats` 替换手写统计。

---

## 7. 回退 / 未决

- **若回放将来也要日内执行模拟**（罕见）:先用滑点模型的量价冲击项近似；仍不足，再引入"研究线"的 Qlib 产出执行基准喂回放——而非现在就为这个小概率绑死架构。
- **自研撮合正确性兜底**:维持/扩充测试，并以历史 Qlib 真机结果（`design/12`）做长期对拍样本。

---

## 8. 参考 / Sources

- 前序决策:`design/02-architecture-decisions.md`（ADR-1，line 62 触发重评条件）、`design/05-backtest-engine-requirements.md`（能力清单）、`design/06-qlib-spike-findings.md`（源码级核对，本文取代其 §6 结论）、`design/12-qlib-realmachine-validation.md`（真机验证，留作对拍样本）。
- 代码证据:`vortex_backtest/qlib_engine.py`（:138 `D.features`、:260 `validate_order`、:286 `execute_order`）、`vortex_backtest/replay_core.py`、`vortex_backtest/market_rules.py`、`vortex_data/data/export/qlib_export.py`（join 口径）。
- git:`73c1e0f`（纯 qlib 重构=删 backtrader、撮合抽进 replay_core）、`4008b37`（测试现状）。
- 通用件库:[empyrical-reloaded](https://github.com/stefan-jansen/empyrical-reloaded)、[quantstats](https://github.com/ranaroussi/quantstats)。

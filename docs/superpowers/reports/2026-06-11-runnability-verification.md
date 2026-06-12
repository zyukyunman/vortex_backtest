# 2026-06-11 · 可运行性验证报告

> spec：`docs/superpowers/specs/2026-06-11-runnability-verification-design.md`
> 计划：`docs/superpowers/plans/2026-06-11-runnability-verification.md`
> 环境注记：执行期间平台权限分类器故障，执行类命令长时间不可用；数据探查以只读手段完成，
> schema 级核验（dividend 列、覆盖区间数值）待工具恢复后补。

## 1. 数据可用性结论（Phase 0 实测，~/vortex/workspace/data；pyarrow 深度探针 2026-06-11）

workspace 共 **57 个数据集**，日频面覆盖完整且新鲜（多数 20260202→20260610，昨日仍在更新）。
回测依赖集实测：

| 数据集 | 存在 | 实测 | 说明 |
|---|---|---|---|
| `stk_mins`（1min 主行情） | ❌ 缺失 | — | **唯一硬阻塞**（两条取数路都依赖）；用户决策：本 session 验收降级 |
| `stk_mins_by_date`（镜像） | ❌ 缺失 | — | 性能项；分钟源都没有，镜像自然没有 |
| `stk_limit`（涨跌停） | ✅ | 83 文件 / 456,639 行 / 5,533 标的 / 20260202→20260610 | 列 `date,symbol,up_limit,down_limit` 与 adapter 期望一致 |
| `adj_factor`（复权因子） | ✅ | 83 文件 / 456,826 行 / 5,534 标的 / 同窗口 | qfq 金标路用 |
| `suspend_d`（停复牌） | ✅ | 1,479 行 / 270 标的 / 同窗口 | 含 `suspend_type` 列 ✓ |
| `stock_st`（历史 ST） | ✅ | 16,896 行 / 283 标的 / 同窗口 | |
| `dividend`（分红） | ✅ | 88 文件 / 12,383 行 / 5,493 标的 | **N8 关键列齐**：含 `ex_date` + `effective_from`，非空 `ex_date` 1,838 行——分红入账数据就绪 |
| `trade_cal` | ✅ | 104 行 / 20260105→20260611 | |
| `daily`（日线，原 bars） | ✅ | 82 文件 | 非回测硬依赖 |

**判定：**
- (a) `stk_mins`×`stk_limit` 覆盖重叠：**无法成立**——分钟主行情整集缺失；规则面 83 个交易日已就绪等它。
- (b) `dividend` N8 列：✅ **实测含 `ex_date`+`effective_from`**（design/18 N8 的"存量需重抓"已完成）。
- (c) by-date 镜像：未生成（vortex_data 侧性能行动项）。

**结论：除 `stk_mins` 外全部就绪。** 一旦分钟数据补抓（.env 的 `TUSHARE_EXTRA_PERMISSIONS`
已含 stk_mins 权限），20260202→20260610 全窗口可立即支撑端到端回测。

## 2. 网关与配置实况

- 8765 健康检查 ✅：`GET /api/health` → `{"ok": true, "workspace": "/workspace"}`（服务在跑、挂载正确）。
- 网关取数 ❌：`POST /api/v1/data` → `{"ok": false, "error": "unauthorized"}`——实测坐实：
  `vortex_data/.env` 的 `VORTEX_DATA_DASHBOARD_TOKEN=` 为空，Docker 形态下请求源非回环被拒。
  **网关主路需先补 token 并重启 data 服务。**
- `vortex_common/config/vortex.generated.env`：端口规范 data=8765 / backtest=8766 /
  qmt=8767 / trader=8768（与 registry.yml 一致）。

## 3. 修复清单（Phase 1/2，文件级已完成并通过两段评审；commit hash 待工具恢复后回填）

| 修复 | 文件 | 状态 |
|---|---|---|
| serve 默认端口 8767→8766（+回归测试；旧测试 test_cli.py 锁 8767 一并改正） | `vortex_backtest/cli.py` `vortex_backtest/app.py` `tests/test_cli.py` `tests/test_cli_defaults.py` | ✅ 文件完成 |
| cli docstring 旧"提交回测/轮询"语汇 → 会话语汇、指向 design/18 | `vortex_backtest/cli.py` | ✅ |
| roundtrip 脚本重写为 sessions API 七步流程 | `scripts/backtest_roundtrip.sh` | ✅（契约经评审逐字段核对） |
| README 整篇对齐 sessions 现实（双路口径表、会话调用示例） | `README.md` | ✅ |
| CLAUDE.md 定位/模块图/关键约定对齐 | `CLAUDE.md` | ✅ |
| usage-and-api.md 整篇重写（会话协议唯一使用文档） | `docs/usage-and-api.md` | ✅ |
| operations.md 定点修订（CLI 仅 serve、鉴权已实现、网关 env） | `docs/operations.md` | ✅ |
| quickstart.md / usage-guide.md 改指路存根（消除三文档漂移） | `docs/quickstart.md` `docs/usage-guide.md` | ✅ |
| examples 默认端口 8767→8766（×4 处） | `examples/README.md` `examples/session_scenarios.py` | ✅ |
| `*_data_missing` 文档承诺纠偏：loader 层报错、会话面表现为 `no_market_data`（评审发现） | README/CLAUDE/usage/operations 四处 | ✅ |

评审记录：规格合规评审（5 项修复全落实）+ 代码质量评审（无 must-fix）。
遗留 nit（不阻塞）：test_cli_defaults.py 可并入 test_cli.py；roundtrip 选项缺值时报错不友好；
数值参数裸插 JSON（服务端 422 兜底）；`-X GET` 风格。

## 4b. 端到端验收补验（2026-06-12，stk_mins 补齐后；原降级决策解除）

数据侧补齐：`stk_mins` 5,527 标的 / 20260202→20260609（82 交易日，每标的 19,762 行 =
82×241 分钟**零缺口**）+ `stk_mins_by_date` 镜像 82 天同窗口。

- **本地直读路端到端 ✅**：`scripts/backtest_roundtrip.sh`（000001.SZ，2026-02-02→06-09，
  买 02-03 / 卖 02-10）七步全过——买卖各 1 笔成交、T+1 正确、推进 82 交易日、close 出报告。
  数值校验：资产恒等式精确（cash+mv==total）；费用三件套精确（最低佣金 5 元、印花税仅卖出
  5.535=11070×0.05%、过户费）；`cash_after` 逐笔对账到分；日级序列 82 天全覆盖。
- **负路径 ✅**：覆盖窗口外日期 → `no_market_data` 显式拒单、脚本明确失败退出，不伪装成交。
- **网关路（代码层）✅**：跨服务集成测试（`test_adv_integration_realdata.py`）以
  `VORTEX_DATA_REPO` 指向真实 data 仓后真跑——自带测试 token 起真 data 服务（8791），
  真实数据撮合：除权日持仓 NAV 连续、**N8 现金分红 50 元真实入账**、停牌拒单、PIT 闸门
  盘中不可见当日收盘、universe 展开全部通过。生产部署的网关接线仍待用户配
  `VORTEX_DATA_DASHBOARD_TOKEN`（用户主用本地直读，此项转为待选）。
- **测试套件（数据补齐后）**：backtest venv **150 passed**（此前 8 个环境性 skip 中 5 个
  转正通过）+ data venv 3 passed（in-process PIT/分红契约）。
- **两个"数据时代"过时测试已更新**：① 除权日测试原断言 `corporate_actions == []`
  （彼时 dividend 缺 ex_date、N8 休眠）→ 现断言入账恰好一次、金额 ≈50；② BUG-RAWGAP /
  BUG-DIVFIELD 两个 xfail 记录的缺陷已被数据重抓（补 ex_date/effective_from）+
  DIVFIELD-1（不点名 fields）双侧修复，xfail 拆除、转正向契约断言。

**最终结论：当前版本明确可运行。** 数据设计符合回测需求且实测充分（PIT 闸门、除权入账、
费用/T+1/涨跌停规则全部在真实数据上验证通过）。

## 4. 验收结果（降级口径：pytest + 文档对齐；端到端取消——已被 §4b 补验取代）

- **pytest 全量：145 passed, 8 skipped, 0 failed**（skip = 对抗实数据测试，因 `stk_mins`
  缺失正确跳过；带 `VORTEX_WORKSPACE` 重跑结果一致）。
- **compileall（vortex_backtest/tests/examples）：通过**。
- **`bash -n` roundtrip：通过**（另经评审代理逐行目检）。
- **残留 grep 扫描：干净**——入口文档/脚本/示例零残留（8767 旧端口、`POST /backtests`、
  `job_id`、CLI 旧子命令、`VORTEX_INDEX_DATA_DIR`、`VORTEX_DATA_WORKSPACE`、`run_30_day`）。
  `docs_site.py` 经 grep 确认零引用（死代码，见 §5a）。
- 两段评审：规格合规 ✅（5 项修复复核通过）+ 代码质量 ✅（无 must-fix，4 nit 记录在案）。
- 端到端（网关主路/直读回退）：**用户决策降级取消**（stk_mins 缺失；其余前置已全部就绪，
  数据补齐 + token 配置后随时可补验，工具即 `scripts/backtest_roundtrip.sh`）。

## 5a. 本仓后续行动项（超出本 session 范围，另开 session）

1. ~~`/ui` 看板对齐 sessions API~~ → **已完成（2026-06-13，spec 2026-06-12 看板+分析报告层）**：
   看板重写为会话列表/详情两页（mock 全删），新增 analytics/benchmark 模块与 5 个报告端点
   （metrics/equity/positions/rebalances/benchmarks）：基准对比指标、年月统计、日/周/时/分
   多粒度持仓、调仓记录。测试 176 passed；真实会话容器+venv 双形态验证通过。
   残留：`web/guide.html` 静态文档站仍是旧内容（二期）；Docker Desktop 宿主机崩溃，
   新前端的容器镜像待 Docker 恢复后 `vortex run up backtest` 重建（代码已在 main）。
2. **`scripts/reconcile_statement.py` 适配会话产物**：现读旧作业的 `account_summary.json`，
   会话 close 产物为 `reports/sessions/<id>/summary.json`，字段兼容性未验。
3. **`docs_site.py` 疑似死代码**：app.py 的 /guide 已改读静态 `web/guide.html`，
   docs_site 的 markdown 渲染器可能无引用（待 grep 确认后删除或恢复使用）。
4. nit：`tests/test_cli_defaults.py` 可并入 `test_cli.py`。

## 5. 跨仓行动项（vortex_data 侧，本 session 只读不改）

1. ~~补抓 `stk_mins`~~ → **已完成（2026-06-12 实测）**：5,527 标的 82 交易日零缺口。
2. 设置 `VORTEX_DATA_DASHBOARD_TOKEN`（vortex_data/.env）并重启——生产形态网关路的唯一
   剩余前提（网关代码路径已经集成测试用测试 token 验证通过）。用户主用本地直读，转待选。
3. ~~生成 `stk_mins_by_date` 镜像~~ → **已完成**：82 天同窗口。
4. ~~`dividend` schema 核验~~ → **已实测通过**：含 `ex_date`+`effective_from`，非空 ex_date
   1,838 行，N8 分红入账数据就绪，无需重抓。

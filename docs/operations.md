# vortex_backtest 部署与操作指南

面向"第一次把服务跑起来 / 跑测试 / 跑 Qlib spike / 提交基线"的操作手册。遇到 `No module named 'pandas'` 或 `cannot import name 'StrEnum'` 先看 §1 与 §10。

## 1. 环境要求（先看这条）

- **Python ≥ 3.11（推荐 3.12）**。代码用了 `enum.StrEnum`（3.11+ 才有）。用系统自带的旧 `python`（常是 3.9/3.10）会报 `cannot import name 'StrEnum' from 'enum'`。
- **必须在项目 venv 里跑**，不要用裸 `python`。`No module named 'pandas'` 几乎都是因为用了系统解释器、没装依赖。
- 依赖：`pandas/pyarrow/pydantic/fastapi/uvicorn/backtrader`（见 `pyproject.toml`）。Qlib spike 另需 `pyqlib`。

查当前版本：

```bash
python3 --version        # 需 ≥ 3.11；不够就用 pyenv/brew 装 3.12
```

## 2. 安装

```bash
cd /Users/zyukyunman/Documents/vortex/vortex_backtest
python3.12 -m venv .venv
.venv/bin/python -m pip install -U pip
.venv/bin/python -m pip install -e '.[dev]'      # 服务 + 测试依赖
# 跑 Qlib spike 再额外装：
.venv/bin/python -m pip install -e '.[spike]'    # = pyqlib
```

之后**一律用 `.venv/bin/python` 或先 `source .venv/bin/activate`**。

## 3. 数据要求

服务读本地 Tushare workspace（由 `vortex_data` 落盘）：

```bash
export VORTEX_DATA_WORKSPACE=/Users/zyukyunman/Documents/vortex_workspace
```

需要的数据集：`data/stk_mins`（1min 主行情）、`data/adj_factor`（qfq）、`data/stk_limit`（涨跌停）、`data/suspend_d`、`data/stock_st`、`data/instruments`、`data/calendar`。缺 `stk_mins/adj_factor/stk_limit` 会让分钟回测明确失败为 `*_data_missing`（这是预检，不是 bug）。

## 4. 启动服务

```bash
export VORTEX_DATA_WORKSPACE=/Users/zyukyunman/Documents/vortex_workspace
export VORTEX_BACKTEST_STATE_DIR=/tmp/vortex-backtest-state
export VORTEX_BACKTEST_HOST=127.0.0.1
export VORTEX_BACKTEST_PORT=8765
.venv/bin/vortex-backtest
# 健康检查
curl http://127.0.0.1:8765/health
```

基本调用（建账户 / 下单 / 回测 / 查询）见 `README.md` 的"基本调用"。

## 5. 跑测试

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m compileall -q vortex_backtest tests
```

本轮新增的 3 个用例验证阶段1修复：`test_qfq_anchors_to_global_latest_factor_not_window`（C3）、`test_tick_check_uses_raw_price_not_qfq`（C1）、`test_limit_price_compared_in_raw_space`（口径）。

## 6. 跑 Qlib spike（验证引擎选型）

```bash
.venv/bin/python -m pip install -e '.[spike]'
# 下载 Qlib 自带 CN 日线样例（命令以你装的 qlib 版本文档为准）
.venv/bin/python -m qlib.run.get_data qlib_data --target_dir ~/.qlib/qlib_data/cn_data --region cn
# 跑 spike（日线机制冒烟）
.venv/bin/python spike/qlib_replay_spike.py \
  --provider-uri ~/.qlib/qlib_data/cn_data \
  --symbol SH600000 --symbol2 SZ000001 \
  --start 2020-01-02 --end 2020-02-28 --freq day
```

> 注意必须用 `.venv/bin/python`。直接 `python spike/...` 报 `No module named 'pandas'` 就是用了系统解释器。
> 分钟级 + 真实 A 股口径（除权日 NAV、科创手数）需要真数据——见 §9。

## 7. 基线提交与工作分支（请在 Mac 上手动执行）

> 自动化环境无法写本仓 `.git`（权限隔离），所以这步要你本机跑。仓库此前只跟踪了 `README.md`，先固化基线再在分支上改。

```bash
cd /Users/zyukyunman/Documents/vortex/vortex_backtest
# 若提示 index.lock 残留：rm -f .git/index.lock
git add -A
git commit -m "baseline: vortex_backtest 现状 + 架构评审设计文档(评审前基线)"
git checkout -b improve/phase-1
git add -A
git commit -m "fix(phase-1): qfq 口径修复 C1/C3 + limit_price 用真实价 + adj≠1 测试"
.venv/bin/python -m pytest -q     # 确认全绿
```

## 8. 阶段1 改了什么（本轮代码改动）

- **C1**：`market_rules.validate_order` 的 tick、用户 `limit_price`、涨跌停判定改为对**真实价(raw)**，撮合/估值仍用 qfq（`backtrader_adapter` 传入 `raw_fill_price`）。修复"adj≠1 真实数据几乎全被 `invalid_price_tick` 误拒"。
- **C3**：`data_adapter` 的 qfq 基准锚定到**该标的全历史最新**复权因子（不再用窗口内最新），绝对价位不随回测窗口漂移。
- 口径：`limit_price` 是真实价，与 raw 成交价比较。
- 兼容：`raw_fill_price` 为可选参数，旧调用与 adj=1 数据行为不变。

## 9. vortex_data 的 Qlib 数据导出（联动）

回测引擎要直接读 Qlib 数据。`vortex_data` 侧已收录需求（`vortex_data/design/10-qlib-export-requirement.md`）并完成评审接受（`11-qlib-export-assessment.md`，规划为其 P7 阶段，提供 `export qlib` CLI）。落地后：

```bash
# 在 vortex_data 侧导出（接口最终以其实现为准）
vortex-data export qlib --freq 1min --universe all_active --start 20240101 --end 20240630 --out /data/qlib_cn_1min
# 然后把 spike / 回测引擎指向它
.venv/bin/python spike/qlib_replay_spike.py --provider-uri /data/qlib_cn_1min --freq 1min --symbol SH600000 --symbol2 SH688981 ...
```

## 10. 排错速查

| 现象 | 原因 | 处理 |
|---|---|---|
| `No module named 'pandas'` | 用了系统 python，非 venv | 用 `.venv/bin/python`；`pip install -e '.[dev]'` |
| `cannot import name 'StrEnum'` | Python < 3.11 | 装并使用 3.11/3.12 建 venv |
| 缺 `pyqlib` | 没装 spike extra | `pip install -e '.[spike]'` |
| 回测 `minute_data_missing` | workspace 缺 `stk_mins` | 用 `vortex_data` 补分钟数据 |
| `unsupported_frequency/price_adjustment` | 仅支持 `1min`/`qfq` | 按约定传参 |
| `.git/index.lock ... exists` | 残留锁 | `rm -f .git/index.lock` 后重试 |

## 11. 容器化（目标，规划中）

当前为本地运行。Docker 化是产品化计划 `design/03` 阶段6 的内容（`vortex_data` 已有 `Dockerfile`/`compose` 可参照）。目标形态：镜像内启动 HTTP API + 后台 worker（异步作业，见 `design/02` ADR-3）+ 结果看板（`design/04`），挂载 workspace 卷，写接口默认本地绑定、对外需 token。

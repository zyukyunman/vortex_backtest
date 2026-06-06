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

## 11. Docker 部署与镜像加速（Linux 迁移）

镜像策略见 `design/08-container-strategy.md`。本仓已提供 `Dockerfile`（运行镜像，`python:3.12-slim`）、`Dockerfile.spike`（验证 qlib）、`docker-compose.yml`、`.env.example`、`.dockerignore`。

### 11.1 国内拉取 Docker Hub 必配镜像加速

国内直连 Docker Hub 经常超时（实测 `docker pull python:3.12-slim` 2 分钟 0 字节、BuildKit 报 `DeadlineExceeded`）。**build 前必须配 registry 镜像加速**，否则卡在第一步拉 base 镜像。

**推荐：阿里云个人加速器（最稳，账号专属）**

1. 登录容器镜像服务控制台 https://cr.console.aliyun.com → 左侧「镜像工具 → 镜像加速器」，复制你的专属地址，形如 `https://<前缀>.mirror.aliyuncs.com`。
2. 配置（Docker Desktop / Mac 二选一）：
   - GUI：Settings → Docker Engine，在 JSON 里加 `"registry-mirrors": ["https://<前缀>.mirror.aliyuncs.com"]`，Apply & Restart。
   - 命令行：编辑 `~/.docker/daemon.json`（与已有键合并，别覆盖 `builder` 等）：
     ```json
     { "registry-mirrors": ["https://<前缀>.mirror.aliyuncs.com"] }
     ```
     重启 Docker：`osascript -e 'quit app "Docker"'; sleep 5; open -a Docker`
3. 验证：`docker info | grep -A2 "Registry Mirrors"`，再 `docker pull python:3.12-slim` 应能正常下载。

> 注：阿里云 ACR 的「公共」加速已停止同步最新镜像，**个人加速器**仍可用；务必用你账号的专属地址。Linux 服务器上同理，文件在 `/etc/docker/daemon.json`，改完 `sudo systemctl restart docker`。

**备选：公共镜像站**（时好时坏，按你网络挑能用的，放进同一个 `registry-mirrors` 数组），例如 `https://docker.m.daocloud.io`。

### 11.2 构建并在镜像内跑 qlib spike

```bash
cd /Users/zyukyunman/Documents/vortex/vortex_backtest
docker build -f Dockerfile.spike -t vortex-backtest-spike .
# 镜像内 import 自检（快）
docker run --rm vortex-backtest-spike \
  python -c "import qlib; from qlib.backtest.exchange import Exchange; print('qlib', qlib.__version__, 'OK')"
# 数据驱动完整 spike（容器内下 qlib CN 样例，较慢）
docker run --rm vortex-backtest-spike sh -lc \
  "python -m qlib.run.get_data qlib_data --target_dir /root/.qlib/qlib_data/cn_data --region cn && \
   python spike/qlib_replay_spike.py --provider-uri /root/.qlib/qlib_data/cn_data \
     --symbol SH600000 --symbol2 SZ000001 --start 2020-01-02 --end 2020-02-28 --freq day"
```

### 11.3 起服务（compose）

```bash
cp .env.example .env        # 按需改 workspace 路径/端口
docker compose up -d
curl http://127.0.0.1:8765/health
```

默认只绑 `127.0.0.1`；对外暴露需把 `VORTEX_BACKTEST_BIND_ADDR=0.0.0.0` 且先实现并配置 token（design/03 阶段6）。

### 11.4 目标形态（产品化）

镜像内启动 HTTP API + 后台 worker（异步作业，见 `design/02` ADR-3）+ 结果看板（`design/04`），挂载 workspace 卷，写接口默认本地绑定、对外需 token。

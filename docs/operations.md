# vortex_backtest 部署与操作指南

两种跑法：**本地 venv**（开发 / 跑测试）和 **Docker 镜像**（部署 / 启动服务）。
镜像里**只启动服务**。你只想用镜像起服务的话，直接看 §4。

---

## 1. 环境要求

- 本地开发：**Python 3.12 或 3.13**。代码用了 3.11+ 的 `enum.StrEnum`——系统自带的 3.9 会报 `cannot import name 'StrEnum'`，Homebrew 的 3.14 的 `ensurepip` 是坏的，都别用。
- 镜像部署：Docker（本机已装 Docker 29.5 + compose v5）。守护进程要在跑（`docker info` 正常）。
- 数据来自 `vortex_data`（本地 workspace 的 parquet）。

---

## 2. 本地 venv（开发 / 测试）

```bash
cd /Users/zyukyunman/Documents/vortex/vortex_backtest
/opt/homebrew/bin/python3.13 -m venv .venv     # 或 python3.12
.venv/bin/python -m pip install -U pip
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m pytest -q
```

之后一律用 `.venv/bin/python ...`（或先 `source .venv/bin/activate`），别用裸 `python3`。

### 2.1 命令行 `vortex-backtest`（serve + 协议客户端）

安装后有命令行入口（改过入口的话先重装一次 `pip install -e '.[dev]'`）。`serve` 起服务，其余子命令通过 HTTP 协议操作运行中的服务：

```bash
.venv/bin/vortex-backtest serve --port 8767           # 起服务
.venv/bin/vortex-backtest account create --id demo --cash 100000
.venv/bin/vortex-backtest order add --account demo --request-id buy-1 \
    --date 2026-01-02 --symbol 000001.SZ --side buy --qty 100 --batch b1
.venv/bin/vortex-backtest backtest run --account demo --start 2026-01-02 --end 2026-01-05 \
    --batch b1 --wait                                  # 提交并轮询到完成
.venv/bin/vortex-backtest report <job_id> --what daily
```

完整协议与命令行参考见 `design/10-api-protocol.md`。**注意 API 是异步的**：`POST /backtests` 返回 `202+job_id`，需轮询 `GET /backtests/{job_id}` 到 `completed` 再取报告（CLI 的 `--wait` 已封装这一步）。

---

## 3. 数据

```bash
export VORTEX_DATA_WORKSPACE=/Users/zyukyunman/Documents/vortex_workspace
```

需要 `data/stk_mins`（1min 主行情）、`data/adj_factor`、`data/stk_limit`，以及可选的 `suspend_d / stock_st / instruments / calendar`。缺关键表时分钟回测会明确失败为 `*_data_missing`（数据预检，不是 bug）。

---

## 4. Docker 起服务（部署，重点）

前提：Docker 守护进程在跑；**基础镜像 `vortex-base:latest` 必须已存在**——没有就先到 vortex_common 构建一次：`(cd ../vortex_common && scripts/build-base-image.sh)`。应用镜像 FROM vortex-base 只叠本仓代码（依赖都在 base，`pip install --no-deps`），改代码秒级重建。迁移背景见 `../vortex_common/docs/migration/README.md`。

### 4.1 一键（compose，推荐）

```bash
cd /Users/zyukyunman/Documents/vortex/vortex_backtest
cp .env.example .env                 # 改 VORTEX_BACKTEST_WORKSPACE 指向你的数据目录
docker compose up -d --build         # FROM vortex-base 构建本仓代码并起服务（端口默认 8767；缺 base 见 §6）
curl http://127.0.0.1:8767/health    # 期望 {"status":"ok"}
docker compose logs -f               # 看日志
docker compose down                  # 停服务
```

- 卷：`/workspace` = 数据（默认**只读**挂载，来自 `VORTEX_BACKTEST_WORKSPACE`）；`/state` = 账户 / 订单 / 作业 / 报告（可写）。
- 端口：默认只绑 `127.0.0.1:8767`（仅本机）。要对外暴露：`.env` 里设 `VORTEX_BACKTEST_BIND_ADDR=0.0.0.0`，并**先实现并配置** `VORTEX_BACKTEST_TOKEN`（写接口鉴权，design/03 阶段6 待做；在此之前靠只绑回环保证安全）。

### 4.2 不用 compose 的等价命令

```bash
docker build --build-arg BASE_IMAGE=vortex-base:latest -t vortex-backtest:latest .
docker run -d --name vortex-backtest \
  -p 127.0.0.1:8767:8767 \
  -v /Users/zyukyunman/Documents/vortex_workspace:/workspace:ro \
  -v "$(pwd)/state:/state" \
  vortex-backtest:latest
curl http://127.0.0.1:8767/health
```

服务调用（建账户 / 下单 / 跑回测 / 查询）见 `README.md`「基本调用」。

---

## 5. 迁移到 Linux 服务器

1. 服务器装 Docker Engine + compose 插件。
2. 拉代码（或把镜像推私有 registry 再拉）。先在该机构建 `vortex-base`（`(cd ../vortex_common && scripts/build-base-image.sh)`）。
3. 准备数据目录（来自 `vortex_data` 的导出），把 `.env` 的 `VORTEX_BACKTEST_WORKSPACE` 指过去。
4. `docker compose up -d --build` → `curl http://127.0.0.1:8767/health`。

镜像策略（统一 base `vortex-base`、各服务各自镜像、后用顶层 compose 整合）见 `design/08-container-strategy.md` 与 `../vortex_common/docs/migration/README.md`。

---

## 6. 排错速查

| 现象 | 原因 | 处理 |
|---|---|---|
| `No module named 'pandas'` | 用了系统 python | 用 `.venv/bin/python`；`pip install -e '.[dev]'` |
| `cannot import name 'StrEnum'` | Python < 3.11 | 用 3.12 / 3.13 建 venv |
| `缺 vortex-base:latest` | 基础镜像没建 | `(cd ../vortex_common && scripts/build-base-image.sh)` 后重试 |
| 回测 `minute_data_missing` | workspace 缺 `stk_mins` | 用 `vortex_data` 补分钟数据 |
| `unsupported_frequency/price_adjustment` | 仅支持 `1min` / `qfq` | 按约定传参 |
| `docker build` 拉 base 很慢 / `DeadlineExceeded` | 直连 Docker Hub 慢 | 重试；**可选**配加速：`~/.docker/daemon.json` 加 `"registry-mirrors": ["https://docker.m.daocloud.io"]`（或你的阿里云个人加速器 `https://<id>.mirror.aliyuncs.com`），重启 Docker |
| `.git/index.lock ... exists` | 残留锁 | `rm -f .git/index.lock` |

---

## 7. 代码 / 分支现状

- 设计文档：`design/01-15`（评审 / ADR / 产品化 / UI 规格 / 引擎选型 / 数据需求 / 容器策略 / API 协议 / trader 完善）。
- 引擎已去 Qlib，回放为自研 A 股分钟撮合 + 直读 parquet（design/14 / ADR-1 rev.2）；backtrader 死依赖已删，正名为 replay（design/15）。
- 部署侧已接入统一 `vortex-base` 基础镜像（见 `../vortex_common/docs/migration/README.md`）。

_（历史"附：本地验证 Qlib"小节已删除——qlib 引擎已于 2026-06-07 移除，见 design/14。）_

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
cd $REPO            # 本仓根目录
python3.13 -m venv .venv     # 或 python3.12
.venv/bin/python -m pip install -U pip
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m pytest -q
```

之后一律用 `.venv/bin/python ...`（或先 `source .venv/bin/activate`），别用裸 `python3`。

### 2.1 命令行 `vortex-backtest`（只剩 serve）

安装后有命令行入口（改过入口的话先重装一次 `pip install -e '.[dev]'`）。命令行只负责起服务；
回测操作（建账户 / 开会话 / advance / close / 报告）统一走 HTTP（见 [usage-and-api.md](usage-and-api.md)）：

```bash
.venv/bin/vortex-backtest serve            # 起服务（默认 127.0.0.1:8766）
```

会话协议参考见 `design/18-session-backtest-engine.md` 与服务自带 `/docs`(Swagger)。

---

## 3. 数据

```bash
export VORTEX_WORKSPACE=$WS                            # 本地直读回退用
export VORTEX_DATA_URL=http://127.0.0.1:8765           # 走 data 取数网关(推荐, RAW+分红入账)
export VORTEX_DATA_DASHBOARD_TOKEN=<token>             # 网关 token(与 data 服务共享同名变量)
```

本地直读需要 `data/stk_mins`（1min 主行情）、`data/adj_factor`、`data/stk_limit`，以及可选的
`suspend_d / stock_st`；网关路另需 data 落盘 `dividend`。缺关键表时 loader 层明确报
`*_data_missing`，会话 advance 表现为 `no_market_data` 拒单/空帧（数据预检，不是 bug）。
两条路口径差异见 README「数据」一节。

---

## 4. Docker 起服务（部署，重点）

前提：Docker 守护进程在跑；**基础镜像 `vortex-base:latest` 必须已存在**——没有就先到 vortex_common 构建一次：`(cd ../vortex_common && scripts/build-base-image.sh)`。应用镜像 FROM vortex-base 只叠本仓代码（依赖都在 base，`pip install --no-deps`），改代码秒级重建。迁移背景见 `../vortex_common/docs/migration/README.md`。

### 4.1 一键（vortex run，推荐）

```bash
vortex run up backtest               # FROM vortex-base 构建本仓代码并起服务（端口 8766；缺 base 见 §6）
curl http://127.0.0.1:8766/health    # 期望 {"status":"ok"}
vortex run logs backtest             # 看日志
vortex run down backtest             # 停服务
```

全栈一把起：`vortex run deploy`。端口规范以 vortex_common 的 `config/registry.yml` + ADR-003 为准（内外一致）。

- 卷：`/workspace` = 数据（默认**只读**挂载，vortex_data 导出）；`/state` = 账户 / 会话 / 报告（可写）。宿主机挂载路径由 `vortex run up backtest` 自动用 `~/vortex/{workspace,state}`，可用 `VORTEX_*_HOST_ROOT` 覆盖。
- 端口：默认只绑 `127.0.0.1:8766`（仅本机，内外一致）。要对外暴露：把 `VORTEX_BACKTEST_BIND_ADDR` 设为 `0.0.0.0` 并用 `vortex run up backtest` 启动，并**先配置** `VORTEX_BACKTEST_TOKEN`（写接口鉴权已实现、fail-closed：未配 token 时非回环 host 直接 403）。

服务调用（建账户 / 下单 / 跑回测 / 查询）见 `README.md`「基本调用」。

---

## 5. 迁移到 Linux 服务器

1. 服务器装 Docker Engine + compose 插件。
2. 拉代码（或把镜像推私有 registry 再拉）。先在该机构建 `vortex-base`（`(cd ../vortex_common && scripts/build-base-image.sh)`）。
3. 准备数据目录（来自 `vortex_data` 的导出）：默认 `~/vortex/{workspace,state}`，可用 `VORTEX_*_HOST_ROOT` 覆盖宿主机根。
4. `vortex run up backtest` → `curl http://127.0.0.1:8766/health`。

镜像策略（统一 base `vortex-base`、各服务各自镜像、后用顶层 compose 整合）见 `design/08-container-strategy.md` 与 `../vortex_common/docs/migration/README.md`。

---

## 6. 排错速查

| 现象 | 原因 | 处理 |
|---|---|---|
| `No module named 'pandas'` | 用了系统 python | 用 `.venv/bin/python`；`pip install -e '.[dev]'` |
| `cannot import name 'StrEnum'` | Python < 3.11 | 用 3.12 / 3.13 建 venv |
| `缺 vortex-base:latest` | 基础镜像没建 | `(cd ../vortex_common && scripts/build-base-image.sh)` 后重试 |
| 回测 `minute_data_missing` | workspace 缺 `stk_mins` | 用 `vortex_data` 补分钟数据 |
| 开会话 `unsupported_level` | `level` 仅支持 `daily` / `1min` | 按约定传参 |
| `docker build` 拉 base 很慢 / `DeadlineExceeded` | 直连 Docker Hub 慢 | 重试；**可选**配加速：`~/.docker/daemon.json` 加 `"registry-mirrors": ["https://docker.m.daocloud.io"]`（或你的阿里云个人加速器 `https://<id>.mirror.aliyuncs.com`），重启 Docker |
| `.git/index.lock ... exists` | 残留锁 | `rm -f .git/index.lock` |

---

## 7. 代码 / 分支现状

- 设计文档：`design/01-19`（评审 / ADR / 产品化 / 引擎选型 / 数据需求 / 容器策略 / **18 会话式引擎** / 对抗测试）。
- 引擎已去 Qlib，撮合内核为自研 A 股分钟撮合（design/14 / ADR-1 rev.2）；HTTP 面已迁会话式
  sessions/advance/close（design/18），旧异步作业面已删；取数走 data PIT 网关（直读 parquet 为离线回退）。
- 部署侧已接入统一 `vortex-base` 基础镜像（见 `../vortex_common/docs/migration/README.md`）。

_（历史"附：本地验证 Qlib"小节已删除——qlib 引擎已于 2026-06-07 移除，见 design/14。）_

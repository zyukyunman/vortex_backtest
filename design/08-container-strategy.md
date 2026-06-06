---
title: 容器化与镜像策略建议（Linux 迁移）
created: 2026-06-06
status: recommendation
depends_on: design/03-productization-plan.md
references: vortex_data/Dockerfile, vortex_data/docker-compose.yml
---

# 容器化与镜像策略建议（迁移到 Linux 服务器）

## 结论（建议）

采纳你的方向并细化：**初期 = 共享 base 镜像 + 各服务各自镜像；后期 = 一个顶层 compose 把两服务编排到一起（不是合并成一个镜像）。**

为什么不合成一个镜像：

- 两服务边界清晰、依赖与生命周期不同：`vortex_data`（duckdb/tushare/pyarrow + 调度 + 看板）vs `vortex_backtest`（fastapi + 撮合/Qlib，计算重）。塞进单镜像会互相拖累构建/发版/扩缩容，也违背你们已经画好的"数据服务 vs 回测服务"边界。
- 各自镜像 = 独立构建、独立发版、独立扩缩；回测要多副本跑大批量时不必带上数据服务。

为什么仍要共享 base：

- 共享 `vortex-base:py312`（= `python:3.12-slim` + 公共系统库 + TZ + 非 root 用户）可消除两仓的环境漂移、复用构建缓存，Python/OS 版本只在一处钉死。各服务 `FROM vortex-base:py312` 再装自己的依赖。
- **落地节奏**：本轮先让两仓各自 `FROM python:3.12-slim`（零额外基建）；等做整合部署时再把公共层抽成 `vortex-base` 推到私有 registry。避免现在就引入镜像仓库依赖。

## Base 版本：统一到 python:3.12-slim

- 现状：`vortex_data` 用 `python:3.11-slim`；`vortex_backtest` 本机开发用 3.13。
- **Qlib 的 wheel 支持以 3.11/3.12 最稳**（3.13/3.14 有风险，3.14 连 venv 的 ensurepip 都坏）。3.12 同时满足两仓 `requires-python`。
- 建议共享 base 取 **3.12-slim**：`vortex_data` 3.11→3.12 风险极小；`vortex_backtest` 镜像统一 3.12（本机可继续用 3.13 venv 跑非 qlib 的活）。

## 直接照搬 vortex_data 的好实践

- compose 端口**默认绑 `127.0.0.1`**；要对外暴露必须显式 `BIND_ADDR=0.0.0.0` 且配 token —— 避免 vortex_data 评审里 S1 那种"零鉴权对公网"。
- `/workspace` 卷、healthcheck（urllib 打 `/health`）、`restart: unless-stopped`、`TZ=Asia/Shanghai`、镜像里装 `build-essential`。
- 凭证/token 走环境变量，不入镜像、不入库、不上页面。
- 注意：vortex_backtest 的 token 守卫还没实现（design/03 阶段6）；在此之前**靠默认只绑回环**保证安全，compose 已留好 token 位。

## 两服务的数据耦合

- `vortex_data` 产出 Qlib 导出（其 P7，见 `vortex_data/design/10`、`11`）到一个卷；`vortex_backtest` **只读**挂载该卷作为 `provider_uri`。
- 整合 compose 里用 named volume 或同一宿主目录共享；回测对数据只读，写操作只发生在数据服务侧（与边界一致）。

## 本仓已加的容器化交付物

- `Dockerfile` —— 运行镜像（`python:3.12-slim`，精简，不含 qlib；引擎迁移到 Qlib 后改装 `.[qlib]`）。
- `Dockerfile.spike` —— 验证用镜像（装 `pyqlib`，在 Linux 镜像内跑 `spike/qlib_replay_spike.py`）。
- `docker-compose.yml` —— 默认 loopback + token 位 + healthcheck + 只读 workspace 卷 + 可写 state 卷。
- `.env.example`、`.dockerignore`（排除 `.venv/.git/` 等，避免把巨大上下文发给 daemon）。

## 迁移到 Linux 服务器（概要步骤）

1. 服务器装 Docker Engine + compose 插件。
2. 拉两仓代码（或把镜像推到私有 registry 再拉）。
3. `vortex_data`：配 `.env`（`TUSHARE_TOKEN` 等）→ `docker compose up -d` → 落数据 + 跑 Qlib 导出（P7）。
4. `vortex_backtest`：配 `.env`（`VORTEX_BACKTEST_WORKSPACE` 指向 data 的 Qlib 导出卷）→ `docker compose up -d`。
5. 整合：写一个顶层 `compose.yml` 同时起两者 + 共享网络 + 共享数据卷；按需把公共层抽成 `vortex-base:py312`。

## 在镜像内验证 Qlib spike

构建与运行（本机已验证 Docker 可用）：

```bash
cd /Users/zyukyunman/Documents/vortex/vortex_backtest
docker build -f Dockerfile.spike -t vortex-backtest-spike .
# 镜像内 import 自检（快）：
docker run --rm vortex-backtest-spike \
  python -c "import qlib; from qlib.backtest.exchange import Exchange; print('qlib', qlib.__version__, 'OK')"
# 数据驱动的完整 spike（需在容器内下载 qlib CN 样例，较慢）：
docker run --rm vortex-backtest-spike sh -lc \
  "python -m qlib.run.get_data qlib_data --target_dir /root/.qlib/qlib_data/cn_data --region cn && \
   python spike/qlib_replay_spike.py --provider-uri /root/.qlib/qlib_data/cn_data \
     --symbol SH600000 --symbol2 SZ000001 --start 2020-01-02 --end 2020-02-28 --freq day"
```

实测结果记录于本轮对话与 `design/06`（spike 结论）。

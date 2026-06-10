# vortex_backtest 应用镜像：FROM vortex-base（统一依赖底座）→ 只叠加本仓代码，秒级重建、零重下依赖。
#
# 第三方依赖（fastapi/uvicorn/pydantic/pandas/pyarrow/setuptools 等）统一收敛进 vortex-base，
# 应用镜像只 COPY 本仓代码并 `pip install --no-deps .`。
# 引擎为自研 A 股分钟撮合，本机直读 parquet（不依赖 qlib/backtrader）。
#
# 前置：基础镜像 vortex-base:latest 必须已存在（由 vortex_common 构建）：
#   (cd ../vortex_common && scripts/build-base-image.sh)
# 本地构建+运行：docker compose up -d --build（compose 已带 build.args.BASE_IMAGE，直接构建本仓代码）。
ARG BASE_IMAGE=vortex-base:latest
FROM ${BASE_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VORTEX_DATA_WORKSPACE=/workspace \
    VORTEX_BACKTEST_STATE_DIR=/state \
    VORTEX_BACKTEST_HOST=0.0.0.0 \
    VORTEX_BACKTEST_PORT=8766 \
    TZ=Asia/Shanghai

WORKDIR /app

COPY pyproject.toml README.md /app/
COPY vortex_backtest /app/vortex_backtest

# 依赖已在基础镜像内：
#   --no-deps            不再解析/下载依赖
#   --no-build-isolation 复用镜像内 setuptools，不临时下载构建后端
RUN pip install --no-deps --no-build-isolation .

# /workspace = 数据（来自 vortex_data，建议只读挂载）；/state = 账户/订单/作业/报告
VOLUME ["/workspace", "/state"]
EXPOSE 8766

CMD ["vortex-backtest", "serve"]

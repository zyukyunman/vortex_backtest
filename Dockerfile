# vortex_backtest 运行镜像（与 vortex_data 共享同一 base：python:3.12-slim）
# 说明：当前引擎仍是自研撮合（不含 qlib）。引擎迁移到 Qlib 后，把下面的安装改为 `.[qlib]` 即可。
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VORTEX_DATA_WORKSPACE=/workspace \
    VORTEX_BACKTEST_STATE_DIR=/state \
    VORTEX_BACKTEST_HOST=0.0.0.0 \
    VORTEX_BACKTEST_PORT=8765 \
    TZ=Asia/Shanghai

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md /app/
COPY vortex_backtest /app/vortex_backtest

RUN pip install --no-cache-dir .

# /workspace = 数据（来自 vortex_data，建议只读挂载）；/state = 账户/订单/作业/报告
VOLUME ["/workspace", "/state"]
EXPOSE 8765

CMD ["vortex-backtest", "serve"]

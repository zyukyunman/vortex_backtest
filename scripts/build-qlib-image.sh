#!/usr/bin/env bash
# 构建并(可选)运行 Qlib 回测镜像 —— 在容器里跑 qlib（本机 macOS arm64 装不上 pyqlib）。
#
# 背景/坑：
#   - pyqlib 只有 x86_64(manylinux) wheel，没有 linux-arm64 wheel；Apple Silicon 默认
#     跑 arm64 容器会 "No matching distribution found for pyqlib"。→ 必须按 linux/amd64 构建
#     （Docker Desktop 用 Rosetta 模拟；在 x86_64 Linux 服务器上是原生、更快）。
#   - Docker Hub 偶发超时；底座用「本地 tag」(vbtqlib-base:amd64) 让 BuildKit 用本地镜像，
#     不去 Hub 校验/拉取（思路同 vortex_data scripts/build-image.sh 的离线底座回退）。
#
# 用法：
#   scripts/build-qlib-image.sh                          # 确保 amd64 底座 + 构建镜像
#   scripts/build-qlib-image.sh run /path/to/qlib_out    # 构建并对该 qlib 数据跑 spike
set -euo pipefail
cd "$(dirname "$0")/.."

BASE_LOCAL="${BASE_LOCAL:-vbtqlib-base:amd64}"
IMAGE="${IMAGE:-vortex-backtest-qlib}"
export DOCKER_BUILDKIT=1

command -v docker >/dev/null || { echo "未找到 docker" >&2; exit 1; }

# 1) amd64 底座：本地没有就从 Hub 拉(重试,Hub 偶发超时)，再打本地 tag。
if ! docker image inspect "$BASE_LOCAL" >/dev/null 2>&1; then
  echo ">> 准备 amd64 底座 python:3.12-slim"
  ok=0
  for i in 1 2 3 4 5; do
    if docker pull --platform linux/amd64 python:3.12-slim; then ok=1; break; fi
    echo "   Hub 超时，重试 $i ..."; sleep 4
  done
  [ "$ok" = 1 ] || { echo "拉取 amd64 底座失败(Hub 不可达)。请确保能访问 Docker Hub 或预置 $BASE_LOCAL" >&2; exit 1; }
  docker tag python:3.12-slim "$BASE_LOCAL"
fi

# 2) 构建镜像(amd64)
echo ">> 构建 $IMAGE (linux/amd64)"
docker build --platform linux/amd64 --build-arg BASE_IMAGE="$BASE_LOCAL" -f Dockerfile.qlib -t "$IMAGE" .

# 3) 可选：对 qlib 数据跑 spike
if [ "${1:-}" = "run" ]; then
  DATA="${2:?用法: scripts/build-qlib-image.sh run /path/to/qlib_out}"
  echo ">> 对 $DATA 跑 qlib spike"
  docker run --rm --platform linux/amd64 -v "$DATA:/qlib:ro" "$IMAGE" \
    python spike/qlib_replay_spike.py --provider-uri /qlib --freq day \
      --symbol SH600000 --symbol2 SZ000001 --start 2026-01-05 --end 2026-06-05
fi
echo ">> done"

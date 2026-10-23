#!/bin/bash
# Codespaces / devcontainer boot: local Redis and the API with in-process workers.
set -eu
cd "$(dirname "$0")/.."
if ! redis-cli ping >/dev/null 2>&1; then
  redis-server --daemonize yes --bind 127.0.0.1 --port 6379 --protected-mode yes --dir /tmp --dbfilename swarm.rdb
fi
if ! python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=1)" >/dev/null 2>&1; then
  REDIS_URL=redis://127.0.0.1:6379/0 \
  SWARM_LLM=mock \
  SWARM_GIMBAL_BACKEND=short \
  SWARM_UART_MODE=sim \
  SWARM_EMBEDDED_WORKERS=1 \
  nohup python -m swarm_orchestrator.api >/tmp/swarm-api.log 2>&1 &
fi

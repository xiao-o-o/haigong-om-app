#!/usr/bin/env bash
# 部署启动脚本：平台注入 PORT，这里显式传给它；本地无 PORT 时回落 8501。
set -e
exec python -m streamlit run app.py \
  --server.port "${PORT:-8501}" \
  --server.address 0.0.0.0 \
  --server.headless true \
  --server.fileWatcherType none

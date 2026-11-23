#!/usr/bin/env bash
set -euo pipefail

# 在 macOS（Apple Silicon）上构建 NyaSubFusion.app 与 .dmg
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PY="${ROOT}/.venv/bin/python"
if [ ! -x "$PY" ]; then
  echo "找不到 .venv，请先运行：uv sync --extra asr --extra ocr --extra lang --extra server"
  exit 1
fi

echo "==> 1/3 检查并准备 ffmpeg"
"$PY" packaging/fetch_ffmpeg.py

echo "==> 2/3 构建前端与 Python 内核"
"$PY" packaging/build_core.py

echo "==> 3/3 构建 Tauri macOS 应用包 (.app / .dmg)"
cd "$ROOT/desktop"
pnpm tauri build --config src-tauri/tauri.macos.conf.json

echo "构建完成：desktop/src-tauri/target/release/bundle/"

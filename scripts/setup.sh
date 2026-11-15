#!/usr/bin/env bash
# 从零重建最小虚拟环境（仅 PySide6-Essentials，约 250MB）+ 回归测试。
set -euo pipefail
cd "$(dirname "$0")/.."

PY="${PYTHON:-python3}"

echo "== 重建最小虚拟环境（PySide6-Essentials）=="
rm -rf .venv
"$PY" -m venv .venv
.venv/bin/pip install --upgrade pip >/dev/null
.venv/bin/pip install -e '.[desktop]'

echo "== 回归测试 =="
QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest discover -s tests
.venv/bin/clipllm02 validate

echo "== 完成 =="
du -sh .venv
.venv/bin/python -m pip check

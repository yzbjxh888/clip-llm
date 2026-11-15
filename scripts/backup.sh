#!/usr/bin/env bash
# 生成不含虚拟环境的源码快照（tar.gz），文件名带当前 build 版本。
# 恢复：tar xzf <快照> 后，跑 scripts/setup.sh 重建最小 venv。
set -euo pipefail
cd "$(dirname "$0")/.."

BUILD=$(grep 'BUILD = ' src/clipllm02/studio/__main__.py | head -1 \
        | sed 's/.*"\(.*\)".*/\1/')
mkdir -p dist
OUT="dist/clipllm02-${BUILD}-src.tar.gz"

tar --exclude='.venv' \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    --exclude='dist' \
    --exclude='build' \
    --exclude='*.egg-info' \
    --exclude='.pytest_cache' \
    --exclude='.codebuddy' \
    --exclude='*.deb' \
    --exclude='*.zip' \
    --exclude='*.tar.gz' \
    --exclude='*.tar.xz' \
    -czf "$OUT" .

echo "已生成源码快照（无 venv）：$OUT"
echo "恢复与重建：tar xzf $OUT && cd 解压目录 && bash scripts/setup.sh"

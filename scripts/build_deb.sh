#!/usr/bin/env bash
# 打包为自带 venv 的 .deb（离线安装到 /opt/clipllm02，仅依赖系统 python3）。
# 依赖：dpkg-deb、python3、可访问 PyPI（构建时下载 PySide6-Essentials）。
#
# 关键约束（上一版装完打不开的根因）：
#   绝不能用 `pip install -e`。editable 安装会把**构建时的临时路径**
#   （/tmp/clipllm02-deb/.../opt/clipllm02）写进 site-packages 的路径记录，
#   安装到 /opt/clipllm02 后该路径不存在，import clipllm02 必然失败。
#   这里改为：包目录不进 site-packages，运行期用 PYTHONPATH 指到
#   /opt/clipllm02/src，venv 只负责安装第三方依赖。
set -euo pipefail
cd "$(dirname "$0")/.."

APP=clipllm02
VERSION=$(grep '^version' pyproject.toml | head -1 | sed 's/.*"\(.*\)".*/\1/')
ROOT="/tmp/${APP}-deb"
PKGROOT="${ROOT}/${APP}_${VERSION}_amd64"
INSTALL="${PKGROOT}/opt/${APP}"
ICONS="16 24 32 48 64 128 256 512"

echo "== 打包 ${APP} ${VERSION}（自带最小 venv + 图标）=="
rm -rf "$ROOT"
mkdir -p "$INSTALL" "${PKGROOT}/DEBIAN" "${PKGROOT}/usr/bin" \
         "${PKGROOT}/usr/share/applications" \
         "${PKGROOT}/usr/share/icons/hicolor/scalable/apps"
for s in $ICONS; do
    mkdir -p "${PKGROOT}/usr/share/icons/hicolor/${s}x${s}/apps"
done

# 源码 + 数据 + 图标；README 是 pyproject 的 readme，缺了 pip 会报错
cp -r src spec assets "$INSTALL"/
cp pyproject.toml README.md "$INSTALL"/
# 发布前保险：用户配置不在包里，但若源码混进真实密钥就直接中止打包
if grep -rEl 'sk-[A-Za-z0-9_-]{16,}|AKIA[0-9A-Z]{12,}' \
        "$INSTALL/src" "$INSTALL/spec" "$INSTALL/README.md" >/dev/null 2>&1; then
    echo "打包中止：源码中发现疑似 API 密钥，请先清除再打包" >&2
    exit 1
fi
# 源码是唯一真源：不带构建机上的字节码，避免排错时路径指向临时目录
find "$INSTALL/src" -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
find "$INSTALL/src" -type f -name '*.pyc' -delete 2>/dev/null || true

# 优先用 uv（Ubuntu 上系统 python3 常缺 ensurepip，python3 -m venv 会失败）
if command -v uv >/dev/null 2>&1; then
    uv venv --seed --python /usr/bin/python3.14 "$INSTALL/.venv"
    uv pip install --python "$INSTALL/.venv/bin/python" \
        'PySide6-Essentials>=6.8'
else
    python3 -m venv "$INSTALL/.venv"
    "$INSTALL/.venv/bin/pip" install --no-cache-dir --upgrade pip >/dev/null 2>&1 || true
    "$INSTALL/.venv/bin/pip" install --no-cache-dir 'PySide6-Essentials>=6.8'
fi

# 图标（hicolor 各尺寸 + 矢量）
for s in $ICONS; do
    cp "assets/png/${APP}-${s}.png" \
       "${PKGROOT}/usr/share/icons/hicolor/${s}x${s}/apps/${APP}.png"
done
cp assets/icon.svg "${PKGROOT}/usr/share/icons/hicolor/scalable/apps/${APP}.svg"

# 启动器：PYTHONPATH 指向源码，不依赖 pip 的 editable 记录
cat > "${PKGROOT}/usr/bin/clipllm02-desktop" <<'EOF'
#!/usr/bin/env bash
export PYTHONPATH="/opt/clipllm02/src${PYTHONPATH:+:$PYTHONPATH}"
exec /opt/clipllm02/.venv/bin/python -m clipllm02.studio "$@"
EOF
chmod 755 "${PKGROOT}/usr/bin/clipllm02-desktop"

# 诊断入口：桌面点不开时让用户在终端跑一条命令拿到原因
cat > "${PKGROOT}/usr/bin/clipllm02-doctor" <<'EOF'
#!/usr/bin/env bash
export PYTHONPATH="/opt/clipllm02/src${PYTHONPATH:+:$PYTHONPATH}"
echo "== 解释器 =="
/opt/clipllm02/.venv/bin/python -V || true
echo "== 导入自检 =="
/opt/clipllm02/.venv/bin/python -c "import clipllm02, sys; print('ok', clipllm02.__file__)" || true
echo "== 依赖自检 =="
/opt/clipllm02/.venv/bin/python -c "import PySide6; print('PySide6', PySide6.__version__)" || true
echo "== 应用诊断 =="
exec /opt/clipllm02/.venv/bin/python -m clipllm02.studio --doctor
EOF
chmod 755 "${PKGROOT}/usr/bin/clipllm02-doctor"

cat > "${PKGROOT}/DEBIAN/control" <<EOF
Package: ${APP}
Version: ${VERSION}
Architecture: amd64
Maintainer: you <you@example.com>
Depends: python3 (>= 3.10), libgl1, libxkbcommon0, libegl1
Section: utils
Priority: optional
Description: clip-llm 词典式内容解释（初判 + 大模型请求双层）
 复制或选中内容后自动弹出初判菜单，选择一个查询方式再请求大模型。
EOF

cat > "${PKGROOT}/usr/share/applications/clipllm02.desktop" <<'EOF'
[Desktop Entry]
Type=Application
Name=clip-llm 词典
GenericName=内容解释词典
Comment=复制或选中内容后弹出初判与解释
Exec=clipllm02-desktop
Icon=clipllm02
Terminal=false
Categories=Utility;Education;Qt;
Keywords=dict;dictionary;translate;latex;词典;翻译;
StartupNotify=false
StartupWMClass=clipllm02
EOF

cat > "${PKGROOT}/DEBIAN/postinst" <<'EOF'
#!/bin/sh
# 安装后自检：只提示、不阻断，避免用户装不上或卸不掉
set -e
if [ ! -x /opt/clipllm02/.venv/bin/python ]; then
    echo "clipllm02: 内置 venv 不可用（/opt/clipllm02/.venv）" >&2
else
    if ! PYTHONPATH=/opt/clipllm02/src /opt/clipllm02/.venv/bin/python \
            -c "import clipllm02" >/dev/null 2>&1; then
        echo "clipllm02: 导入自检失败，请运行 clipllm02-doctor 查看详情" >&2
    fi
fi
command -v gtk-update-icon-cache >/dev/null 2>&1 && \
    gtk-update-icon-cache -q -t /usr/share/icons/hicolor 2>/dev/null || true
command -v update-desktop-database >/dev/null 2>&1 && \
    update-desktop-database -q /usr/share/applications 2>/dev/null || true
exit 0
EOF
chmod 755 "${PKGROOT}/DEBIAN/postinst"

mkdir -p dist
OUT="dist/${APP}_${VERSION}_amd64.deb"
dpkg-deb --build --root-owner-group "$PKGROOT" "$OUT"
echo "产出：$OUT"

#!/usr/bin/env bash
# Construit une AppImage Linux autonome de Xbox Image Browser.
#
# Prérequis : python3 (avec venv), curl. appimagetool est téléchargé
# automatiquement dans .build-tools/ au premier lancement.
#
# Usage : ./build_appimage.sh
set -euo pipefail

cd "$(dirname "$0")"

APP="XboxImageBrowser"
ARCH="$(uname -m)"
case "$ARCH" in
  x86_64 | aarch64) ;;
  *)
    echo "Architecture non prise en charge : $ARCH" >&2
    exit 1
    ;;
esac

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 est requis." >&2
  exit 1
fi

VENV=".venv-build"
if [ ! -x "$VENV/bin/python" ]; then
  echo "[1/5] Création de l'environnement de build…"
  python3 -m venv "$VENV"
fi

echo "[2/5] Installation des dépendances…"
"$VENV/bin/python" -m pip install --upgrade pip
"$VENV/bin/python" -m pip install -r requirements.txt pyinstaller

echo "[3/5] Exécution des tests…"
"$VENV/bin/python" -m unittest discover -s tests

echo "[4/5] Construction du bundle (PyInstaller)…"
"$VENV/bin/python" -m PyInstaller --noconfirm --clean packaging/XboxImageBrowser-linux.spec

APPDIR="build/AppDir"
rm -rf "$APPDIR"
mkdir -p \
  "$APPDIR/usr/bin" \
  "$APPDIR/usr/share/applications" \
  "$APPDIR/usr/share/icons/hicolor/512x512/apps"

cp -a "dist/$APP/." "$APPDIR/usr/bin/"
cp packaging/xbox-image-browser.desktop "$APPDIR/"
cp packaging/xbox-image-browser.desktop "$APPDIR/usr/share/applications/"
cp assets/icon.png "$APPDIR/xbox-image-browser.png"
cp assets/icon.png "$APPDIR/usr/share/icons/hicolor/512x512/apps/xbox-image-browser.png"
cp packaging/AppRun "$APPDIR/AppRun"
chmod +x "$APPDIR/AppRun" "$APPDIR/usr/bin/$APP"

echo "[5/5] Création de l'AppImage…"
TOOL=".build-tools/appimagetool-$ARCH.AppImage"
if [ ! -x "$TOOL" ]; then
  mkdir -p .build-tools
  URL="https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-$ARCH.AppImage"
  echo "      Téléchargement de appimagetool…"
  curl -fsSL "$URL" -o "$TOOL"
  chmod +x "$TOOL"
fi

mkdir -p dist
TARGET="dist/$APP-$ARCH.AppImage"
# APPIMAGE_EXTRACT_AND_RUN : appimagetool fonctionne sans FUSE (CI, conteneurs).
ARCH="$ARCH" APPIMAGE_EXTRACT_AND_RUN=1 \
  "$TOOL" --no-appstream "$APPDIR" "$TARGET"

cp demo/demo.iso dist/demo.iso

echo
echo "Build terminé :"
echo "  $TARGET"
echo "  dist/demo.iso"

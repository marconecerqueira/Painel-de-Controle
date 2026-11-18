#!/bin/zsh
# Compila o app de barra de menu e instala em ~/Applications/Painel de Controle.app
# Requer só as Command Line Tools da Apple (xcode-select --install), não o Xcode inteiro.
# Uso: macos/build.sh            (compila, instala e abre)
#      macos/build.sh --no-open  (só compila e instala)
set -euo pipefail

HERE="${0:A:h}"
BUILD="$HERE/build"
APP_NAME="Painel de Controle"
APP="$BUILD/$APP_NAME.app"
DEST="$HOME/Applications/$APP_NAME.app"
ARCH="$(uname -m)"

rm -rf "${BUILD:?}"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

echo "Compilando ($ARCH)…"
swiftc -O -swift-version 5 -parse-as-library \
  -target "$ARCH-apple-macos13.0" \
  "$HERE/PainelBarra.swift" -o "$APP/Contents/MacOS/PainelBarra"

cp "$HERE/AppIcon.icns" "$APP/Contents/Resources/AppIcon.icns"

cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleIdentifier</key><string>local.painel-de-controle.barra</string>
  <key>CFBundleName</key><string>Painel de Controle</string>
  <key>CFBundleDisplayName</key><string>Painel de Controle</string>
  <key>CFBundleExecutable</key><string>PainelBarra</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <!-- só na barra de menu: sem ícone no Dock nem no Cmd+Tab -->
  <key>LSUIElement</key><true/>
  <!-- o painel no próprio Mac é http://127.0.0.1; pela Tailscale é https -->
  <key>NSAppTransportSecurity</key>
  <dict><key>NSAllowsLocalNetworking</key><true/></dict>
</dict>
</plist>
PLIST

# assinatura local (sem conta de desenvolvedor): basta para o macOS abrir o app no próprio Mac
codesign --force --deep --sign - "$APP"

mkdir -p "$HOME/Applications"
pkill -x PainelBarra 2>/dev/null || true
rm -rf "${DEST:?}"
cp -R "$APP" "$DEST"
echo "Instalado em $DEST"

if [[ "${1:-}" != "--no-open" ]]; then
  open "$DEST"
fi

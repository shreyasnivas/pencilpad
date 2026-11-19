#!/bin/zsh
# Builds PencilPad.app, a menu bar app that keeps PencilPad running and starts it
# at login, and installs it into /Applications (or ~/Applications).
# The app runs pencilpad.py from this checkout, so keep the folder where it is;
# after moving it, run this script again.
set -e
cd "${0:A:h}/.."
REPO="$PWD"
source ./find_python.sh
make_venv .venv
.venv/bin/python -c "import aiohttp, Quartz, AppKit, segno, cryptography" 2>/dev/null || .venv/bin/pip install -q -r requirements.txt

DEST=/Applications
[ -w "$DEST" ] || { DEST="$HOME/Applications"; mkdir -p "$DEST"; }
BUILD="$(mktemp -d)"
APP="$BUILD/PencilPad.app"
mkdir -p "$APP/Contents/MacOS"
# Pin the deployment target: left alone, swiftc stamps the binary with the SDK's
# version, and macOS refuses to open it on anything older.
xcrun swiftc -O -target "$(uname -m)-apple-macos12.0" -o "$APP/Contents/MacOS/PencilPad" app/PencilPad.swift
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>PencilPad</string>
  <key>CFBundleDisplayName</key><string>PencilPad</string>
  <key>CFBundleIdentifier</key><string>io.github.pencilpad.app</string>
  <key>CFBundleExecutable</key><string>PencilPad</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>0.1</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>LSMinimumSystemVersion</key><string>12.0</string>
  <key>LSUIElement</key><true/>
  <key>NSSpeechRecognitionUsageDescription</key><string>PencilPad turns what you say into the iPad's microphone into text and types it on this Mac.</string>
  <key>PencilPadRepo</key><string>$REPO</string>
</dict></plist>
PLIST
codesign --force --sign - "$APP"
rm -rf "$DEST/PencilPad.app"
mv "$APP" "$DEST/PencilPad.app"
rmdir "$BUILD"
echo "Installed $DEST/PencilPad.app"

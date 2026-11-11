#!/bin/bash
# Build "Film Look Studio.app": a real Mac app with its own window, Dock icon and menu bar
# (mac-app/FilmLookStudio.swift, compiled with swiftc). It starts the server with launch.sh and shows
# the app in a WebKit window. ⌘Q or closing the window quits; the server then stops by itself.
#   ./mac-app/make_app.sh            -> /Applications/Film Look Studio.app + a Desktop alias
#   ./mac-app/make_app.sh <folder>   -> <folder>/Film Look Studio.app (no alias)
# Rebuilding changes the app's signature, so macOS asks once more for Documents / Removable Volumes.
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
PROJECT="$(cd "$HERE/.." && pwd)"
DEST="${1:-/Applications}"
APP="$DEST/Film Look Studio.app"
TMP="$(mktemp -d)"

swiftc -O -framework Cocoa -framework WebKit "$HERE/FilmLookStudio.swift" -o "$TMP/Film Look Studio"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$TMP/Film Look Studio" "$APP/Contents/MacOS/Film Look Studio"

# icon
"$PROJECT/.venv/bin/python" "$HERE/make_icon.py" "$TMP/icon.png"
mkdir "$TMP/icon.iconset"
for s in 16 32 128 256 512; do
  sips -z $s $s "$TMP/icon.png" --out "$TMP/icon.iconset/icon_${s}x${s}.png" >/dev/null
  sips -z $((s*2)) $((s*2)) "$TMP/icon.png" --out "$TMP/icon.iconset/icon_${s}x${s}@2x.png" >/dev/null
done
iconutil -c icns "$TMP/icon.iconset" -o "$APP/Contents/Resources/AppIcon.icns"

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleExecutable</key><string>Film Look Studio</string>
  <key>CFBundleIdentifier</key><string>com.andrewchew.filmlookstudio</string>
  <key>CFBundleName</key><string>Film Look Studio</string>
  <key>CFBundleDisplayName</key><string>Film Look Studio</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>2.0</string>
  <key>CFBundleVersion</key><string>2</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSSupportsAutomaticGraphicsSwitching</key><true/>
  <key>NSAppTransportSecurity</key><dict><key>NSAllowsLocalNetworking</key><true/></dict>
  <key>NSDocumentsFolderUsageDescription</key><string>Film Look Studio runs from your Documents folder.</string>
  <key>NSRemovableVolumesUsageDescription</key><string>Film Look Studio reads your photos on your external drives (it never changes them).</string>
  <key>FLSProjectDir</key><string>$PROJECT</string>
</dict></plist>
PLIST
codesign --force --deep -s - "$APP" >/dev/null 2>&1 || true
touch "$APP"
rm -rf "$TMP"

if [ -z "$1" ]; then                                         # a Desktop shortcut to the app
  rm -f "$HOME/Desktop/Film Look Studio"
  osascript -e "tell application \"Finder\" to make alias file to (POSIX file \"$APP\") at (path to desktop folder)" >/dev/null
fi
echo "Made $APP"

#!/bin/bash
# Build "Film Look Studio.app" (no Terminal window; Quit button in the app, or auto-stop when no page is open).
#   ./mac-app/make_app.sh            -> /Applications/Film Look Studio.app + a Desktop alias
#   ./mac-app/make_app.sh <folder>   -> <folder>/Film Look Studio.app (no alias)
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
PROJECT="$(cd "$HERE/.." && pwd)"
DEST="${1:-/Applications}"
APP="$DEST/Film Look Studio.app"
TMP="$(mktemp -d)"

sed "s|__PROJECT_DIR__|$PROJECT|" "$HERE/launcher.applescript" > "$TMP/launcher.applescript"
rm -rf "$APP"
osacompile -o "$APP" "$TMP/launcher.applescript"          # runs, opens the browser, and is done

# icon
"$PROJECT/.venv/bin/python" "$HERE/make_icon.py" "$TMP/icon.png"
mkdir "$TMP/icon.iconset"
for s in 16 32 128 256 512; do
  sips -z $s $s "$TMP/icon.png" --out "$TMP/icon.iconset/icon_${s}x${s}.png" >/dev/null
  sips -z $((s*2)) $((s*2)) "$TMP/icon.png" --out "$TMP/icon.iconset/icon_${s}x${s}@2x.png" >/dev/null
done
iconutil -c icns "$TMP/icon.iconset" -o "$APP/Contents/Resources/applet.icns"
rm -f "$APP/Contents/Resources/Assets.car"                    # else macOS shows the default script icon
/usr/libexec/PlistBuddy -c "Delete :CFBundleIconName" "$APP/Contents/Info.plist" 2>/dev/null || true
/usr/libexec/PlistBuddy -c "Set :CFBundleName Film Look Studio" "$APP/Contents/Info.plist" 2>/dev/null || true
/usr/libexec/PlistBuddy -c "Add :CFBundleIdentifier string com.andrewchew.filmlookstudio" "$APP/Contents/Info.plist" 2>/dev/null \
  || /usr/libexec/PlistBuddy -c "Set :CFBundleIdentifier com.andrewchew.filmlookstudio" "$APP/Contents/Info.plist"
codesign --force --deep -s - "$APP" >/dev/null 2>&1 || true   # re-sign after changing the icon
touch "$APP"
rm -rf "$TMP"

if [ -z "$1" ]; then                                         # a Desktop shortcut to the app
  rm -f "$HOME/Desktop/Film Look Studio"
  osascript -e "tell application \"Finder\" to make alias file to (POSIX file \"$APP\") at (path to desktop folder)" >/dev/null
fi
echo "Made $APP"

#!/usr/bin/env bash
# Assemble "MailRoom.app". The bundle is a thin launcher around the repo -
# it bootstraps the venv on first run, so rebuilding it after a code change is
# never necessary. Re-run this only if you move the repo or change the icon.
set -euo pipefail
SRC="$(cd "$(dirname "$0")/.." && pwd)"          # where the files are read from
REPO="${MAILROOM_REPO:-$SRC}"                  # path baked into the launcher
DEST="${1:-$SRC/dist}"
APP="$DEST/MailRoom.app"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
[ -f "$SRC/assets/AppIcon.icns" ] && cp "$SRC/assets/AppIcon.icns" "$APP/Contents/Resources/"

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>MailRoom</string>
  <key>CFBundleDisplayName</key><string>MailRoom</string>
  <key>CFBundleIdentifier</key><string>io.github.mailroom</string>
  <key>CFBundleVersion</key><string>0.1.0</string>
  <key>CFBundleShortVersionString</key><string>0.1.0</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>MailRoom</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>LSMinimumSystemVersion</key><string>12.0</string>
  <key>LSApplicationCategoryType</key><string>public.app-category.productivity</string>
</dict></plist>
PLIST

sed "s|__REPO__|$REPO|g" "$SRC/scripts/launcher.template.sh" > "$APP/Contents/MacOS/MailRoom"
chmod +x "$APP/Contents/MacOS/MailRoom"
touch "$APP"
echo "built: $APP"
echo "drag it to /Applications, or run: open '$APP'"

#!/bin/bash
# Build GymHost.app (ad hoc signed) into tools/gym/host/build/.
set -euo pipefail
cd "$(dirname "$0")"
APP=build/GymHost.app
mkdir -p "$APP/Contents/MacOS"
swiftc -O -parse-as-library GymHost.swift -o "$APP/Contents/MacOS/GymHost"
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleExecutable</key><string>GymHost</string>
  <key>CFBundleIdentifier</key><string>ai.deskmind.gymhost</string>
  <key>CFBundleName</key><string>GymHost</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>LSUIElement</key><true/>
  <key>NSAppTransportSecurity</key><dict><key>NSAllowsLocalNetworking</key><true/></dict>
</dict></plist>
PLIST
codesign -f -s - "$APP"
echo "built $APP"

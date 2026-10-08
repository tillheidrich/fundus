#!/bin/bash
# Builds Fundus.app from the Swift package plus the Python server.
#
# Works with the Command Line Tools alone — no Xcode required. If you do
# install Xcode later, `open Package.swift` gives you the same thing as a
# project, and this script keeps working either way.
#
#   ./build-app.sh              → build/Fundus.app
#   ./build-app.sh --install    → also copies it to /Applications
#   ./build-app.sh --run        → build, then launch it

set -euo pipefail
cd "$(dirname "$0")"

APP_NAME="Fundus"
BUNDLE_ID="app.fundus.desktop"
# The update check compares this against the newest published tag, so it has
# to be a version and not a commit hash: `git describe --always` falls back to
# a bare hash on a repo with no tags, and nothing sensible compares to that.
VERSION="$(git describe --tags --abbrev=0 2>/dev/null || echo 0.1.0)"
VERSION="${VERSION#v}"
OUT="build/${APP_NAME}.app"
SERVER_SRC=".."

bar() { printf "\033[1m›\033[0m %s\n" "$1"; }

# ── 1. Compile ────────────────────────────────────────────────────────────────
bar "Swift wird übersetzt…"
swift build -c release --disable-sandbox

BIN=".build/release/FundusApp"
[ -x "$BIN" ] || { echo "Build fehlgeschlagen: $BIN fehlt"; exit 1; }

# ── 2. Bundle ─────────────────────────────────────────────────────────────────
bar "App-Bundle wird gebaut…"
rm -rf "$OUT"
mkdir -p "$OUT/Contents/MacOS" "$OUT/Contents/Resources"
cp "$BIN" "$OUT/Contents/MacOS/${APP_NAME}"

cat > "$OUT/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>${APP_NAME}</string>
  <key>CFBundleDisplayName</key><string>${APP_NAME}</string>
  <key>CFBundleIdentifier</key><string>${BUNDLE_ID}</string>
  <key>CFBundleVersion</key><string>${VERSION}</string>
  <key>CFBundleShortVersionString</key><string>${VERSION}</string>
  <key>CFBundleExecutable</key><string>${APP_NAME}</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>NSHighResolutionCapable</key><true/>
  <!-- Where the app looks for a newer build. The GitHub releases API needs no
       infrastructure and is where the downloads live anyway. Point it
       elsewhere with UPDATE_FEED=… when building. -->
  <key>FundusUpdateFeed</key><string>${UPDATE_FEED:-https://api.github.com/repos/tillheidrich/fundus/releases/latest}</string>
  <!-- The server it talks to is on loopback, which is plain HTTP. Scoped to
       localhost rather than switching off transport security wholesale. -->
  <key>NSAppTransportSecurity</key>
  <dict>
    <key>NSAllowsLocalNetworking</key><true/>
  </dict>
</dict>
</plist>
PLIST

# ── 3. Python server into the bundle ──────────────────────────────────────────
# Copied, not referenced: the app has to keep working when the repo moves.
#
# Every *.py at the root, not a hand-kept list. The list version shipped an
# app without podcast.py, because the module was written after the list — and
# the failure surfaced as an ImportError at launch, long after the build said
# it had succeeded. A glob cannot forget a new module.
bar "Server wird eingebettet…"
SRV="$OUT/Contents/Resources/server"
mkdir -p "$SRV"
# Full `if`, not `[ … ] && cp`: a test that fails on the loop's last iteration
# is the loop's exit status, and under `set -e` that ends the script — here,
# right after it said the server was embedded. `locales` not existing was
# enough to do it.
for f in "$SERVER_SRC"/*.py; do
  if [ -e "$f" ]; then cp "$f" "$SRV/"; fi
done
for item in requirements.txt requirements-extractors.txt templates locales fonts; do
  if [ -e "$SERVER_SRC/$item" ]; then cp -R "$SERVER_SRC/$item" "$SRV/"; fi
done

# Guard against the next variant of the same mistake: if main.py imports a
# local module that did not make it in, say so now rather than at launch.
python3 - "$SERVER_SRC" "$SRV" <<'PY' || exit 1
import ast, pathlib, sys
src, dest = (pathlib.Path(a) for a in sys.argv[1:3])
local = {p.stem for p in src.glob("*.py")}
missing = set()
for mod in ("main.py", "mcp_tools.py"):
    f = src / mod
    if not f.exists():
        continue
    for node in ast.walk(ast.parse(f.read_text())):
        names = ([a.name for a in node.names] if isinstance(node, ast.Import)
                 else [node.module] if isinstance(node, ast.ImportFrom) and node.module
                 else [])
        for n in names:
            head = (n or "").split(".")[0]
            if head in local and not (dest / f"{head}.py").exists():
                missing.add(head)
if missing:
    print(f"   FEHLER: nicht eingebettet: {', '.join(sorted(missing))}")
    sys.exit(1)
PY
# Never ship the developer's database, downloads or virtualenv.
rm -rf "$SRV/data" "$SRV/downloads" "$SRV/tmp" "$SRV/__pycache__" "$SRV/models"
find "$SRV" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true

# ── 4. Icon ───────────────────────────────────────────────────────────────────
if [ -f "AppIcon.icns" ]; then
  cp AppIcon.icns "$OUT/Contents/Resources/AppIcon.icns"
elif [ -f "icon.png" ]; then
  bar "Icon wird erzeugt…"
  TMPSET="$(mktemp -d)/AppIcon.iconset"; mkdir -p "$TMPSET"
  for s in 16 32 64 128 256 512; do
    sips -z $s $s icon.png --out "$TMPSET/icon_${s}x${s}.png" >/dev/null 2>&1
    d=$((s*2))
    sips -z $d $d icon.png --out "$TMPSET/icon_${s}x${s}@2x.png" >/dev/null 2>&1
  done
  iconutil -c icns "$TMPSET" -o "$OUT/Contents/Resources/AppIcon.icns" 2>/dev/null || true
fi

# ── 5. Signing ────────────────────────────────────────────────────────────────
# Three tiers, and the difference matters the moment someone else opens this:
#
#   ad-hoc                   runs on this machine only
#   Apple Development        runs on machines registered to the account
#   Developer ID + notarised runs anywhere — this is the one for handing out
#
# Gatekeeper refuses an un-notarised app on a machine that did not build it,
# no matter how carefully it was signed here.
bar "Wird signiert…"
IDENTITY="${CODESIGN_IDENTITY:-}"
TIER="ad-hoc"
# `|| true` on both: grep exits 1 when the certificate is not in the keychain,
# and under `set -e` a command substitution hands that status to the
# assignment — so on a machine with no Developer ID the script quietly
# stopped right here, after announcing that it was signing and before signing
# anything. The bundle kept Swift's ad-hoc linker signature, every later step
# (notarisation, --install, --dmg) never ran, and nothing said so.
if [ -z "$IDENTITY" ]; then
  IDENTITY="$(security find-identity -v -p codesigning 2>/dev/null \
              | grep -o '"Developer ID Application[^"]*"' | head -1 | tr -d '"' || true)"
  if [ -n "$IDENTITY" ]; then TIER="developer-id"; fi
fi
if [ -z "$IDENTITY" ]; then
  IDENTITY="$(security find-identity -v -p codesigning 2>/dev/null \
              | grep -o '"Apple Development[^"]*"' | head -1 | tr -d '"' || true)"
  if [ -n "$IDENTITY" ]; then TIER="development"; fi
fi

if [ -n "$IDENTITY" ]; then
  # --options runtime is required for notarisation and harmless otherwise.
  codesign --force --deep --options runtime --timestamp --sign "$IDENTITY" "$OUT" 2>/dev/null \
    || codesign --force --deep --options runtime --sign "$IDENTITY" "$OUT"
  echo "   $TIER · $IDENTITY"
else
  codesign --force --deep --sign - "$OUT"
  echo "   ad-hoc (läuft nur auf diesem Mac)"
fi

# ── 6. Notarisation ───────────────────────────────────────────────────────────
# Only possible with a Developer ID, and only when credentials were stored:
#   xcrun notarytool store-credentials fundus \
#     --apple-id you@example.com --team-id TEAMID --password <app-specific>
if [ "$TIER" = "developer-id" ] && [ "${NOTARIZE:-1}" = "1" ]; then
  if xcrun notarytool history --keychain-profile "${NOTARY_PROFILE:-fundus}" >/dev/null 2>&1; then
    bar "Wird notarisiert (dauert ein paar Minuten)…"
    ZIP="build/${APP_NAME}.zip"
    ditto -c -k --keepParent "$OUT" "$ZIP"
    if xcrun notarytool submit "$ZIP" --keychain-profile "${NOTARY_PROFILE:-fundus}" --wait; then
      # Stapling puts the ticket inside the bundle so it opens offline too.
      if xcrun stapler staple "$OUT"; then
        echo "   notarisiert und geheftet"
      else
        echo "   notarisiert, aber das Ticket liess sich nicht heften"
      fi
    else
      echo "   Notarisierung fehlgeschlagen — die App läuft lokal trotzdem"
    fi
    rm -f "$ZIP"
  else
    echo "   (nicht notarisiert: kein Schlüsselbund-Profil '${NOTARY_PROFILE:-fundus}')"
  fi
fi

echo
bar "Fertig: $(pwd)/$OUT"
du -sh "$OUT" | awk '{print "   Größe: " $1}'
case "$TIER" in
  ad-hoc)      echo "   Weitergabe: nein — läuft nur hier" ;;
  development) echo "   Weitergabe: nur an registrierte Geräte. Für Family & Friends"
               echo "               brauchst du ein 'Developer ID Application'-Zertifikat" ;;
  developer-id) echo "   Weitergabe: ja" ;;
esac

case "${1:-}" in
  --install)
    bar "Wird nach /Applications kopiert…"
    rm -rf "/Applications/${APP_NAME}.app"
    cp -R "$OUT" /Applications/
    echo "   /Applications/${APP_NAME}.app"
    ;;
  --run)
    open "$OUT"
    ;;
  --dmg)
    # A disk image is the expected way to hand a Mac app to someone: it keeps
    # the signature intact, which a zip through some chat apps does not.
    bar "Disk-Image wird gebaut…"
    STAGE="$(mktemp -d)/${APP_NAME}"
    mkdir -p "$STAGE"
    cp -R "$OUT" "$STAGE/"
    ln -s /Applications "$STAGE/Applications"
    hdiutil create -volname "$APP_NAME" -srcfolder "$STAGE" -ov -format ULFO \
      "build/${APP_NAME}-${VERSION}.dmg" >/dev/null
    DMG="build/${APP_NAME}-${VERSION}.dmg"
    if [ -n "$IDENTITY" ]; then
      codesign --force --sign "$IDENTITY" "$DMG" 2>/dev/null || true
    fi

    # Das Disk-Image muss selbst notarisiert werden, nicht nur die App darin.
    # Die App bekommt ihr Ticket oben angeheftet, und sobald sie
    # herauskopiert ist, akzeptiert Gatekeeper sie. Geprüft wird beim
    # Herunterladen aber das DMG, und ein DMG ohne eigenes Ticket meldet
    # „nicht überprüft" — also genau der Dialog, den die Notarisierung
    # beseitigen soll. Bis 10/2026 endete dieses Skript hier, und das Ergebnis
    # sah vollständig aus: die App war notarisiert, das DMG nicht, und nichts
    # sagte das.
    if [ "$TIER" = "developer-id" ] && [ "${NOTARIZE:-1}" = "1" ] \
       && xcrun notarytool history --keychain-profile "${NOTARY_PROFILE:-fundus}" >/dev/null 2>&1; then
      bar "Disk-Image wird notarisiert…"
      if xcrun notarytool submit "$DMG" \
           --keychain-profile "${NOTARY_PROFILE:-fundus}" --wait; then
        if xcrun stapler staple "$DMG"; then
          echo "   notarisiert und geheftet"
        else
          echo "   ⚠︎ Ticket liegt bei Apple, ließ sich aber nicht anheften"
        fi
      else
        echo "   ⚠︎ Notarisierung des DMG fehlgeschlagen"
      fi
    fi
    echo "   $DMG"

    # Sagen, was tatsächlich gilt, statt was gemeint war. Die Zeile oben
    # („Weitergabe: ja") kennt nur das Zertifikat; dies hier fragt Gatekeeper.
    if spctl -a -t install "$DMG" >/dev/null 2>&1; then
      echo "   Gatekeeper: akzeptiert — ohne Rechtsklick-Trick zu öffnen"
    else
      echo "   Gatekeeper: abgelehnt — Empfänger sehen eine Warnung"
      spctl -a -vvv -t install "$DMG" 2>&1 | sed -n 's/^source=/   Grund: /p'
    fi
    ;;
esac

#!/bin/bash
# Installs Fundus on a Mac by building it from source. No disk image, no
# download of a prebuilt app: the source comes from the public repository,
# the app is compiled and signed on this Mac, and because nothing was
# downloaded as an app, Gatekeeper has nothing to quarantine.
#
#   curl -fsSL https://raw.githubusercontent.com/tillheidrich/fundus/main/macos/install.sh | bash
#
# Run the same line again to update. Options (pass after `bash -s --`):
#
#   --ref <tag|branch>   build that version instead of the newest release tag
#   --user               install to ~/Applications even if /Applications is writable
#   --no-open            do not launch the app afterwards
#   --uninstall          remove the app and the build sources (your data stays)
#
# Needs macOS 13 or newer and Apple's Command Line Tools (not full Xcode).
# Everything the app needs at runtime (Python, ffmpeg, Whisper) it sets up on
# first launch. It installs no media extractor unless you ask it to.
set -euo pipefail

REPO="${FUNDUS_REPO:-https://github.com/tillheidrich/fundus.git}"
SRC="${FUNDUS_SRC:-$HOME/Library/Application Support/Fundus-Source}"
DATA="$HOME/Library/Application Support/Fundus"
REF=""
TARGET_DIR=""
OPEN=1
UNINSTALL=0

while [ $# -gt 0 ]; do
  case "$1" in
    --ref) REF="${2:?--ref needs a value}"; shift 2 ;;
    --user) TARGET_DIR="$HOME/Applications"; shift ;;
    --no-open) OPEN=0; shift ;;
    --uninstall) UNINSTALL=1; shift ;;
    -h|--help) sed -n '2,19p' "$0" 2>/dev/null || true; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done

say()  { printf "\033[1m›\033[0m %s\n" "$1"; }
fail() { printf "\033[31m✗\033[0m %s\n" "$1" >&2; exit 1; }

[ "$(uname -s)" = "Darwin" ] || fail "This installer is for macOS. On Linux or Windows, use Docker (see README)."
MAJOR="$(sw_vers -productVersion | cut -d. -f1)"
[ "$MAJOR" -ge 13 ] || fail "Fundus needs macOS 13 (Ventura) or newer."

quit_running_app() {
  if pgrep -x Fundus >/dev/null 2>&1; then
    say "Quitting the running Fundus…"
    osascript -e 'tell application id "app.fundus.desktop" to quit' >/dev/null 2>&1 || true
    for _ in 1 2 3 4 5 6 7 8 9 10; do
      pgrep -x Fundus >/dev/null 2>&1 || break
      sleep 1
    done
  fi
}

# ── Uninstall ────────────────────────────────────────────────────────────────
if [ "$UNINSTALL" = 1 ]; then
  quit_running_app
  for d in /Applications "$HOME/Applications"; do
    if [ -d "$d/Fundus.app" ]; then rm -rf "$d/Fundus.app"; echo "   removed $d/Fundus.app"; fi
  done
  rm -rf "$SRC"
  echo
  say "Fundus is removed. Your data is still in:"
  echo "   $DATA"
  echo "   Delete that folder too if you want everything gone."
  exit 0
fi

# ── Command Line Tools ───────────────────────────────────────────────────────
# `xcode-select -p` alone is not enough: the path can exist while swift is
# missing (a half-finished or removed install). Ask swift itself.
if ! xcode-select -p >/dev/null 2>&1 || ! xcrun --find swift >/dev/null 2>&1; then
  say "Apple's Command Line Tools are missing. A system dialog will ask to install them."
  xcode-select --install >/dev/null 2>&1 || true
  echo
  echo "   Click \"Install\" in that dialog (about 1–2 GB, takes a few minutes),"
  echo "   then run this installer again."
  exit 1
fi
command -v git >/dev/null 2>&1 || fail "git not found, even though the Command Line Tools are there."

# ── Source ───────────────────────────────────────────────────────────────────
# Default: the newest release tag, not main. Tags are what was reviewed and
# released; main may be halfway through something.
if [ -z "$REF" ]; then
  REF="$(git ls-remote --tags --refs "$REPO" 'v*' 2>/dev/null \
         | sed 's#.*refs/tags/##' | sort -V | tail -1 || true)"
  [ -n "$REF" ] || fail "Could not read the release tags from $REPO (offline?)."
fi
say "Fetching Fundus ${REF}…"

# A fresh shallow clone every time: small, and it never has to reconcile a
# checkout someone edited. The tag comes along, so the build gets its version.
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
git -c advice.detachedHead=false clone --quiet --depth 1 --branch "$REF" "$REPO" "$TMP/fundus" \
  || fail "Could not fetch $REF from $REPO."
mkdir -p "$(dirname "$SRC")"
rm -rf "$SRC"
mv "$TMP/fundus" "$SRC"

# ── Build ────────────────────────────────────────────────────────────────────
say "Building (first time takes a few minutes)…"
( cd "$SRC/macos" && NOTARIZE=0 ./build-app.sh ) || fail "The build failed. The output above says where."
APP="$SRC/macos/build/Fundus.app"
[ -d "$APP" ] || fail "The build finished without producing Fundus.app."

# ── Install ──────────────────────────────────────────────────────────────────
if [ -z "$TARGET_DIR" ]; then
  if [ -w /Applications ]; then TARGET_DIR="/Applications"; else TARGET_DIR="$HOME/Applications"; fi
fi
mkdir -p "$TARGET_DIR"
quit_running_app
say "Installing to ${TARGET_DIR}…"
# Swap via a temporary name so a failed copy never leaves you without an app.
rm -rf "$TARGET_DIR/Fundus.app.new"
ditto "$APP" "$TARGET_DIR/Fundus.app.new"
rm -rf "$TARGET_DIR/Fundus.app"
mv "$TARGET_DIR/Fundus.app.new" "$TARGET_DIR/Fundus.app"

# The other location may still hold an older copy; two apps with the same
# bundle id confuse Launch Services and Spotlight.
for d in /Applications "$HOME/Applications"; do
  if [ "$d" != "$TARGET_DIR" ] && [ -d "$d/Fundus.app" ]; then
    echo "   note: an older copy is still at $d/Fundus.app — you can delete it."
  fi
done

# Keep the sources small: the build products are not needed any more.
rm -rf "$SRC/macos/build" "$SRC/macos/.build"

VERSION="$(defaults read "$TARGET_DIR/Fundus.app/Contents/Info" CFBundleShortVersionString 2>/dev/null || echo "$REF")"
echo
say "Fundus $VERSION is installed: $TARGET_DIR/Fundus.app"
echo "   To update later, run the same install command again."
echo "   Your data lives in: $DATA"

if [ "$OPEN" = 1 ]; then
  open "$TARGET_DIR/Fundus.app"
fi

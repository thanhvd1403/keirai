#!/usr/bin/env bash
# Installs the Lightpanda browser binary into <project>/tools/lightpanda.
# The agent's `browse` tool only appears after this script has been run.
#
# Usage:  bash deploy/install_lightpanda.sh
# Lightpanda is AGPLv3 (same license as Keirai) - license-compatible.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="$ROOT/tools"
mkdir -p "$DEST"

ARCH="$(uname -m)"
OS="$(uname -s)"
case "$OS" in
  Linux)
    case "$ARCH" in
      x86_64)          asset="lightpanda-x86_64-linux" ;;
      aarch64|arm64)   asset="lightpanda-aarch64-linux" ;;
      *) echo "unsupported architecture: $ARCH" >&2; exit 1 ;;
    esac ;;
  Darwin)
    case "$ARCH" in
      arm64|aarch64)   asset="lightpanda-aarch64-macos" ;;
      x86_64)          asset="lightpanda-x86_64-macos" ;;
      *) echo "unsupported architecture: $ARCH" >&2; exit 1 ;;
    esac ;;
  *)
    echo "unsupported OS: $OS (Lightpanda has no Windows build - use WSL)" >&2
    exit 1 ;;
esac

URL="https://github.com/lightpanda-io/browser/releases/download/nightly/$asset"
echo "downloading $URL"
curl -fL -o "$DEST/lightpanda.tmp" "$URL"
chmod +x "$DEST/lightpanda.tmp"

echo "verifying..."
"$DEST/lightpanda.tmp" version

mv "$DEST/lightpanda.tmp" "$DEST/lightpanda"
echo "installed: $DEST/lightpanda"
echo "the agent's browse tool is now available (restart keirai to pick it up)"

#!/usr/bin/env bash
#
# Build the Odoo Companion .deb from package/.
# Version is read from package/DEBIAN/control so there is a single source of truth.
#
# Usage: ./scripts/build-deb.sh
#
set -euo pipefail

# Resolve the project root regardless of where the script is called from.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT_DIR"

if ! command -v dpkg-deb >/dev/null 2>&1; then
    echo "error: dpkg-deb not found. Install it with: sudo apt install dpkg-dev" >&2
    exit 1
fi
if ! command -v fakeroot >/dev/null 2>&1; then
    echo "error: fakeroot not found. Install it with: sudo apt install fakeroot" >&2
    exit 1
fi

VERSION="$(awk -F': ' '/^Version:/ {print $2; exit}' package/DEBIAN/control)"
if [ -z "$VERSION" ]; then
    echo "error: could not read Version from package/DEBIAN/control" >&2
    exit 1
fi

OUTPUT="odoo-companion_${VERSION}_all.deb"

# Drop stale byte-compiled files so they are not shipped in the package.
find package -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true

echo "Building $OUTPUT ..."
fakeroot dpkg-deb --build --root-owner-group package "$OUTPUT"

echo
echo "Done: $OUTPUT"
dpkg-deb -I "$OUTPUT" | sed -n '1,12p'

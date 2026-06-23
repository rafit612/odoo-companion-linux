#!/usr/bin/env bash
#
# Create a clean source tarball (odoo-companion-linux-<version>.tar.gz) for use
# with the RPM spec / OBS / a manual AUR source. It mirrors what GitHub produces
# for a release tag, extracting to odoo-companion-linux-<version>/.
#
# Usage: ./scripts/make-source-tarball.sh
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT_DIR"

# Strip any Debian revision (e.g. 2.20.1-1 -> 2.20.1) to get the upstream version.
VERSION="$(awk -F': ' '/^Version:/ {print $2; exit}' package/DEBIAN/control)"
VERSION="${VERSION%-*}"
NAME="odoo-companion-linux"
PREFIX="${NAME}-${VERSION}"
OUT="${PREFIX}.tar.gz"

# Prefer git archive (respects .gitignore) when this is a git checkout.
if git -C "$ROOT_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    git -C "$ROOT_DIR" archive --format=tar.gz --prefix="${PREFIX}/" -o "$OUT" HEAD
else
    tmp="$(mktemp -d)"
    mkdir -p "$tmp/$PREFIX"
    tar --exclude='*.deb' --exclude='public' --exclude='__pycache__' \
        --exclude='.git' -cf - . | tar -xf - -C "$tmp/$PREFIX"
    tar -czf "$OUT" -C "$tmp" "$PREFIX"
    rm -rf "$tmp"
fi

echo "Created $OUT"

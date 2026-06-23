#!/usr/bin/env bash
#
# Generate a signed, flat APT repository under public/ from the .deb files in the
# project root. The output is static and can be hosted on GitHub Pages, S3, or any
# web server. Users then add it with one `deb [signed-by=...]` line and run
# `sudo apt install odoo-companion`.
#
# Usage:
#   ./scripts/make-apt-repo.sh <GPG_KEY_ID>
#
# Requirements: apt-utils (apt-ftparchive), gpg, and a GPG signing key.
#   sudo apt install apt-utils gnupg
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT_DIR"

KEY_ID="${1:-}"
if [ -z "$KEY_ID" ]; then
    echo "usage: $0 <GPG_KEY_ID>" >&2
    echo "  (create one with: gpg --quick-generate-key \"Odoo Companion Repo <you@example.com>\" rsa4096 sign never)" >&2
    exit 1
fi
if ! command -v apt-ftparchive >/dev/null 2>&1; then
    echo "error: apt-ftparchive not found. Install it with: sudo apt install apt-utils" >&2
    exit 1
fi
if ! command -v gpg >/dev/null 2>&1; then
    echo "error: gpg not found. Install it with: sudo apt install gnupg" >&2
    exit 1
fi

DEBS=( odoo-companion_*_all.deb )
if [ ! -e "${DEBS[0]}" ]; then
    echo "error: no odoo-companion_*_all.deb files found in $ROOT_DIR" >&2
    echo "       build one first with: ./scripts/build-deb.sh" >&2
    exit 1
fi

DIST="stable"
COMPONENT="main"
ARCH="all"
PUBLIC="public"
POOL="$PUBLIC/pool/$COMPONENT"
DISTDIR="$PUBLIC/dists/$DIST"
BINDIR="$DISTDIR/$COMPONENT/binary-$ARCH"

echo "Resetting $PUBLIC/ ..."
rm -rf "$PUBLIC"
mkdir -p "$POOL" "$BINDIR"

echo "Copying .deb files into the pool ..."
cp odoo-companion_*_all.deb "$POOL/"

echo "Generating Packages index ..."
( cd "$PUBLIC" && apt-ftparchive packages "pool/$COMPONENT" ) > "$BINDIR/Packages"
gzip -9c "$BINDIR/Packages" > "$BINDIR/Packages.gz"

echo "Generating Release ..."
apt-ftparchive \
    -o "APT::FTPArchive::Release::Origin=odoo-companion" \
    -o "APT::FTPArchive::Release::Label=Odoo Companion" \
    -o "APT::FTPArchive::Release::Suite=$DIST" \
    -o "APT::FTPArchive::Release::Codename=$DIST" \
    -o "APT::FTPArchive::Release::Architectures=$ARCH" \
    -o "APT::FTPArchive::Release::Components=$COMPONENT" \
    release "$DISTDIR" > "$DISTDIR/Release"

echo "Signing Release ..."
gpg --default-key "$KEY_ID" --batch --yes -abs -o "$DISTDIR/Release.gpg" "$DISTDIR/Release"
gpg --default-key "$KEY_ID" --batch --yes --clearsign -o "$DISTDIR/InRelease" "$DISTDIR/Release"

echo "Exporting public key ..."
gpg --armor --export "$KEY_ID" > "$PUBLIC/odoo-companion.gpg.key"

cat > "$PUBLIC/index.html" <<'HTML'
<!doctype html><meta charset="utf-8"><title>Odoo Companion APT repo</title>
<h1>Odoo Companion APT repository</h1>
<pre>
curl -fsSL https://YOUR-HOST/odoo-companion.gpg.key | sudo gpg --dearmor -o /usr/share/keyrings/odoo-companion.gpg
echo "deb [signed-by=/usr/share/keyrings/odoo-companion.gpg] https://YOUR-HOST/ stable main" | sudo tee /etc/apt/sources.list.d/odoo-companion.list
sudo apt update
sudo apt install odoo-companion
</pre>
HTML

echo
echo "Done. Upload the '$PUBLIC/' directory to GitHub Pages or any static host."
echo "Then users run (replace YOUR-HOST):"
echo
echo "  curl -fsSL https://YOUR-HOST/odoo-companion.gpg.key | sudo gpg --dearmor -o /usr/share/keyrings/odoo-companion.gpg"
echo "  echo \"deb [signed-by=/usr/share/keyrings/odoo-companion.gpg] https://YOUR-HOST/ stable main\" | sudo tee /etc/apt/sources.list.d/odoo-companion.list"
echo "  sudo apt update && sudo apt install odoo-companion"

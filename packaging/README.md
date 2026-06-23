# Publishing Odoo Companion to Linux repositories

This folder contains everything needed to publish the app so users can install it
with their normal package manager. Pick the channel(s) that match your audience.

| Channel | User installs with | Build inputs |
|---------|-------------------|--------------|
| **Launchpad PPA** (Ubuntu) | `add-apt-repository ppa:rafit612/odoo-companion && sudo apt install odoo-companion` | `debian/` (repo root) |
| **OBS** (Debian/Ubuntu/Fedora/openSUSE) | add repo, then `apt`/`dnf install` | `debian/` + `packaging/rpm/odoo-companion.spec` |
| **AUR** (Arch/Manjaro) | `yay -S odoo-companion` | `packaging/aur/PKGBUILD` |
| **GitHub Pages APT repo** | one-time setup, then `apt install` | `scripts/make-apt-repo.sh` |

The version is read from `package/DEBIAN/control`. Bump it there **and** in
`debian/changelog`, `packaging/rpm/odoo-companion.spec`, `packaging/aur/PKGBUILD`, and
`package/usr/lib/odoo-companion/odoo_companion/constants.py` when you release.

---

## 1. Launchpad PPA (best for Ubuntu)

A PPA builds from a **source package**. The `debian/` directory at the repo root is
already set up (native 3.0 format), so:

```bash
# one-time prerequisites
sudo apt install devscripts debhelper dput-ng

# from the repo root (the dir containing debian/):
debuild -S -sa            # builds and signs the source package (uses your GPG key)
                          # produces ../odoo-companion_2.21.0_source.changes

dput ppa:rafit612/odoo-companion ../odoo-companion_2.21.0_source.changes
```

Then create the PPA once at <https://launchpad.net/~rafit612> → "Create a new PPA",
and upload your GPG key to Launchpad. Launchpad builds the binary `.deb` for each
Ubuntu series you target. Users then run:

```bash
sudo add-apt-repository ppa:rafit612/odoo-companion
sudo apt update
sudo apt install odoo-companion
```

Notes:
- The `Maintainer:` / changelog email must match the GPG key registered on Launchpad.
- To target multiple Ubuntu series, copy the changelog top stanza and change the
  suite name (e.g. `noble`, `jammy`), bumping the version each time.

---

## 2. openSUSE Build Service (OBS) — one source, many distros

OBS builds **.deb and .rpm** for Debian, Ubuntu, Fedora and openSUSE and hosts the
repositories for free.

```bash
# one-time
sudo apt install osc
osc checkout home:rafit612            # your OBS home project

# create the package and add the sources
cd home:rafit612
osc mkpac odoo-companion
cd odoo-companion

# generate and drop in the source tarball + recipes
../../scripts/make-source-tarball.sh                  # -> odoo-companion-linux-2.21.0.tar.gz
cp ../../odoo-companion-linux-2.21.0.tar.gz .
cp ../../packaging/rpm/odoo-companion.spec .
# for the .deb build, OBS also needs the debianization; the simplest route is to
# add a debian.tar.gz of the repo-root debian/ folder, or enable the "Debian"
# build from the same spec via the dsc. See OBS docs: "Building Debian packages".

osc add *
osc commit -m "odoo-companion 2.21.0"
```

In the OBS web UI, enable the repositories you want (Debian, Ubuntu, Fedora,
openSUSE Tumbleweed/Leap). OBS then publishes per-distro repos with install
instructions it generates for your users.

---

## 3. AUR (Arch / Manjaro)

```bash
# tag a release on GitHub first so the source URL resolves:
#   git tag v2.21.0 && git push --tags

git clone ssh://aur@aur.archlinux.org/odoo-companion.git aur-odoo-companion
cp packaging/aur/PKGBUILD packaging/aur/odoo-companion.install aur-odoo-companion/
cd aur-odoo-companion

updpkgsums                 # fills in the real sha256 from the released tarball
makepkg --printsrcinfo > .SRCINFO
git add PKGBUILD odoo-companion.install .SRCINFO
git commit -m "odoo-companion 2.21.0"
git push
```

Users then install with any AUR helper, e.g. `yay -S odoo-companion`.

---

## 4. GitHub Pages APT repo (no gatekeeper, full control)

Already scripted — see the main `README.md` section "Publishing as an APT
repository" and `scripts/make-apt-repo.sh`.

---

## Which gets a bare `sudo apt install odoo-companion`?

- **PPA / your APT repo / OBS**: after a one-time `add-apt-repository`/repo line.
- **No setup at all**: only once the package is accepted into the *official*
  Debian/Ubuntu archives — that requires going through the Debian maintainer/
  sponsorship process and is a longer-term goal, not a quick upload.

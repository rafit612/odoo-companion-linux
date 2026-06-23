Name:           odoo-companion
Version:        2.21.0
Release:        1%{?dist}
Summary:        Native Odoo dashboard, task timer and desktop notifications

# Replace with your real license (e.g. MIT) before public distribution.
License:        Proprietary
URL:            https://github.com/rafit612/odoo-companion-linux
# GitHub auto-generates this tarball for tag v%{version}; on OBS upload it as
# odoo-companion-linux-%{version}.tar.gz (see packaging/README.md).
Source0:        https://github.com/rafit612/odoo-companion-linux/archive/refs/tags/v%{version}.tar.gz#/odoo-companion-linux-%{version}.tar.gz
BuildArch:      noarch

# Fedora package names. For openSUSE, swap:
#   python3-gobject -> python3-gobject-Gdk, gtk4 -> libgtk-4-1,
#   libadwaita -> libadwaita-1-0, libnotify -> libnotify4,
#   libappindicator-gtk3 -> libayatana-appindicator3-1
Requires:       python3
Requires:       python3-gobject
Requires:       python3-cairo
Requires:       python3-requests
Requires:       gtk4
Requires:       gtk3
Requires:       libadwaita
Requires:       libsecret
Requires:       libnotify
Requires:       libappindicator-gtk3
Requires:       xdg-utils

%description
Odoo Companion connects directly to your own Odoo instance using Odoo's
JSON-RPC external API. It provides native desktop notifications, a GTK4
dashboard with charts, a background task timer, live attendance status in
the system tray, and clickable, filterable module pages. Credentials are
stored in the system keyring; data never leaves your own Odoo server.

%prep
%setup -q -n odoo-companion-linux-%{version}

%build
# Nothing to build (pure Python + data files).

%install
mkdir -p %{buildroot}
cp -a package/usr %{buildroot}%{_prefix}
find %{buildroot} -depth -name '__pycache__' -type d -exec rm -rf {} +

%post
if [ -x /usr/bin/systemctl ]; then
    systemctl --global enable odoo-companion.service >/dev/null 2>&1 || :
fi
/usr/bin/gtk-update-icon-cache -q -t -f %{_datadir}/icons/hicolor >/dev/null 2>&1 || :
/usr/bin/update-desktop-database -q %{_datadir}/applications >/dev/null 2>&1 || :

%preun
if [ "$1" = "0" ] && [ -x /usr/bin/systemctl ]; then
    systemctl --global disable odoo-companion.service >/dev/null 2>&1 || :
fi

%files
%{_bindir}/odoo-companion
%{_bindir}/odoo-companion-service
%{_prefix}/lib/odoo-companion/
%{_prefix}/lib/systemd/user/odoo-companion.service
%{_datadir}/applications/odoo-companion.desktop
%{_datadir}/icons/hicolor/*/apps/odoo-companion.png
%{_datadir}/doc/odoo-companion/README

%changelog
* Wed Jun 24 2026 Rafiur Rahman Rafit <support@dotbdsolutions.com> - 2.21.0-1
- Live attendance in the system tray; smart check-in warning; hardened autostart.
- All module pages with charts, filters and sortable, clickable tables.

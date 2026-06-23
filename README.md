# Odoo Companion (Native Linux App)

A fast, native **GTK4 / Libadwaita** desktop app for Linux that connects directly to
your own **Odoo** server and gives you live desktop notifications, a full multi‑module
dashboard with charts, a background task timer, and a system‑tray indicator — without
ever opening a browser.

It talks to Odoo over Odoo's official **JSON‑RPC external API**, so your data never
passes through any third‑party server. Credentials are kept in your system keyring
(libsecret/GNOME Keyring).

> Developed by **[Rafiur Rahman Rafit](https://rafiurrahmanrafit.com)** for
> **[DotBD Solutions Limited](https://www.dotbdsolutions.com)**.

---

## ✨ Features

### Live desktop notifications (via libnotify)
- **Discuss** direct messages & channel messages (near real‑time, default every 5s)
- **Discuss** voice/video **call** alerts
- **Inbox** mentions, assignments and follower updates
- Employee **check‑in / check‑out** events
- **Server online/offline** alerts
- **Smart "you haven't checked in" warning** — only fires when it's genuinely a
  working day for you: no check‑in yet, the scheduled start + grace window has passed,
  it isn't a public holiday, and you aren't on time off. Clicking it opens the Odoo
  **Time Off** page so you can file leave.
- More reminders: stale CRM lead, meeting‑starting‑soon, helpdesk SLA breach
- **Task‑timer still‑running** reminder (configurable interval)
- Every notification is **click‑to‑open** straight to the relevant Odoo record/action
- Full in‑app notification history with reply‑from‑notification for chats

### Full dashboard with charts, filters & clickable tables
Dedicated pages, each with **donut/bar charts**, **filters** (year/month/status/
salesperson/employee/…), **search‑as‑you‑type**, **sortable columns**, and rows that
**click through to the Odoo record**:

| Page | Highlights |
|------|------------|
| **Dashboard** | At‑a‑glance KPIs across Work, Time Off, Sales/CRM, Ops, Expenses, Purchase, Recruitment + quick "Open in Odoo" buttons |
| **Attendance** | Hours‑worked donut, today's company‑wide table, **monthly view by employee**, **working‑hours schedules**, biometric device locations |
| **Time Off** | By type & by employee charts, full leave table |
| **Activities** | By type & by assignee charts, overdue/today/planned status |
| **Sales** | Orders by status (bar) + by salesperson (donut), order table |
| **CRM** | Leads by stage & by salesperson, lead/opportunity table |
| **Project** | Tasks by stage & by project, task table |
| **Purchase** | Orders by status, PO table |
| **Inventory** | Transfers by status & responsible, picking table |
| **Accounting** | Payment‑status & invoiced‑by‑salesperson charts, invoice table |
| **Expenses** | Expenses by status, expense table |
| **Recruitment** | Applicants by stage & by position, applicant table |

### Task timer
- Start/stop a timer against any of your Odoo project tasks
- Live‑ticking clock in the app **and** in the system‑tray indicator (`⏱ 0:00:05`)
- Checkpoints time to `account.analytic.line` so nothing is lost if the app closes
- Today's timesheet summary + a full team **Timesheets** page with grouping

### Desktop integration & live tray status
- App launcher + optional desktop shortcut
- System‑tray / app‑indicator menu (Open, Poll now, Open Odoo, autostart toggle, Quit)
- **Live attendance in the tray**, updated every second:
  - Checked in → `🟢 3:45:12` live worked time today (hover: "Checked in 10:12 · Working 3:45:12")
  - Checked out → `✓ 18:45` (hover shows last check‑out with date)
  - Task timer running → `⏱` live task time
- Background **systemd user service** that keeps notifications & the tray live when the window is closed
- Toggle "start automatically after login" from Settings or the tray

---

## 🖥️ Requirements

- A Linux desktop with **GTK 4** and **libadwaita** (GNOME, or any modern DE)
- An **Odoo** server (tested with Odoo 16–19) reachable over HTTP(S)
- An Odoo **login** + **password or API key**

Runtime dependencies (pulled in automatically by the `.deb`):

```
python3, python3-gi, python3-gi-cairo, python3-cairo, python3-requests,
gir1.2-gtk-4.0, gir1.2-gtk-3.0, gir1.2-adw-1, gir1.2-secret-1,
gir1.2-notify-0.7, gir1.2-ayatanaappindicator3-0.1, libnotify4, xdg-utils
```

---

## 📦 Installation

### Option A — Download the `.deb` (simplest)

Grab the latest `odoo-companion_*_all.deb` from the
[**Releases**](../../releases) page, then:

```bash
sudo apt install ./odoo-companion_2.20.1-1_all.deb
```

`apt` resolves the dependencies automatically. (If you use `dpkg -i` instead, run
`sudo apt -f install` afterwards to pull missing deps.)

### Option B — `sudo apt install odoo-companion` from an APT repository

To get the plain `sudo apt install odoo-companion` experience **with automatic
updates**, install it from an APT repository. See
[**Publishing as an APT repository**](#-publishing-as-an-apt-repository-so-apt-install-works)
below for how to host one (e.g. free on GitHub Pages). Once a repo is set up, end
users run a one‑time setup and then:

```bash
sudo apt update
sudo apt install odoo-companion
```

---

## 🚀 First run & configuration

1. Launch **Odoo Companion** from your app menu (or run `odoo-companion`).
2. Go to the **Settings** tab and fill in:
   - **Odoo base URL** — e.g. `https://mycompany.odoo.com`
   - **Database** — click **Detect** to auto‑fill, or type it
   - **Username/email**
   - **Password or API key** — an API key is recommended
     (Odoo → *Preferences → Account Security → New API Key*)
3. Click **Test login**, then **Save settings**.

The background service restarts automatically and starts polling. In **Settings** you
can tune the poll intervals (chat/calls seconds, dashboard minutes, timer‑reminder
minutes), set the **check‑in grace/tolerance** (minutes after your scheduled start
before the "not checked in" warning fires), and mute any notification categories you
don't want.

---

## 🔐 Privacy & security

- The app connects **only** to the Odoo URL you configure. No telemetry, no third‑party servers.
- Credentials are stored in your **system keyring** via libsecret — not in plaintext config.
- Config & runtime state live under your home directory:
  - `~/.config/odoo-companion/config.json`
  - `~/.local/share/odoo-companion/state.json`

> **Note on "hiding the code":** this is a Python app, so the source ships as `.py`
> files under `/usr/lib/odoo-companion/`. Anyone with root on the machine can read
> them. That's normal for desktop apps and is **not** a security hole — your *secrets*
> (passwords/API keys) are what matter, and those are in the keyring, never in the code.
> If you need the source obfuscated/compiled, that's a separate packaging step and
> generally discouraged.

---

## 🗂️ Project layout

```
odoo-companion-native/
├── package/                         # Debian package root (built with dpkg-deb)
│   ├── DEBIAN/
│   │   ├── control                  # package metadata, dependencies, version
│   │   ├── postinst                 # enables service, desktop shortcut, icon cache
│   │   └── prerm
│   └── usr/
│       ├── bin/                     # odoo-companion, odoo-companion-service launchers
│       ├── lib/odoo-companion/odoo_companion/
│       │   ├── app.py               # GTK4 GUI (pages, charts, tables, timer)
│       │   ├── service.py           # background polling service + tray indicator
│       │   ├── features.py          # all Odoo JSON-RPC queries / business logic
│       │   ├── client.py            # JSON-RPC client + auth
│       │   ├── constants.py         # config defaults, models, version
│       │   ├── storage.py           # config/state JSON stores
│       │   ├── secret_store.py      # libsecret keyring access
│       │   └── desktop_integration.py
│       ├── lib/systemd/user/odoo-companion.service
│       └── share/                   # .desktop, hicolor icons, doc
├── scripts/
│   ├── build-deb.sh                 # build the .deb from package/
│   └── make-apt-repo.sh             # generate a signed APT repo for hosting
└── README.md
```

---

## 🔧 Building from source

You only need `dpkg-deb` and `fakeroot` (both standard on Debian/Ubuntu):

```bash
sudo apt install dpkg-dev fakeroot
./scripts/build-deb.sh
```

This reads the version from `package/DEBIAN/control` and produces
`odoo-companion_<version>_all.deb` in the project root.

To bump the version, edit **both**:
- `package/DEBIAN/control` → `Version:`
- `package/usr/lib/odoo-companion/odoo_companion/constants.py` → `APP_VERSION`

---

## 🌐 Publishing as an APT repository (so `apt install` works)

There are three realistic ways for Linux users to `apt install` your app. The honest
summary:

| Method | What the user types | Effort for you | Auto‑updates |
|--------|--------------------|----------------|--------------|
| Just ship the `.deb` | `sudo apt install ./file.deb` | none | no |
| **Your own APT repo** (GitHub Pages) | one‑time setup, then `sudo apt install odoo-companion` | low | ✅ yes |
| Ubuntu **PPA** (Launchpad) | `add-apt-repository ppa:you/odoo-companion` | medium | ✅ yes |
| Official Debian/Ubuntu repos | `sudo apt install odoo-companion` (no setup) | high (maintainer process) | ✅ yes |

A bare `sudo apt install odoo-companion` with **zero** extra steps only works if the
package is in the distro's *official* repositories — that requires going through the
Debian/Ubuntu maintainer process and is a long road. For your own distribution, the
practical answer is **host your own APT repo**. Below is the GitHub Pages route (free).

### 1. Generate a GPG signing key (once)

```bash
gpg --quick-generate-key "Odoo Companion Repo <claude.dbsl@gmail.com>" rsa4096 sign never
gpg --list-secret-keys --keyid-format=long      # note your KEY_ID
gpg --armor --export <KEY_ID> > public/odoo-companion.gpg.key   # public key users will import
```

### 2. Build the repo

Put all the `.deb` files you want to publish in the project root and run:

```bash
./scripts/make-apt-repo.sh <KEY_ID>
```

This creates a `public/` folder containing the pool, a signed `Packages`/`Release`/
`InRelease`, and the public key — ready to upload anywhere static (GitHub Pages, S3,
any web server).

### 3. Host it

Push `public/` to a GitHub Pages branch (or any static host). Say it ends up at:

```
https://<you>.github.io/odoo-companion/
```

### 4. What your users run (one time)

```bash
# import the repo signing key
curl -fsSL https://<you>.github.io/odoo-companion/odoo-companion.gpg.key \
  | sudo gpg --dearmor -o /usr/share/keyrings/odoo-companion.gpg

# add the repo
echo "deb [signed-by=/usr/share/keyrings/odoo-companion.gpg] https://<you>.github.io/odoo-companion/ stable main" \
  | sudo tee /etc/apt/sources.list.d/odoo-companion.list

# install
sudo apt update
sudo apt install odoo-companion
```

From then on, `sudo apt upgrade` picks up new versions you publish. Document those
four commands on your Releases page or website.

### Alternative: Ubuntu PPA (Launchpad)

If you specifically target Ubuntu and want `add-apt-repository ppa:...`, create a
Launchpad account, upload a **source** package (`debuild -S` + `dput`), and Launchpad
builds the binary `.deb` for each Ubuntu series. Users then run:

```bash
sudo add-apt-repository ppa:<you>/odoo-companion
sudo apt update
sudo apt install odoo-companion
```

---

## ⚙️ The background service & autostart

The notifier runs as a **systemd user service** so it keeps working — tray status,
live work timer, and notifications — with the app window closed.

**It starts automatically; you never have to launch it by hand.** On install the
service is enabled both globally and for your user (a persistent symlink in
`~/.config/systemd/user/default.target.wants/`). After every reboot or PC power‑on,
as soon as you **log in** the service starts on its own and the tray/notifications
come up — no manual step.

> Why "on login" and not "on boot"? A user service needs your desktop session to show
> notifications and the tray icon, so it (correctly) starts when you log in. With
> auto‑login that's immediately after boot.

The unit uses `Restart=always` with `StartLimitIntervalSec=0`, so if something it needs
(network, keyring, the Odoo server) isn't ready right at login, it keeps retrying
forever instead of giving up.

Manual controls if you ever want them:

```bash
systemctl --user status  odoo-companion.service     # check it
systemctl --user restart odoo-companion.service     # restart it
systemctl --user enable --now odoo-companion.service     # enable + start (first session)
systemctl --user disable --now odoo-companion.service    # stop & disable autostart
```

You can also toggle autostart from the app's **Settings** tab or the tray menu
("Start automatically after login").

---

## 🧹 Uninstall

```bash
sudo apt remove odoo-companion        # remove the app
sudo apt purge odoo-companion         # also remove package-managed config
```

Your personal config/keyring entries under `~/.config/odoo-companion` and the keyring
are left untouched; delete them manually if you want a truly clean slate.

---

## 📋 Changelog (recent)

- **2.20.1** — Hardened autostart: the background service (tray + notifications) starts
  automatically on login after every reboot, with `StartLimitIntervalSec=0` so it never
  stops retrying if network/keyring/server aren't ready yet at login.
- **2.20.0** — Live attendance in the system tray (check‑in time + live worked‑time
  counter, last check‑out with date); smarter "you haven't checked in" warning that
  respects working‑hours calendar, public holidays, time off and a configurable grace
  window, linking straight to the Odoo Time Off page.
- **2.19.0** — Properly aligned, sortable, clickable tables everywhere; Attendance
  monthly view + working‑hours schedules; added missing filters (Expenses month/
  employee/status, Time Off month, Recruitment year/month); auto‑load on open +
  search‑as‑you‑type across all modules.
- **2.18.0** — All module pages (Time Off, Activities, Sales, CRM, Project, Purchase,
  Inventory, Accounting, Expenses, Recruitment) with donut/bar charts, filters and
  clickable rows; modern wrapping pill navigation; live‑ticking timer + tray countdown.
- **2.17.0** — Modern libadwaita UI pass, card styling, Attendance page.
- **2.16.0** — Configurable real‑time notification poll, configurable timer reminder,
  dashboard quick‑actions, Timesheets page, device map.

---

## 📄 License

Copyright © DotBD Solutions Limited. All rights reserved.
(Replace this with your chosen open‑source license — e.g. MIT — before publishing
publicly, and add a `LICENSE` file.)

---

## 🙋 Support

Issues and feature requests: please use the GitHub **Issues** tab.
Built by [Rafiur Rahman Rafit](https://rafiurrahmanrafit.com) ·
[DotBD Solutions Limited](https://www.dotbdsolutions.com).

import argparse
import fcntl
import os
import signal
import subprocess
import sys
import threading
import time

import gi

gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
gi.require_version("Notify", "0.7")
from gi.repository import Gio, GLib, Notify

from .constants import (
    APP_NAME,
    DATA_DIR,
    DEFAULT_NOTIFICATION_POLL_SECONDS,
    DESKTOP_ID,
    ICON_NAME,
    MIN_NOTIFICATION_POLL_SECONDS,
    SERVER_STATUS_INTERVAL_SECONDS,
)
from .desktop_integration import set_autostart_enabled
from .features import FeatureRunner, check_server_status, elapsed_hours, format_clock, notification_target_url, open_target
from .storage import config_store, state_store


class NativeNotifier:
    def __init__(self):
        Notify.init(APP_NAME)
        self._notifications = {}

    def show(self, entry):
        notification = Notify.Notification.new(entry["title"], (entry.get("body") or "")[:300], ICON_NAME)
        try:
            notification.set_hint("desktop-entry", GLib.Variant("s", DESKTOP_ID))
        except Exception:
            pass
        target = entry.get("target") or {}
        if notification_target_url(target):
            try:
                notification.add_action("open", "Open in Odoo", self._on_open, target, None)
            except TypeError:
                notification.add_action("open", "Open in Odoo", self._on_open, target)
        try:
            notification.show()
            self._notifications[entry["id"]] = notification
        except Exception as exc:
            print(f"Odoo Companion: could not show notification: {exc}", file=sys.stderr)

    def _on_open(self, notification, _action, target):
        open_target(target)
        try:
            notification.close()
        except Exception:
            pass


class TrayIndicator:
    def __init__(self, service):
        self.service = service
        self.available = False
        try:
            self.Gtk, self.AppIndicator = self._load_modules()
            self.indicator = self.AppIndicator.Indicator.new(
                "odoo-companion-indicator",
                ICON_NAME,
                self.AppIndicator.IndicatorCategory.APPLICATION_STATUS,
            )
            if hasattr(self.indicator, "set_title"):
                self.indicator.set_title(APP_NAME)
            self.indicator.set_status(self.AppIndicator.IndicatorStatus.ACTIVE)
            self._build_menu()
            self.available = True
        except Exception as exc:
            print(f"Odoo Companion: tray indicator unavailable: {exc}", file=sys.stderr)

    def _load_modules(self):
        gi.require_version("Gtk", "3.0")
        from gi.repository import Gtk

        try:
            gi.require_version("AyatanaAppIndicator3", "0.1")
            from gi.repository import AyatanaAppIndicator3 as AppIndicator
        except (ImportError, ValueError):
            gi.require_version("AppIndicator3", "0.1")
            from gi.repository import AppIndicator3 as AppIndicator
        return Gtk, AppIndicator

    def _build_menu(self):
        menu = self.Gtk.Menu()

        open_item = self.Gtk.MenuItem(label="Open Odoo Companion")
        open_item.connect("activate", self._open_app)
        menu.append(open_item)

        poll_item = self.Gtk.MenuItem(label="Poll Odoo now")
        poll_item.connect("activate", self._poll_now)
        menu.append(poll_item)

        open_odoo_item = self.Gtk.MenuItem(label="Open Odoo in browser")
        open_odoo_item.connect("activate", self._open_odoo)
        menu.append(open_odoo_item)

        menu.append(self.Gtk.SeparatorMenuItem())

        self.autostart_item = self.Gtk.CheckMenuItem(label="Start automatically after login")
        self.autostart_item.set_active(bool(config_store.read().get("autostart_enabled", True)))
        self.autostart_item.connect("toggled", self._toggle_autostart)
        menu.append(self.autostart_item)

        quit_item = self.Gtk.MenuItem(label="Quit background service")
        quit_item.connect("activate", lambda _item: self.service._stop())
        menu.append(quit_item)

        menu.show_all()
        self.indicator.set_menu(menu)
        if hasattr(self.indicator, "set_secondary_activate_target"):
            self.indicator.set_secondary_activate_target(open_item)

    def set_timer_text(self, text, title=None):
        if not self.available:
            return
        try:
            if hasattr(self.indicator, "set_label"):
                self.indicator.set_label(text or "", "00:00:00")
            if hasattr(self.indicator, "set_title"):
                self.indicator.set_title(title or text or APP_NAME)
        except Exception:
            pass

    def _open_app(self, _item):
        subprocess.Popen(["odoo-companion"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)

    def _poll_now(self, _item):
        self.service.trigger_poll_now()

    def _open_odoo(self, _item):
        url = config_store.read().get("odoo_url")
        if url:
            subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)

    def _toggle_autostart(self, item):
        enabled = item.get_active()
        config_store.update(lambda config: config.__setitem__("autostart_enabled", enabled))
        errors = set_autostart_enabled(enabled)
        for error in errors:
            print(f"Odoo Companion: could not update autostart: {error}", file=sys.stderr)


class OdooCompanionService:
    def __init__(self):
        self.notifier = NativeNotifier()
        self.loop = GLib.MainLoop()
        self.poll_thread = None
        self.notification_thread = None
        self.server_thread = None
        self.next_poll_at = 0
        self.next_notification_poll_at = 0
        self.next_server_check_at = 0
        self.lock_file = None
        self.tray = None

    def acquire_lock(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.lock_file = (DATA_DIR / "service.lock").open("w", encoding="utf-8")
        try:
            fcntl.flock(self.lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            return False

    def run(self):
        if not config_store.read().get("autostart_enabled", True):
            print("Odoo Companion service is disabled in settings.")
            return 0

        if not self.acquire_lock():
            print("Odoo Companion service is already running.")
            return 0

        self.tray = TrayIndicator(self)
        network = Gio.NetworkMonitor.get_default()
        if network:
            network.connect("notify::network-available", self._on_network_changed)

        signal.signal(signal.SIGTERM, self._stop)
        signal.signal(signal.SIGINT, self._stop)
        GLib.timeout_add_seconds(5, self._tick)
        GLib.timeout_add_seconds(1, self._update_tray_timer)
        self._tick()
        self.loop.run()
        return 0

    def _update_tray_timer(self):
        if not self.tray:
            return GLib.SOURCE_CONTINUE
        state = state_store.read()
        att = state.get("attendance_self") or {}
        active = state.get("active_timer")
        label = ""
        title = APP_NAME
        if att.get("checked_in") and att.get("current_start_epoch"):
            worked = (att.get("worked_seconds_completed") or 0) + max(0, time.time() - att["current_start_epoch"])
            label = f"🟢 {format_clock(worked)}"
            title = f"Checked in {att.get('check_in_local') or ''} · Working {format_clock(worked)}"
        elif active:
            seconds = elapsed_hours(active["started_at"]) * 3600
            label = f"⏱ {format_clock(seconds)}"
            title = f"Timer: {active.get('task_name') or ''} ({format_clock(seconds)})"
        elif att.get("last_check_out_local"):
            label = f"✓ {att.get('last_check_out_hm') or ''}"
            title = f"Last check-out {att.get('last_check_out_local')}"
        self.tray.set_timer_text(label, title)
        return GLib.SOURCE_CONTINUE

    def _stop(self, *_args):
        self.loop.quit()

    def _on_network_changed(self, monitor, *_args):
        if monitor.get_network_available():
            self.next_poll_at = 0
            self.next_server_check_at = 0

    def trigger_poll_now(self):
        self.next_poll_at = 0
        self.next_notification_poll_at = 0
        self.next_server_check_at = 0
        self._tick()

    def _tick(self):
        now = time.monotonic()
        if now >= self.next_notification_poll_at and not (self.notification_thread and self.notification_thread.is_alive()):
            self.notification_thread = threading.Thread(target=self._run_notification_poll, daemon=True)
            self.notification_thread.start()

        if now >= self.next_poll_at and not (self.poll_thread and self.poll_thread.is_alive()):
            self.poll_thread = threading.Thread(target=self._run_poll, daemon=True)
            self.poll_thread.start()

        if now >= self.next_server_check_at and not (self.server_thread and self.server_thread.is_alive()):
            self.server_thread = threading.Thread(target=self._run_server_check, daemon=True)
            self.server_thread.start()
        return GLib.SOURCE_CONTINUE

    def _poll_interval_seconds(self):
        config = config_store.read()
        poll_minutes = float(config.get("poll_minutes") or 1)
        return max(30, int(poll_minutes * 60))

    def _notification_poll_interval_seconds(self):
        config = config_store.read()
        seconds = float(config.get("notification_poll_seconds") or DEFAULT_NOTIFICATION_POLL_SECONDS)
        return max(MIN_NOTIFICATION_POLL_SECONDS, seconds)

    def _run_notification_poll(self):
        try:
            FeatureRunner(notifier=self.notifier).poll_notifications()
            now_ms = int(time.time() * 1000)
            state_store.update(lambda state: state.__setitem__("last_notification_poll_at", now_ms))
            self.next_notification_poll_at = time.monotonic() + self._notification_poll_interval_seconds()
        except Exception as exc:
            print(f"Odoo Companion notification poll failed: {exc}", file=sys.stderr)
            self.next_notification_poll_at = time.monotonic() + self._notification_poll_interval_seconds()

    def _run_poll(self):
        try:
            runner = FeatureRunner(notifier=self.notifier)
            runner.poll_dashboard_extras()
            now_ms = int(time.time() * 1000)

            def update(state):
                state["last_poll_at"] = now_ms
                state["last_error"] = None

            state_store.update(update)
            self.next_poll_at = time.monotonic() + self._poll_interval_seconds()
        except Exception as exc:
            message = str(exc)
            print(f"Odoo Companion poll failed: {message}", file=sys.stderr)

            def update(state):
                state["last_error"] = message

            state_store.update(update)
            self.next_poll_at = time.monotonic() + 30

    def _run_server_check(self):
        try:
            check_server_status(self.notifier)
        except Exception as exc:
            print(f"Odoo Companion server-status check failed: {exc}", file=sys.stderr)
        self.next_server_check_at = time.monotonic() + SERVER_STATUS_INTERVAL_SECONDS


def run_oneshot():
    service = OdooCompanionService()
    FeatureRunner(notifier=service.notifier).poll_all()
    check_server_status(service.notifier)


def main(argv=None):
    # Safety guard: only ever run inside a real user's session. System/service
    # accounts - crucially the GDM login greeter (user "gdm") - have a uid below
    # 1000. Running the tray + Notify + GTK init inside the greeter session can
    # break the login screen (black screen, no login prompt), so we bail out
    # immediately there. Real desktop users have uid >= 1000.
    if hasattr(os, "getuid") and os.getuid() < 1000:
        return 0

    parser = argparse.ArgumentParser(description="Odoo Companion background service")
    parser.add_argument("--oneshot", action="store_true", help="run one poll and exit")
    args = parser.parse_args(argv)
    if args.oneshot:
        run_oneshot()
        return 0
    return OdooCompanionService().run()


if __name__ == "__main__":
    raise SystemExit(main())

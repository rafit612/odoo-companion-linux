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
    DEFAULT_TIMER_IDLE_MINUTES,
    DEFAULT_NOTIFICATION_POLL_SECONDS,
    DESKTOP_ID,
    ICON_NAME,
    MIN_NOTIFICATION_POLL_SECONDS,
    SERVER_STATUS_INTERVAL_SECONDS,
)
from .desktop_integration import set_autostart_enabled
from .features import FeatureRunner, check_server_status, elapsed_hours, format_clock, notification_target_url, open_target
from .storage import config_store, state_store

try:
    from .idle import get_user_idle_seconds
except Exception:
    def get_user_idle_seconds():
        return None


def _clip(text, limit):
    text = str(text or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "..."


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

        self.floating_item = self.Gtk.CheckMenuItem(label="Show floating desktop widget")
        self.floating_item.set_active(bool(config_store.read().get("floating_widget_enabled", True)))
        self.floating_item.connect("toggled", self._toggle_floating)
        menu.append(self.floating_item)

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

    def _toggle_floating(self, item):
        enabled = item.get_active()
        config_store.update(lambda config: config.__setitem__("floating_widget_enabled", enabled))
        self.service.apply_floating_setting()

    def set_floating_checked(self, enabled):
        if not self.available or not hasattr(self, "floating_item"):
            return
        try:
            self.floating_item.handler_block_by_func(self._toggle_floating)
            self.floating_item.set_active(bool(enabled))
            self.floating_item.handler_unblock_by_func(self._toggle_floating)
        except Exception:
            pass


class FloatingWidget:
    RESTING_OPACITY = 0.72
    HOVER_OPACITY = 0.97
    WIDTH = 330
    HEIGHT = 190

    def __init__(self, service):
        self.service = service
        self.available = False
        self.window = None
        self._press = None
        self._moved = False
        try:
            self.Gtk, self.Gdk = self._load_modules()
            if hasattr(self.Gtk, "init_check"):
                ok, _args = self.Gtk.init_check([])
                if not ok:
                    raise RuntimeError("GTK display is not available")
            self._install_css()
            self._build()
            self.available = True
        except Exception as exc:
            print(f"Odoo Companion: floating widget unavailable: {exc}", file=sys.stderr)

    def _load_modules(self):
        gi.require_version("Gtk", "3.0")
        from gi.repository import Gdk, Gtk

        return Gtk, Gdk

    def _install_css(self):
        css = b"""
        #floating {
            background-color: #20242b;
            border: 1px solid rgba(255,255,255,0.22);
            border-radius: 10px;
        }
        .muted { color: #aab2bf; font-size: 11px; }
        .heading { color: #d7deea; font-size: 11px; font-weight: 600; }
        .badge { background-color: #3f88ff; color: #ffffff; border-radius: 9px; padding: 2px 7px; font-size: 11px; font-weight: 700; }
        .badge-muted { background-color: #4b5563; color: #d1d5db; border-radius: 9px; padding: 2px 7px; font-size: 11px; font-weight: 700; }
        .close { color: #b7beca; font-size: 15px; font-weight: 700; padding: 0 4px; }
        .primary-time { color: #ffffff; font-size: 24px; font-weight: 800; }
        .timer-time { color: #9fe6bd; font-size: 19px; font-weight: 700; }
        .task { color: #f4f7fb; font-size: 12px; font-weight: 600; }
        .metric {
            background-color: rgba(255,255,255,0.08);
            border: 1px solid rgba(255,255,255,0.10);
            border-radius: 8px;
        }
        """
        provider = self.Gtk.CssProvider()
        provider.load_from_data(css)
        screen = self.Gdk.Screen.get_default()
        if screen:
            self.Gtk.StyleContext.add_provider_for_screen(screen, provider, self.Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    def _label(self, text="", *classes):
        label = self.Gtk.Label(label=text)
        label.set_xalign(0)
        for class_name in classes:
            label.get_style_context().add_class(class_name)
        return label

    def _metric_box(self, title):
        frame = self.Gtk.Frame()
        frame.set_shadow_type(self.Gtk.ShadowType.NONE)
        frame.get_style_context().add_class("metric")
        box = self.Gtk.Box(orientation=self.Gtk.Orientation.VERTICAL, spacing=1)
        box.set_margin_top(7)
        box.set_margin_bottom(8)
        box.set_margin_start(9)
        box.set_margin_end(9)
        box.pack_start(self._label(title, "muted"), False, False, 0)
        frame.add(box)
        return frame, box

    def _build(self):
        self.window = self.Gtk.Window(type=self.Gtk.WindowType.TOPLEVEL)
        self.window.set_name("floating")
        self.window.set_title(APP_NAME)
        self.window.set_decorated(False)
        self.window.set_keep_above(True)
        self.window.set_default_size(self.WIDTH, self.HEIGHT)
        self.window.set_size_request(self.WIDTH, self.HEIGHT)
        self.window.set_opacity(self.RESTING_OPACITY)
        self.window.add_events(
            self.Gdk.EventMask.BUTTON_PRESS_MASK
            | self.Gdk.EventMask.BUTTON_RELEASE_MASK
            | self.Gdk.EventMask.POINTER_MOTION_MASK
            | self.Gdk.EventMask.ENTER_NOTIFY_MASK
            | self.Gdk.EventMask.LEAVE_NOTIFY_MASK
        )
        self.window.connect("enter-notify-event", self._on_enter)
        self.window.connect("leave-notify-event", self._on_leave)
        self.window.connect("button-press-event", self._on_press)
        self.window.connect("motion-notify-event", self._on_motion)
        self.window.connect("button-release-event", self._on_release)
        self.window.connect("delete-event", self._on_close)

        root = self.Gtk.Box(orientation=self.Gtk.Orientation.VERTICAL, spacing=7)
        root.set_margin_top(9)
        root.set_margin_bottom(12)
        root.set_margin_start(12)
        root.set_margin_end(12)
        self.window.add(root)

        top = self.Gtk.Box(orientation=self.Gtk.Orientation.HORIZONTAL, spacing=8)
        heading = self._label("Odoo Companion", "heading")
        heading.set_hexpand(True)
        top.pack_start(heading, True, True, 0)

        self.notification_badge = self._label("0", "badge-muted")
        badge_box = self.Gtk.EventBox()
        badge_box.add(self.notification_badge)
        badge_box.connect("button-release-event", self._on_badge_clicked)
        top.pack_start(badge_box, False, False, 0)

        close = self._label("x", "close")
        close_box = self.Gtk.EventBox()
        close_box.add(close)
        close_box.connect("button-release-event", self._on_close_clicked)
        top.pack_start(close_box, False, False, 0)
        root.pack_start(top, False, False, 0)

        metrics = self.Gtk.Box(orientation=self.Gtk.Orientation.HORIZONTAL, spacing=8)
        work_frame, work_box = self._metric_box("WORKING TIME")
        self.work_time = self._label("-", "primary-time")
        self.work_status = self._label("Not checked in", "muted")
        work_box.pack_start(self.work_time, False, False, 0)
        work_box.pack_start(self.work_status, False, False, 0)
        metrics.pack_start(work_frame, True, True, 0)

        timer_frame, timer_box = self._metric_box("TIMESHEET TIMER")
        self.timer_time = self._label("-", "timer-time")
        self.timer_status = self._label("No timer", "muted")
        timer_box.pack_start(self.timer_time, False, False, 0)
        timer_box.pack_start(self.timer_status, False, False, 0)
        metrics.pack_start(timer_frame, True, True, 0)
        root.pack_start(metrics, False, False, 0)

        self.task_label = self._label("No task timer running", "task")
        root.pack_start(self.task_label, False, False, 0)

        detail = self.Gtk.Box(orientation=self.Gtk.Orientation.HORIZONTAL, spacing=8)
        self.check_in_label = self._label("Check-in: -", "muted")
        self.check_in_label.set_hexpand(True)
        detail.pack_start(self.check_in_label, True, True, 0)
        self.notification_text = self._label("Notifications: 0", "muted")
        detail.pack_start(self.notification_text, False, False, 0)
        root.pack_start(detail, False, False, 0)

        self.idle_label = self._label("", "muted")
        root.pack_start(self.idle_label, False, False, 0)
        self.window.show_all()
        self.window.hide()

    def _on_enter(self, *_args):
        self.window.set_opacity(self.HOVER_OPACITY)
        return False

    def _on_leave(self, *_args):
        self.window.set_opacity(self.RESTING_OPACITY)
        return False

    def _on_press(self, _widget, event):
        if int(event.button) != 1:
            return False
        self._press = (event.x_root, event.y_root)
        self._moved = False
        self.window.set_opacity(self.HOVER_OPACITY)
        self.window.begin_move_drag(int(event.button), int(event.x_root), int(event.y_root), int(event.time))
        return False

    def _on_motion(self, _widget, event):
        if self._press and (abs(event.x_root - self._press[0]) > 6 or abs(event.y_root - self._press[1]) > 6):
            self._moved = True
        return False

    def _on_release(self, _widget, event):
        was_drag = self._moved
        self._press = None
        self._moved = False
        self.service.save_floating_position()
        if int(event.button) == 1 and not was_drag:
            self.service.open_app()
        return False

    def _on_close(self, *_args):
        self.service.set_floating_enabled(False)
        return True

    def _on_badge_clicked(self, *_args):
        self.service.open_app()
        return True

    def _on_close_clicked(self, *_args):
        self.service.set_floating_enabled(False)
        return True

    def show(self):
        if not self.available:
            return
        self.service.place_floating_widget()
        self.update_state()
        self.window.show_all()
        self.window.present()

    def hide(self):
        if self.available and self.window:
            self.window.hide()

    def is_visible(self):
        return bool(self.available and self.window and self.window.get_visible())

    def get_position(self):
        if not self.available or not self.window:
            return None
        x, y = self.window.get_position()
        return {"x": int(x), "y": int(y)}

    def move(self, x, y):
        if self.available and self.window:
            self.window.move(int(x), int(y))

    def _idle_countdown_text(self, config):
        if not config.get("timer_idle_auto_stop", True):
            return ""
        try:
            limit_seconds = max(0.0, float(config.get("timer_idle_minutes") or DEFAULT_TIMER_IDLE_MINUTES) * 60)
        except (TypeError, ValueError):
            limit_seconds = DEFAULT_TIMER_IDLE_MINUTES * 60
        if limit_seconds <= 0:
            return ""
        idle_seconds = get_user_idle_seconds()
        if idle_seconds is None:
            return ""
        remaining = max(0.0, limit_seconds - idle_seconds)
        return f"Idle auto-stop in {format_clock(remaining)}"

    def _set_badge_class(self, has_notifications):
        ctx = self.notification_badge.get_style_context()
        ctx.remove_class("badge")
        ctx.remove_class("badge-muted")
        ctx.add_class("badge" if has_notifications else "badge-muted")

    def update_state(self):
        if not self.available or not self.window:
            return
        state = state_store.read()
        config = config_store.read()
        att = state.get("attendance_self") or {}
        active = state.get("active_timer")
        notification_count = len(state.get("notification_log") or [])
        self.notification_badge.set_text("99+" if notification_count > 99 else str(notification_count))
        self._set_badge_class(notification_count > 0)
        self.notification_text.set_text(f"Notifications: {notification_count}")

        if att.get("checked_in") and att.get("current_start_epoch"):
            worked = (att.get("worked_seconds_completed") or 0) + max(0, time.time() - att["current_start_epoch"])
            check_in = att.get("check_in_local") or "-"
            self.work_time.set_text(format_clock(worked))
            self.work_status.set_text(f"Since {check_in}")
            self.check_in_label.set_text(f"Check-in: {check_in}")
        elif att.get("last_check_out_local"):
            self.work_time.set_text("-")
            self.work_status.set_text("Checked out")
            self.check_in_label.set_text(f"Last out: {att.get('last_check_out_local')}")
        else:
            self.work_time.set_text("-")
            self.work_status.set_text("Not checked in")
            self.check_in_label.set_text("Check-in: -")

        if active:
            self.timer_time.set_text(format_clock(elapsed_hours(active["started_at"]) * 3600))
            self.timer_status.set_text("Live")
            self.task_label.set_text(_clip(active.get("task_name") or "Running task", 48))
            self.idle_label.set_text(self._idle_countdown_text(config))
        else:
            self.timer_time.set_text("-")
            self.timer_status.set_text("No timer")
            self.task_label.set_text("No task timer running")
            self.idle_label.set_text("")


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
        self.floating = None
        self._last_seen_write_at = 0
        self._system_bus = None
        self._session_bus = None

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

        self._recover_timer_after_restart()
        self.tray = TrayIndicator(self)
        self.floating = FloatingWidget(self)
        self.apply_floating_setting()
        self._subscribe_system_events()
        network = Gio.NetworkMonitor.get_default()
        if network:
            network.connect("notify::network-available", self._on_network_changed)

        signal.signal(signal.SIGTERM, self._stop)
        signal.signal(signal.SIGINT, self._stop)
        GLib.timeout_add_seconds(5, self._tick)
        GLib.timeout_add_seconds(1, self._update_live_status)
        self._tick()
        self.loop.run()
        return 0

    def _mark_service_seen(self):
        now = time.monotonic()
        if now - self._last_seen_write_at < 5:
            return
        self._last_seen_write_at = now
        now_ms = int(time.time() * 1000)
        state_store.update(lambda state: state.__setitem__("last_service_seen_at", now_ms))

    def _update_live_status(self):
        self._mark_service_seen()
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
        if self.tray:
            self.tray.set_timer_text(label, title)
        if self.floating and self.floating.is_visible():
            self.floating.update_state()
        return GLib.SOURCE_CONTINUE

    def _stop(self, *_args):
        self._mark_timer_stop_pending("background service stopped")
        self.loop.quit()

    def open_app(self):
        subprocess.Popen(["odoo-companion"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)

    def set_floating_enabled(self, enabled):
        enabled = bool(enabled)
        config_store.update(lambda config: config.__setitem__("floating_widget_enabled", enabled))
        if self.tray:
            self.tray.set_floating_checked(enabled)
        self.apply_floating_setting()

    def apply_floating_setting(self):
        enabled = bool(config_store.read().get("floating_widget_enabled", True))
        if self.tray:
            self.tray.set_floating_checked(enabled)
        if not self.floating or not self.floating.available:
            return
        if enabled:
            self.floating.show()
        else:
            self.floating.hide()

    def _screen_geometry(self):
        if not self.floating or not self.floating.available:
            return None
        screen = self.floating.Gdk.Screen.get_default()
        if not screen:
            return None
        monitor = screen.get_primary_monitor()
        if monitor < 0:
            monitor = 0
        return screen.get_monitor_workarea(monitor)

    def place_floating_widget(self):
        if not self.floating or not self.floating.available:
            return
        geometry = self._screen_geometry()
        if not geometry:
            return
        saved = state_store.read().get("floating_widget_pos") or {}
        try:
            x = int(saved.get("x"))
            y = int(saved.get("y"))
        except (TypeError, ValueError):
            x = geometry.x + geometry.width - self.floating.WIDTH - 24
            y = geometry.y + 24
        max_x = max(geometry.x, geometry.x + geometry.width - self.floating.WIDTH)
        max_y = max(geometry.y, geometry.y + geometry.height - self.floating.HEIGHT)
        x = min(max(geometry.x, x), max_x)
        y = min(max(geometry.y, y), max_y)
        self.floating.move(x, y)

    def save_floating_position(self):
        if not self.floating:
            return
        position = self.floating.get_position()
        if position:
            state_store.update(lambda state: state.__setitem__("floating_widget_pos", position))

    def _recover_timer_after_restart(self):
        state = state_store.read()
        active = state.get("active_timer")
        last_seen = state.get("last_service_seen_at")
        if not active or not last_seen:
            return
        try:
            last_seen = int(last_seen)
        except (TypeError, ValueError):
            return
        now_ms = int(time.time() * 1000)
        if now_ms - last_seen < 60 * 1000:
            return
        pending = {
            **active,
            "stopped_at": last_seen,
            "stop_reason": "the computer or companion service was stopped",
            "notify": True,
        }

        def mark(saved):
            saved["active_timer"] = None
            saved["timer_stop_pending"] = pending

        state_store.update(mark)

    def _mark_timer_stop_pending(self, reason):
        try:
            FeatureRunner(notifier=self.notifier).close_task_timer_for_system_event(reason, notify=True, flush=False)
        except Exception as exc:
            print(f"Odoo Companion: could not mark timer stopped for {reason}: {exc}", file=sys.stderr)

    def _queue_timer_stop(self, reason):
        self._mark_timer_stop_pending(reason)
        self.next_notification_poll_at = 0
        if not (self.notification_thread and self.notification_thread.is_alive()):
            self.notification_thread = threading.Thread(target=self._run_notification_poll, daemon=True)
            self.notification_thread.start()

    def _subscribe_system_events(self):
        try:
            self._system_bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
            self._system_bus.signal_subscribe(
                "org.freedesktop.login1",
                "org.freedesktop.login1.Manager",
                "PrepareForSleep",
                "/org/freedesktop/login1",
                None,
                Gio.DBusSignalFlags.NONE,
                self._on_prepare_for_sleep,
            )
            session_id = os.environ.get("XDG_SESSION_ID")
            if session_id:
                result = self._system_bus.call_sync(
                    "org.freedesktop.login1",
                    "/org/freedesktop/login1",
                    "org.freedesktop.login1.Manager",
                    "GetSession",
                    GLib.Variant("(s)", (session_id,)),
                    GLib.VariantType("(o)"),
                    Gio.DBusCallFlags.NONE,
                    1000,
                    None,
                )
                session_path = result.unpack()[0]
                self._system_bus.signal_subscribe(
                    "org.freedesktop.login1",
                    "org.freedesktop.login1.Session",
                    None,
                    session_path,
                    None,
                    Gio.DBusSignalFlags.NONE,
                    self._on_login1_session_signal,
                )
        except Exception as exc:
            print(f"Odoo Companion: login/session event hooks unavailable: {exc}", file=sys.stderr)

        try:
            self._session_bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            for interface in ("org.freedesktop.ScreenSaver", "org.gnome.ScreenSaver"):
                self._session_bus.signal_subscribe(
                    None,
                    interface,
                    "ActiveChanged",
                    "/ScreenSaver",
                    None,
                    Gio.DBusSignalFlags.NONE,
                    self._on_screensaver_active_changed,
                )
        except Exception as exc:
            print(f"Odoo Companion: screensaver event hooks unavailable: {exc}", file=sys.stderr)

    def _on_prepare_for_sleep(self, _connection, _sender, _path, _interface, _signal, parameters):
        sleeping = bool(parameters.unpack()[0])
        if sleeping:
            self._queue_timer_stop("the computer is going to sleep")
        else:
            self.trigger_poll_now()

    def _on_login1_session_signal(self, _connection, _sender, _path, _interface, signal_name, _parameters):
        if signal_name in ("Lock", "Unlock"):
            if signal_name == "Lock":
                self._queue_timer_stop("the computer session was locked")
            else:
                self.trigger_poll_now()

    def _on_screensaver_active_changed(self, _connection, _sender, _path, _interface, _signal, parameters):
        try:
            active = bool(parameters.unpack()[0])
        except Exception:
            active = False
        if active:
            self._queue_timer_stop("the computer session was locked")

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

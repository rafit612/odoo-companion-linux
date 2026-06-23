import math
import threading
import time
import webbrowser
from datetime import datetime, timezone

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk

try:
    import cairo

    HAVE_CAIRO = True
except Exception:
    HAVE_CAIRO = False

from .client import OdooClient
from .constants import (
    APP_ID,
    APP_NAME,
    APP_VERSION,
    DEFAULT_ATTENDANCE_GRACE_MINUTES,
    DEFAULT_NOTIFICATION_POLL_SECONDS,
    DEFAULT_TIMER_REMINDER_MINUTES,
    ICON_NAME,
    MIN_NOTIFICATION_POLL_SECONDS,
    MODULE_LABELS,
    MUTE_LABELS,
)
from .desktop_integration import create_desktop_shortcut, restart_background_service, set_autostart_enabled
from .features import FeatureRunner, check_server_status, elapsed_hours, format_clock, notification_target_url, open_target
from .secret_store import lookup_secret, store_secret
from .storage import config_store, reset_cached_identity, state_store


def run_async(task, callback):
    def target():
        try:
            result = task()
            GLib.idle_add(callback, result, None)
        except Exception as exc:
            GLib.idle_add(callback, None, exc)

    threading.Thread(target=target, daemon=True).start()


def clear_box(box):
    child = box.get_first_child()
    while child:
        nxt = child.get_next_sibling()
        box.remove(child)
        child = nxt


# Quick-action buttons per dashboard tab, mirroring the "Open in Odoo" links
# the Chrome/Firefox extension shows on each panel - lets the user jump
# straight to the matching backend app instead of digging through menus.
TAB_APP_LINKS = {
    "Work": [("Project", "project"), ("Timesheets", "timesheets")],
    "Time Off": [("Time Off", "time-off"), ("Calendar", "calendar")],
    "Sales/CRM": [("CRM", "crm"), ("Sales", "sales"), ("Point of Sale", "point-of-sale")],
    "Ops": [("Helpdesk", "helpdesk"), ("Calendar", "calendar"), ("Inventory", "inventory"), ("Approvals", "approvals")],
    "Expenses": [("Expenses", "expenses")],
    "Purchase": [("Purchase", "purchase")],
    "Recruitment": [("Recruitment", "recruitment")],
}


def row_box(title, value):
    box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
    box.set_margin_top(7)
    box.set_margin_bottom(7)
    box.set_margin_start(10)
    box.set_margin_end(10)
    label = Gtk.Label(label=title, xalign=0)
    label.set_hexpand(True)
    box.append(label)
    if isinstance(value, Gtk.Widget):
        value.set_hexpand(True)
        box.append(value)
    else:
        value_label = Gtk.Label(label=str(value), xalign=1)
        value_label.set_selectable(True)
        value_label.set_wrap(True)
        box.append(value_label)
    return box


def section_title(title):
    label = Gtk.Label(label=title, xalign=0)
    label.add_css_class("heading")
    label.set_margin_top(14)
    label.set_margin_bottom(6)
    return label


def fmt_dt(value):
    if not value:
        return "-"
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(value)


def fmt_date(value):
    return str(value or "-")[:10]


def fmt_hours(decimal_hours):
    if decimal_hours is None:
        return "-"
    total_minutes = round(float(decimal_hours) * 60)
    return f"{total_minutes // 60}:{total_minutes % 60:02d}"


def fmt_money(value):
    try:
        return f"{float(value or 0):,.2f}"
    except Exception:
        return str(value)


# Donut/legend colour palette - mirrors the extension's chart colours so the
# native app's graphs look familiar.
CHART_COLORS = [
    (0.42, 0.31, 0.64), (0.20, 0.60, 0.86), (0.90, 0.49, 0.13),
    (0.18, 0.80, 0.44), (0.95, 0.77, 0.06), (0.91, 0.30, 0.24),
    (0.10, 0.74, 0.61), (0.61, 0.35, 0.71), (0.20, 0.29, 0.37),
    (0.83, 0.33, 0.00), (0.16, 0.50, 0.73), (0.56, 0.27, 0.68),
    (0.45, 0.55, 0.13), (0.74, 0.20, 0.64), (0.30, 0.69, 0.31),
]


def _color(i):
    return CHART_COLORS[i % len(CHART_COLORS)]


class DonutChart(Gtk.DrawingArea):
    def __init__(self, segments, size=170):
        super().__init__()
        self.segments = segments
        self.set_content_width(size)
        self.set_content_height(size)
        self.set_draw_func(self._draw)

    def _draw(self, _area, cr, width, height, *_args):
        total = sum(max(0.0, float(v or 0)) for _, v in self.segments)
        cx, cy = width / 2, height / 2
        radius = min(width, height) / 2 - 4
        if total <= 0:
            cr.set_source_rgb(0.85, 0.85, 0.85)
            cr.arc(cx, cy, radius, 0, 2 * math.pi)
            cr.fill()
        else:
            start = -math.pi / 2
            for i, (_label, value) in enumerate(self.segments):
                frac = max(0.0, float(value or 0)) / total
                if frac <= 0:
                    continue
                end = start + frac * 2 * math.pi
                r, g, b = _color(i)
                cr.set_source_rgb(r, g, b)
                cr.move_to(cx, cy)
                cr.arc(cx, cy, radius, start, end)
                cr.close_path()
                cr.fill()
                start = end
        if HAVE_CAIRO:
            cr.set_operator(cairo.OPERATOR_CLEAR)
            cr.arc(cx, cy, radius * 0.58, 0, 2 * math.pi)
            cr.fill()
            cr.set_operator(cairo.OPERATOR_OVER)


def _legend_swatch(i):
    area = Gtk.DrawingArea()
    area.set_content_width(14)
    area.set_content_height(14)
    area.set_valign(Gtk.Align.CENTER)

    def draw(_a, cr, w, h, *_args):
        r, g, b = _color(i)
        cr.set_source_rgb(r, g, b)
        cr.rectangle(2, 2, w - 4, h - 4)
        cr.fill()

    area.set_draw_func(draw)
    return area


def chart_card(title, segments, value_fmt=None, kind="donut", note=None):
    """Build a titled card with a donut (or horizontal bar) chart and legend.

    segments: list of (label, value). value_fmt formats the numeric value.
    """
    value_fmt = value_fmt or (lambda v: str(int(v)) if float(v or 0).is_integer() else f"{v:.2f}")
    segments = [(label, float(value or 0)) for label, value in segments if (value or 0)]
    card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    card.add_css_class("card")
    card.set_margin_top(6)
    card.set_margin_bottom(6)
    card.append(section_title(title))
    if not segments:
        card.append(Gtk.Label(label="No data for this filter.", xalign=0))
        return card

    if kind == "bar":
        max_value = max(v for _, v in segments) or 1
        for i, (label, value) in enumerate(segments):
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            name = Gtk.Label(label=label, xalign=0)
            name.set_size_request(170, -1)
            row.append(name)
            bar = Gtk.ProgressBar()
            bar.set_fraction(value / max_value)
            bar.set_hexpand(True)
            bar.set_valign(Gtk.Align.CENTER)
            row.append(bar)
            value_label = Gtk.Label(label=value_fmt(value), xalign=1)
            value_label.set_size_request(90, -1)
            row.append(value_label)
            card.append(row)
        if note:
            card.append(Gtk.Label(label=note, xalign=0, wrap=True))
        return card

    body = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
    body.append(DonutChart(segments))
    legend = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
    legend.set_valign(Gtk.Align.CENTER)
    for i, (label, value) in enumerate(segments):
        item = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        item.append(_legend_swatch(i))
        item.append(Gtk.Label(label=f"{label}: {value_fmt(value)}", xalign=0))
        legend.append(item)
    body.append(legend)
    card.append(body)
    if note:
        card.append(Gtk.Label(label=note, xalign=0, wrap=True))
    return card


def aggregate(rows, key_fn, value_fn=None):
    """Group rows -> {label: summed value}, preserving first-seen order."""
    value_fn = value_fn or (lambda _row: 1)
    totals = {}
    for row in rows:
        label = key_fn(row)
        totals[label] = totals.get(label, 0) + (value_fn(row) or 0)
    return list(totals.items())


def m2o_name(row, field, default="(none)"):
    value = row.get(field)
    if value and isinstance(value, (list, tuple)) and len(value) > 1:
        return value[1]
    return default


def m2o_id(row, field):
    value = row.get(field)
    if value and isinstance(value, (list, tuple)):
        return value[0]
    return None


SALE_STATE = {"draft": "Quotation", "sent": "Quotation Sent", "sale": "Sales Order", "done": "Locked", "cancel": "Cancelled"}
PURCHASE_STATE = {"draft": "RFQ", "sent": "RFQ Sent", "to approve": "To Approve", "purchase": "Purchase Order", "done": "Locked", "cancel": "Cancelled"}
PICKING_STATE = {"draft": "Draft", "waiting": "Waiting", "confirmed": "Waiting", "assigned": "Ready", "done": "Done", "cancel": "Cancelled"}
PAYMENT_STATE = {"not_paid": "Not Paid", "in_payment": "In Payment", "paid": "Paid", "partial": "Partially Paid", "reversed": "Reversed", "invoicing_legacy": "Legacy"}
EXPENSE_STATE = {"draft": "To Report", "reported": "To Submit", "submit": "Submitted", "approve": "Approved", "post": "Posted", "done": "Done", "refused": "Refused"}
LEAVE_STATE = {"draft": "To Submit", "confirm": "To Approve", "validate1": "Second Approval", "validate": "Approved", "refuse": "Refused", "cancel": "Cancelled"}
POS_STATE = {"draft": "New", "paid": "Paid", "done": "Posted", "invoiced": "Invoiced", "cancel": "Cancelled"}

# Maps a page's stack name to the module-access key (see MODULE_MODELS). Pages
# whose module isn't installed or the user can't read are hidden. Pages not
# listed here (Dashboard, Activities, Timer, Notifications, Settings) always show.
PAGE_MODULE = {
    "attendance": "attendance",
    "time-off": "leave",
    "sales": "sale",
    "crm": "crm",
    "project": "project",
    "purchase": "purchase",
    "inventory": "stock",
    "accounting": "account",
    "expenses": "expense",
    "recruitment": "recruitment",
    "helpdesk": "helpdesk",
    "point-of-sale": "pos",
    "timesheets": "timesheet",
}


def _status_cell(label, kind=None):
    return (label, kind)


def _sort_value(text):
    """Sort key that orders numbers numerically and text alphabetically,
    keeping empty cells last-ish. Returns a (rank, value) tuple so a numeric
    value is never compared against a string."""
    s = str(text if text is not None else "").strip()
    if s in ("", "-"):
        return (3, "")
    cleaned = s.replace(",", "").replace("d", "").replace("⏱", "").replace(":", "").strip()
    try:
        return (1, float(cleaned))
    except ValueError:
        return (2, s.lower())


class MainWindow(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title=APP_NAME)
        self.set_default_size(920, 680)
        self.set_icon_name(ICON_NAME)
        self.timer_tasks = {}

        header = Gtk.HeaderBar()
        title = Gtk.Label(label=APP_NAME)
        title.add_css_class("title-2")
        header.set_title_widget(title)
        self.set_titlebar(header)

        self._pages = {}
        self._nav_buttons = {}
        self._nav_syncing = False
        self._suppress_filters = False
        self._loaded = set()

        self.view_stack = Adw.ViewStack()
        self.view_stack.set_vexpand(True)

        self.nav_flow = Gtk.FlowBox()
        self.nav_flow.set_selection_mode(Gtk.SelectionMode.NONE)
        self.nav_flow.set_max_children_per_line(30)
        self.nav_flow.set_halign(Gtk.Align.START)
        self.nav_flow.set_column_spacing(4)
        self.nav_flow.set_row_spacing(4)
        self.nav_flow.set_margin_top(8)
        self.nav_flow.set_margin_start(12)
        self.nav_flow.set_margin_end(12)
        self.nav_flow.set_margin_bottom(6)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        outer.append(self.nav_flow)
        outer.append(Gtk.Separator())
        outer.append(self.view_stack)
        self.set_child(outer)

        self._build_dashboard_page()
        self._build_attendance_page()
        for spec in self._module_page_specs():
            self._build_module_page(spec)
        self._build_timer_page()
        self._build_timesheets_page()
        self._build_notifications_page()
        self._build_settings_page()

        first = next(iter(self._nav_buttons))
        self._nav_buttons[first].set_active(True)

        self.reload_settings()
        self.refresh_status()
        self.refresh_dashboard()
        self.refresh_timer()
        self.refresh_notifications()
        self._loaded.update({"dashboard", "notifications", "settings"})
        self.load_module_access()
        GLib.timeout_add_seconds(1, self._tick_timer_label)

    def add_page(self, title, icon_name=None):
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_margin_top(18)
        box.set_margin_bottom(18)
        box.set_margin_start(20)
        box.set_margin_end(20)
        scroller.set_child(box)
        name = title.lower().replace(" ", "-")
        page = self.view_stack.add_titled(scroller, name, title)
        if icon_name:
            page.set_icon_name(icon_name)
        self._add_nav_pill(name, title)
        return box

    def _add_nav_pill(self, name, title):
        button = Gtk.ToggleButton(label=title)
        button.add_css_class("pill")
        button.connect("toggled", self._on_nav_toggled, name)
        self._nav_buttons[name] = button
        self.nav_flow.append(button)

    def _on_nav_toggled(self, button, name):
        if self._nav_syncing:
            return
        if not button.get_active():
            button.set_active(True)
            return
        self._nav_syncing = True
        for other_name, other in self._nav_buttons.items():
            other.set_active(other_name == name)
        self.view_stack.set_visible_child_name(name)
        self._nav_syncing = False
        self._lazy_load(name)

    def load_module_access(self):
        """Hide pages for modules that aren't installed or the user can't read."""
        def task():
            return OdooClient().check_module_access()

        def done(access, error):
            if error or not access:
                return
            self.apply_module_access(access)

        run_async(task, done)

    def apply_module_access(self, access):
        for name, module_key in PAGE_MODULE.items():
            # Default to visible when detection is missing, so a detection hiccup
            # never hides a working page.
            allowed = access.get(module_key, True)
            button = self._nav_buttons.get(name)
            if not button:
                continue
            target = button.get_parent() or button  # the FlowBoxChild wrapper
            target.set_visible(bool(allowed))
            if not allowed and self.view_stack.get_visible_child_name() == name:
                dashboard_btn = self._nav_buttons.get("dashboard")
                if dashboard_btn:
                    dashboard_btn.set_active(True)

    def _lazy_load(self, name):
        if name in self._loaded:
            return
        self._loaded.add(name)
        if name == "attendance":
            self.refresh_attendance()
            self.load_attendance_extras()
        elif name == "timesheets":
            self.load_timesheet_filters()
            self.refresh_timesheets()
        elif name == "timer":
            self.load_timer_tasks()
        elif name in self._pages:
            self._module_refresh(name)

    def set_status(self, text):
        self.status_label.set_label(text)

    def _build_dashboard_page(self):
        self.dashboard_page = self.add_page("Dashboard", "go-home-symbolic")
        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.dashboard_page.append(top)
        self.status_label = Gtk.Label(label="Loading...", xalign=0)
        self.status_label.set_hexpand(True)
        top.append(self.status_label)
        refresh = Gtk.Button(label="Refresh")
        refresh.connect("clicked", lambda _button: self.refresh_dashboard())
        top.append(refresh)
        poll = Gtk.Button(label="Poll now")
        poll.connect("clicked", lambda _button: self.poll_now())
        top.append(poll)

        self.dashboard_tabs = Gtk.Notebook()
        self.dashboard_page.append(self.dashboard_tabs)
        self.approvals_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.dashboard_page.append(self.approvals_box)

    def _build_attendance_page(self):
        self.attendance_page = self.add_page("Attendance", "contact-new-symbolic")
        self.attendance_page.append(section_title("Today's attendance — all employees"))

        filters = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.att_department_combo = Gtk.ComboBoxText()
        self.att_department_combo.append("", "All departments")
        self.att_department_combo.set_active(0)
        self.att_department_combo.connect("changed", lambda _c: self.refresh_attendance())
        filters.append(self.att_department_combo)
        self.att_search = Gtk.Entry()
        self.att_search.set_placeholder_text("Search employee (auto-searches as you type)")
        self.att_search.set_hexpand(True)
        self._att_search_debounce_id = None
        self.att_search.connect("changed", self._on_attendance_search_changed)
        filters.append(self.att_search)
        refresh = Gtk.Button(label="Refresh")
        refresh.connect("clicked", lambda _b: self.refresh_attendance())
        filters.append(refresh)
        self.attendance_page.append(filters)

        self.att_summary_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.att_summary_box.add_css_class("card")
        self.att_summary_box.set_margin_top(4)
        self.att_summary_box.set_margin_bottom(4)
        self.attendance_page.append(self.att_summary_box)

        self.attendance_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.attendance_page.append(self.attendance_box)

        # ---- Monthly view by employee ----
        self.attendance_page.append(section_title("Monthly view by employee"))
        month_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.att_month_employee = Gtk.ComboBoxText()
        self.att_month_employee.append("", "Pick an employee")
        self.att_month_employee.set_active(0)
        month_row.append(self.att_month_employee)
        self.att_month_year = Gtk.ComboBoxText()
        current_year = datetime.now().year
        for year in range(current_year, current_year - 6, -1):
            self.att_month_year.append(str(year), str(year))
        self.att_month_year.set_active_id(str(current_year))
        month_row.append(self.att_month_year)
        self.att_month_month = Gtk.ComboBoxText()
        for i, name in enumerate(
            ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"], 1
        ):
            self.att_month_month.append(str(i), name)
        self.att_month_month.set_active(datetime.now().month - 1)
        month_row.append(self.att_month_month)
        load_month = Gtk.Button(label="Load")
        load_month.connect("clicked", lambda _b: self.refresh_attendance_month())
        month_row.append(load_month)
        self.attendance_page.append(month_row)
        self.att_month_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.attendance_page.append(self.att_month_box)

        # ---- Working hours schedules ----
        self.attendance_page.append(section_title("Working hours schedules"))
        self.att_schedules_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.attendance_page.append(self.att_schedules_box)

        self._attendance_rows = []
        self._attendance_departments_loaded = False
        self._attendance_extras_loaded = False

    def load_attendance_extras(self):
        if self._attendance_extras_loaded:
            return
        self._attendance_extras_loaded = True

        def task():
            return FeatureRunner().fetch_employees(), FeatureRunner().fetch_working_schedules()

        def done(result, error):
            if error:
                return
            employees, schedules = result
            self.att_month_employee.remove_all()
            self.att_month_employee.append("", "Pick an employee")
            for employee in employees or []:
                self.att_month_employee.append(str(employee["id"]), employee["name"])
            self.att_month_employee.set_active(0)
            clear_box(self.att_schedules_box)
            rows = [
                ([s.get("name"), f"{s.get('hours_per_week')}h", s.get("workdays"), str(s.get("employees"))], None, None)
                for s in schedules or []
            ]
            self.att_schedules_box.append(
                self._table_card(["Working hours", "Hours/week", "Workdays", "Employees assigned"], rows)
            )

        run_async(task, done)

    def refresh_attendance_month(self):
        employee_id = int(self.att_month_employee.get_active_id()) if self.att_month_employee.get_active_id() else None
        if not employee_id:
            clear_box(self.att_month_box)
            self.att_month_box.append(Gtk.Label(label="Pick an employee and month, then Load.", xalign=0))
            return
        year = int(self.att_month_year.get_active_id())
        month = int(self.att_month_month.get_active_id())

        def task():
            return FeatureRunner().fetch_attendance_month(employee_id, year, month)

        def done(rows, error):
            clear_box(self.att_month_box)
            if error:
                self.att_month_box.append(Gtk.Label(label=f"Could not load: {error}", xalign=0, wrap=True))
                return
            table_rows = []
            total = 0.0
            for entry in rows or []:
                hours = entry.get("worked_hours") or 0
                total += hours
                table_rows.append(([fmt_dt(entry.get("check_in")), fmt_dt(entry.get("check_out")), fmt_hours(hours)], "hr.attendance", entry.get("id")))
            self.att_month_box.append(Gtk.Label(label=f"Total this month: {fmt_hours(total)} across {len(rows or [])} days", xalign=0))
            self.att_month_box.append(self._table_card(["Check in", "Check out", "Hours"], table_rows))

        run_async(task, done)

    def _on_attendance_search_changed(self, _entry):
        if self._att_search_debounce_id is not None:
            GLib.source_remove(self._att_search_debounce_id)

        def fire():
            self._att_search_debounce_id = None
            self.refresh_attendance()
            return False

        self._att_search_debounce_id = GLib.timeout_add(300, fire)

    def load_attendance_departments(self):
        def task():
            return FeatureRunner().fetch_departments()

        def done(departments, error):
            if error:
                return
            self.att_department_combo.remove_all()
            self.att_department_combo.append("", "All departments")
            for dept in departments or []:
                self.att_department_combo.append(str(dept["id"]), dept["name"])
            self.att_department_combo.set_active(0)
            self._attendance_departments_loaded = True

        run_async(task, done)

    def refresh_attendance(self):
        if not self._attendance_departments_loaded:
            self.load_attendance_departments()
        department_id = int(self.att_department_combo.get_active_id()) if self.att_department_combo.get_active_id() else None
        search = self.att_search.get_text().strip() or None

        def task():
            return FeatureRunner().fetch_attendance_today(department_id=department_id, search=search)

        def done(rows, error):
            if error:
                self.set_status(f"Attendance failed: {error}")
                return
            self._attendance_rows = rows or []
            self.render_attendance()

        run_async(task, done)

    def render_attendance(self):
        clear_box(self.att_summary_box)
        clear_box(self.attendance_box)
        rows = self._attendance_rows

        segments = [(m2o_name(r, "employee_id"), r.get("hours") or 0) for r in rows]
        segments = sorted(segments, key=lambda kv: kv[1], reverse=True)
        self.att_summary_box.append(chart_card("Hours worked today by employee", segments, fmt_hours))

        table_rows = []
        for entry in rows:
            late = entry.get("late")
            status = ("Late", "error") if late else ("On time", "success") if late is False else ("-", None)
            cells = [
                m2o_name(entry, "employee_id"),
                fmt_dt(entry.get("check_in")),
                fmt_dt(entry.get("check_out")),
                fmt_hours(entry.get("hours") or 0),
                status,
            ]
            table_rows.append((cells, "hr.attendance", entry.get("id")))
        self.attendance_box.append(
            self._table_card(["Employee", "Check in", "Check out", "Hours", "Status"], table_rows, "attendances")
        )

    def _build_timer_page(self):
        self.timer_page = self.add_page("Timer", "alarm-symbolic")
        self.timer_status = Gtk.Label(label="", xalign=0)
        self.timer_page.append(self.timer_status)

        self.timer_running_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.timer_running_box.add_css_class("card")
        self.timer_running_box.set_margin_top(6)
        self.timer_running_box.set_margin_bottom(6)
        self.timer_page.append(self.timer_running_box)
        self.timer_task_label = Gtk.Label(label="", xalign=0, wrap=True)
        self.timer_task_label.add_css_class("title-4")
        self.timer_running_box.append(self.timer_task_label)
        self.timer_time_label = Gtk.Label(label="0:00:00", xalign=0)
        self.timer_time_label.add_css_class("title-1")
        self.timer_running_box.append(self.timer_time_label)
        self.timer_description = Gtk.Entry()
        self.timer_description.set_placeholder_text("What are you working on? (optional)")
        self.timer_running_box.append(self.timer_description)
        stop = Gtk.Button(label="Stop timer")
        stop.add_css_class("destructive-action")
        stop.add_css_class("pill")
        stop.connect("clicked", lambda _button: self.stop_timer())
        self.timer_running_box.append(stop)
        self.timer_running_label = self.timer_task_label  # backwards-compat alias

        self.timer_idle_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.timer_page.append(self.timer_idle_box)
        search_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.timer_search = Gtk.Entry()
        self.timer_search.set_placeholder_text("Filter tasks (auto-searches as you type)")
        self.timer_search.set_hexpand(True)
        self._timer_search_debounce_id = None
        self.timer_search.connect("changed", self._on_timer_search_changed)
        search_row.append(self.timer_search)
        load = Gtk.Button(label="Load tasks")
        load.connect("clicked", lambda _button: self.load_timer_tasks())
        search_row.append(load)
        self.timer_idle_box.append(search_row)
        self.timer_task_combo = Gtk.ComboBoxText()
        self.timer_idle_box.append(self.timer_task_combo)
        start = Gtk.Button(label="Start timer")
        start.connect("clicked", lambda _button: self.start_timer())
        self.timer_idle_box.append(start)

        self.timesheet_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.timer_page.append(section_title("Today's timesheet"))
        self.timer_page.append(self.timesheet_box)
        timesheet_refresh = Gtk.Button(label="Refresh timesheet")
        timesheet_refresh.connect("clicked", lambda _button: self.refresh_timesheet())
        self.timer_page.append(timesheet_refresh)

    def _build_timesheets_page(self):
        self.timesheets_page = self.add_page("Timesheets", "x-office-calendar-symbolic")
        self.timesheets_page.append(section_title("Team timesheets"))

        filters = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.ts_project_combo = Gtk.ComboBoxText()
        self.ts_project_combo.append("", "All projects")
        self.ts_project_combo.set_active(0)
        filters.append(self.ts_project_combo)
        self.ts_employee_combo = Gtk.ComboBoxText()
        self.ts_employee_combo.append("", "All employees")
        self.ts_employee_combo.set_active(0)
        filters.append(self.ts_employee_combo)
        self.timesheets_page.append(filters)

        date_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.ts_date_from = Gtk.Entry()
        self.ts_date_from.set_placeholder_text("From (YYYY-MM-DD)")
        date_row.append(self.ts_date_from)
        self.ts_date_to = Gtk.Entry()
        self.ts_date_to.set_placeholder_text("To (YYYY-MM-DD)")
        date_row.append(self.ts_date_to)
        today_btn = Gtk.Button(label="Today")
        today_btn.connect("clicked", lambda _b: self._timesheet_quick_filter("today"))
        date_row.append(today_btn)
        month_btn = Gtk.Button(label="This month")
        month_btn.connect("clicked", lambda _b: self._timesheet_quick_filter("month"))
        date_row.append(month_btn)
        self.timesheets_page.append(date_row)

        search_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.ts_search = Gtk.Entry()
        self.ts_search.set_placeholder_text("Search description/task/project (auto-searches as you type)")
        self.ts_search.set_hexpand(True)
        self._ts_search_debounce_id = None
        self.ts_search.connect("changed", self._on_timesheet_search_changed)
        search_row.append(self.ts_search)
        self.ts_group_by = Gtk.ComboBoxText()
        for value, label in (("", "No grouping"), ("project_id", "Group by project"), ("task_id", "Group by task"), ("employee_id", "Group by employee"), ("date", "Group by date")):
            self.ts_group_by.append(value, label)
        self.ts_group_by.set_active(0)
        self.ts_group_by.connect("changed", lambda _c: self.render_timesheet_entries())
        search_row.append(self.ts_group_by)
        refresh = Gtk.Button(label="Refresh")
        refresh.connect("clicked", lambda _b: self.refresh_timesheets())
        search_row.append(refresh)
        self.timesheets_page.append(search_row)

        self.ts_total_label = Gtk.Label(label="", xalign=0)
        self.timesheets_page.append(self.ts_total_label)

        self.timesheets_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.timesheets_box.add_css_class("card")
        self.timesheets_page.append(self.timesheets_box)

        self._timesheet_entries = []
        self._timesheet_filters_loaded = False

    def _timesheet_quick_filter(self, kind):
        today = datetime.now().date()
        if kind == "today":
            self.ts_date_from.set_text(today.isoformat())
            self.ts_date_to.set_text(today.isoformat())
        else:
            self.ts_date_from.set_text(today.replace(day=1).isoformat())
            self.ts_date_to.set_text(today.isoformat())
        self.refresh_timesheets()

    def _on_timesheet_search_changed(self, _entry):
        if self._ts_search_debounce_id is not None:
            GLib.source_remove(self._ts_search_debounce_id)

        def fire():
            self._ts_search_debounce_id = None
            self.render_timesheet_entries()
            return False

        self._ts_search_debounce_id = GLib.timeout_add(300, fire)

    def load_timesheet_filters(self):
        def task():
            return FeatureRunner().fetch_employees(), FeatureRunner().fetch_projects()

        def done(result, error):
            if error:
                self.set_status(f"Could not load timesheet filters: {error}")
                return
            employees, projects = result
            self.ts_employee_combo.remove_all()
            self.ts_employee_combo.append("", "All employees")
            for employee in employees or []:
                self.ts_employee_combo.append(str(employee["id"]), employee["name"])
            self.ts_employee_combo.set_active(0)
            self.ts_project_combo.remove_all()
            self.ts_project_combo.append("", "All projects")
            for project in projects or []:
                self.ts_project_combo.append(str(project["id"]), project["name"])
            self.ts_project_combo.set_active(0)
            self._timesheet_filters_loaded = True

        run_async(task, done)

    def refresh_timesheets(self):
        if not self._timesheet_filters_loaded:
            self.load_timesheet_filters()
        project_id = int(self.ts_project_combo.get_active_id()) if self.ts_project_combo.get_active_id() else None
        employee_id = int(self.ts_employee_combo.get_active_id()) if self.ts_employee_combo.get_active_id() else None
        date_from = self.ts_date_from.get_text().strip() or None
        date_to = self.ts_date_to.get_text().strip() or None
        self.set_status("Loading timesheets...")

        def task():
            return FeatureRunner().fetch_timesheet_entries(
                project_id=project_id, employee_id=employee_id, date_from=date_from, date_to=date_to
            )

        def done(rows, error):
            if error:
                self.set_status(f"Timesheets failed: {error}")
                return
            self._timesheet_entries = rows or []
            self.render_timesheet_entries()
            self.refresh_status()

        run_async(task, done)

    def _open_record(self, model, res_id):
        odoo_url = config_store.read().get("odoo_url")
        if odoo_url and model and res_id:
            open_target({"kind": "record", "model": model, "res_id": res_id, "odoo_url": odoo_url})

    def _table_card(self, columns, rows, open_in_odoo_path=None, title=None):
        """A properly column-aligned, sortable, clickable table.

        columns: list of header strings.
        rows: list of (cells, model, res_id); cells is a list whose entries are
        either a string or a (text, css_class) tuple. If model/res_id are set,
        the row is clickable and opens that record in Odoo.
        """
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        card.add_css_class("card")
        card.set_margin_top(6)
        card.set_margin_bottom(6)
        if title or open_in_odoo_path:
            top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            top.set_margin_start(6)
            top.set_margin_end(6)
            top.set_margin_top(4)
            if title:
                heading = section_title(title)
                heading.set_hexpand(True)
                top.append(heading)
            else:
                top.append(Gtk.Label(label="", hexpand=True))
            if open_in_odoo_path:
                link = Gtk.Button(label="Open in Odoo")
                link.add_css_class("flat")
                link.connect("clicked", lambda _b, p=open_in_odoo_path: self._open_odoo_app(p))
                top.append(link)
            card.append(top)

        # One horizontal SizeGroup per column keeps every cell in that column
        # the same width, so headers and rows line up exactly.
        size_groups = [Gtk.SizeGroup(mode=Gtk.SizeGroupMode.HORIZONTAL) for _ in columns]
        listbox = Gtk.ListBox()
        listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        listbox.add_css_class("rich-list")
        state = {"rows": list(rows), "sort_col": None, "asc": True}

        def cell_widget(text, css, is_header=False):
            label = Gtk.Label(label=str(text if text not in (None, "") else ("" if is_header else "-")), xalign=0)
            label.set_ellipsize(3)  # PANGO_ELLIPSIZE_END
            label.set_max_width_chars(34)
            if is_header:
                label.add_css_class("heading")
            elif css:
                label.add_css_class(css)
            return label

        def make_line(cells, header=False):
            line = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            line.set_margin_top(5)
            line.set_margin_bottom(5)
            line.set_margin_start(8)
            line.set_margin_end(8)
            for i in range(len(columns)):
                cell = cells[i] if i < len(cells) else ""
                text, css = (cell if isinstance(cell, tuple) else (cell, None))
                widget = cell_widget(text, css, is_header=header)
                size_groups[i].add_widget(widget)
                line.append(widget)
            return line

        def make_header():
            row = Gtk.ListBoxRow()
            row.set_activatable(False)
            row.set_selectable(False)
            line = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            line.set_margin_top(5)
            line.set_margin_bottom(5)
            line.set_margin_start(8)
            line.set_margin_end(8)
            for i, col in enumerate(columns):
                label = Gtk.Label(label=col, xalign=0)
                label.add_css_class("heading")
                indicator = " ▲" if (state["sort_col"] == i and state["asc"]) else " ▼" if state["sort_col"] == i else ""
                label.set_label(col + indicator)
                gesture = Gtk.GestureClick()
                gesture.connect("released", lambda *a, idx=i: sort_by(idx))
                label.add_controller(gesture)
                size_groups[i].add_widget(label)
                line.append(label)
            row.set_child(line)
            return row

        def rebuild():
            child = listbox.get_first_child()
            while child:
                nxt = child.get_next_sibling()
                listbox.remove(child)
                child = nxt
            listbox.append(make_header())
            data = list(state["rows"])
            if state["sort_col"] is not None:
                ci = state["sort_col"]

                def sort_key(item):
                    cells = item[0]
                    cell = cells[ci] if ci < len(cells) else ""
                    text = cell[0] if isinstance(cell, tuple) else cell
                    return _sort_value(text)

                data.sort(key=sort_key, reverse=not state["asc"])
            if not data:
                empty = Gtk.ListBoxRow()
                empty.set_activatable(False)
                empty.set_selectable(False)
                empty.set_child(Gtk.Label(label="No records.", xalign=0, margin_top=6, margin_bottom=6, margin_start=8))
                listbox.append(empty)
            for cells, model, res_id in data:
                row = Gtk.ListBoxRow()
                row.set_activatable(bool(model and res_id))
                row._target = (model, res_id)
                row.set_child(make_line(cells))
                listbox.append(row)

        def sort_by(i):
            if state["sort_col"] == i:
                state["asc"] = not state["asc"]
            else:
                state["sort_col"] = i
                state["asc"] = True
            rebuild()

        def on_activate(_lb, row):
            target = getattr(row, "_target", None)
            if target and target[0] and target[1]:
                self._open_record(target[0], target[1])

        listbox.connect("row-activated", on_activate)
        rebuild()
        card.append(listbox)
        return card

    def _year_combo(self, on_change):
        combo = Gtk.ComboBoxText()
        current = datetime.now().year
        for year in range(current, current - 6, -1):
            combo.append(str(year), str(year))
        combo.set_active_id(str(current))
        combo.connect("changed", lambda _c: on_change())
        return combo

    def _month_combo(self, on_change):
        combo = Gtk.ComboBoxText()
        combo.append("", "All months")
        for i, name in enumerate(
            ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"], 1
        ):
            combo.append(str(i), name)
        combo.set_active(0)
        combo.connect("changed", lambda _c: on_change())
        return combo

    def _choice_combo(self, choices, on_change):
        combo = Gtk.ComboBoxText()
        for value, label in choices:
            combo.append(value, label)
        combo.set_active(0)
        combo.connect("changed", lambda _c: on_change())
        return combo

    def _search_entry(self, placeholder, on_change):
        entry = Gtk.Entry()
        entry.set_placeholder_text(placeholder)
        entry.set_hexpand(True)
        entry._debounce_id = None

        def changed(_e):
            if entry._debounce_id is not None:
                GLib.source_remove(entry._debounce_id)

            def fire():
                entry._debounce_id = None
                on_change()
                return False

            entry._debounce_id = GLib.timeout_add(350, fire)

        entry.connect("changed", changed)
        return entry

    def _combo_int(self, combo):
        value = combo.get_active_id()
        return int(value) if value else None

    def _populate_combo(self, combo, pairs, all_label):
        """Repopulate a filter combo from data while preserving the selection.
        pairs: iterable of (id, name)."""
        current = combo.get_active_id()
        self._suppress_filters = True
        combo.remove_all()
        combo.append("", all_label)
        for id_, name in sorted({str(i): n for i, n in pairs}.items(), key=lambda kv: kv[1]):
            combo.append(id_, name)
        if not (current and combo.set_active_id(current)):
            combo.set_active(0)
        self._suppress_filters = False

    # ---- Generic, spec-driven module pages (charts + filters + clickable tables) ----

    def _build_module_page(self, spec):
        key = spec["key"]
        state = {"spec": spec, "rows": [], "widgets": {}}
        self._pages[key] = state
        box = self.add_page(spec["title"], spec.get("icon"))

        filter_row = Gtk.FlowBox()
        filter_row.set_selection_mode(Gtk.SelectionMode.NONE)
        filter_row.set_max_children_per_line(20)
        filter_row.set_column_spacing(8)
        filter_row.set_row_spacing(6)
        filter_row.set_halign(Gtk.Align.START)
        widgets = state["widgets"]
        for spec_filter in spec.get("filters", []):
            kind = spec_filter["kind"]
            if kind == "year":
                widget = self._year_combo(lambda k=key: self._module_refresh(k))
            elif kind == "month":
                widget = self._month_combo(lambda k=key: self._module_refresh(k))
            elif kind == "search":
                widget = self._search_entry(spec_filter.get("placeholder", "Search..."), lambda k=key: self._module_refresh(k))
                widget.set_size_request(280, -1)
            elif kind == "choice":
                widget = self._choice_combo(spec_filter["choices"], lambda k=key: self._module_render(k))
            elif kind == "m2o":
                widget = Gtk.ComboBoxText()
                widget.append("", spec_filter["all_label"])
                widget.set_active(0)
                widget.connect("changed", lambda _c, k=key: None if self._suppress_filters else self._module_render(k))
            else:
                continue
            widgets[spec_filter["id"]] = widget
            filter_row.append(widget)
        refresh = Gtk.Button(label="Refresh")
        refresh.connect("clicked", lambda _b, k=key: self._module_refresh(k))
        filter_row.append(refresh)
        box.append(filter_row)

        state["summary_label"] = Gtk.Label(label="", xalign=0)
        state["summary_label"].add_css_class("dim-label")
        box.append(state["summary_label"])

        state["charts_box"] = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.append(state["charts_box"])
        state["table_box"] = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.append(state["table_box"])

    def _module_refresh(self, key):
        state = self._pages[key]
        spec = state["spec"]
        widgets = state["widgets"]
        params = {"year": None, "month": None, "search": None}
        for spec_filter in spec.get("filters", []):
            widget = widgets[spec_filter["id"]]
            if spec_filter["kind"] == "year":
                params["year"] = self._combo_int(widget)
            elif spec_filter["kind"] == "month":
                params["month"] = self._combo_int(widget)
            elif spec_filter["kind"] == "search":
                params["search"] = widget.get_text().strip() or None
        self.set_status(f"Loading {spec['title']}...")

        def task():
            return spec["fetch"](params)

        def done(rows, error):
            if error:
                self.set_status(f"{spec['title']} failed: {error}")
                clear_box(state["table_box"])
                state["table_box"].append(Gtk.Label(label=str(error), xalign=0, wrap=True))
                return
            state["rows"] = rows or []
            for spec_filter in spec.get("filters", []):
                if spec_filter["kind"] == "m2o":
                    field = spec_filter["field"]
                    pairs = [(m2o_id(r, field), m2o_name(r, field)) for r in state["rows"] if m2o_id(r, field)]
                    self._populate_combo(widgets[spec_filter["id"]], pairs, spec_filter["all_label"])
            self._module_render(key)
            self.refresh_status()

        run_async(task, done)

    def _module_render(self, key):
        state = self._pages[key]
        spec = state["spec"]
        widgets = state["widgets"]
        rows = list(state["rows"])
        for spec_filter in spec.get("filters", []):
            widget = widgets[spec_filter["id"]]
            if spec_filter["kind"] == "choice":
                value = widget.get_active_id()
                if value:
                    rows = [r for r in rows if spec_filter["match"](r, value)]
            elif spec_filter["kind"] == "m2o":
                value = widget.get_active_id()
                if value:
                    rows = [r for r in rows if str(m2o_id(r, spec_filter["field"])) == value]

        clear_box(state["charts_box"])
        for chart in spec.get("charts", []):
            segments = aggregate(rows, chart["key"], chart.get("value"))
            segments = sorted(segments, key=lambda kv: kv[1], reverse=True)[: chart.get("limit", 12)]
            state["charts_box"].append(
                chart_card(chart["title"], segments, chart.get("fmt"), chart.get("kind", "donut"), chart.get("note"))
            )

        summary = spec.get("summary")
        state["summary_label"].set_label(summary(rows) if summary else f"{len(rows)} records")

        clear_box(state["table_box"])
        table_rows = [spec["row"](r) for r in rows[:400]]
        state["table_box"].append(self._table_card(spec["columns"], table_rows, spec.get("odoo_path")))

    @staticmethod
    def _inventory_match(row, value):
        state = row.get("state")
        if value == "open":
            return state in ("assigned", "confirmed", "waiting")
        return state == value

    @staticmethod
    def _accounting_match(row, value):
        payment = row.get("payment_state")
        if value == "overdue":
            due = row.get("invoice_date_due") or "9999-99-99"
            return payment in ("not_paid", "partial") and due < datetime.now().date().isoformat()
        return payment == value

    @staticmethod
    def _activity_status(row):
        deadline = (row.get("date_deadline") or "")[:10]
        if not deadline:
            return ("-", None)
        today = datetime.now().date().isoformat()
        if deadline < today:
            return ("Overdue", "error")
        if deadline == today:
            return ("Today", "warning")
        return ("Planned", "success")

    def _module_page_specs(self):
        return [
            {
                "key": "time-off", "title": "Time Off", "icon": "weather-clear-night-symbolic",
                "fetch": lambda p: FeatureRunner().fetch_timeoff(year=p["year"], month=p["month"], search=p["search"]),
                "filters": [
                    {"id": "year", "kind": "year"},
                    {"id": "month", "kind": "month"},
                    {"id": "type", "kind": "m2o", "field": "holiday_status_id", "all_label": "All types"},
                    {"id": "employee", "kind": "m2o", "field": "employee_id", "all_label": "All employees"},
                    {"id": "search", "kind": "search", "placeholder": "Search employee..."},
                ],
                "charts": [
                    {"title": "Time Off by Type", "key": lambda r: m2o_name(r, "holiday_status_id"), "value": lambda r: r.get("number_of_days") or 0, "fmt": lambda v: f"{v:.2f}d"},
                    {"title": "Time Off by Employee", "key": lambda r: m2o_name(r, "employee_id"), "value": lambda r: r.get("number_of_days") or 0, "fmt": lambda v: f"{v:.2f}d"},
                ],
                "columns": ["Employee", "Type", "From", "To", "Days", "Status"],
                "row": lambda r: ([m2o_name(r, "employee_id"), m2o_name(r, "holiday_status_id"), fmt_date(r.get("request_date_from")), fmt_date(r.get("request_date_to")), f"{r.get('number_of_days') or 0:g}", _status_cell(LEAVE_STATE.get(r.get("state"), r.get("state")), "success" if r.get("state") == "validate" else "error" if r.get("state") == "refuse" else None)], "hr.leave", r.get("id")),
                "summary": lambda rows: f"{len(rows)} requests · {sum(r.get('number_of_days') or 0 for r in rows):.2f} days total",
                "odoo_path": "time-off",
            },
            {
                "key": "activities", "title": "Activities", "icon": "task-due-symbolic",
                "fetch": lambda p: FeatureRunner().fetch_activities(search=p["search"]),
                "filters": [
                    {"id": "user", "kind": "m2o", "field": "user_id", "all_label": "All employees"},
                    {"id": "type", "kind": "m2o", "field": "activity_type_id", "all_label": "All activities"},
                    {"id": "search", "kind": "search", "placeholder": "Search related document..."},
                ],
                "charts": [
                    {"title": "Activities by Type", "key": lambda r: m2o_name(r, "activity_type_id")},
                    {"title": "Activities by Assignee", "key": lambda r: m2o_name(r, "user_id")},
                ],
                "columns": ["Related to", "Assigned to", "Activity", "Due", "Status"],
                "row": lambda r: ([r.get("res_name") or r.get("summary") or "-", m2o_name(r, "user_id"), m2o_name(r, "activity_type_id"), fmt_date(r.get("date_deadline")), self._activity_status(r)], r.get("res_model"), r.get("res_id")),
                "summary": lambda rows: f"{len(rows)} activities",
            },
            {
                "key": "sales", "title": "Sales", "icon": "emblem-shared-symbolic",
                "fetch": lambda p: FeatureRunner().fetch_sales(year=p["year"], month=p["month"], search=p["search"]),
                "filters": [
                    {"id": "year", "kind": "year"},
                    {"id": "month", "kind": "month"},
                    {"id": "status", "kind": "choice", "choices": [("", "All statuses"), ("draft", "Quotation"), ("sent", "Quotation Sent"), ("sale", "Sales Order")], "match": lambda r, v: r.get("state") == v},
                    {"id": "user", "kind": "m2o", "field": "user_id", "all_label": "All salespeople"},
                    {"id": "search", "kind": "search", "placeholder": "Search customer or order..."},
                ],
                "charts": [
                    {"title": "Orders by Status", "kind": "bar", "key": lambda r: SALE_STATE.get(r.get("state"), r.get("state"))},
                    {"title": "Sales by Salesperson", "key": lambda r: m2o_name(r, "user_id")},
                ],
                "columns": ["Order", "Customer", "Salesperson", "Total", "Status", "Date"],
                "row": lambda r: ([r.get("name"), m2o_name(r, "partner_id"), m2o_name(r, "user_id"), fmt_money(r.get("amount_total")), SALE_STATE.get(r.get("state"), r.get("state")), fmt_date(r.get("date_order"))], "sale.order", r.get("id")),
                "summary": lambda rows: f"{len(rows)} orders · total {fmt_money(sum(r.get('amount_total') or 0 for r in rows))}",
                "odoo_path": "sales",
            },
            {
                "key": "crm", "title": "CRM", "icon": "system-users-symbolic",
                "fetch": lambda p: FeatureRunner().fetch_crm(year=p["year"], month=p["month"], search=p["search"]),
                "filters": [
                    {"id": "year", "kind": "year"},
                    {"id": "month", "kind": "month"},
                    {"id": "type", "kind": "choice", "choices": [("", "All types"), ("lead", "Lead"), ("opportunity", "Opportunity")], "match": lambda r, v: r.get("type") == v},
                    {"id": "user", "kind": "m2o", "field": "user_id", "all_label": "All salespeople"},
                    {"id": "search", "kind": "search", "placeholder": "Search name or customer..."},
                ],
                "charts": [
                    {"title": "Leads by Stage", "key": lambda r: m2o_name(r, "stage_id")},
                    {"title": "Leads by Salesperson", "key": lambda r: m2o_name(r, "user_id", "(unassigned)"), "note": "Note: leads with no salesperson are grouped as (unassigned)."},
                ],
                "columns": ["Name", "Customer", "Salesperson", "Stage", "Expected", "Type"],
                "row": lambda r: ([r.get("name"), r.get("contact_name") or m2o_name(r, "partner_id", "-"), m2o_name(r, "user_id", "-"), m2o_name(r, "stage_id"), fmt_money(r.get("expected_revenue")), (r.get("type") or "").title()], "crm.lead", r.get("id")),
                "summary": lambda rows: f"{len(rows)} leads/opportunities · expected {fmt_money(sum(r.get('expected_revenue') or 0 for r in rows))}",
                "odoo_path": "crm",
            },
            {
                "key": "project", "title": "Project", "icon": "view-list-bullet-symbolic",
                "fetch": lambda p: FeatureRunner().fetch_project_tasks(search=p["search"]),
                "filters": [
                    {"id": "project", "kind": "m2o", "field": "project_id", "all_label": "All projects"},
                    {"id": "stage", "kind": "m2o", "field": "stage_id", "all_label": "All stages"},
                    {"id": "search", "kind": "search", "placeholder": "Search task..."},
                ],
                "charts": [
                    {"title": "Tasks by Stage", "key": lambda r: m2o_name(r, "stage_id")},
                    {"title": "Tasks by Project", "key": lambda r: m2o_name(r, "project_id")},
                ],
                "columns": ["Task", "Project", "Stage", "Deadline"],
                "row": lambda r: ([r.get("name"), m2o_name(r, "project_id"), m2o_name(r, "stage_id"), fmt_date(r.get("date_deadline"))], "project.task", r.get("id")),
                "summary": lambda rows: f"{len(rows)} tasks",
                "odoo_path": "project",
            },
            {
                "key": "purchase", "title": "Purchase", "icon": "emblem-documents-symbolic",
                "fetch": lambda p: FeatureRunner().fetch_purchase(year=p["year"], month=p["month"], search=p["search"]),
                "filters": [
                    {"id": "year", "kind": "year"},
                    {"id": "month", "kind": "month"},
                    {"id": "status", "kind": "choice", "choices": [("", "All statuses"), ("draft", "RFQ"), ("sent", "RFQ Sent"), ("to approve", "To Approve"), ("purchase", "Purchase Order")], "match": lambda r, v: r.get("state") == v},
                    {"id": "search", "kind": "search", "placeholder": "Search vendor or reference..."},
                ],
                "charts": [
                    {"title": "Orders by Status", "kind": "bar", "key": lambda r: PURCHASE_STATE.get(r.get("state"), r.get("state"))},
                ],
                "columns": ["Reference", "Vendor", "Buyer", "Order date", "Receipt", "Total", "Status"],
                "row": lambda r: ([r.get("name"), m2o_name(r, "partner_id"), m2o_name(r, "user_id"), fmt_date(r.get("date_order")), fmt_date(r.get("date_planned")), fmt_money(r.get("amount_total")), PURCHASE_STATE.get(r.get("state"), r.get("state"))], "purchase.order", r.get("id")),
                "summary": lambda rows: f"{len(rows)} orders · total {fmt_money(sum(r.get('amount_total') or 0 for r in rows))}",
                "odoo_path": "purchase",
            },
            {
                "key": "inventory", "title": "Inventory", "icon": "network-server-symbolic",
                "fetch": lambda p: FeatureRunner().fetch_inventory(year=p["year"], month=p["month"], open_only=False, search=p["search"]),
                "filters": [
                    {"id": "year", "kind": "year"},
                    {"id": "month", "kind": "month"},
                    {"id": "status", "kind": "choice", "choices": [("", "All"), ("open", "Open only"), ("assigned", "Ready"), ("done", "Done")], "match": self._inventory_match},
                    {"id": "search", "kind": "search", "placeholder": "Search partner or reference..."},
                ],
                "charts": [
                    {"title": "Transfers by Status", "key": lambda r: PICKING_STATE.get(r.get("state"), r.get("state"))},
                    {"title": "Transfers by Responsible", "key": lambda r: m2o_name(r, "user_id", "Unassigned")},
                ],
                "columns": ["Reference", "Operation", "Partner", "Scheduled", "Status"],
                "row": lambda r: ([r.get("name"), m2o_name(r, "picking_type_id"), m2o_name(r, "partner_id"), fmt_dt(r.get("scheduled_date")), PICKING_STATE.get(r.get("state"), r.get("state"))], "stock.picking", r.get("id")),
                "summary": lambda rows: f"{len(rows)} transfers",
                "odoo_path": "inventory",
            },
            {
                "key": "accounting", "title": "Accounting", "icon": "accessories-calculator-symbolic",
                "fetch": lambda p: FeatureRunner().fetch_invoices(year=p["year"], month=p["month"], overdue_only=False, search=p["search"]),
                "filters": [
                    {"id": "year", "kind": "year"},
                    {"id": "month", "kind": "month"},
                    {"id": "status", "kind": "choice", "choices": [("", "All invoices"), ("overdue", "Overdue only"), ("not_paid", "Not Paid"), ("paid", "Paid")], "match": self._accounting_match},
                    {"id": "search", "kind": "search", "placeholder": "Search customer or number..."},
                ],
                "charts": [
                    {"title": "Payment Status", "key": lambda r: PAYMENT_STATE.get(r.get("payment_state"), r.get("payment_state"))},
                    {"title": "Invoiced by Salesperson", "key": lambda r: m2o_name(r, "invoice_user_id", "-")},
                ],
                "columns": ["Invoice", "Customer", "Date", "Due", "Total", "Residual", "Payment"],
                "row": lambda r: ([r.get("name"), m2o_name(r, "partner_id"), fmt_date(r.get("invoice_date")), fmt_date(r.get("invoice_date_due")), fmt_money(r.get("amount_total")), fmt_money(r.get("amount_residual")), PAYMENT_STATE.get(r.get("payment_state"), r.get("payment_state"))], "account.move", r.get("id")),
                "summary": lambda rows: f"{len(rows)} invoices · total {fmt_money(sum(r.get('amount_total') or 0 for r in rows))} · residual {fmt_money(sum(r.get('amount_residual') or 0 for r in rows))}",
                "odoo_path": "accounting",
            },
            {
                "key": "expenses", "title": "Expenses", "icon": "emblem-money-symbolic",
                "fetch": lambda p: FeatureRunner().fetch_expenses(year=p["year"], month=p["month"], search=p["search"]),
                "filters": [
                    {"id": "year", "kind": "year"},
                    {"id": "month", "kind": "month"},
                    {"id": "employee", "kind": "m2o", "field": "employee_id", "all_label": "All employees"},
                    {"id": "status", "kind": "choice", "choices": [("", "All statuses"), ("draft", "To Report"), ("reported", "To Submit"), ("submit", "Submitted"), ("approve", "Approved"), ("post", "Posted"), ("refused", "Refused")], "match": lambda r, v: r.get("state") == v},
                    {"id": "search", "kind": "search", "placeholder": "Search expense or employee..."},
                ],
                "charts": [
                    {"title": "Expenses by Status", "key": lambda r: EXPENSE_STATE.get(r.get("state"), r.get("state")), "value": lambda r: r.get("total_amount_currency") or 0, "fmt": fmt_money},
                ],
                "columns": ["Expense", "Employee", "Amount", "Status", "Date"],
                "row": lambda r: ([r.get("name"), m2o_name(r, "employee_id"), fmt_money(r.get("total_amount_currency")), EXPENSE_STATE.get(r.get("state"), r.get("state")), fmt_date(r.get("date"))], "hr.expense", r.get("id")),
                "summary": lambda rows: f"{len(rows)} expenses · total {fmt_money(sum(r.get('total_amount_currency') or 0 for r in rows))}",
                "odoo_path": "expenses",
            },
            {
                "key": "recruitment", "title": "Recruitment", "icon": "avatar-default-symbolic",
                "fetch": lambda p: FeatureRunner().fetch_recruitment(year=p["year"], month=p["month"], search=p["search"]),
                "filters": [
                    {"id": "year", "kind": "year"},
                    {"id": "month", "kind": "month"},
                    {"id": "job", "kind": "m2o", "field": "job_id", "all_label": "All positions"},
                    {"id": "stage", "kind": "m2o", "field": "stage_id", "all_label": "All stages"},
                    {"id": "search", "kind": "search", "placeholder": "Search applicant..."},
                ],
                "charts": [
                    {"title": "Applicants by Stage", "key": lambda r: m2o_name(r, "stage_id")},
                    {"title": "Applicants by Position", "key": lambda r: m2o_name(r, "job_id")},
                ],
                "columns": ["Applicant", "Position", "Stage", "Applied"],
                "row": lambda r: ([r.get("partner_name") or "-", m2o_name(r, "job_id"), m2o_name(r, "stage_id"), fmt_date(r.get("create_date"))], "hr.applicant", r.get("id")),
                "summary": lambda rows: f"{len(rows)} applicants",
                "odoo_path": "recruitment",
            },
            {
                "key": "helpdesk", "title": "Helpdesk", "icon": "help-browser-symbolic",
                "fetch": lambda p: FeatureRunner().fetch_helpdesk(year=p["year"], month=p["month"], search=p["search"]),
                "filters": [
                    {"id": "year", "kind": "year"},
                    {"id": "month", "kind": "month"},
                    {"id": "team", "kind": "m2o", "field": "team_id", "all_label": "All teams"},
                    {"id": "stage", "kind": "m2o", "field": "stage_id", "all_label": "All stages"},
                    {"id": "user", "kind": "m2o", "field": "user_id", "all_label": "All assignees"},
                    {"id": "search", "kind": "search", "placeholder": "Search ticket or customer..."},
                ],
                "charts": [
                    {"title": "Tickets by Stage", "key": lambda r: m2o_name(r, "stage_id")},
                    {"title": "Tickets by Assignee", "key": lambda r: m2o_name(r, "user_id", "(unassigned)")},
                ],
                "columns": ["Ticket", "Customer", "Assigned", "Team", "Stage", "Created"],
                "row": lambda r: ([r.get("name"), m2o_name(r, "partner_id", "-"), m2o_name(r, "user_id", "-"), m2o_name(r, "team_id", "-"), m2o_name(r, "stage_id"), fmt_date(r.get("create_date"))], "helpdesk.ticket", r.get("id")),
                "summary": lambda rows: f"{len(rows)} tickets",
                "odoo_path": "helpdesk",
            },
            {
                "key": "point-of-sale", "title": "Point of Sale", "icon": "emblem-money-symbolic",
                "fetch": lambda p: FeatureRunner().fetch_pos(year=p["year"], month=p["month"], search=p["search"]),
                "filters": [
                    {"id": "year", "kind": "year"},
                    {"id": "month", "kind": "month"},
                    {"id": "status", "kind": "choice", "choices": [("", "All statuses"), ("draft", "New"), ("paid", "Paid"), ("done", "Posted"), ("invoiced", "Invoiced")], "match": lambda r, v: r.get("state") == v},
                    {"id": "user", "kind": "m2o", "field": "user_id", "all_label": "All cashiers"},
                    {"id": "search", "kind": "search", "placeholder": "Search order or customer..."},
                ],
                "charts": [
                    {"title": "Orders by Status", "kind": "bar", "key": lambda r: POS_STATE.get(r.get("state"), r.get("state"))},
                    {"title": "Sales by Cashier", "key": lambda r: m2o_name(r, "user_id"), "value": lambda r: r.get("amount_total") or 0, "fmt": fmt_money},
                ],
                "columns": ["Order", "Customer", "Cashier", "Total", "Status", "Date"],
                "row": lambda r: ([r.get("name"), m2o_name(r, "partner_id", "-"), m2o_name(r, "user_id", "-"), fmt_money(r.get("amount_total")), POS_STATE.get(r.get("state"), r.get("state")), fmt_date(r.get("date_order"))], "pos.order", r.get("id")),
                "summary": lambda rows: f"{len(rows)} orders · total {fmt_money(sum(r.get('amount_total') or 0 for r in rows))}",
                "odoo_path": "point-of-sale",
            },
        ]

    def render_timesheet_entries(self):
        clear_box(self.timesheets_box)
        search = self.ts_search.get_text().strip().lower()
        rows = self._timesheet_entries
        if search:
            def matches(row):
                haystacks = [row.get("name") or "", (row.get("task_id") or [None, ""])[1], (row.get("project_id") or [None, ""])[1]]
                return any(search in (text or "").lower() for text in haystacks)

            rows = [row for row in rows if matches(row)]

        group_by = self.ts_group_by.get_active_id()
        total_hours = sum(row.get("unit_amount") or 0 for row in rows)
        self.ts_total_label.set_label(f"Total: {fmt_hours(total_hours)} across {len(rows)} entries")

        if group_by:
            groups = {}
            order = []
            for row in rows:
                key, label, link = self._timesheet_group_key(row, group_by)
                if key not in groups:
                    groups[key] = {"label": label, "link": link, "count": 0, "hours": 0.0}
                    order.append(key)
                groups[key]["count"] += 1
                groups[key]["hours"] += row.get("unit_amount") or 0

            header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            for text in (MODULE_LABELS.get(group_by, group_by), "Entries", "Hours"):
                label = Gtk.Label(label=text, xalign=0, hexpand=True)
                label.add_css_class("heading")
                header.append(label)
            self.timesheets_box.append(header)

            for key in sorted(order, key=lambda k: groups[k]["hours"], reverse=True):
                info = groups[key]
                row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
                if info["link"]:
                    button = Gtk.LinkButton.new_with_label("internal:record", info["label"])
                    button.connect("activate-link", lambda _b, link=info["link"]: (self._open_record(*link), True)[1])
                    button.set_hexpand(True)
                    row.append(button)
                else:
                    row.append(Gtk.Label(label=info["label"], xalign=0, hexpand=True))
                row.append(Gtk.Label(label=str(info["count"]), xalign=0, hexpand=True))
                row.append(Gtk.Label(label=fmt_hours(info["hours"]), xalign=0, hexpand=True))
                self.timesheets_box.append(row)
            return

        table_rows = []
        for row in rows[:400]:
            task_id = m2o_id(row, "task_id")
            cells = [
                fmt_date(row.get("date")),
                m2o_name(row, "employee_id"),
                m2o_name(row, "project_id"),
                m2o_name(row, "task_id"),
                row.get("name") or "",
                fmt_hours(row.get("unit_amount") or 0),
            ]
            if task_id:
                table_rows.append((cells, "project.task", task_id))
            else:
                table_rows.append((cells, "account.analytic.line", row.get("id")))
        self.timesheets_box.append(
            self._table_card(["Date", "Employee", "Project", "Task", "Description", "Hours"], table_rows)
        )

    def _timesheet_group_key(self, row, group_by):
        if group_by == "date":
            value = row.get("date")
            return value, fmt_date(value), None
        m2o = row.get(group_by)
        if not m2o:
            return False, "(none)", None
        model = {"project_id": "project.project", "task_id": "project.task"}.get(group_by)
        link = (model, m2o[0]) if model else None
        return m2o[0], m2o[1], link

    def _build_notifications_page(self):
        self.notifications_page = self.add_page("Notifications", "preferences-system-notifications-symbolic")
        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.notifications_page.append(controls)
        refresh = Gtk.Button(label="Refresh")
        refresh.connect("clicked", lambda _button: self.refresh_notifications())
        controls.append(refresh)
        clear = Gtk.Button(label="Clear history")
        clear.connect("clicked", lambda _button: self.clear_notifications())
        controls.append(clear)
        self.notifications_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.notifications_page.append(self.notifications_box)

    def _build_settings_page(self):
        self.settings_page = self.add_page("Settings", "preferences-system-symbolic")

        self.settings_page.append(section_title("Connection"))
        self.url_entry = Gtk.Entry()
        self.url_entry.set_placeholder_text("https://mycompany.odoo.com")
        self.settings_page.append(row_box("Odoo base URL", self.url_entry))

        db_line = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.db_entry = Gtk.Entry()
        self.db_entry.set_hexpand(True)
        self.db_entry.set_placeholder_text("Database name")
        db_line.append(self.db_entry)
        self.db_combo = Gtk.ComboBoxText()
        self.db_combo.set_visible(False)
        self.db_combo.connect("changed", self._db_selected)
        db_line.append(self.db_combo)
        detect = Gtk.Button(label="Detect")
        detect.connect("clicked", lambda _button: self.detect_databases())
        db_line.append(detect)
        self.settings_page.append(row_box("Database", db_line))

        self.login_entry = Gtk.Entry()
        self.login_entry.set_placeholder_text("you@company.com")
        self.settings_page.append(row_box("Username/email", self.login_entry))

        self.password_entry = Gtk.PasswordEntry()
        self.password_entry.set_property("placeholder-text", "Password or API key")
        self.settings_page.append(row_box("Password/API key", self.password_entry))

        self.poll_spin = Gtk.SpinButton()
        self.poll_spin.set_adjustment(Gtk.Adjustment(value=1, lower=0.5, upper=60, step_increment=0.5))
        self.poll_spin.set_digits(1)
        self.settings_page.append(row_box("Dashboard data poll every (minutes)", self.poll_spin))

        self.notification_poll_spin = Gtk.SpinButton()
        self.notification_poll_spin.set_adjustment(
            Gtk.Adjustment(value=DEFAULT_NOTIFICATION_POLL_SECONDS, lower=MIN_NOTIFICATION_POLL_SECONDS, upper=120, step_increment=1)
        )
        self.notification_poll_spin.set_digits(0)
        self.settings_page.append(row_box("Check for chats/calls/mentions every (seconds)", self.notification_poll_spin))

        self.timer_reminder_spin = Gtk.SpinButton()
        self.timer_reminder_spin.set_adjustment(
            Gtk.Adjustment(value=DEFAULT_TIMER_REMINDER_MINUTES, lower=1, upper=120, step_increment=1)
        )
        self.timer_reminder_spin.set_digits(0)
        self.settings_page.append(row_box("Remind me about a running task timer every (minutes)", self.timer_reminder_spin))

        self.attendance_grace_spin = Gtk.SpinButton()
        self.attendance_grace_spin.set_adjustment(
            Gtk.Adjustment(value=DEFAULT_ATTENDANCE_GRACE_MINUTES, lower=0, upper=240, step_increment=5)
        )
        self.attendance_grace_spin.set_digits(0)
        self.settings_page.append(row_box("Warn if not checked in, grace after start time (minutes)", self.attendance_grace_spin))

        self.settings_page.append(section_title("Desktop integration"))
        self.autostart_check = Gtk.CheckButton(label="Start background service automatically after login")
        self.settings_page.append(self.autostart_check)
        desktop_actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        desktop_icon = Gtk.Button(label="Create desktop icon")
        desktop_icon.connect("clicked", lambda _button: self.create_desktop_icon())
        restart_service = Gtk.Button(label="Restart background service")
        restart_service.connect("clicked", lambda _button: self.restart_service())
        desktop_actions.append(desktop_icon)
        desktop_actions.append(restart_service)
        self.settings_page.append(desktop_actions)

        self.settings_page.append(section_title("Mute notification categories"))
        self.mute_checks = {}
        for key, label in MUTE_LABELS.items():
            check = Gtk.CheckButton(label=label)
            self.mute_checks[key] = check
            self.settings_page.append(check)

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        test = Gtk.Button(label="Test login")
        test.connect("clicked", lambda _button: self.test_login())
        save = Gtk.Button(label="Save settings")
        save.add_css_class("suggested-action")
        save.connect("clicked", lambda _button: self.save_settings())
        modules = Gtk.Button(label="Check modules")
        modules.connect("clicked", lambda _button: self.check_modules())
        actions.append(test)
        actions.append(save)
        actions.append(modules)
        self.settings_page.append(actions)

        self.settings_message = Gtk.Label(label="", xalign=0)
        self.settings_message.set_wrap(True)
        self.settings_page.append(self.settings_message)

        self.module_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.settings_page.append(self.module_box)

        self.settings_page.append(section_title("About"))
        about_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        about_box.append(Gtk.Label(label=f"{APP_NAME} v{APP_VERSION}", xalign=0))
        about_box.append(Gtk.Label(label="Developed by Rafiur Rahman Rafit", xalign=0))
        about_box.append(self._link_label("rafiurrahmanrafit.com", "https://rafiurrahmanrafit.com"))
        about_box.append(Gtk.Label(label="for DotBD Solutions Limited", xalign=0))
        about_box.append(self._link_label("www.dotbdsolutions.com", "https://www.dotbdsolutions.com"))
        self.settings_page.append(about_box)

    def _link_label(self, text, url):
        label = Gtk.LinkButton.new_with_label(url, text)
        label.set_halign(Gtk.Align.START)
        return label

    def reload_settings(self):
        config = config_store.read()
        self.url_entry.set_text(config.get("odoo_url") or "")
        self.db_entry.set_text(config.get("db") or "")
        self.login_entry.set_text(config.get("login") or "")
        self.poll_spin.set_value(float(config.get("poll_minutes") or 1))
        self.notification_poll_spin.set_value(float(config.get("notification_poll_seconds") or DEFAULT_NOTIFICATION_POLL_SECONDS))
        self.timer_reminder_spin.set_value(float(config.get("timer_reminder_minutes") or DEFAULT_TIMER_REMINDER_MINUTES))
        self.attendance_grace_spin.set_value(float(config.get("attendance_grace_minutes") if config.get("attendance_grace_minutes") is not None else DEFAULT_ATTENDANCE_GRACE_MINUTES))
        self.autostart_check.set_active(bool(config.get("autostart_enabled", True)))
        for key, check in self.mute_checks.items():
            check.set_active(bool((config.get("mute") or {}).get(key)))
        secret = lookup_secret(config.get("login"))
        if secret:
            self.password_entry.set_text(secret)

    def refresh_status(self):
        config = config_store.read()
        state = state_store.read()
        url = config.get("odoo_url") or "No Odoo URL configured"
        server = state.get("server_status") or "not checked"
        last_poll = state.get("last_poll_at")
        last_text = datetime.fromtimestamp(last_poll / 1000).strftime("%Y-%m-%d %H:%M:%S") if last_poll else "never"
        error = state.get("last_error")
        text = f"Watching: {url} | Server: {server} | Last poll: {last_text}"
        if error:
            text += f" | Last error: {error}"
        self.set_status(text)

    def poll_now(self):
        self.set_status("Polling Odoo now...")

        def task():
            runner = FeatureRunner()
            runner.poll_all()
            check_server_status()
            return True

        def done(_result, error):
            self.set_status(f"Poll failed: {error}" if error else "Poll complete.")
            self.refresh_status()
            self.refresh_notifications()

        run_async(task, done)

    def detect_databases(self):
        self.settings_message.set_text("Detecting databases...")

        def task():
            client = OdooClient({"odoo_url": self.url_entry.get_text(), "db": "", "login": "", "poll_minutes": 1, "mute": {}})
            return client.list_databases()

        def done(databases, error):
            if error:
                self.settings_message.set_text(str(error))
                return
            databases = databases or []
            if not databases:
                self.settings_message.set_text("No databases found. Listing may be disabled; enter the name manually.")
                return
            if len(databases) == 1:
                self.db_entry.set_text(databases[0])
                self.settings_message.set_text(f"Auto-detected database: {databases[0]}")
                return
            self.db_combo.remove_all()
            for db in databases:
                self.db_combo.append(db, db)
            self.db_combo.set_visible(True)
            self.settings_message.set_text(f"Found {len(databases)} databases. Select one from the dropdown.")

        run_async(task, done)

    def _db_selected(self, combo):
        selected = combo.get_active_id()
        if selected:
            self.db_entry.set_text(selected)

    def current_settings(self):
        mute = {key: check.get_active() for key, check in self.mute_checks.items()}
        return {
            "odoo_url": self.url_entry.get_text().strip().rstrip("/"),
            "db": self.db_entry.get_text().strip(),
            "login": self.login_entry.get_text().strip(),
            "poll_minutes": max(0.5, self.poll_spin.get_value()),
            "notification_poll_seconds": max(MIN_NOTIFICATION_POLL_SECONDS, self.notification_poll_spin.get_value()),
            "timer_reminder_minutes": max(1, self.timer_reminder_spin.get_value()),
            "attendance_grace_minutes": max(0, self.attendance_grace_spin.get_value()),
            "mute": mute,
            "autostart_enabled": self.autostart_check.get_active(),
        }

    def test_login(self):
        values = self.current_settings()
        secret = self.password_entry.get_text()
        if not values["odoo_url"] or not values["db"] or not values["login"] or not secret:
            self.settings_message.set_text("Fill in URL, database, username, and password/API key first.")
            return
        self.settings_message.set_text("Testing login...")

        def task():
            client = OdooClient(values, secret)
            return client.authenticate(values["odoo_url"], values["db"], values["login"], secret)

        def done(result, error):
            if error:
                self.settings_message.set_text(f"Login failed: {error}")
            else:
                self.settings_message.set_text(f"Login OK (uid {result['uid']}).")

        run_async(task, done)

    def save_settings(self):
        values = self.current_settings()
        secret = self.password_entry.get_text()
        if not values["login"]:
            self.settings_message.set_text("Username/email is required.")
            return
        if secret:
            try:
                store_secret(values["login"], secret)
            except Exception as exc:
                self.settings_message.set_text(f"Settings saved, but keyring save failed: {exc}")
                return

        def update(config):
            config.update(values)
            config["uid"] = None
            config["partner_id"] = None
            config["employee_id"] = None
            config["odoo_version"] = None

        config_store.update(update)
        reset_cached_identity()
        errors = set_autostart_enabled(values["autostart_enabled"])
        if errors:
            self.settings_message.set_text("Saved, but startup update failed: " + " | ".join(errors))
        else:
            self.settings_message.set_text("Saved. Fetching your data now...")
        self.refresh_status()

        # Restart the background service so it picks up the new credentials
        # immediately, instead of waiting for its next scheduled poll - and
        # refresh this window's own views right away too.
        def task():
            errors = restart_background_service()
            FeatureRunner().poll_all()
            return errors

        def done(restart_errors, error):
            if error:
                self.settings_message.set_text(f"Saved, but could not fetch fresh data yet: {error}")
            elif restart_errors:
                self.settings_message.set_text("Saved. Background service restart had issues: " + " | ".join(restart_errors))
            else:
                self.settings_message.set_text("Saved and refreshed.")
            self.refresh_dashboard()
            self.refresh_attendance()
            self.refresh_timer()
            self.refresh_notifications()
            self.refresh_timesheets()
            self.load_module_access()

        run_async(task, done)

    def create_desktop_icon(self):
        try:
            shortcut = create_desktop_shortcut()
            self.settings_message.set_text(f"Desktop icon created: {shortcut}")
        except Exception as exc:
            self.settings_message.set_text(f"Could not create desktop icon: {exc}")

    def restart_service(self):
        self.settings_message.set_text("Restarting background service...")

        def task():
            return restart_background_service()

        def done(errors, error):
            if error:
                self.settings_message.set_text(f"Could not restart service: {error}")
            elif errors:
                self.settings_message.set_text("Service restart failed: " + " | ".join(errors))
            else:
                self.settings_message.set_text("Background service restarted.")

        run_async(task, done)

    def check_modules(self):
        self.settings_message.set_text("Checking installed/accesssible Odoo modules...")

        def task():
            return OdooClient().check_module_access()

        def done(access, error):
            clear_box(self.module_box)
            if error:
                self.settings_message.set_text(f"Module check failed: {error}")
                return
            self.settings_message.set_text("Module check complete.")
            for key, ok in sorted((access or {}).items()):
                self.module_box.append(row_box(MODULE_LABELS.get(key, key), "Yes" if ok else "No"))

        run_async(task, done)

    def refresh_dashboard(self):
        self.set_status("Loading dashboard...")

        def task():
            return FeatureRunner().fetch_dashboard()

        def done(data, error):
            if error:
                self.set_status(f"Dashboard failed: {error}")
                return
            self.render_dashboard(data)
            self.refresh_status()

        run_async(task, done)

    def _open_odoo_app(self, path):
        odoo_url = config_store.read().get("odoo_url")
        if odoo_url:
            open_target({"kind": "url", "url": f"{odoo_url}/odoo/{path}"})

    def render_dashboard(self, data):
        while self.dashboard_tabs.get_n_pages():
            self.dashboard_tabs.remove_page(0)

        tabs = [
            ("Work", [
                ("Checked in", fmt_dt(data.get("checkIn"))),
                ("Checked out", fmt_dt(data.get("checkOut"))),
                ("Hours worked today", self._hours_today_text(data)),
                ("Today's status", self._late_status_widget(data)),
                ("Open tasks", data.get("tasksOpen")),
                ("Projects", data.get("projectsCount")),
                ("Timesheet hours today", fmt_hours(data.get("timesheetHoursToday")) if data.get("timesheetHoursToday") is not None else None),
            ]),
            ("Time Off", [
                ("Time off pending", data.get("leavePending")),
                ("Time off approved", data.get("leaveApproved")),
                ("Last time off", self._last_timeoff_text(data)),
                ("Time off this month", f"{data.get('timeOffDaysThisMonth')}d" if data.get("timeOffDaysThisMonth") is not None else None),
                ("Time off this year", f"{data.get('timeOffDaysThisYear')}d" if data.get("timeOffDaysThisYear") is not None else None),
                ("Next holiday", self._named_date(data.get("nextHoliday"))),
                ("Next mandatory day", self._named_date(data.get("nextMandatoryDay"))),
            ]),
            ("Sales/CRM", [
                ("CRM leads assigned", data.get("crmLeadsAssigned")),
                ("Quotations sent", data.get("quotationsSent")),
                ("Sales this month", self._money(data.get("salesThisMonth"))),
                ("POS orders today", data.get("posOrdersToday")),
                ("POS sales today", self._money(data.get("posSalesToday"))),
                ("Your open POS session", "Yes" if data.get("posOpenSession") else "No" if data.get("posOpenSession") is not None else None),
                ("Open POS sessions (all)", data.get("posOpenSessionsAll")),
            ]),
            ("Ops", [
                ("Helpdesk tickets", data.get("ticketsAssigned")),
                ("Today's meetings", data.get("todaysMeetings")),
                ("Transfers to validate", data.get("transfersWaitingValidation")),
                ("Overdue invoices", self._invoice_text(data)),
                ("Pending approvals", data.get("pendingApprovals")),
            ]),
            ("Expenses", [
                ("My pending expenses", data.get("myExpensesPending")),
                ("Pending amount", self._money(data.get("myExpensesTotal"))),
                ("Awaiting my approval", data.get("expensesPendingApproval")),
            ]),
            ("Purchase", [
                ("RFQs (Draft)", data.get("purchaseDraft")),
                ("To Approve", data.get("purchaseToApprove")),
                ("Confirmed this month", self._money(data.get("purchaseTotal"))),
            ]),
            ("Recruitment", [
                ("New applicants", data.get("applicantsNew")),
                ("Total in pipeline", data.get("applicantsInProgress")),
            ]),
        ]

        for title, rows in tabs:
            filtered = [(label, value) for label, value in rows if value is not None]
            if not filtered:
                continue
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            box.add_css_class("card")
            box.set_margin_top(8)
            box.set_margin_bottom(8)
            links = TAB_APP_LINKS.get(title)
            if links:
                link_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
                for label, path in links:
                    button = Gtk.Button(label=f"Open {label}")
                    button.connect("clicked", lambda _b, path=path: self._open_odoo_app(path))
                    link_row.append(button)
                box.append(link_row)
            for label, value in filtered:
                box.append(row_box(label, value))
            self.dashboard_tabs.append_page(box, Gtk.Label(label=title))

        self.render_approvals(data.get("pendingApprovalsList") or [])
        self.refresh_devices()

    def refresh_devices(self):
        def task():
            return FeatureRunner().fetch_devices()

        def done(devices, error):
            if error or not devices:
                return
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            box.add_css_class("card")
            box.set_margin_top(8)
            box.set_margin_bottom(8)
            header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            for text in ("Device", "Location", "Device IP", "Map"):
                label = Gtk.Label(label=text, xalign=0, hexpand=True)
                label.add_css_class("heading")
                header.append(label)
            box.append(header)
            for device in devices:
                row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
                row.append(Gtk.Label(label=device.get("name") or "-", xalign=0, hexpand=True))
                row.append(Gtk.Label(label=device.get("location_name") or "-", xalign=0, hexpand=True))
                row.append(Gtk.Label(label=device.get("device_ip") or "-", xalign=0, hexpand=True))
                lat, lon = device.get("latitude"), device.get("longitude")
                if lat and lon:
                    link = Gtk.LinkButton.new_with_label(f"https://www.google.com/maps?q={lat},{lon}", "View on map")
                    link.set_hexpand(True)
                    row.append(link)
                else:
                    row.append(Gtk.Label(label="-", xalign=0, hexpand=True))
                box.append(row)
            self.dashboard_tabs.append_page(box, Gtk.Label(label="Devices"))

        run_async(task, done)

    def _hours_today_text(self, data):
        seconds = data.get("hoursWorkedTodaySeconds")
        if seconds is None:
            return None
        if data.get("isCheckedIn") and data.get("currentSessionStart"):
            try:
                started = datetime.strptime(data["currentSessionStart"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
                seconds += max(0, time.time() - started)
            except Exception:
                pass
        return format_clock(seconds)

    def _late_text(self, data):
        if data.get("isLateToday") is None:
            return None
        return f"Late {data.get('lateMinutesToday') or 0}m" if data.get("isLateToday") else "On time"

    def _late_status_widget(self, data):
        text = self._late_text(data)
        if text is None:
            return None
        label = Gtk.Label(label=text, xalign=1)
        if data.get("isLateToday"):
            label.add_css_class("error")
        else:
            label.add_css_class("success")
        return label

    def _last_timeoff_text(self, data):
        item = data.get("lastTimeOff")
        if item is None:
            return "-"
        return f"{fmt_date(item.get('date'))} - {item.get('days')}d ({item.get('type')})"

    def _named_date(self, item):
        if item is None:
            return "-"
        return f"{item.get('name')} ({fmt_date(item.get('date'))})"

    def _money(self, value):
        if value is None:
            return None
        return f"{float(value):.2f}"

    def _invoice_text(self, data):
        if data.get("overdueInvoicesCount") is None:
            return None
        return f"{data.get('overdueInvoicesCount')} ({self._money(data.get('overdueInvoicesAmount'))})"

    def render_approvals(self, approvals):
        clear_box(self.approvals_box)
        if not approvals:
            return
        self.approvals_box.append(section_title("Pending approvals"))
        for approval in approvals:
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            row.append(Gtk.Label(label=approval["name"], xalign=0, hexpand=True))
            approve = Gtk.Button(label="Approve")
            refuse = Gtk.Button(label="Refuse")
            approve.connect("clicked", lambda _b, request_id=approval["requestId"]: self.approval_action(request_id, True))
            refuse.connect("clicked", lambda _b, request_id=approval["requestId"]: self.approval_action(request_id, False))
            row.append(approve)
            row.append(refuse)
            self.approvals_box.append(row)

    def approval_action(self, request_id, approve):
        self.set_status("Sending approval action...")

        def task():
            return FeatureRunner().approve_request(request_id, approve)

        def done(_result, error):
            self.set_status(f"Approval action failed: {error}" if error else "Approval action complete.")
            self.refresh_dashboard()

        run_async(task, done)

    def refresh_timer(self):
        active = state_store.read().get("active_timer")
        if active:
            self.timer_idle_box.set_visible(False)
            self.timer_running_box.set_visible(True)
            self.timer_task_label.set_text(active.get("task_name") or "Running task")
            seconds = elapsed_hours(active["started_at"]) * 3600
            self.timer_time_label.set_text(format_clock(seconds))
        else:
            self.timer_idle_box.set_visible(True)
            self.timer_running_box.set_visible(False)
        self.refresh_timesheet()

    def _tick_timer_label(self):
        """Update the running-timer clock once per second so it counts live."""
        try:
            active = state_store.read().get("active_timer")
            if active and self.timer_running_box.get_visible():
                seconds = elapsed_hours(active["started_at"]) * 3600
                self.timer_time_label.set_text(format_clock(seconds))
        except Exception:
            pass
        return GLib.SOURCE_CONTINUE

    def _on_timer_search_changed(self, _entry):
        if self._timer_search_debounce_id is not None:
            GLib.source_remove(self._timer_search_debounce_id)

        def fire():
            self._timer_search_debounce_id = None
            self.load_timer_tasks()
            return False

        self._timer_search_debounce_id = GLib.timeout_add(400, fire)

    def load_timer_tasks(self):
        self.timer_status.set_text("Loading tasks...")
        filter_text = self.timer_search.get_text()

        def task():
            return FeatureRunner().fetch_timer_tasks(filter_text)

        def done(rows, error):
            self.timer_task_combo.remove_all()
            self.timer_tasks = {}
            if error:
                self.timer_status.set_text(f"Could not load tasks: {error}")
                return
            for task in rows or []:
                project = task.get("project_id", [None, ""])[1] if task.get("project_id") else ""
                label = f"{task.get('name')} ({project})" if project else task.get("name")
                self.timer_task_combo.append(str(task["id"]), label)
                self.timer_tasks[str(task["id"])] = task
            if rows:
                self.timer_task_combo.set_active(0)
            self.timer_status.set_text(f"Loaded {len(rows or [])} tasks.")

        run_async(task, done)

    def start_timer(self):
        task_id = self.timer_task_combo.get_active_id()
        if not task_id:
            self.timer_status.set_text("Choose a task first.")
            return
        task = self.timer_tasks.get(task_id)
        if not task:
            self.timer_status.set_text("Reload the task list first.")
            return
        project_id = task.get("project_id", [None])[0] if task.get("project_id") else None
        self.timer_status.set_text("Starting timer...")

        def task_fn():
            return FeatureRunner().start_task_timer(int(task_id), task.get("name"), project_id)

        def done(_result, error):
            self.timer_status.set_text(f"Could not start timer: {error}" if error else "Timer started.")
            self.refresh_timer()

        run_async(task_fn, done)

    def stop_timer(self):
        description = self.timer_description.get_text().strip()
        self.timer_status.set_text("Stopping timer...")

        def task():
            return FeatureRunner().stop_task_timer(description)

        def done(_result, error):
            self.timer_status.set_text(f"Could not stop timer: {error}" if error else "Timer stopped.")
            self.timer_description.set_text("")
            self.refresh_timer()

        run_async(task, done)

    def refresh_timesheet(self):
        clear_box(self.timesheet_box)

        def task():
            return FeatureRunner().fetch_my_timesheet_today()

        def done(rows, error):
            clear_box(self.timesheet_box)
            if error:
                self.timesheet_box.append(Gtk.Label(label=f"Could not load timesheet: {error}", xalign=0))
                return
            total = 0
            for row in rows or []:
                total += row.get("unit_amount") or 0
                task_name = row.get("task_id", [None, ""])[1] if row.get("task_id") else row.get("project_id", [None, "-"])[1] if row.get("project_id") else "-"
                self.timesheet_box.append(row_box(task_name, f"{row.get('name') or ''} - {fmt_hours(row.get('unit_amount') or 0)}"))
            self.timesheet_box.append(row_box("Total", fmt_hours(total)))

        run_async(task, done)

    def refresh_notifications(self):
        clear_box(self.notifications_box)
        log = state_store.read().get("notification_log") or []
        if not log:
            self.notifications_box.append(Gtk.Label(label="No notifications yet.", xalign=0))
            return
        for entry in log:
            outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            outer.add_css_class("card")
            outer.set_margin_top(8)
            outer.set_margin_bottom(8)
            title = Gtk.Label(label=entry.get("title") or "", xalign=0)
            title.add_css_class("heading")
            body = Gtk.Label(label=(entry.get("body") or "")[:220], xalign=0)
            body.set_wrap(True)
            controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            when = datetime.fromtimestamp((entry.get("time") or 0) / 1000).strftime("%Y-%m-%d %H:%M:%S")
            controls.append(Gtk.Label(label=when, xalign=0, hexpand=True))
            if notification_target_url(entry.get("target")):
                open_btn = Gtk.Button(label="Open")
                open_btn.connect("clicked", lambda _b, target=entry.get("target"): open_target(target))
                controls.append(open_btn)
            snooze_key = (entry.get("target") or {}).get("snooze_key")
            if snooze_key:
                snooze = Gtk.Button(label="Snooze 1h")
                snooze.connect("clicked", lambda _b, entry_id=entry.get("id"), key=snooze_key: self.snooze_notification(entry_id, key))
                controls.append(snooze)
            remove = Gtk.Button(label="Remove")
            remove.connect("clicked", lambda _b, entry_id=entry.get("id"): self.remove_notification(entry_id))
            controls.append(remove)
            outer.append(title)
            outer.append(body)
            outer.append(controls)
            channel_id = (entry.get("target") or {}).get("channel_id")
            if channel_id:
                outer.append(self._build_reply_row(entry.get("id"), channel_id))
            self.notifications_box.append(outer)

    def _build_reply_row(self, entry_id, channel_id):
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        reply_entry = Gtk.Entry()
        reply_entry.set_placeholder_text("Reply...")
        reply_entry.set_hexpand(True)
        send = Gtk.Button(label="Send")

        def do_send(*_args):
            body = reply_entry.get_text().strip()
            if not body:
                return
            send.set_sensitive(False)
            reply_entry.set_sensitive(False)

            def task():
                return FeatureRunner().post_reply(channel_id, body)

            def done(_result, error):
                send.set_sensitive(True)
                reply_entry.set_sensitive(True)
                if error:
                    self.set_status(f"Reply failed: {error}")
                    return
                reply_entry.set_text("")
                self.remove_notification(entry_id)

            run_async(task, done)

        send.connect("clicked", do_send)
        reply_entry.connect("activate", do_send)
        row.append(reply_entry)
        row.append(send)
        return row

    def snooze_notification(self, entry_id, snooze_key):
        FeatureRunner().snooze_reminder(snooze_key)
        self.remove_notification(entry_id)

    def remove_notification(self, entry_id):
        def update(state):
            state["notification_log"] = [entry for entry in state.get("notification_log", []) if entry.get("id") != entry_id]

        state_store.update(update)
        self.refresh_notifications()

    def clear_notifications(self):
        state_store.update(lambda state: state.__setitem__("notification_log", []))
        self.refresh_notifications()


class OdooCompanionApplication(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID)
        self.window = None

    def do_activate(self):
        if not self.window:
            self.window = MainWindow(self)
        self.window.present()


def main(argv=None):
    app = OdooCompanionApplication()
    return app.run(argv)


if __name__ == "__main__":
    raise SystemExit(main())

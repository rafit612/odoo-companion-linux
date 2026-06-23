import math
import re
import time
import urllib.parse
import webbrowser
from datetime import datetime, timedelta, timezone

from .client import OdooClient, OdooError
from .constants import DEFAULT_ATTENDANCE_GRACE_MINUTES, DEFAULT_TIMER_REMINDER_MINUTES, NOTIFICATION_LOG_LIMIT
from .storage import config_store, state_store

try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None

ATTENDANCE_REMINDER_INTERVAL_SECONDS = 60 * 60
STALE_LEAD_DAYS = 7
STALE_LEAD_REMINDER_INTERVAL_SECONDS = 24 * 60 * 60
MEETING_LOOKAHEAD_MINUTES = 10


def strip_html(html):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]*>", " ", html or "")).strip()


def local_date_str(date=None):
    date = date or datetime.now()
    return date.strftime("%Y-%m-%d")


def to_odoo_datetime(dt):
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def now_odoo_datetime():
    return to_odoo_datetime(datetime.now().astimezone())


def local_day_bounds_for_odoo(date=None):
    date = date or datetime.now().astimezone()
    start = date.replace(hour=0, minute=0, second=0, microsecond=0)
    end = date.replace(hour=23, minute=59, second=59, microsecond=0)
    return {"start": to_odoo_datetime(start), "end": to_odoo_datetime(end)}


def parse_odoo_datetime(value):
    if not value:
        return None
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


def format_clock(total_seconds):
    total_seconds = max(0, int(total_seconds or 0))
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60
    return f"{hours}:{minutes:02d}:{seconds:02d}"


def elapsed_hours(started_at_ms):
    return (time.time() * 1000 - started_at_ms) / 1000 / 60 / 60


def notification_target_url(target):
    if not target:
        return None
    odoo_url = target.get("odoo_url") or ""
    kind = target.get("kind")
    if kind == "channel":
        return f"{odoo_url}/odoo/action-mail.action_discuss?active_id={target.get('channel_id')}"
    if kind in ("url", "reminder"):
        return target.get("url")
    model = target.get("model")
    res_id = target.get("res_id") or target.get("id")
    if model and res_id:
        return f"{odoo_url}/web#model={urllib.parse.quote(str(model))}&id={res_id}&view_type=form"
    return odoo_url or None


def open_target(target):
    url = notification_target_url(target)
    if url:
        webbrowser.open(url)


def is_muted(config, category):
    return bool((config.get("mute") or {}).get(category))


def append_notification(entry):
    def update(state):
        log = [entry] + list(state.get("notification_log") or [])
        state["notification_log"] = log[:NOTIFICATION_LOG_LIMIT]

    state_store.update(update)


class FeatureRunner:
    def __init__(self, client=None, notifier=None):
        self.client = client or OdooClient()
        self.notifier = notifier

    @property
    def config(self):
        return self.client.config

    def notify_and_remember(self, notification_id, title, body, target):
        target_with_url = {"odoo_url": self.client.odoo_url, **(target or {})}
        entry = {
            "id": notification_id,
            "title": title,
            "body": (body or "")[:500],
            "odoo_url": self.client.odoo_url,
            "target": target_with_url,
            "time": int(time.time() * 1000),
        }
        append_notification(entry)
        if self.notifier:
            self.notifier.show(entry)

    def get_employee_id(self):
        if self.config.get("employee_id"):
            return self.config.get("employee_id")
        row = self.client.call_kw("res.users", "read", [[self.config["uid"],], ["employee_id"]])[0]
        employee_id = row.get("employee_id", [None])[0] if row.get("employee_id") else None

        def update(config):
            config["employee_id"] = employee_id

        config_store.update(update)
        self.config["employee_id"] = employee_id
        return employee_id

    def my_channel_ids(self):
        partner_id = self.config.get("partner_id")
        if not partner_id:
            return []
        memberships = self.client.call_kw(
            "discuss.channel.member",
            "search_read",
            [[["partner_id", "=", partner_id]], ["channel_id"]],
        )
        return sorted({row["channel_id"][0] for row in memberships or [] if row.get("channel_id")})

    def poll_inbox(self):
        state = state_store.read()
        if not state.get("inbox_baseline_done"):
            latest = self.client.call_kw(
                "mail.message",
                "search_read",
                [[["needaction", "=", True]], ["id"]],
                {"order": "id desc", "limit": 1},
            )
            last_id = latest[0]["id"] if latest else 0

            def update(saved):
                saved["last_message_id"] = last_id
                saved["inbox_baseline_done"] = True

            state_store.update(update)
            return

        last_id = int(state.get("last_message_id") or 0)
        messages = self.client.call_kw(
            "mail.message",
            "search_read",
            [
                [["needaction", "=", True], ["id", ">", last_id]],
                ["id", "subject", "body", "record_name", "model", "res_id", "author_id", "date"],
            ],
            {"order": "id asc", "limit": 50},
        )
        max_id = last_id
        for msg in messages or []:
            max_id = max(max_id, msg["id"])
            if is_muted(self.config, "inbox"):
                continue
            author = msg.get("author_id", ["", "Odoo"])[1] if msg.get("author_id") else "Odoo"
            title = f"{author} - {msg['record_name']}" if msg.get("record_name") else author
            body = strip_html(msg.get("body")) or msg.get("subject") or "New notification"
            self.notify_and_remember(
                f"odoo-msg-{msg['id']}",
                title,
                body,
                {"kind": "record", "model": msg.get("model"), "res_id": msg.get("res_id")},
            )
        if max_id != last_id:
            state_store.update(lambda saved: saved.__setitem__("last_message_id", max_id))

    def poll_channels(self):
        partner_id = self.config.get("partner_id")
        if not partner_id:
            return
        my_channel_ids = self.my_channel_ids()
        if not my_channel_ids:
            return

        state = state_store.read()
        if not state.get("channel_baseline_done"):
            latest = self.client.call_kw(
                "mail.message",
                "search_read",
                [[["model", "=", "discuss.channel"], ["res_id", "in", my_channel_ids]], ["id"]],
                {"order": "id desc", "limit": 1},
            )
            last_id = latest[0]["id"] if latest else 0

            def update(saved):
                saved["last_channel_message_id"] = last_id
                saved["channel_baseline_done"] = True

            state_store.update(update)
            return

        last_id = int(state.get("last_channel_message_id") or 0)
        messages = self.client.call_kw(
            "mail.message",
            "search_read",
            [
                [
                    ["model", "=", "discuss.channel"],
                    ["res_id", "in", my_channel_ids],
                    ["id", ">", last_id],
                    ["author_id", "!=", partner_id],
                    ["message_type", "in", ["comment", "notification"]],
                ],
                ["id", "body", "author_id", "res_id", "date", "attachment_ids"],
            ],
            {"order": "id asc", "limit": 50},
        )
        if not messages:
            return

        channel_ids = sorted({msg["res_id"] for msg in messages})
        channels = self.client.call_kw("discuss.channel", "read", [channel_ids, ["name", "channel_type"]])
        channel_by_id = {row["id"]: row for row in channels or []}

        other_member_by_channel = {}
        chat_channel_ids = [row["id"] for row in channels or [] if row.get("channel_type") == "chat"]
        if chat_channel_ids:
            members = self.client.call_kw(
                "discuss.channel.member",
                "search_read",
                [[["channel_id", "in", chat_channel_ids], ["partner_id", "!=", partner_id]], ["channel_id", "partner_id"]],
            )
            for member in members or []:
                if member.get("channel_id") and member.get("partner_id"):
                    other_member_by_channel[member["channel_id"][0]] = member["partner_id"][1]

        max_id = last_id
        for msg in messages:
            max_id = max(max_id, msg["id"])
            if is_muted(self.config, "channels"):
                continue
            channel = channel_by_id.get(msg["res_id"], {})
            author = msg.get("author_id", ["", "Odoo"])[1] if msg.get("author_id") else "Odoo"
            if channel.get("channel_type") == "chat":
                title = author
            else:
                title = f"{author} - {channel.get('name') or 'Channel'}"
            body = strip_html(msg.get("body")) or ("Sent an attachment" if msg.get("attachment_ids") else "New message")
            self.notify_and_remember(
                f"odoo-chan-{msg['id']}",
                title,
                body,
                {"kind": "channel", "channel_id": msg["res_id"], "channel_name": other_member_by_channel.get(msg["res_id"])},
            )
        state_store.update(lambda saved: saved.__setitem__("last_channel_message_id", max_id))

    def poll_calls(self):
        partner_id = self.config.get("partner_id")
        if not partner_id:
            return
        my_channel_ids = self.my_channel_ids()
        if not my_channel_ids:
            return

        sessions = self.client.call_kw(
            "discuss.channel.rtc.session",
            "search_read",
            [[["channel_id", "in", my_channel_ids], ["partner_id", "!=", partner_id]], ["channel_id", "partner_id"]],
        )
        active_ids = sorted({row["channel_id"][0] for row in sessions or [] if row.get("channel_id")})
        state = state_store.read()
        previous = set(state.get("active_call_channels") or [])
        newly_active = [cid for cid in active_ids if cid not in previous]

        if newly_active and not is_muted(self.config, "calls"):
            callers_by_channel = {}
            for session in sessions or []:
                cid = session.get("channel_id", [None])[0]
                if cid not in newly_active:
                    continue
                callers_by_channel.setdefault(cid, []).append(
                    session.get("partner_id", [None, "Someone"])[1] if session.get("partner_id") else "Someone"
                )
            channels = self.client.call_kw("discuss.channel", "read", [newly_active, ["name", "channel_type"]])
            channel_by_id = {row["id"]: row for row in channels or []}
            for cid in newly_active:
                callers = callers_by_channel.get(cid) or ["Someone"]
                channel = channel_by_id.get(cid, {})
                is_direct = channel.get("channel_type") == "chat"
                title = f"{callers[0]} is calling" if is_direct else f"Call started - {channel.get('name') or 'Channel'}"
                body = "Incoming call" if is_direct else f"{', '.join(callers)} started a call"
                self.notify_and_remember(
                    f"odoo-call-{cid}-{int(time.time() * 1000)}",
                    title,
                    body,
                    {"kind": "channel", "channel_id": cid},
                )

        state_store.update(lambda saved: saved.__setitem__("active_call_channels", active_ids))

    def _today_work_info(self, employee_id):
        """What does today look like on the employee's working-hours calendar?

        Returns {is_workday, start_hour, calendar_id, tz, now_cal} or None.
        start_hour is the earliest scheduled start (float hours) for today's
        weekday, evaluated in the calendar's own timezone.
        """
        employee = self.client.call_kw("hr.employee", "read", [[employee_id], ["resource_calendar_id"]])
        if not employee:
            return None
        rc = employee[0].get("resource_calendar_id")
        if not rc:
            return None
        calendar_id = rc[0]
        tz = None
        try:
            calendar = self.client.call_kw("resource.calendar", "read", [[calendar_id], ["tz"]])
            tz = (calendar and calendar[0].get("tz")) or None
        except OdooError:
            pass
        now_cal = datetime.now().astimezone()
        if tz and ZoneInfo:
            try:
                now_cal = datetime.now(ZoneInfo(tz))
            except Exception:
                now_cal = datetime.now().astimezone()
        weekday = now_cal.weekday()  # Monday=0, matches resource.calendar.attendance.dayofweek
        slots = self.client.call_kw(
            "resource.calendar.attendance",
            "search_read",
            [[["calendar_id", "=", calendar_id], ["dayofweek", "=", str(weekday)]], ["hour_from", "hour_to"]],
            {"limit": 20},
        )
        if not slots:
            return {"is_workday": False, "calendar_id": calendar_id, "tz": tz, "now_cal": now_cal}
        start_hour = min(s.get("hour_from") or 0 for s in slots)
        return {"is_workday": True, "start_hour": start_hour, "calendar_id": calendar_id, "tz": tz, "now_cal": now_cal}

    def has_timeoff_today(self, employee_id):
        today = local_date_str()
        return bool(self.client.call_kw(
            "hr.leave",
            "search_count",
            [[["employee_id", "=", employee_id], ["state", "in", ["confirm", "validate1", "validate"]], ["request_date_from", "<=", today], ["request_date_to", ">=", today]]],
        ))

    def is_public_holiday_today(self):
        now = now_odoo_datetime()
        return bool(self.client.call_kw(
            "resource.calendar.leaves",
            "search_count",
            [[["resource_id", "=", False], ["date_from", "<=", now], ["date_to", ">=", now]]],
        ))

    def cache_self_attendance(self):
        """Snapshot the current user's attendance state for the tray indicator."""
        employee_id = self.get_employee_id()
        if not employee_id:
            return
        bounds = local_day_bounds_for_odoo()
        sessions = self.client.call_kw(
            "hr.attendance",
            "search_read",
            [[["employee_id", "=", employee_id], ["check_in", ">=", bounds["start"]], ["check_in", "<=", bounds["end"]]], ["check_in", "check_out"]],
            {"order": "check_in asc", "limit": 50},
        )
        completed = 0.0
        checked_in = False
        current_start_epoch = None
        first_check_in = None
        last_check_out = None
        for session in sessions or []:
            ci = parse_odoo_datetime(session.get("check_in"))
            co = parse_odoo_datetime(session.get("check_out")) if session.get("check_out") else None
            if first_check_in is None:
                first_check_in = ci
            if co:
                completed += (co - ci).total_seconds()
                last_check_out = co
            else:
                checked_in = True
                current_start_epoch = ci.timestamp() if ci else None

        def hm(dt):
            return dt.astimezone().strftime("%H:%M") if dt else None

        def datehm(dt):
            return dt.astimezone().strftime("%b %d, %H:%M") if dt else None

        payload = {
            "checked_in": checked_in,
            "current_start_epoch": current_start_epoch,
            "worked_seconds_completed": completed,
            "check_in_local": hm(first_check_in),
            "last_check_out_local": datehm(last_check_out),
            "last_check_out_hm": hm(last_check_out),
            "updated_at": int(time.time() * 1000),
        }
        state_store.update(lambda saved: saved.__setitem__("attendance_self", payload))

    def poll_attendance_reminder(self):
        if is_muted(self.config, "attendance"):
            return
        employee_id = self.get_employee_id()
        if not employee_id:
            return

        # Already checked in today? Nothing to warn about.
        bounds = local_day_bounds_for_odoo()
        checked_in = self.client.call_kw(
            "hr.attendance",
            "search_count",
            [[["employee_id", "=", employee_id], ["check_in", ">=", bounds["start"]], ["check_in", "<=", bounds["end"]]]],
        )
        if checked_in > 0:
            return

        # Only warn on an actual working day, and only once the scheduled start
        # time plus the grace/tolerance window has passed.
        info = self._today_work_info(employee_id)
        if not info or not info.get("is_workday"):
            return
        now_cal = info["now_cal"]
        now_hour = now_cal.hour + now_cal.minute / 60 + now_cal.second / 3600
        grace = float(self.config.get("attendance_grace_minutes") or DEFAULT_ATTENDANCE_GRACE_MINUTES) / 60
        if now_hour < (info["start_hour"] + grace):
            return

        # Don't nag on a public holiday or when the user is on approved/pending leave.
        try:
            if self.is_public_holiday_today():
                return
        except OdooError:
            pass
        try:
            if self.has_timeoff_today(employee_id):
                return
        except OdooError:
            pass

        today_key = datetime.now().date().isoformat()
        state = state_store.read()
        last_at = state.get("attendance_reminder_at")
        if state.get("attendance_reminder_date") != today_key:
            last_at = None

            def reset(saved):
                saved["attendance_reminder_date"] = today_key
                saved["attendance_reminder_at"] = None

            state_store.update(reset)

        now_ms = int(time.time() * 1000)
        if last_at and now_ms - last_at < ATTENDANCE_REMINDER_INTERVAL_SECONDS * 1000:
            return
        state_store.update(lambda saved: saved.__setitem__("attendance_reminder_at", now_ms))
        self.notify_and_remember(
            f"odoo-attendance-{today_key}-{now_ms}",
            "You haven't checked in",
            "It's a working day and you're not checked in. If you're off today, create a time off request.",
            {"kind": "reminder", "url": f"{self.client.odoo_url}/odoo/time-off", "snooze_key": "attendance_reminder_at"},
        )

    def poll_stale_leads(self):
        if is_muted(self.config, "crm"):
            return
        cutoff = to_odoo_datetime(datetime.now().astimezone() - timedelta(days=STALE_LEAD_DAYS))
        try:
            leads = self.client.call_kw(
                "crm.lead",
                "search_read",
                [[["user_id", "=", self.config["uid"]], ["active", "=", True], ["date_last_stage_update", "<=", cutoff]], ["name", "date_last_stage_update"]],
                {"limit": 20},
            )
        except OdooError:
            return
        if not leads:
            return

        state = state_store.read()
        reminders = dict(state.get("stale_lead_reminders") or {})
        now_ms = int(time.time() * 1000)
        changed = False
        for lead in leads:
            last_sent = reminders.get(str(lead["id"]))
            if last_sent and now_ms - last_sent < STALE_LEAD_REMINDER_INTERVAL_SECONDS * 1000:
                continue
            self.notify_and_remember(
                f"odoo-lead-{lead['id']}-{now_ms}",
                "Stale lead needs attention",
                f"\"{lead.get('name')}\" has not moved stage in {STALE_LEAD_DAYS}+ days.",
                {"kind": "record", "model": "crm.lead", "res_id": lead["id"]},
            )
            reminders[str(lead["id"])] = now_ms
            changed = True
        if changed:
            state_store.update(lambda saved: saved.__setitem__("stale_lead_reminders", reminders))

    def poll_meetings_starting_soon(self):
        if is_muted(self.config, "calendar") or not self.config.get("partner_id"):
            return
        now = datetime.now().astimezone()
        window_end = now + timedelta(minutes=MEETING_LOOKAHEAD_MINUTES)
        try:
            events = self.client.call_kw(
                "calendar.event",
                "search_read",
                [
                    [["partner_ids", "in", [self.config["partner_id"]]], ["start", ">=", to_odoo_datetime(now)], ["start", "<=", to_odoo_datetime(window_end)]],
                    ["name", "start"],
                ],
                {"limit": 20},
            )
        except OdooError:
            return
        if not events:
            return

        state = state_store.read()
        reminded = set(state.get("reminded_meeting_ids") or [])
        changed = False
        for event in events:
            if event["id"] in reminded:
                continue
            self.notify_and_remember(
                f"odoo-meeting-{event['id']}",
                "Meeting starting soon",
                f"\"{event.get('name')}\" starts at {event.get('start')}.",
                {"kind": "record", "model": "calendar.event", "res_id": event["id"]},
            )
            reminded.add(event["id"])
            changed = True
        if changed:
            state_store.update(lambda saved: saved.__setitem__("reminded_meeting_ids", list(reminded)[-200:]))

    def poll_helpdesk_sla(self):
        if is_muted(self.config, "helpdesk"):
            return
        try:
            tickets = self.client.call_kw(
                "helpdesk.ticket",
                "search_read",
                [[["user_id", "=", self.config["uid"]], ["sla_deadline", "!=", False], ["sla_deadline", "<=", now_odoo_datetime()]], ["name", "sla_deadline"]],
                {"limit": 20},
            )
        except OdooError:
            return
        if not tickets:
            return

        state = state_store.read()
        reminders = dict(state.get("helpdesk_sla_reminders") or {})
        now_ms = int(time.time() * 1000)
        changed = False
        for ticket in tickets:
            last_sent = reminders.get(str(ticket["id"]))
            if last_sent and now_ms - last_sent < ATTENDANCE_REMINDER_INTERVAL_SECONDS * 1000:
                continue
            self.notify_and_remember(
                f"odoo-ticket-{ticket['id']}-{now_ms}",
                "Helpdesk ticket SLA breached",
                f"\"{ticket.get('name')}\" has passed its SLA deadline.",
                {"kind": "record", "model": "helpdesk.ticket", "res_id": ticket["id"]},
            )
            reminders[str(ticket["id"])] = now_ms
            changed = True
        if changed:
            state_store.update(lambda saved: saved.__setitem__("helpdesk_sla_reminders", reminders))

    def poll_attendance_events(self):
        if is_muted(self.config, "attendanceEvents"):
            return
        state = state_store.read()
        since = state.get("last_attendance_poll_at") or to_odoo_datetime(datetime.now().astimezone() - timedelta(minutes=2))
        now = now_odoo_datetime()
        try:
            new_checkins = self.client.call_kw(
                "hr.attendance",
                "search_read",
                [[["check_in", ">=", since], ["check_in", "<=", now]], ["employee_id", "check_in"]],
                {"order": "check_in desc", "limit": 20},
            )
            new_checkouts = self.client.call_kw(
                "hr.attendance",
                "search_read",
                [[["check_out", ">=", since], ["check_out", "<=", now], ["check_out", "!=", False]], ["employee_id", "check_out"]],
                {"order": "check_out desc", "limit": 20},
            )
        except OdooError:
            return

        notified = set(state.get("notified_attendance_ids") or [])
        for record in new_checkins or []:
            key = f"att_in_{record['id']}"
            if key in notified:
                continue
            notified.add(key)
            employee = record.get("employee_id", [None, "Employee"])[1] if record.get("employee_id") else "Employee"
            self.notify_and_remember(
                f"att-checkin-{record['id']}",
                f"{employee} checked in",
                f"Checked in at {record.get('check_in')}",
                {"kind": "record", "model": "hr.attendance", "res_id": record["id"]},
            )
        for record in new_checkouts or []:
            key = f"att_out_{record['id']}"
            if key in notified:
                continue
            notified.add(key)
            employee = record.get("employee_id", [None, "Employee"])[1] if record.get("employee_id") else "Employee"
            self.notify_and_remember(
                f"att-checkout-{record['id']}",
                f"{employee} checked out",
                f"Checked out at {record.get('check_out')}",
                {"kind": "record", "model": "hr.attendance", "res_id": record["id"]},
            )

        def update(saved):
            saved["notified_attendance_ids"] = list(notified)[-500:]
            saved["last_attendance_poll_at"] = now

        state_store.update(update)

    def checkpoint_task_timer(self):
        active_timer = state_store.read().get("active_timer")
        if not active_timer:
            return None
        hours = elapsed_hours(active_timer["started_at"])
        self.client.call_kw("account.analytic.line", "write", [[active_timer["line_id"]], {"unit_amount": hours}])

        last_reminder_at = active_timer.get("last_reminder_at") or active_timer["started_at"]
        now_ms = int(time.time() * 1000)
        reminder_minutes = float(self.config.get("timer_reminder_minutes") or DEFAULT_TIMER_REMINDER_MINUTES)
        if now_ms - last_reminder_at >= reminder_minutes * 60 * 1000:
            if not is_muted(self.config, "timesheetReminder"):
                whole = math.floor(hours)
                minutes = round((hours - whole) * 60)
                self.notify_and_remember(
                    f"timer-reminder-{active_timer['line_id']}-{now_ms}",
                    "Timer still running",
                    f"{active_timer.get('task_name')} - {whole}h {minutes}m so far.",
                    {"kind": "record", "model": "account.analytic.line", "res_id": active_timer["line_id"]},
                )
            active_timer["last_reminder_at"] = now_ms
            state_store.update(lambda saved: saved.__setitem__("active_timer", active_timer))
        return active_timer

    def start_task_timer(self, task_id, task_name, project_id):
        self.client.ensure_logged_in()
        values = {
            "project_id": project_id or False,
            "task_id": task_id,
            "user_id": self.config["uid"],
            "unit_amount": 0,
            "name": f"Timer - {task_name}",
            "date": local_date_str(),
        }
        employee_id = self.get_employee_id()
        if employee_id:
            values["employee_id"] = employee_id
        line_id = self.client.call_kw(
            "account.analytic.line",
            "create",
            [values],
        )
        active_timer = {
            "task_id": task_id,
            "task_name": task_name,
            "project_id": project_id,
            "line_id": line_id,
            "started_at": int(time.time() * 1000),
        }
        state_store.update(lambda saved: saved.__setitem__("active_timer", active_timer))
        return active_timer

    def stop_task_timer(self, description=None):
        active_timer = state_store.read().get("active_timer")
        if not active_timer:
            return None
        hours = elapsed_hours(active_timer["started_at"])
        values = {"unit_amount": hours}
        if description:
            values["name"] = description
        self.client.call_kw("account.analytic.line", "write", [[active_timer["line_id"]], values])
        state_store.update(lambda saved: saved.__setitem__("active_timer", None))
        return {**active_timer, "final_hours": hours}

    def fetch_timer_tasks(self, filter_text=""):
        self.client.ensure_logged_in()
        domain = [
            ["user_ids", "in", [self.config["uid"]]],
            ["state", "not in", ["1_done", "1_canceled"]],
            ["project_id", "!=", False],
            ["allow_timesheets", "=", True],
        ]
        rows = self.client.call_kw(
            "project.task",
            "search_read",
            [domain, ["name", "project_id"]],
            {"order": "name asc", "limit": 200},
        )
        seen = set()
        result = []
        needle = (filter_text or "").lower().strip()
        for row in rows or []:
            if row["id"] in seen:
                continue
            seen.add(row["id"])
            project_name = row.get("project_id", [None, ""])[1] if row.get("project_id") else ""
            if needle and needle not in row.get("name", "").lower() and needle not in project_name.lower():
                continue
            result.append(row)
        return result

    def fetch_departments(self):
        self.client.ensure_logged_in()
        return self.client.call_kw(
            "hr.department",
            "search_read",
            [[], ["name"]],
            {"order": "name asc", "limit": 200},
        )

    def fetch_attendance_today(self, department_id=None, search=None):
        self.client.ensure_logged_in()
        bounds = local_day_bounds_for_odoo()
        domain = [["check_in", ">=", bounds["start"]], ["check_in", "<=", bounds["end"]]]
        if department_id:
            domain.append(["employee_id.department_id", "=", department_id])
        if search:
            domain.append(["employee_id.name", "ilike", search])
        rows = self.client.call_kw(
            "hr.attendance",
            "search_read",
            [domain, ["employee_id", "check_in", "check_out", "tolerance_late_time", "is_within_tolerance"]],
            {"order": "check_in asc", "limit": 500},
        )
        now = datetime.now(timezone.utc)
        for row in rows or []:
            start = parse_odoo_datetime(row.get("check_in"))
            end = parse_odoo_datetime(row.get("check_out"))
            row["hours"] = ((end or now) - start).total_seconds() / 3600 if start else 0
            row["late"] = (not row.get("is_within_tolerance")) if "is_within_tolerance" in row else None
        return rows

    def _period_domain(self, field, year, month=None, is_datetime=True):
        if not year:
            return []
        if month:
            start = datetime(year, month, 1)
            next_month = datetime(year + (month == 12), (month % 12) + 1, 1)
            end = next_month - timedelta(seconds=1)
        else:
            start = datetime(year, 1, 1)
            end = datetime(year, 12, 31, 23, 59, 59)
        fmt = "%Y-%m-%d %H:%M:%S" if is_datetime else "%Y-%m-%d"
        return [[field, ">=", start.strftime(fmt)], [field, "<=", end.strftime(fmt)]]

    def fetch_timeoff(self, year=None, month=None, employee_id=None, search=None):
        self.client.ensure_logged_in()
        domain = self._period_domain("request_date_from", year, month, is_datetime=False)
        if employee_id:
            domain.append(["employee_id", "=", employee_id])
        if search:
            domain.append(["employee_id.name", "ilike", search])
        return self.client.call_kw(
            "hr.leave",
            "search_read",
            [domain, ["employee_id", "holiday_status_id", "request_date_from", "request_date_to", "number_of_days", "state"]],
            {"order": "request_date_from desc", "limit": 500},
        )

    def fetch_activities(self, user_id=None, search=None):
        self.client.ensure_logged_in()
        domain = []
        if user_id:
            domain.append(["user_id", "=", user_id])
        if search:
            domain.append(["res_name", "ilike", search])
        return self.client.call_kw(
            "mail.activity",
            "search_read",
            [domain, ["res_name", "res_model", "res_id", "user_id", "activity_type_id", "date_deadline", "summary"]],
            {"order": "date_deadline asc", "limit": 500},
        )

    def fetch_sales(self, year=None, month=None, state=None, user_id=None, search=None):
        self.client.ensure_logged_in()
        domain = self._period_domain("date_order", year, month, is_datetime=True)
        if state:
            domain.append(["state", "=", state])
        if user_id:
            domain.append(["user_id", "=", user_id])
        if search:
            domain.append("|")
            domain.append(["name", "ilike", search])
            domain.append(["partner_id.name", "ilike", search])
        return self.client.call_kw(
            "sale.order",
            "search_read",
            [domain, ["name", "partner_id", "user_id", "amount_total", "state", "date_order"]],
            {"order": "date_order desc", "limit": 500},
        )

    def fetch_crm(self, year=None, month=None, lead_type=None, user_id=None, search=None):
        self.client.ensure_logged_in()
        domain = self._period_domain("create_date", year, month, is_datetime=True)
        if lead_type:
            domain.append(["type", "=", lead_type])
        if user_id:
            domain.append(["user_id", "=", user_id])
        if search:
            domain.append("|")
            domain.append(["name", "ilike", search])
            domain.append(["contact_name", "ilike", search])
        return self.client.call_kw(
            "crm.lead",
            "search_read",
            [domain, ["name", "contact_name", "partner_id", "user_id", "stage_id", "expected_revenue", "type"]],
            {"order": "create_date desc", "limit": 500},
        )

    def fetch_project_tasks(self, project_id=None, user_id=None, search=None):
        self.client.ensure_logged_in()
        domain = [["project_id", "!=", False]]
        if project_id:
            domain.append(["project_id", "=", project_id])
        if user_id:
            domain.append(["user_ids", "in", [user_id]])
        if search:
            domain.append(["name", "ilike", search])
        return self.client.call_kw(
            "project.task",
            "search_read",
            [domain, ["name", "project_id", "user_ids", "stage_id", "date_deadline", "state"]],
            {"order": "date_deadline asc", "limit": 500},
        )

    def fetch_purchase(self, year=None, month=None, state=None, search=None):
        self.client.ensure_logged_in()
        domain = self._period_domain("date_order", year, month, is_datetime=True)
        if state:
            domain.append(["state", "=", state])
        if search:
            domain.append("|")
            domain.append(["name", "ilike", search])
            domain.append(["partner_id.name", "ilike", search])
        return self.client.call_kw(
            "purchase.order",
            "search_read",
            [domain, ["name", "partner_id", "user_id", "date_order", "date_planned", "amount_total", "state"]],
            {"order": "date_order desc", "limit": 500},
        )

    def fetch_inventory(self, year=None, month=None, open_only=True, search=None):
        self.client.ensure_logged_in()
        domain = self._period_domain("scheduled_date", year, month, is_datetime=True)
        if open_only:
            domain.append(["state", "in", ["assigned", "confirmed", "waiting"]])
        if search:
            domain.append("|")
            domain.append(["name", "ilike", search])
            domain.append(["partner_id.name", "ilike", search])
        return self.client.call_kw(
            "stock.picking",
            "search_read",
            [domain, ["name", "picking_type_id", "partner_id", "user_id", "scheduled_date", "state"]],
            {"order": "scheduled_date desc", "limit": 500},
        )

    def fetch_invoices(self, year=None, month=None, overdue_only=False, search=None):
        self.client.ensure_logged_in()
        domain = [["move_type", "=", "out_invoice"], ["state", "=", "posted"]]
        domain += self._period_domain("invoice_date", year, month, is_datetime=False)
        if overdue_only:
            domain.append(["payment_state", "in", ["not_paid", "partial"]])
            domain.append(["invoice_date_due", "<", local_date_str()])
        if search:
            domain.append("|")
            domain.append(["name", "ilike", search])
            domain.append(["partner_id.name", "ilike", search])
        return self.client.call_kw(
            "account.move",
            "search_read",
            [domain, ["name", "partner_id", "invoice_user_id", "invoice_date", "invoice_date_due", "amount_total", "amount_residual", "payment_state"]],
            {"order": "invoice_date desc", "limit": 500},
        )

    def fetch_expenses(self, year=None, month=None, search=None):
        self.client.ensure_logged_in()
        domain = self._period_domain("date", year, month, is_datetime=False)
        if search:
            domain.append("|")
            domain.append(["name", "ilike", search])
            domain.append(["employee_id.name", "ilike", search])
        return self.client.call_kw(
            "hr.expense",
            "search_read",
            [domain, ["name", "employee_id", "total_amount_currency", "state", "date"]],
            {"order": "date desc", "limit": 500},
        )

    def fetch_recruitment(self, year=None, month=None, job_id=None, search=None):
        self.client.ensure_logged_in()
        domain = [["active", "=", True]]
        domain += self._period_domain("create_date", year, month, is_datetime=True)
        if job_id:
            domain.append(["job_id", "=", job_id])
        if search:
            domain.append(["partner_name", "ilike", search])
        return self.client.call_kw(
            "hr.applicant",
            "search_read",
            [domain, ["partner_name", "job_id", "stage_id", "create_date"]],
            {"order": "create_date desc", "limit": 500},
        )

    def fetch_attendance_month(self, employee_id, year, month):
        self.client.ensure_logged_in()
        start = datetime(year, month, 1)
        next_month = datetime(year + (month == 12), (month % 12) + 1, 1)
        end = next_month - timedelta(seconds=1)
        rows = self.client.call_kw(
            "hr.attendance",
            "search_read",
            [
                [["employee_id", "=", employee_id], ["check_in", ">=", to_odoo_datetime(start.astimezone())], ["check_in", "<=", to_odoo_datetime(end.astimezone())]],
                ["check_in", "check_out", "worked_hours"],
            ],
            {"order": "check_in asc", "limit": 200},
        )
        return rows or []

    def fetch_working_schedules(self):
        self.client.ensure_logged_in()
        calendars = self.client.call_kw("resource.calendar", "search_read", [[], ["name"]], {"order": "name asc", "limit": 100})
        if not calendars:
            return []
        cal_ids = [c["id"] for c in calendars]
        attendances = self.client.call_kw(
            "resource.calendar.attendance",
            "search_read",
            [[["calendar_id", "in", cal_ids]], ["calendar_id", "dayofweek", "hour_from", "hour_to"]],
            {"limit": 2000},
        )
        # Count employees assigned to each working-hours calendar.
        employee_counts = {}
        try:
            groups = self.client.call_kw(
                "hr.employee",
                "read_group",
                [[["resource_calendar_id", "in", cal_ids]], ["resource_calendar_id"], ["resource_calendar_id"]],
                {},
            )
            for group in groups or []:
                rc = group.get("resource_calendar_id")
                if rc:
                    employee_counts[rc[0]] = group.get("resource_calendar_id_count") or group.get("__count") or 0
        except OdooError:
            pass

        day_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

        def hhmm(value):
            value = float(value or 0)
            return f"{int(value):02d}:{int(round((value - int(value)) * 60)):02d}"

        by_cal = {}
        for att in attendances or []:
            cid = att.get("calendar_id", [None])[0]
            if cid is None:
                continue
            by_cal.setdefault(cid, {}).setdefault(int(att.get("dayofweek") or 0), []).append((att.get("hour_from"), att.get("hour_to")))

        result = []
        for cal in calendars:
            days = by_cal.get(cal["id"], {})
            weekly_hours = 0.0
            day_range = {}
            for dow, slots in days.items():
                start_h = min(s[0] for s in slots)
                end_h = max(s[1] for s in slots)
                weekly_hours += sum((s[1] or 0) - (s[0] or 0) for s in slots)
                day_range[dow] = f"{hhmm(start_h)}-{hhmm(end_h)}"
            # Group consecutive weekdays that share the same hour range.
            parts = []
            ordered = sorted(day_range.items())
            i = 0
            while i < len(ordered):
                j = i
                while j + 1 < len(ordered) and ordered[j + 1][0] == ordered[j][0] + 1 and ordered[j + 1][1] == ordered[i][1]:
                    j += 1
                if i == j:
                    parts.append(f"{day_names[ordered[i][0]]} {ordered[i][1]}")
                else:
                    parts.append(f"{day_names[ordered[i][0]]}-{day_names[ordered[j][0]]} {ordered[i][1]}")
                i = j + 1
            result.append({
                "name": cal["name"],
                "hours_per_week": round(weekly_hours, 1),
                "workdays": ", ".join(parts) or "-",
                "employees": employee_counts.get(cal["id"], 0),
            })
        return result

    def fetch_devices(self):
        self.client.ensure_logged_in()
        return self.client.call_kw(
            "biometric.device.details",
            "search_read",
            [[["active", "=", True]], ["name", "location_name", "latitude", "longitude", "device_ip"]],
            {"order": "name asc", "limit": 200},
        )

    def fetch_employees(self):
        self.client.ensure_logged_in()
        return self.client.call_kw(
            "hr.employee",
            "search_read",
            [[["active", "=", True]], ["name"]],
            {"order": "name asc", "limit": 1000},
        )

    def fetch_projects(self):
        self.client.ensure_logged_in()
        return self.client.call_kw(
            "project.project",
            "search_read",
            [[["active", "=", True]], ["name"]],
            {"order": "name asc", "limit": 500},
        )

    def fetch_timesheet_entries(self, project_id=None, employee_id=None, date_from=None, date_to=None):
        self.client.ensure_logged_in()
        # project_timesheet_holidays auto-generates analytic lines for approved
        # time off (holiday_id) and company-wide holidays (global_leave_id) -
        # those aren't real work and shouldn't show up as "tasks" here.
        domain = [["holiday_id", "=", False], ["global_leave_id", "=", False]]
        if project_id:
            domain.append(["project_id", "=", project_id])
        if employee_id:
            domain.append(["employee_id", "=", employee_id])
        if date_from:
            domain.append(["date", ">=", date_from])
        if date_to:
            domain.append(["date", "<=", date_to])
        return self.client.call_kw(
            "account.analytic.line",
            "search_read",
            [domain, ["date", "employee_id", "project_id", "task_id", "name", "unit_amount"]],
            {"order": "date desc", "limit": 1000},
        )

    def fetch_my_timesheet_today(self):
        self.client.ensure_logged_in()
        return self.client.call_kw(
            "account.analytic.line",
            "search_read",
            [[["user_id", "=", self.config["uid"]], ["date", "=", local_date_str()]], ["task_id", "project_id", "name", "unit_amount"]],
            {"order": "id desc", "limit": 20},
        )

    def approve_request(self, request_id, approve=True):
        method = "action_approve" if approve else "action_refuse"
        return self.client.call_kw("approval.request", method, [[request_id]])

    def snooze_reminder(self, snooze_key):
        if snooze_key:
            state_store.update(lambda saved: saved.__setitem__(snooze_key, int(time.time() * 1000)))

    def post_reply(self, channel_id, body):
        return self.client.call_kw(
            "discuss.channel",
            "message_post",
            [[channel_id]],
            {"body": body, "message_type": "comment"},
        )

    def fetch_dashboard(self):
        self.client.ensure_logged_in()
        uid = self.config["uid"]
        today = local_date_str()
        employee_id = self.get_employee_id()
        result = {
            "checkIn": None,
            "checkOut": None,
            "isCheckedIn": False,
            "currentSessionStart": None,
            "hoursWorkedTodaySeconds": None,
            "isLateToday": None,
            "lateMinutesToday": None,
            "tasksOpen": None,
            "projectsCount": None,
            "leavePending": None,
            "leaveApproved": None,
            "nextHoliday": None,
            "nextMandatoryDay": None,
            "lastTimeOff": None,
            "timeOffDaysThisMonth": None,
            "timeOffDaysThisYear": None,
            "crmLeadsAssigned": None,
            "quotationsSent": None,
            "salesThisMonth": None,
            "ticketsAssigned": None,
            "todaysMeetings": None,
            "transfersWaitingValidation": None,
            "overdueInvoicesCount": None,
            "overdueInvoicesAmount": None,
            "timesheetHoursToday": None,
            "pendingApprovals": None,
            "pendingApprovalsList": None,
            "posOpenSession": None,
            "posOpenSessionsAll": None,
            "posOrdersToday": None,
            "posSalesToday": None,
            "expensesPendingApproval": None,
            "myExpensesPending": None,
            "myExpensesTotal": None,
            "purchaseDraft": None,
            "purchaseToApprove": None,
            "purchaseTotal": None,
            "applicantsNew": None,
            "applicantsInProgress": None,
        }

        if employee_id:
            try:
                bounds = local_day_bounds_for_odoo()
                sessions = self.client.call_kw(
                    "hr.attendance",
                    "search_read",
                    [
                        [["employee_id", "=", employee_id], ["check_in", ">=", bounds["start"]], ["check_in", "<=", bounds["end"]]],
                        ["check_in", "check_out", "tolerance_late_time", "is_within_tolerance"],
                    ],
                    {"order": "check_in asc", "limit": 50},
                )
                if sessions:
                    first = sessions[0]
                    last = sessions[-1]
                    result["checkIn"] = first.get("check_in")
                    result["checkOut"] = last.get("check_out") or None
                    result["isCheckedIn"] = not bool(last.get("check_out"))
                    result["currentSessionStart"] = last.get("check_in") if result["isCheckedIn"] else None
                    total = 0
                    for session in sessions:
                        if not session.get("check_out"):
                            continue
                        total += (parse_odoo_datetime(session["check_out"]) - parse_odoo_datetime(session["check_in"])).total_seconds()
                    result["hoursWorkedTodaySeconds"] = total
                    if "is_within_tolerance" in first:
                        result["isLateToday"] = not bool(first.get("is_within_tolerance"))
                        result["lateMinutesToday"] = first.get("tolerance_late_time")
            except OdooError:
                pass

            try:
                result["leavePending"] = self.client.call_kw("hr.leave", "search_count", [[["employee_id", "=", employee_id], ["state", "in", ["confirm", "validate1"]]]])
                result["leaveApproved"] = self.client.call_kw("hr.leave", "search_count", [[["employee_id", "=", employee_id], ["state", "=", "validate"]]])
                last_leave = self.client.call_kw(
                    "hr.leave",
                    "search_read",
                    [[["employee_id", "=", employee_id], ["state", "=", "validate"], ["request_date_from", "<=", today]], ["request_date_from", "number_of_days", "holiday_status_id"]],
                    {"order": "request_date_from desc", "limit": 1},
                )
                result["lastTimeOff"] = (
                    {
                        "date": last_leave[0].get("request_date_from"),
                        "days": last_leave[0].get("number_of_days"),
                        "type": last_leave[0].get("holiday_status_id", [None, "Time off"])[1] if last_leave[0].get("holiday_status_id") else "Time off",
                    }
                    if last_leave
                    else None
                )
                now = datetime.now()
                month_start = local_date_str(datetime(now.year, now.month, 1))
                year_start = local_date_str(datetime(now.year, 1, 1))
                leaves_this_year = self.client.call_kw(
                    "hr.leave",
                    "search_read",
                    [[["employee_id", "=", employee_id], ["state", "=", "validate"], ["request_date_from", ">=", year_start]], ["request_date_from", "number_of_days"]],
                    {"limit": 200},
                )
                result["timeOffDaysThisYear"] = sum(row.get("number_of_days") or 0 for row in leaves_this_year or [])
                result["timeOffDaysThisMonth"] = sum(
                    row.get("number_of_days") or 0 for row in leaves_this_year or [] if row.get("request_date_from", "") >= month_start
                )
            except OdooError:
                pass

        try:
            open_task_domain = [["user_ids", "in", [uid]], ["state", "not in", ["1_done", "1_canceled"]]]
            result["tasksOpen"] = self.client.call_kw("project.task", "search_count", [open_task_domain])
            tasks = self.client.call_kw("project.task", "search_read", [open_task_domain, ["project_id"]], {"limit": 200})
            result["projectsCount"] = len({task["project_id"][0] for task in tasks or [] if task.get("project_id")})
        except OdooError:
            pass

        try:
            holiday = self.client.call_kw(
                "resource.calendar.leaves",
                "search_read",
                [[["resource_id", "=", False], ["date_to", ">=", today]], ["name", "date_from"]],
                {"order": "date_from asc", "limit": 1},
            )
            result["nextHoliday"] = {"name": holiday[0].get("name"), "date": holiday[0].get("date_from")} if holiday else None
        except OdooError:
            pass

        try:
            mandatory = self.client.call_kw(
                "hr.leave.mandatory.day",
                "search_read",
                [[["end_date", ">=", today]], ["name", "start_date"]],
                {"order": "start_date asc", "limit": 1},
            )
            result["nextMandatoryDay"] = {"name": mandatory[0].get("name"), "date": mandatory[0].get("start_date")} if mandatory else None
        except OdooError:
            pass

        optional_jobs = [
            ("crmLeadsAssigned", lambda: self.client.call_kw("crm.lead", "search_count", [[["user_id", "=", uid], ["active", "=", True]]])),
            ("ticketsAssigned", lambda: self.client.call_kw("helpdesk.ticket", "search_count", [[["user_id", "=", uid]]])),
            ("transfersWaitingValidation", lambda: self.client.call_kw("stock.picking", "search_count", [[["user_id", "=", uid], ["state", "=", "assigned"]]])),
            ("timesheetHoursToday", self._dashboard_timesheet_hours),
            ("pendingApprovalsData", self._dashboard_pending_approvals),
            ("posData", self._dashboard_pos),
            ("expenseData", self._dashboard_expenses),
            ("purchaseData", self._dashboard_purchase),
            ("recruitmentData", self._dashboard_recruitment),
            ("salesData", self._dashboard_sales),
            ("calendarData", self._dashboard_calendar),
            ("accountData", self._dashboard_account),
        ]
        for key, fn in optional_jobs:
            try:
                value = fn()
            except OdooError:
                continue
            if isinstance(value, dict):
                result.update(value)
            else:
                result[key] = value
        return result

    def _dashboard_sales(self):
        uid = self.config["uid"]
        month_start = local_date_str(datetime(datetime.now().year, datetime.now().month, 1))
        quotations = self.client.call_kw("sale.order", "search_count", [[["user_id", "=", uid], ["state", "=", "sent"]]])
        orders = self.client.call_kw(
            "sale.order",
            "search_read",
            [[["user_id", "=", uid], ["state", "=", "sale"], ["date_order", ">=", month_start]], ["amount_total"]],
            {"limit": 200},
        )
        return {"quotationsSent": quotations, "salesThisMonth": sum(row.get("amount_total") or 0 for row in orders or [])}

    def _dashboard_calendar(self):
        if not self.config.get("partner_id"):
            return {}
        bounds = local_day_bounds_for_odoo()
        meetings = self.client.call_kw(
            "calendar.event",
            "search_count",
            [[["partner_ids", "in", [self.config["partner_id"]]], ["start", ">=", bounds["start"]], ["start", "<=", bounds["end"]]]],
        )
        return {"todaysMeetings": meetings}

    def _dashboard_account(self):
        today = local_date_str()
        invoices = self.client.call_kw(
            "account.move",
            "search_read",
            [[["invoice_user_id", "=", self.config["uid"]], ["move_type", "=", "out_invoice"], ["state", "=", "posted"], ["payment_state", "in", ["not_paid", "partial"]], ["invoice_date_due", "<", today]], ["amount_residual"]],
            {"limit": 200},
        )
        return {
            "overdueInvoicesCount": len(invoices or []),
            "overdueInvoicesAmount": sum(row.get("amount_residual") or 0 for row in invoices or []),
        }

    def _dashboard_timesheet_hours(self):
        rows = self.client.call_kw(
            "account.analytic.line",
            "search_read",
            [[["user_id", "=", self.config["uid"]], ["project_id", "!=", False], ["date", "=", local_date_str()]], ["unit_amount"]],
            {"limit": 200},
        )
        return sum(row.get("unit_amount") or 0 for row in rows or [])

    def _dashboard_pending_approvals(self):
        rows = self.client.call_kw(
            "approval.approver",
            "search_read",
            [[["user_id", "=", self.config["uid"]], ["status", "=", "pending"]], ["request_id"]],
            {"limit": 20},
        )
        approvals = [
            {"requestId": row["request_id"][0], "name": row["request_id"][1]}
            for row in rows or []
            if row.get("request_id")
        ]
        return {"pendingApprovals": len(approvals), "pendingApprovalsList": approvals}

    def _dashboard_pos(self):
        bounds = local_day_bounds_for_odoo()
        my_open = self.client.call_kw("pos.session", "search_count", [[["user_id", "=", self.config["uid"]], ["state", "=", "opened"]]])
        all_open = self.client.call_kw("pos.session", "search_count", [[["state", "=", "opened"]]])
        orders = self.client.call_kw(
            "pos.order",
            "search_read",
            [[["date_order", ">=", bounds["start"]], ["date_order", "<=", bounds["end"]], ["state", "in", ["paid", "done", "invoiced"]]], ["amount_total"]],
            {"limit": 500},
        )
        return {
            "posOpenSession": my_open,
            "posOpenSessionsAll": all_open,
            "posOrdersToday": len(orders or []),
            "posSalesToday": sum(row.get("amount_total") or 0 for row in orders or []),
        }

    def _dashboard_expenses(self):
        rows = self.client.call_kw(
            "hr.expense",
            "search_read",
            [[["employee_id.user_id", "=", self.config["uid"]], ["state", "in", ["draft", "reported"]]], ["total_amount_currency"]],
            {"limit": 200},
        )
        result = {
            "myExpensesPending": len(rows or []),
            "myExpensesTotal": sum(row.get("total_amount_currency") or 0 for row in rows or []),
        }
        result["expensesPendingApproval"] = self.client.call_kw("hr.expense.sheet", "search_count", [[["state", "=", "submit"], ["can_approve", "=", True]]])
        return result

    def _dashboard_purchase(self):
        uid = self.config["uid"]
        today = local_date_str()
        month_start = f"{today[:7]}-01"
        draft = self.client.call_kw("purchase.order", "search_count", [[["state", "=", "draft"], ["user_id", "=", uid]]])
        to_approve = self.client.call_kw("purchase.order", "search_count", [[["state", "=", "to approve"], ["user_id", "=", uid]]])
        groups = self.client.call_kw(
            "purchase.order",
            "read_group",
            [[["state", "=", "purchase"], ["user_id", "=", uid], ["date_approve", ">=", month_start]], ["amount_total"], []],
            {},
        )
        return {"purchaseDraft": draft, "purchaseToApprove": to_approve, "purchaseTotal": (groups or [{}])[0].get("amount_total") or 0}

    def _dashboard_recruitment(self):
        new_count = self.client.call_kw("hr.applicant", "search_count", [[["active", "=", True], ["stage_id.sequence", "<=", 1]]])
        total = self.client.call_kw("hr.applicant", "search_count", [[["active", "=", True]]])
        return {"applicantsNew": new_count, "applicantsInProgress": total}

    def poll_notifications(self):
        """The latency-sensitive checks: inbox, chats, calls, timer checkpoint.

        Run this on its own fast/short cadence (see service.py) so chat
        messages and calls show up close to real time, independent of the
        slower dashboard-data poll interval the user configures.
        """
        if not self.client.odoo_url:
            return
        self.client.ensure_logged_in()
        for fn in (self.checkpoint_task_timer, self.poll_inbox, self.poll_channels, self.poll_calls):
            try:
                fn()
            except Exception as exc:
                print(f"Odoo Companion: {fn.__name__} failed: {exc}")

    def poll_dashboard_extras(self):
        """Slower, less latency-sensitive reminder checks."""
        if not self.client.odoo_url:
            return
        self.client.ensure_logged_in()
        for fn in (
            self.cache_self_attendance,
            self.poll_attendance_reminder,
            self.poll_stale_leads,
            self.poll_meetings_starting_soon,
            self.poll_helpdesk_sla,
            self.poll_attendance_events,
        ):
            try:
                fn()
            except Exception as exc:
                print(f"Odoo Companion: {fn.__name__} failed: {exc}")

    def poll_all(self):
        self.poll_notifications()
        self.poll_dashboard_extras()


def check_server_status(notifier=None):
    client = OdooClient()
    if not client.odoo_url:
        return None
    online = False
    try:
        client.fetch_odoo_version()
        online = True
    except OdooError:
        online = False

    new_status = "online" if online else "offline"
    state = state_store.read()
    previous = state.get("server_status")
    now_ms = int(time.time() * 1000)

    def update(saved):
        saved["server_status"] = new_status
        saved["server_status_checked_at"] = now_ms

    state_store.update(update)

    config = config_store.read()
    if previous and previous != new_status and not is_muted(config, "serverStatus"):
        title = "Odoo server is back online" if new_status == "online" else "Odoo server unreachable"
        body = f"{client.odoo_url} is reachable again." if new_status == "online" else f"Could not reach {client.odoo_url}."
        runner = FeatureRunner(client, notifier)
        runner.notify_and_remember(
            f"server-status-{now_ms}",
            title,
            body,
            {"kind": "url", "url": client.odoo_url},
        )
    return new_status

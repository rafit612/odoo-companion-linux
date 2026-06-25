import math
import re
import time
import urllib.parse
import webbrowser
from datetime import datetime, timedelta, timezone

from .client import OdooClient, OdooError
from .constants import (
    DEFAULT_ATTENDANCE_GRACE_MINUTES,
    DEFAULT_LUNCH_REMINDER_MINUTES,
    DEFAULT_TIMER_IDLE_MINUTES,
    DEFAULT_TIMER_IDLE_WARNING_SECONDS,
    DEFAULT_TIMER_REMINDER_MINUTES,
    NOTIFICATION_LOG_LIMIT,
)
from .storage import config_store, state_store

try:
    from .idle import get_user_idle_seconds
except Exception:
    def get_user_idle_seconds():
        return None

LUNCH_OFF_WEEKDAY = 4  # Friday - lunch voting runs every day except Friday

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


def elapsed_hours_at(started_at_ms, now_ms=None):
    now_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    return max(0, now_ms - int(started_at_ms)) / 1000 / 60 / 60


def elapsed_hours(started_at_ms):
    return elapsed_hours_at(started_at_ms)


def format_minutes(total_minutes):
    total_minutes = max(0, int(round(total_minutes or 0)))
    hours = total_minutes // 60
    minutes = total_minutes % 60
    if hours and minutes:
        return f"{hours}h {minutes}m"
    if hours:
        return f"{hours}h"
    return f"{minutes}m"


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
        end_hour = max(s.get("hour_to") or 0 for s in slots)
        return {"is_workday": True, "start_hour": start_hour, "end_hour": end_hour, "calendar_id": calendar_id, "tz": tz, "now_cal": now_cal}

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

    def _timer_idle_minutes(self):
        try:
            return max(0.0, float(self.config.get("timer_idle_minutes") or DEFAULT_TIMER_IDLE_MINUTES))
        except (TypeError, ValueError):
            return float(DEFAULT_TIMER_IDLE_MINUTES)

    def _timer_idle_warning_seconds(self):
        try:
            return max(0.0, float(self.config.get("timer_idle_warning_seconds") or DEFAULT_TIMER_IDLE_WARNING_SECONDS))
        except (TypeError, ValueError):
            return float(DEFAULT_TIMER_IDLE_WARNING_SECONDS)

    def _clear_idle_pre_stop_warning(self, active_timer):
        if "idle_pre_stop_warning_key" not in active_timer:
            return
        active_timer.pop("idle_pre_stop_warning_key", None)
        state_store.update(lambda saved: saved.__setitem__("active_timer", active_timer))

    def _handle_idle_task_timer(self, active_timer, now_ms):
        idle_limit_minutes = self._timer_idle_minutes()
        if idle_limit_minutes <= 0:
            return None
        idle_limit_seconds = idle_limit_minutes * 60
        try:
            idle_seconds = get_user_idle_seconds()
        except Exception:
            idle_seconds = None
        if idle_seconds is None:
            return None

        target = {"kind": "record", "model": "account.analytic.line", "res_id": active_timer["line_id"]}
        task_name = active_timer.get("task_name") or "Timer"
        idle_text = format_minutes(math.ceil(idle_seconds / 60))
        auto_stop = bool(self.config.get("timer_idle_auto_stop", True))
        warning_seconds = self._timer_idle_warning_seconds()
        warning_at_seconds = max(0.0, idle_limit_seconds - warning_seconds)
        if idle_seconds < idle_limit_seconds:
            if auto_stop and warning_seconds > 0 and idle_seconds >= warning_at_seconds:
                warning_key = f"{active_timer['started_at']}-{int(idle_limit_seconds)}-{int(warning_seconds)}"
                if active_timer.get("idle_pre_stop_warning_key") != warning_key:
                    remaining_seconds = max(1, int(math.ceil(idle_limit_seconds - idle_seconds)))
                    if not is_muted(self.config, "timesheetIdle"):
                        self.notify_and_remember(
                            f"timer-idle-pre-stop-{active_timer['line_id']}-{now_ms}",
                            "Timer will auto-stop soon",
                            f"{task_name} will stop in about {remaining_seconds}s if no activity is detected.",
                            target,
                        )
                    active_timer["idle_pre_stop_warning_key"] = warning_key
                    active_timer["last_reminder_at"] = now_ms
                    state_store.update(lambda saved: saved.__setitem__("active_timer", active_timer))
            else:
                self._clear_idle_pre_stop_warning(active_timer)
            return None

        if auto_stop:
            stop_at_ms = now_ms - int(max(0.0, idle_seconds - idle_limit_seconds) * 1000)
            stop_at_ms = max(int(active_timer["started_at"]), min(now_ms, stop_at_ms))
            hours = elapsed_hours_at(active_timer["started_at"], stop_at_ms)
            self.client.call_kw("account.analytic.line", "write", [[active_timer["line_id"]], {"unit_amount": hours}])
            state_store.update(lambda saved: saved.__setitem__("active_timer", None))
            if not is_muted(self.config, "timesheetIdle"):
                self.notify_and_remember(
                    f"timer-idle-stop-{active_timer['line_id']}-{now_ms}",
                    "Timer auto-stopped after idle",
                    f"{task_name} was idle for {idle_text}. Recorded {format_clock(hours * 3600)} and stopped the timer.",
                    target,
                )
            return {**active_timer, "final_hours": hours, "auto_stopped": True, "idle_seconds": idle_seconds}

        last_idle_reminder_at = active_timer.get("last_idle_reminder_at") or 0
        reminder_minutes = float(self.config.get("timer_reminder_minutes") or DEFAULT_TIMER_REMINDER_MINUTES)
        if now_ms - last_idle_reminder_at >= max(60 * 1000, reminder_minutes * 60 * 1000):
            if not is_muted(self.config, "timesheetIdle"):
                self.notify_and_remember(
                    f"timer-idle-warning-{active_timer['line_id']}-{now_ms}",
                    "Timer running while idle",
                    f"{task_name} has been idle for {idle_text}. Stop the timer if you are away from work.",
                    target,
                )
            active_timer["last_idle_reminder_at"] = now_ms
            state_store.update(lambda saved: saved.__setitem__("active_timer", active_timer))
        return None

    def checkpoint_task_timer(self):
        active_timer = state_store.read().get("active_timer")
        if not active_timer:
            return None
        now_ms = int(time.time() * 1000)
        idle_result = self._handle_idle_task_timer(active_timer, now_ms)
        if idle_result:
            return idle_result
        hours = elapsed_hours_at(active_timer["started_at"], now_ms)
        self.client.call_kw("account.analytic.line", "write", [[active_timer["line_id"]], {"unit_amount": hours}])

        last_reminder_at = active_timer.get("last_reminder_at") or active_timer["started_at"]
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

    def _write_finished_task_timer(self, active_timer, stopped_at_ms=None, description=None):
        stopped_at_ms = int(time.time() * 1000) if stopped_at_ms is None else int(stopped_at_ms)
        hours = elapsed_hours_at(active_timer["started_at"], stopped_at_ms)
        values = {"unit_amount": hours}
        if description:
            values["name"] = description
        self.client.call_kw("account.analytic.line", "write", [[active_timer["line_id"]], values])
        return hours

    def flush_pending_task_timer_stop(self):
        pending = state_store.read().get("timer_stop_pending")
        if not pending:
            return None
        stopped_at_ms = pending.get("stopped_at") or int(time.time() * 1000)
        hours = self._write_finished_task_timer(pending, stopped_at_ms, pending.get("description"))

        def clear(saved):
            current = saved.get("timer_stop_pending") or {}
            if current.get("line_id") == pending.get("line_id") and current.get("stopped_at") == pending.get("stopped_at"):
                saved["timer_stop_pending"] = None

        state_store.update(clear)
        if pending.get("notify") and not is_muted(self.config, "timesheetIdle"):
            task_name = pending.get("task_name") or "Timer"
            reason = pending.get("stop_reason") or "system event"
            self.notify_and_remember(
                f"timer-system-stop-{pending['line_id']}-{stopped_at_ms}",
                "Timer stopped automatically",
                f"{task_name} stopped because {reason}. Recorded {format_clock(hours * 3600)}.",
                {"kind": "record", "model": "account.analytic.line", "res_id": pending["line_id"]},
            )
        return {**pending, "final_hours": hours}

    def close_task_timer_for_system_event(self, reason, stopped_at_ms=None, notify=True, flush=True):
        stopped_at_ms = int(time.time() * 1000) if stopped_at_ms is None else int(stopped_at_ms)
        active_timer = state_store.read().get("active_timer")
        if not active_timer:
            return self.flush_pending_task_timer_stop() if flush else None
        pending = {
            **active_timer,
            "stopped_at": stopped_at_ms,
            "stop_reason": reason,
            "notify": bool(notify),
        }

        def mark(saved):
            saved["active_timer"] = None
            saved["timer_stop_pending"] = pending

        state_store.update(mark)
        if not flush:
            return {**pending, "pending_stop": True}
        return self.flush_pending_task_timer_stop()

    def stop_task_timer(self, description=None):
        active_timer = state_store.read().get("active_timer")
        if not active_timer:
            return None
        hours = self._write_finished_task_timer(active_timer, description=description)
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

    def fetch_helpdesk(self, year=None, month=None, search=None):
        self.client.ensure_logged_in()
        domain = self._period_domain("create_date", year, month, is_datetime=True)
        if search:
            domain.append("|")
            domain.append(["name", "ilike", search])
            domain.append(["partner_id.name", "ilike", search])
        return self.client.call_kw(
            "helpdesk.ticket",
            "search_read",
            [domain, ["name", "partner_id", "user_id", "team_id", "stage_id", "priority", "create_date"]],
            {"order": "create_date desc", "limit": 500},
        )

    def fetch_pos(self, year=None, month=None, search=None):
        self.client.ensure_logged_in()
        domain = self._period_domain("date_order", year, month, is_datetime=True)
        if search:
            domain.append("|")
            domain.append(["name", "ilike", search])
            domain.append(["partner_id.name", "ilike", search])
        return self.client.call_kw(
            "pos.order",
            "search_read",
            [domain, ["name", "partner_id", "user_id", "amount_total", "state", "date_order"]],
            {"order": "date_order desc", "limit": 500},
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

    # ── Lunch vote (dbsl_info_board, Odoo 18) ────────────────────────────────
    @staticmethod
    def _hour_to_hm(value):
        value = float(value or 0)
        return f"{int(value):02d}:{int(round((value - int(value)) * 60)):02d}"

    def _user_company_id(self):
        if self.config.get("company_id"):
            return self.config.get("company_id")
        rows = self.client.call_kw("res.users", "read", [[self.config["uid"]], ["company_id"]])
        return rows[0].get("company_id", [None])[0] if rows and rows[0].get("company_id") else None

    def _is_checked_in_today(self):
        employee_id = self.get_employee_id()
        if not employee_id:
            return False
        bounds = local_day_bounds_for_odoo()
        return bool(self.client.call_kw(
            "hr.attendance",
            "search_count",
            [[["employee_id", "=", employee_id], ["check_in", ">=", bounds["start"]], ["check_in", "<=", bounds["end"]]]],
        ))

    def fetch_lunch_vote(self):
        """Today's lunch-vote state for the current user, or None when the
        dbsl_info_board module isn't installed or the user isn't an eligible
        voter. Read-only mirror of the module's _get_lunch_voting logic."""
        self.client.ensure_logged_in()
        uid = self.config["uid"]
        try:
            company_id = self._user_company_id()
            if not company_id:
                return None
            company = self.client.call_kw(
                "res.company",
                "read",
                [[company_id], ["name", "lunch_voting_enabled", "lunch_vote_open_time", "lunch_vote_close_time",
                                "lunch_voting_user_ids", "lunch_subsidy_user_ids", "lunch_full_amount"]],
            )
        except OdooError:
            return None  # module not installed (fields/model absent)
        if not company:
            return None
        company = company[0]
        if not company.get("lunch_voting_enabled"):
            return None
        if uid not in (company.get("lunch_voting_user_ids") or []):
            return None  # only selected users may vote

        now = datetime.now().astimezone()
        if now.date().weekday() == LUNCH_OFF_WEEKDAY:
            return {"eligible": True, "off_day": True, "company_name": company.get("name"), "company_id": company_id}

        open_time = company.get("lunch_vote_open_time") or 0.0
        close_time = company.get("lunch_vote_close_time") or 0.0
        now_f = now.hour + now.minute / 60.0 + now.second / 3600.0
        is_open = (open_time <= now_f < close_time) if close_time > open_time else False
        not_yet = now_f < open_time
        today = local_date_str()
        mine = self.client.call_kw(
            "dbsl.lunch.vote",
            "search_read",
            [[["user_id", "=", uid], ["date", "=", today], ["company_id", "=", company_id]], ["choice"]],
            {"limit": 1},
        )
        yes = self.client.call_kw("dbsl.lunch.vote", "search_count", [[["company_id", "=", company_id], ["date", "=", today], ["choice", "=", "yes"]]])
        no = self.client.call_kw("dbsl.lunch.vote", "search_count", [[["company_id", "=", company_id], ["date", "=", today], ["choice", "=", "no"]]])
        is_subsidized = uid in (company.get("lunch_subsidy_user_ids") or [])
        full_amount = company.get("lunch_full_amount") or 0.0
        pay_note = None
        if not is_subsidized and full_amount:
            amt = int(full_amount) if float(full_amount).is_integer() else full_amount
            pay_note = f"You have to pay {amt} Taka for lunch."
        return {
            "eligible": True,
            "off_day": False,
            "company_id": company_id,
            "company_name": company.get("name"),
            "open_time": open_time,
            "close_time": close_time,
            "open_hm": self._hour_to_hm(open_time),
            "close_hm": self._hour_to_hm(close_time),
            "is_open": is_open,
            "not_yet": not_yet,
            "closed": (not is_open) and (not not_yet),
            "checked_in": self._is_checked_in_today(),
            "my_choice": mine[0]["choice"] if mine else None,
            "yes_count": yes,
            "no_count": no,
            "total_count": yes + no,
            "pay_note": pay_note,
        }

    def cast_lunch_vote(self, choice):
        if choice not in ("yes", "no"):
            raise OdooError("Invalid lunch choice")
        self.client.ensure_logged_in()
        if not self._is_checked_in_today():
            raise OdooError("You must check in today before voting for lunch.")
        uid = self.config["uid"]
        company_id = self._user_company_id()
        today = local_date_str()
        existing = self.client.call_kw(
            "dbsl.lunch.vote",
            "search",
            [[["user_id", "=", uid], ["date", "=", today], ["company_id", "=", company_id]]],
            {"limit": 1},
        )
        if existing:
            self.client.call_kw("dbsl.lunch.vote", "write", [[existing[0]], {"choice": choice}])
        else:
            self.client.call_kw("dbsl.lunch.vote", "create", [{"user_id": uid, "date": today, "choice": choice, "company_id": company_id}])
        return True

    def fetch_lunch_overview(self):
        """Who voted yes / no / not-yet among the eligible users today."""
        self.client.ensure_logged_in()
        company_id = self._user_company_id()
        if not company_id:
            return None
        try:
            company = self.client.call_kw("res.company", "read", [[company_id], ["lunch_voting_user_ids"]])
        except OdooError:
            return None
        eligible_ids = (company and company[0].get("lunch_voting_user_ids")) or []
        if not eligible_ids:
            return {"yes": [], "no": [], "not_voted": []}
        users = self.client.call_kw("res.users", "read", [eligible_ids, ["name"]])
        name_by_id = {u["id"]: u["name"] for u in users or []}
        today = local_date_str()
        votes = self.client.call_kw(
            "dbsl.lunch.vote",
            "search_read",
            [[["date", "=", today], ["company_id", "=", company_id], ["user_id", "in", eligible_ids]], ["user_id", "choice"]],
            {"limit": 500},
        )
        choice_by_uid = {v["user_id"][0]: v["choice"] for v in votes or [] if v.get("user_id")}
        yes, no, not_voted = [], [], []
        for eid in eligible_ids:
            name = name_by_id.get(eid, "User")
            choice = choice_by_uid.get(eid)
            (yes if choice == "yes" else no if choice == "no" else not_voted).append(name)
        return {"yes": sorted(yes), "no": sorted(no), "not_voted": sorted(not_voted)}

    def _workday_ended(self, now_f):
        attendance = state_store.read().get("attendance_self") or {}
        if attendance.get("last_check_out_local") and not attendance.get("checked_in"):
            return True
        employee_id = self.get_employee_id()
        if employee_id:
            try:
                info = self._today_work_info(employee_id)
            except OdooError:
                info = None
            if info and info.get("is_workday") and info.get("end_hour"):
                return now_f >= info["end_hour"]
        return False

    def poll_lunch_vote(self):
        if is_muted(self.config, "lunch"):
            return
        try:
            state = self.fetch_lunch_vote()
        except OdooError:
            return
        if not state or not state.get("eligible") or state.get("off_day"):
            return
        if state.get("my_choice"):
            return  # already voted - nothing to nag about
        if not state.get("is_open"):
            return  # can only usefully remind while the window is open
        if not state.get("checked_in"):
            return  # not checked in today -> can't vote, so don't nag

        now = datetime.now().astimezone()
        now_f = now.hour + now.minute / 60.0 + now.second / 3600.0
        today_key = now.date().isoformat()
        saved = state_store.read()
        target = {"kind": "url", "url": f"{self.client.odoo_url}/odoo"}

        # Reminder #1 - a configurable lead time before the window closes.
        lead = float(self.config.get("lunch_vote_reminder_minutes") or DEFAULT_LUNCH_REMINDER_MINUTES)
        minutes_to_close = (state.get("close_time", 0.0) - now_f) * 60
        if 0 < minutes_to_close <= lead and saved.get("lunch_close_reminded_date") != today_key:
            state_store.update(lambda s: s.__setitem__("lunch_close_reminded_date", today_key))
            self.notify_and_remember(
                f"lunch-close-{today_key}",
                "Vote for lunch",
                f"Lunch voting closes at {state.get('close_hm')} - you haven't voted yet.",
                target,
            )

        # Reminder #2 - when the employee's work hours end / they check out.
        if saved.get("lunch_workend_reminded_date") != today_key and self._workday_ended(now_f):
            state_store.update(lambda s: s.__setitem__("lunch_workend_reminded_date", today_key))
            self.notify_and_remember(
                f"lunch-workend-{today_key}",
                "Vote for lunch before you leave",
                "Your work hours are over and you haven't voted for lunch yet.",
                target,
            )

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
        for fn in (self.flush_pending_task_timer_stop, self.checkpoint_task_timer, self.poll_inbox, self.poll_channels, self.poll_calls):
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
            self.poll_lunch_vote,
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

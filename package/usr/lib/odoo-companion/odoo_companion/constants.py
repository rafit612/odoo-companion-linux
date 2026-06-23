from pathlib import Path
import os

APP_ID = "com.dotbdsolutions.OdooCompanion"
APP_NAME = "Odoo Companion"
APP_VERSION = "2.21.0"
DESKTOP_ID = "odoo-companion"
ICON_NAME = "odoo-companion"

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "odoo-companion"
DATA_DIR = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "odoo-companion"

CONFIG_FILE = CONFIG_DIR / "config.json"
STATE_FILE = DATA_DIR / "state.json"

NOTIFICATION_LOG_LIMIT = 30
SERVER_STATUS_INTERVAL_SECONDS = 10 * 60
DEFAULT_POLL_MINUTES = 1.0
# Inbox/chat/call checks run on their own fast cadence regardless of the
# user's dashboard poll-interval setting, so chat messages and calls arrive
# close to real time. Default is short (5s) since the user explicitly wants
# message replies/notifications to feel instant; it's still configurable via
# config["notification_poll_seconds"] for slower connections/servers.
DEFAULT_NOTIFICATION_POLL_SECONDS = 5
MIN_NOTIFICATION_POLL_SECONDS = 3
# How often checkpoint_task_timer() nags the user that a running task timer
# is still going - the extension hardcoded 15 min, the desktop app makes it
# user-configurable (default 10 min, per explicit user request).
DEFAULT_TIMER_REMINDER_MINUTES = 10
# Grace period after the scheduled work-day start before warning that the user
# has not checked in (and isn't on leave / it isn't a holiday). User-configurable.
DEFAULT_ATTENDANCE_GRACE_MINUTES = 15

MUTE_LABELS = {
    "inbox": "Inbox (mentions, assignments, follower updates)",
    "channels": "Discuss chats and channels",
    "calls": "Discuss voice/video calls",
    "attendance": "No-check-in reminder",
    "crm": "Stale CRM lead reminder",
    "calendar": "Meeting-starting-soon reminder",
    "helpdesk": "Helpdesk SLA reminder",
    "expense": "Expense approval reminder",
    "attendanceEvents": "Employee check-in/check-out alerts",
    "serverStatus": "Server online/offline alerts",
    "timesheetReminder": "Task timer still-running reminder",
}

MODULE_MODELS = {
    "crm": "crm.lead",
    "sale": "sale.order",
    "helpdesk": "helpdesk.ticket",
    "calendar": "calendar.event",
    "stock": "stock.picking",
    "account": "account.move",
    "timesheet": "account.analytic.line",
    "approval": "approval.approver",
    "pos": "pos.session",
    "expense": "hr.expense",
    "project": "project.task",
    "hr": "hr.employee",
    "attendance": "hr.attendance",
    "leave": "hr.leave",
    "purchase": "purchase.order",
    "recruitment": "hr.applicant",
    "zkAttendance": "biometric.device.details",
}

MODULE_LABELS = {
    "crm": "CRM",
    "sale": "Sales",
    "helpdesk": "Helpdesk",
    "calendar": "Calendar",
    "stock": "Inventory",
    "account": "Accounting",
    "timesheet": "Timesheets",
    "approval": "Approvals",
    "pos": "Point of Sale",
    "expense": "Expenses",
    "project": "Project",
    "hr": "HR",
    "attendance": "Attendance",
    "leave": "Time Off",
    "purchase": "Purchase",
    "recruitment": "Recruitment",
    "zkAttendance": "ZK Attendance Suite",
}

UNSAFE_MODELS = {"attendance.summary.analysis"}

DEFAULT_CONFIG = {
    "odoo_url": "",
    "db": "",
    "login": "",
    "poll_minutes": DEFAULT_POLL_MINUTES,
    "notification_poll_seconds": DEFAULT_NOTIFICATION_POLL_SECONDS,
    "timer_reminder_minutes": DEFAULT_TIMER_REMINDER_MINUTES,
    "attendance_grace_minutes": DEFAULT_ATTENDANCE_GRACE_MINUTES,
    "mute": {key: False for key in MUTE_LABELS},
    "uid": None,
    "partner_id": None,
    "employee_id": None,
    "odoo_version": None,
    "module_access": {},
    "autostart_enabled": True,
}

DEFAULT_STATE = {
    "last_message_id": 0,
    "inbox_baseline_done": False,
    "last_channel_message_id": 0,
    "channel_baseline_done": False,
    "active_call_channels": [],
    "notification_log": [],
    "server_status": None,
    "server_status_checked_at": None,
    "attendance_reminder_date": None,
    "attendance_reminder_at": None,
    "stale_lead_reminders": {},
    "reminded_meeting_ids": [],
    "helpdesk_sla_reminders": {},
    "last_attendance_poll_at": None,
    "notified_attendance_ids": [],
    "active_timer": None,
    "last_error": None,
    "last_poll_at": None,
    "last_notification_poll_at": None,
    "attendance_self": None,
}

import os
import shutil
import subprocess
from pathlib import Path


SERVICE_NAME = "odoo-companion.service"
APP_DESKTOP_FILE = Path("/usr/share/applications/odoo-companion.desktop")

# Autostart is handled exclusively through the systemd --user unit. Earlier
# builds also shipped a system-wide XDG autostart .desktop entry that
# launched the same binary; both could win the service's startup lock at
# slightly different points during a cold boot, leaving an unsupervised
# orphan process outside systemd's Restart= handling — the root cause of
# "no data/notifications after a reboot" reports. _purge_stale_autostart_files
# clears leftovers from those older installs.


def _config_home():
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))


def _run(args):
    if not shutil.which(args[0]):
        return None
    return subprocess.run(args, capture_output=True, text=True, check=False)


def _error_text(result):
    if result is None or result.returncode == 0:
        return None
    return (result.stderr or result.stdout or "").strip() or f"{result.args[0]} exited with {result.returncode}"


def purge_stale_autostart_overrides():
    for path in (
        _config_home() / "autostart" / "odoo-companion-service.desktop",
        Path("/etc/xdg/autostart/odoo-companion-service.desktop"),
    ):
        try:
            path.unlink()
        except (FileNotFoundError, PermissionError):
            pass


def set_autostart_enabled(enabled):
    purge_stale_autostart_overrides()
    errors = []
    if enabled:
        commands = [
            ["systemctl", "--user", "daemon-reload"],
            ["systemctl", "--user", "enable", "--now", SERVICE_NAME],
        ]
    else:
        commands = [
            ["systemctl", "--user", "disable", "--now", SERVICE_NAME],
        ]

    for command in commands:
        error = _error_text(_run(command))
        if error:
            errors.append(error)
    return errors


def restart_background_service():
    errors = []
    for command in (
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "restart", SERVICE_NAME],
    ):
        error = _error_text(_run(command))
        if error:
            errors.append(error)
    return errors


def user_desktop_dir():
    result = _run(["xdg-user-dir", "DESKTOP"])
    if result and result.returncode == 0:
        value = result.stdout.strip()
        if value:
            return Path(value)
    return Path.home() / "Desktop"


def create_desktop_shortcut():
    desktop_dir = user_desktop_dir()
    desktop_dir.mkdir(parents=True, exist_ok=True)
    shortcut = desktop_dir / "Odoo Companion.desktop"
    shutil.copyfile(APP_DESKTOP_FILE, shortcut)
    shortcut.chmod(0o755)
    _run(["gio", "set", "-t", "string", str(shortcut), "metadata::trusted", "true"])
    return shortcut

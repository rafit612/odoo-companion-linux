"""Desktop user-idle detection used by the task-timer safeguard."""
import ctypes
import os
import re
import shutil
import subprocess


def _run(args):
    if not shutil.which(args[0]):
        return None
    return subprocess.run(args, capture_output=True, text=True, timeout=2, check=False)


def _last_number(text):
    numbers = re.findall(r"\b\d+\b", text or "")
    if not numbers:
        return None
    return int(numbers[-1])


def _idle_from_gnome():
    result = _run(
        [
            "gdbus",
            "call",
            "--session",
            "--dest",
            "org.gnome.Mutter.IdleMonitor",
            "--object-path",
            "/org/gnome/Mutter/IdleMonitor/Core",
            "--method",
            "org.gnome.Mutter.IdleMonitor.GetIdletime",
        ]
    )
    if not result or result.returncode != 0:
        return None
    idle_ms = _last_number(result.stdout)
    return None if idle_ms is None else idle_ms / 1000.0


def _idle_from_qdbus():
    commands = (
        ["qdbus", "org.freedesktop.ScreenSaver", "/ScreenSaver", "org.freedesktop.ScreenSaver.GetSessionIdleTime"],
        ["qdbus", "org.kde.screensaver", "/ScreenSaver", "org.freedesktop.ScreenSaver.GetSessionIdleTime"],
    )
    for command in commands:
        result = _run(command)
        if result and result.returncode == 0:
            idle_ms = _last_number(result.stdout)
            if idle_ms is not None:
                return idle_ms / 1000.0
    return None


def _idle_from_xprintidle():
    result = _run(["xprintidle"])
    if not result or result.returncode != 0:
        return None
    idle_ms = _last_number(result.stdout)
    return None if idle_ms is None else idle_ms / 1000.0


class XScreenSaverInfo(ctypes.Structure):
    _fields_ = [
        ("window", ctypes.c_ulong),
        ("state", ctypes.c_int),
        ("kind", ctypes.c_int),
        ("since", ctypes.c_ulong),
        ("idle", ctypes.c_ulong),
        ("event_mask", ctypes.c_ulong),
    ]


def _idle_from_xscreensaver():
    display_name = os.environ.get("DISPLAY")
    if not display_name:
        return None
    try:
        x11 = ctypes.cdll.LoadLibrary("libX11.so.6")
        xss = ctypes.cdll.LoadLibrary("libXss.so.1")
    except OSError:
        return None

    x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
    x11.XOpenDisplay.restype = ctypes.c_void_p
    x11.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
    x11.XDefaultRootWindow.restype = ctypes.c_ulong
    x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
    x11.XFree.argtypes = [ctypes.c_void_p]
    xss.XScreenSaverAllocInfo.restype = ctypes.POINTER(XScreenSaverInfo)
    xss.XScreenSaverQueryInfo.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(XScreenSaverInfo)]
    xss.XScreenSaverQueryInfo.restype = ctypes.c_int

    display = x11.XOpenDisplay(display_name.encode("utf-8"))
    if not display:
        return None
    info = xss.XScreenSaverAllocInfo()
    try:
        root = x11.XDefaultRootWindow(display)
        if not info or not xss.XScreenSaverQueryInfo(display, root, info):
            return None
        return max(0.0, float(info.contents.idle) / 1000.0)
    finally:
        if info:
            try:
                x11.XFree(ctypes.cast(info, ctypes.c_void_p))
            except Exception:
                pass
        x11.XCloseDisplay(display)


def get_user_idle_seconds():
    """Return seconds since the last keyboard/mouse input, or None if unknown."""
    for detector in (_idle_from_gnome, _idle_from_qdbus, _idle_from_xprintidle, _idle_from_xscreensaver):
        try:
            idle_seconds = detector()
        except Exception:
            idle_seconds = None
        if idle_seconds is not None:
            return max(0.0, idle_seconds)
    return None

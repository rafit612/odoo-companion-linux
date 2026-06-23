import copy
import fcntl
import json
import os
from contextlib import contextmanager
from pathlib import Path

from .constants import CONFIG_FILE, DEFAULT_CONFIG, DEFAULT_STATE, STATE_FILE


def _deep_merge(defaults, value):
    if isinstance(defaults, dict):
        result = copy.deepcopy(defaults)
        if isinstance(value, dict):
            for key, item in value.items():
                result[key] = _deep_merge(defaults.get(key), item) if key in defaults else item
        return result
    return copy.deepcopy(value if value is not None else defaults)


class JsonStore:
    def __init__(self, path: Path, defaults: dict):
        self.path = path
        self.defaults = defaults
        self.lock_path = path.with_suffix(path.suffix + ".lock")

    def ensure_parent(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def locked(self):
        self.ensure_parent()
        with self.lock_path.open("a", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def read_unlocked(self):
        if not self.path.exists():
            return copy.deepcopy(self.defaults)
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError):
            data = {}
        return _deep_merge(self.defaults, data)

    def write_unlocked(self, data):
        self.ensure_parent()
        tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        with tmp_path.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp_path, self.path)

    def read(self):
        with self.locked():
            return self.read_unlocked()

    def write(self, data):
        with self.locked():
            self.write_unlocked(_deep_merge(self.defaults, data))

    def update(self, callback):
        with self.locked():
            data = self.read_unlocked()
            result = callback(data)
            self.write_unlocked(data)
            return result


config_store = JsonStore(CONFIG_FILE, DEFAULT_CONFIG)
state_store = JsonStore(STATE_FILE, DEFAULT_STATE)


def reset_cached_identity():
    def update(config):
        config["uid"] = None
        config["partner_id"] = None
        config["employee_id"] = None
        config["odoo_version"] = None

    config_store.update(update)

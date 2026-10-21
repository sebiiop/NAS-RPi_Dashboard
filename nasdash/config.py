"""Configuration: defaults, loading, saving and password hashing."""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import os
import secrets
import threading
from pathlib import Path

DEFAULT_PATH = Path(os.environ.get("NASDASH_CONFIG", "/etc/nasdash/config.json"))

# Built-in profiles cannot be deleted from the UI (but can be edited).
BUILTIN_PROFILES = {"silent", "balanced", "performance", "full"}

DEFAULT_CONFIG: dict = {
    "server": {"host": "0.0.0.0", "port": 8080},
    "auth": {"user": "admin", "hash": "", "secret": ""},  # empty hash = no login (not recommended)
    "control": {
        # Which temperature drives the fan: drives_max | drives_avg | cpu
        "source": "drives_max",
        "interval_s": 5,          # fan update interval
        "drive_poll_s": 60,       # SMART poll interval (keep this slow)
        "spindown_min": 20,       # spin hard drives down after this many idle minutes (0 = never)
        "ramp_step": 10,          # max fan % change per interval
        "idle_percent": 20,       # used when every drive is spun down
        "failsafe_percent": 100,  # used on sensor errors and on exit
        "stale_after_s": 300,     # drive readings older than this are ignored
        "history_hours": 6,
    },
    "active_profile": "balanced",
    "profiles": {
        "silent": {
            "name": "Silent",
            "mode": "curve",
            "points": [[30, 20], [38, 30], [44, 55], [50, 100]],
            "hysteresis": 2,
        },
        "balanced": {
            "name": "Balanced",
            "mode": "curve",
            "points": [[30, 25], [35, 35], [40, 55], [45, 80], [48, 100]],
            "hysteresis": 2,
        },
        "performance": {
            "name": "Performance",
            "mode": "curve",
            "points": [[25, 40], [35, 60], [40, 80], [44, 100]],
            "hysteresis": 1,
        },
        "full": {"name": "Full speed", "mode": "fixed", "percent": 100},
        "pid_38": {
            "name": "PID · hold 38 °C",
            "mode": "pid",
            "target": 38,
            "kp": 8.0,
            "ki": 0.15,
            "kd": 0.0,
            "min": 20,
            "max": 100,
        },
    },
}


def hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000)
    return f"pbkdf2${salt}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, salt, _ = stored.split("$")
    except ValueError:
        return False
    return hmac.compare_digest(hash_password(password, salt), stored)


def _merge(defaults: dict, loaded: dict) -> dict:
    out = copy.deepcopy(defaults)
    for key, value in loaded.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict) and key != "profiles":
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def validate_profile(p: dict) -> dict:
    """Check and normalise a profile coming from the UI. Raises ValueError."""
    mode = p.get("mode")
    name = str(p.get("name", "")).strip()[:40] or "Custom"
    if mode == "curve":
        pts = sorted([[float(t), float(s)] for t, s in p.get("points", [])])
        if len(pts) < 2:
            raise ValueError("A curve needs at least 2 points")
        for t, s in pts:
            if not (0 <= t <= 90) or not (0 <= s <= 100):
                raise ValueError("Points must be 0–90 °C and 0–100 %")
        return {
            "name": name,
            "mode": "curve",
            "points": pts,
            "hysteresis": max(0.0, min(10.0, float(p.get("hysteresis", 2)))),
        }
    if mode == "fixed":
        return {"name": name, "mode": "fixed", "percent": max(0, min(100, int(p.get("percent", 100))))}
    if mode == "pid":
        lo = max(0, min(100, int(p.get("min", 20))))
        hi = max(lo, min(100, int(p.get("max", 100))))
        return {
            "name": name,
            "mode": "pid",
            "target": max(20.0, min(60.0, float(p.get("target", 38)))),
            "kp": max(0.0, float(p.get("kp", 8))),
            "ki": max(0.0, float(p.get("ki", 0.15))),
            "kd": max(0.0, float(p.get("kd", 0))),
            "min": lo,
            "max": hi,
        }
    raise ValueError(f"Unknown mode: {mode!r}")


class Config:
    """Thread-safe config holder that writes changes back to disk."""

    def __init__(self, path: Path = DEFAULT_PATH):
        self.path = Path(path)
        self._lock = threading.RLock()
        loaded = {}
        if self.path.exists():
            loaded = json.loads(self.path.read_text())
        self.data = _merge(DEFAULT_CONFIG, loaded)
        if self.data["active_profile"] not in self.data["profiles"]:
            self.data["active_profile"] = "balanced"

    def get(self) -> dict:
        with self._lock:
            return copy.deepcopy(self.data)

    def _disk_auth(self) -> dict | None:
        try:
            return json.loads(self.path.read_text()).get("auth")
        except Exception:
            return None

    def auth(self) -> dict:
        """Login settings, re-read from disk so `nasdash.setpass` works without a restart."""
        with self._lock:
            try:
                mtime = self.path.stat().st_mtime
            except OSError:
                mtime = None
            if mtime != getattr(self, "_auth_mtime", None):
                self._auth_mtime = mtime
                disk = self._disk_auth()
                if disk is not None:
                    self.data["auth"] = _merge(DEFAULT_CONFIG["auth"], disk)
            return dict(self.data["auth"])

    def update(self, fn, keep_disk_auth: bool = True) -> dict:
        """Apply fn(data) under the lock, then save.

        By default the login stored on disk wins, so a running service never
        overwrites a password that was just set with `nasdash.setpass`."""
        with self._lock:
            if keep_disk_auth:
                self.auth()
            fn(self.data)
            self.save()
            return copy.deepcopy(self.data)

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            owner = None
            if self.path.exists():
                st = self.path.stat()
                owner = (st.st_uid, st.st_gid)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, indent=2))
            os.chmod(tmp, 0o600)
            if owner and os.geteuid() == 0:
                os.chown(tmp, *owner)  # keep the service user as owner when root edits
            tmp.replace(self.path)
            try:
                self._auth_mtime = self.path.stat().st_mtime
            except OSError:
                pass

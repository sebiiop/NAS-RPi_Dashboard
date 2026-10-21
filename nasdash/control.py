"""Fan control logic: curves with hysteresis, PID, fail-safe, history."""

from __future__ import annotations

import logging
import threading
import time
from collections import deque

log = logging.getLogger("nasdash.control")


def interpolate(points: list[list[float]], temp: float) -> float:
    """Linear interpolation on a sorted list of [temp, percent] points."""
    pts = sorted(points)
    if temp <= pts[0][0]:
        return pts[0][1]
    if temp >= pts[-1][0]:
        return pts[-1][1]
    for (t0, s0), (t1, s1) in zip(pts, pts[1:]):
        if t0 <= temp <= t1:
            if t1 == t0:
                return s1
            return s0 + (s1 - s0) * (temp - t0) / (t1 - t0)
    return pts[-1][1]


class CurveState:
    """Hysteresis: the fan speeds up right away, but only slows down once the
    temperature has dropped `hysteresis` degrees below where it was."""

    def __init__(self):
        self.effective = None

    def step(self, temp: float, profile: dict) -> float:
        h = float(profile.get("hysteresis", 0))
        if self.effective is None:
            self.effective = temp
        self.effective = min(max(self.effective, temp), temp + h)
        return interpolate(profile["points"], self.effective)


class PIDState:
    def __init__(self):
        self.integral = 0.0
        self.prev_error = None
        self.prev_time = None

    def step(self, temp: float, p: dict) -> float:
        now = time.monotonic()
        dt = (now - self.prev_time) if self.prev_time else 0.0
        error = temp - p["target"]  # positive = too hot
        deriv = (error - self.prev_error) / dt if (dt > 0 and self.prev_error is not None) else 0.0
        lo, hi = p["min"], p["max"]
        base = lo
        out = base + p["kp"] * error + p["ki"] * self.integral + p["kd"] * deriv
        # Anti-windup: only integrate while not saturated in the same direction.
        if dt > 0 and not ((out >= hi and error > 0) or (out <= lo and error < 0)):
            self.integral += error * dt
            if p["ki"] > 0:
                limit = (hi - lo) / p["ki"]
                self.integral = max(-limit, min(limit, self.integral))
        self.prev_error, self.prev_time = error, now
        out = base + p["kp"] * error + p["ki"] * self.integral + p["kd"] * deriv
        return max(lo, min(hi, out))


class Controller:
    def __init__(self, config, fan, drives, system):
        self.config = config
        self.fan = fan
        self.drives = drives
        self.system = system
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.poll_now = threading.Event()

        self.drive_data: list[dict] = []
        self.drive_polled = 0.0
        self.sys_data: dict = {}
        self.fan_data: dict = {}
        self.status = {"temp": None, "target": None, "reason": "starting", "profile": None}
        self.override: dict | None = None  # {"percent": int, "until": ts}
        self.history: deque = deque()
        self.last_io: dict[str, float] = {}      # drive name -> time of last real read/write
        self._io_counts: dict[str, tuple] = {}
        self._standby_sent: dict[str, float] = {}
        self.spin_downs: deque = deque(maxlen=500)  # (time, name)
        self._states: dict[str, object] = {}
        self._profile_key = None
        self._thread = threading.Thread(target=self._drive_loop, daemon=True, name="drive-poll")
        self._thread2 = threading.Thread(target=self._loop, daemon=True, name="fan-control")

    # ------------------------------------------------------------------ lifecycle
    def start(self):
        self._thread.start()
        self._thread2.start()

    def stop(self):
        self.stop_event.set()
        self.poll_now.set()
        self._thread2.join(timeout=10)
        self.failsafe("service stopped")

    def failsafe(self, reason: str):
        pct = self.config.get()["control"]["failsafe_percent"]
        try:
            self.fan.set_percent(pct)
            log.warning("Fail-safe: fan at %s%% (%s)", pct, reason)
        except Exception:
            log.exception("Could not set fail-safe fan speed")

    # ------------------------------------------------------------------ drives
    def _drive_loop(self):
        """Every 30 s: note which drives had real I/O and spin down idle ones.
        Every drive_poll_s: read SMART data (sleeping drives are skipped)."""
        next_poll = 0.0
        while not self.stop_event.is_set():
            c = self.config.get()["control"]
            now = time.time()
            self._track_activity(now)
            if now >= next_poll or self.poll_now.is_set():
                self.poll_now.clear()
                try:
                    data = self.drives.poll()
                    with self.lock:
                        self.drive_data = data
                        self.drive_polled = time.time()
                except Exception:
                    log.exception("Drive poll failed")
                next_poll = time.time() + max(10, c["drive_poll_s"])
            if c.get("spindown_min", 0) > 0:
                self._auto_spin_down(c["spindown_min"] * 60)
            self.poll_now.wait(timeout=max(1, min(30, next_poll - time.time())))

    def _track_activity(self, now: float):
        try:
            counts = self.drives.activity()
        except Exception:
            log.exception("Reading disk activity failed")
            return
        with self.lock:
            for name, cnt in counts.items():
                if self._io_counts.get(name) != cnt:
                    self.last_io[name] = now
                    self._io_counts[name] = cnt

    def _auto_spin_down(self, after_s: float):
        now = time.time()
        with self.lock:
            candidates = [
                d for d in self.drive_data
                if d.get("rotational") and d.get("state") == "active"
                and d["name"] in self.last_io
                and now - self.last_io[d["name"]] >= after_s
                # only once per idle period, even if hdparm failed
                and self._standby_sent.get(d["name"]) != self.last_io[d["name"]]
            ]
        for d in candidates:
            self.spin_down(d["name"], reason=f"idle for {int((now - self.last_io[d['name']]) / 60)} min")

    def spin_down(self, name: str, reason: str = "manual") -> str | None:
        with self.lock:
            drive = next((d for d in self.drive_data if d["name"] == name), None)
        if drive is None:
            return "unknown drive"
        err = self.drives.spin_down(drive["dev"])
        with self.lock:
            self._standby_sent[name] = self.last_io.get(name)
            if err:
                log.warning("Could not spin down %s: %s", name, err)
                drive["spindown_error"] = err
            else:
                log.info("Spun down %s (%s)", name, reason)
                drive["state"] = "standby"
                drive.pop("spindown_error", None)
                self.spin_downs.append((time.time(), name))
        return err

    # ------------------------------------------------------------------ control
    def _source_temp(self, cfg: dict, drives: list[dict]):
        """Return (temp or None, reason)."""
        c = cfg["control"]
        if c["source"] == "cpu":
            t = self.sys_data.get("cpu_temp")
            return (t, "CPU temperature") if t is not None else (None, "error: no CPU temperature")
        fresh = [
            d["temp"]
            for d in drives
            if d.get("state") == "active" and d.get("temp") is not None and time.time() - d.get("updated", 0) < c["stale_after_s"]
        ]
        if fresh:
            if c["source"] == "drives_avg":
                return sum(fresh) / len(fresh), f"average of {len(fresh)} drive(s)"
            return max(fresh), f"hottest of {len(fresh)} drive(s)"
        if drives and all(d.get("state") == "standby" for d in drives):
            return None, "idle: all drives spun down"
        if not drives and time.time() - self.drive_polled > 30 and self.drive_polled:
            return None, "error: no drives found"
        if not self.drive_polled:
            return None, "waiting for first drive reading"
        return None, "error: no drive temperature available"

    def _compute(self, cfg: dict, temp):
        c = cfg["control"]
        key = cfg["active_profile"]
        profile = cfg["profiles"].get(key) or cfg["profiles"]["balanced"]
        if key != self._profile_key:
            self._states = {}
            self._profile_key = key

        if self.override and self.override["until"] > time.time():
            return self.override["percent"], "manual override"
        self.override = None

        if profile["mode"] == "fixed":
            return profile["percent"], None
        if temp is None:
            return None, None
        if profile["mode"] == "curve":
            st = self._states.setdefault("curve", CurveState())
            return st.step(temp, profile), None
        if profile["mode"] == "pid":
            st = self._states.setdefault("pid", PIDState())
            return st.step(temp, profile), None
        return c["failsafe_percent"], "unknown profile mode"

    def _loop(self):
        last = None
        while not self.stop_event.is_set():
            cfg = self.config.get()
            c = cfg["control"]
            try:
                self.sys_data = self.system.read()
            except Exception:
                log.exception("System read failed")
            with self.lock:
                drives = list(self.drive_data)
            try:
                temp, reason = self._source_temp(cfg, drives)
                target, why = self._compute(cfg, temp)
                if why:
                    reason = why
                if target is None:
                    if reason.startswith("idle"):
                        target = c["idle_percent"]
                    elif reason.startswith("waiting"):
                        target = c["failsafe_percent"] if last is None else last
                    else:
                        target = c["failsafe_percent"]
                        reason = reason + " → fail-safe"
                # Ramp limit, except when heading to fail-safe or override.
                if last is not None and "fail-safe" not in reason and reason != "manual override":
                    step = c["ramp_step"]
                    target = max(last - step, min(last + step, target))
                target = round(float(target), 1)
                self.fan.set_percent(target)
                last = target
                fan = self.fan.read()
            except Exception as exc:
                log.exception("Control step failed")
                self.failsafe(str(exc))
                temp, target, reason = None, c["failsafe_percent"], f"error: {exc} → fail-safe"
                try:
                    fan = self.fan.read()
                except Exception:
                    fan = {}

            now = time.time()
            with self.lock:
                self.fan_data = fan
                self.status = {
                    "temp": round(temp, 1) if temp is not None else None,
                    "target": target,
                    "reason": reason,
                    "profile": cfg["active_profile"],
                    "override": self.override,
                    "time": now,
                }
                self.history.append(
                    {
                        "t": now,
                        "fan": target,
                        "rpm": fan.get("rpm"),
                        "temp": self.status["temp"],
                        "cpu": self.sys_data.get("cpu_temp"),
                        "load": self.sys_data.get("cpu_percent"),
                        "mem": (self.sys_data.get("mem") or {}).get("percent"),
                        "rx": (self.sys_data.get("net") or {}).get("rx_rate"),
                        "tx": (self.sys_data.get("net") or {}).get("tx_rate"),
                        "drives": {d["name"]: d.get("temp") if d.get("state") == "active" else None for d in drives},
                        "io": self._io_rates(now, [d["name"] for d in drives]),
                    }
                )
                keep = now - c["history_hours"] * 3600
                while self.history and self.history[0]["t"] < keep:
                    self.history.popleft()
            self.stop_event.wait(timeout=max(1, c["interval_s"]))

    def _io_rates(self, now: float, names: list[str]) -> dict[str, list]:
        """Bytes/s read and written per drive since the previous sample."""
        try:
            cur = self.drives.io_bytes()
        except Exception:
            log.exception("Reading disk I/O counters failed")
            return {}
        prev_t, prev = getattr(self, "_io_prev", (None, {}))
        self._io_prev = (now, cur)
        if prev_t is None or now <= prev_t:
            return {}
        dt = now - prev_t
        out = {}
        for n in names:
            if n in cur and n in prev:
                r = max(0, cur[n][0] - prev[n][0]) / dt
                w = max(0, cur[n][1] - prev[n][1]) / dt
                out[n] = [round(r), round(w)]
        return out

    # ------------------------------------------------------------------ public
    def set_override(self, percent, minutes):
        with self.lock:
            if percent is None:
                self.override = None
            else:
                self.override = {"percent": max(0, min(100, int(percent))), "until": time.time() + max(1, int(minutes)) * 60}

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "status": dict(self.status),
                "fan": dict(self.fan_data),
                "drives": [self._drive_view(d) for d in self.drive_data],
                "drives_polled": self.drive_polled,
                "system": dict(self.sys_data),
            }

    def _drive_view(self, d: dict) -> dict:
        v = dict(d)
        now = time.time()
        last = self.last_io.get(d["name"])
        v["idle_s"] = round(now - last) if last is not None else None
        v["spindowns_24h"] = sum(1 for t, n in self.spin_downs if n == d["name"] and t > now - 86400)
        return v

    def get_history(self, minutes: int, max_points: int = 600) -> list[dict]:
        cutoff = time.time() - minutes * 60
        with self.lock:
            rows = [r for r in self.history if r["t"] >= cutoff]
        if len(rows) > max_points:
            step = len(rows) / max_points
            rows = [rows[int(i * step)] for i in range(max_points)]
        return rows

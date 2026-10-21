"""Hardware access: EMC2101 fan controller, SMART drive data, system info.

Every class has a mock variant so the dashboard can be developed and tested
without a Pi (start with --mock).
"""

from __future__ import annotations

import json
import math
import os
import platform
import random
import shutil
import socket
import subprocess
import time
from pathlib import Path

import psutil

# --------------------------------------------------------------------------- fan


class Fan:
    """EMC2101 in manual PWM mode."""

    def __init__(self):
        import board  # from adafruit-blinka
        from adafruit_emc2101 import EMC2101

        self.emc = EMC2101(board.I2C())
        # Make sure the chip's own lookup table is off, so manual_fan_speed
        # controls the fan. The base EMC2101 class only lets you *read*
        # lut_enabled, so write the underlying register bit directly
        # (FAN_CONFIG bit 5: 1 = fan setting register in control = manual).
        if self.emc.lut_enabled:
            self.emc._fan_lut_prog = True
        self.percent = None

    def set_percent(self, percent: float) -> None:
        percent = max(0.0, min(100.0, float(percent)))
        self.emc.manual_fan_speed = percent
        self.percent = percent

    def read(self) -> dict:
        rpm = None
        try:
            rpm = int(self.emc.fan_speed)
        except Exception:  # no tach signal / stalled
            rpm = None
        chip = None
        try:
            chip = float(self.emc.internal_temperature)
        except Exception:
            pass
        return {"percent": self.percent, "rpm": rpm, "chip_temp": chip}


class BrokenFan:
    """Stands in when the EMC2101 can't be opened, so the dashboard still runs
    and shows the error instead of the service crash-looping."""

    def __init__(self, error: str):
        self.error = error
        self.percent = None

    def set_percent(self, percent: float) -> None:
        raise RuntimeError(f"fan controller unavailable: {self.error}")

    def read(self) -> dict:
        return {"percent": None, "rpm": None, "chip_temp": None, "error": self.error}


class MockFan:
    def __init__(self):
        self.percent = None
        self._rpm = 0.0

    def set_percent(self, percent: float) -> None:
        self.percent = max(0.0, min(100.0, float(percent)))

    def read(self) -> dict:
        target = 300 + 1500 * (self.percent or 0) / 100 if self.percent else 0
        self._rpm += (target - self._rpm) * 0.5
        return {"percent": self.percent, "rpm": int(self._rpm), "chip_temp": 31.0 + random.random()}


# ------------------------------------------------------------------------ drives


def _run(cmd: list[str], timeout: int = 20) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def list_disks() -> list[dict]:
    """Physical disks (SATA/NVMe/USB), excluding SD card, loop and zram."""
    try:
        out = _run(["lsblk", "-d", "-J", "-b", "-o", "NAME,MODEL,SERIAL,SIZE,TYPE,TRAN,ROTA"]).stdout
        devs = json.loads(out)["blockdevices"]
    except Exception:
        return []
    disks = []
    for d in devs:
        name = d.get("name", "")
        if d.get("type") != "disk" or name.startswith(("mmcblk", "zram", "loop", "ram")):
            continue
        disks.append(
            {
                "dev": f"/dev/{name}",
                "name": name,
                "model": (d.get("model") or "").strip(),
                "serial": (d.get("serial") or "").strip(),
                "size": int(d.get("size") or 0),
                "rotational": bool(int(d.get("rota") or 0)) if str(d.get("rota")).isdigit() else bool(d.get("rota")),
                "transport": d.get("tran"),
            }
        )
    return disks


def read_diskstats() -> dict[str, tuple]:
    """I/O counters per whole disk from /proc/diskstats.

    Only real reads/writes/discards/flushes are counted. SMART queries and
    hdparm commands are "passthrough" requests, which the kernel doesn't
    count here, so they never make a drive look busy."""
    out = {}
    try:
        lines = Path("/proc/diskstats").read_text().splitlines()
    except OSError:
        return out
    for line in lines:
        f = line.split()
        if len(f) < 14:
            continue
        name = f[2]
        # fields: reads completed (3), writes completed (7), discards (14), flushes (18)
        counts = (f[3], f[7], f[14] if len(f) > 14 else "0", f[18] if len(f) > 18 else "0")
        out[name] = counts
    return out


def read_disk_bytes() -> dict[str, tuple[int, int]]:
    """Total bytes read and written per disk since boot, from /proc/diskstats.
    The kernel always counts these in 512-byte sectors."""
    out = {}
    try:
        lines = Path("/proc/diskstats").read_text().splitlines()
    except OSError:
        return out
    for line in lines:
        f = line.split()
        if len(f) < 10:
            continue
        out[f[2]] = (int(f[5]) * 512, int(f[9]) * 512)
    return out


def _smart_attr(data: dict, attr_id: int):
    for a in data.get("ata_smart_attributes", {}).get("table", []):
        if a.get("id") == attr_id:
            return a.get("raw", {}).get("value")
    return None


class Drives:
    """Reads SMART data with smartctl, never waking sleeping drives."""

    def __init__(self, use_sudo: bool | None = None):
        if use_sudo is None:
            use_sudo = os.geteuid() != 0
        self.prefix = ["sudo", "-n"] if use_sudo else []
        # Fixed path: it has to match the sudoers rule exactly.
        self.smartctl = "/usr/sbin/smartctl" if os.path.exists("/usr/sbin/smartctl") else (shutil.which("smartctl") or "smartctl")
        self.hdparm = "/usr/sbin/hdparm" if os.path.exists("/usr/sbin/hdparm") else (shutil.which("hdparm") or "hdparm")

    def activity(self) -> dict[str, tuple]:
        return read_diskstats()

    def io_bytes(self) -> dict[str, tuple[int, int]]:
        return read_disk_bytes()

    def spin_down(self, dev: str) -> str | None:
        """Put a drive into standby now. Returns an error message, or None."""
        try:
            proc = _run(self.prefix + [self.hdparm, "-y", dev], timeout=30)
        except Exception as exc:
            return str(exc)
        if proc.returncode != 0:
            return (proc.stderr.strip() or proc.stdout.strip() or f"hdparm exit {proc.returncode}")[:200]
        return None

    def poll(self) -> list[dict]:
        results = []
        for disk in list_disks():
            info = dict(disk)
            info.update(self._smart(disk["dev"]))
            info["updated"] = time.time()
            results.append(info)
        return results

    def _smart(self, dev: str) -> dict:
        cmd = self.prefix + [self.smartctl, "-n", "standby,3", "-j", "-i", "-A", "-H", dev]
        try:
            proc = _run(cmd)
        except Exception as exc:
            return {"state": "error", "error": str(exc)}
        try:
            data = json.loads(proc.stdout or "{}")
        except json.JSONDecodeError:
            data = {}
        msgs = " ".join(m.get("string", "") for m in data.get("smartctl", {}).get("messages", []))
        if proc.returncode == 3 or "STANDBY" in msgs.upper() or "SLEEP" in msgs.upper():
            return {"state": "standby"}
        temp = data.get("temperature", {}).get("current")
        if temp is None:
            err = msgs or proc.stderr.strip() or f"smartctl exit {proc.returncode}"
            return {"state": "error", "error": err[:200]}
        hours = data.get("power_on_time", {}).get("hours")
        return {
            "state": "active",
            "temp": float(temp),
            "health": data.get("smart_status", {}).get("passed"),
            "model": data.get("model_name") or None,
            "serial": data.get("serial_number") or None,
            "power_on_hours": hours,
            "power_cycles": data.get("power_cycle_count"),
            "reallocated": _smart_attr(data, 5),
            "pending": _smart_attr(data, 197),
            "uncorrectable": _smart_attr(data, 198),
            "rpm": data.get("rotation_rate"),
        }


class MockDrives:
    MODELS = [
        ("sda", "WDC WD40EFPX-68C6CN0", 4_000_787_030_016),
        ("sdb", "WDC WD40EFPX-68C6CN0", 4_000_787_030_016),
        ("sdc", "ST4000VN006-3CW104", 4_000_787_030_016),
        ("sdd", "Samsung SSD 870 EVO 1TB", 1_000_204_886_016),
    ]

    def __init__(self):
        self.t0 = time.time()
        self.standby: set[str] = set()
        self.io = {name: 0 for name, _, _ in self.MODELS}

    def activity(self) -> dict[str, tuple]:
        # sda is always busy (like a drive something keeps writing to), the others are quiet.
        self.io["sda"] += 1
        self.standby.discard("sda")
        return {name: (n, 0, 0, 0) for name, n in self.io.items()}

    def spin_down(self, dev: str) -> str | None:
        self.standby.add(dev.rsplit("/", 1)[-1])
        return None

    def io_bytes(self) -> dict[str, tuple[int, int]]:
        # Simulated traffic: sdb/sdc receive a backup, sda serves media, sdd is quiet.
        if not hasattr(self, "_io"):
            self._io = {name: [0, 0] for name, _, _ in self.MODELS}
            self._io_t = time.time()
        now = time.time()
        dt = now - self._io_t
        self._io_t = now
        rates = {
            "sda": (random.choice([0, 0, 0, 3e6, 12e6]), random.choice([0, 0, 40e3])),
            "sdb": (random.random() * 2e5, 1.8e6 + random.random() * 6e5),
            "sdc": (random.random() * 2e5, 1.8e6 + random.random() * 6e5),
            "sdd": (0, random.choice([0, 0, 0, 8e3])),
        }
        for name, (r, w) in rates.items():
            if name in self.standby:
                continue
            self._io[name][0] += int(r * dt)
            self._io[name][1] += int(w * dt)
        return {n: tuple(v) for n, v in self._io.items()}

    def poll(self) -> list[dict]:
        out = []
        t = time.time() - self.t0
        for i, (name, model, size) in enumerate(self.MODELS):
            ssd = "SSD" in model
            base = 33 + i * 1.5 + 4 * math.sin(t / 300 + i)
            state = "standby" if name in self.standby else "active"
            d = {
                "dev": f"/dev/{name}",
                "name": name,
                "model": model,
                "serial": f"MOCK{i:04d}",
                "size": size,
                "rotational": not ssd,
                "transport": "sata",
                "state": state,
                "updated": time.time(),
            }
            if state == "active":
                d.update(
                    temp=round(base - (6 if ssd else 0) + random.random(), 1),
                    health=True,
                    power_on_hours=12_000 + i * 900,
                    power_cycles=40 + i,
                    reallocated=0,
                    pending=0,
                    uncorrectable=0,
                    rpm=0 if ssd else 5400,
                )
            out.append(d)
        return out


# ------------------------------------------------------------------------ system


def _read(path: str) -> str | None:
    try:
        return Path(path).read_text().strip("\x00\n ")
    except Exception:
        return None


def cpu_temp() -> float | None:
    raw = _read("/sys/class/thermal/thermal_zone0/temp")
    if raw and raw.lstrip("-").isdigit():
        return int(raw) / 1000
    temps = getattr(psutil, "sensors_temperatures", lambda: {})() or {}
    for entries in temps.values():
        if entries:
            return entries[0].current
    return None


THROTTLE_BITS = {
    0: "Under-voltage now",
    1: "ARM frequency capped now",
    2: "Throttled now",
    3: "Soft temperature limit now",
    16: "Under-voltage has occurred",
    17: "Frequency capping has occurred",
    18: "Throttling has occurred",
    19: "Soft temperature limit has occurred",
}


def throttled() -> dict | None:
    if not shutil.which("vcgencmd"):
        return None
    try:
        out = _run(["vcgencmd", "get_throttled"], timeout=3).stdout.strip()
        value = int(out.split("=")[1], 16)
    except Exception:
        return None
    return {"raw": hex(value), "flags": [txt for bit, txt in THROTTLE_BITS.items() if value & (1 << bit)]}


def _os_name() -> str:
    for line in (_read("/etc/os-release") or "").splitlines():
        if line.startswith("PRETTY_NAME="):
            return line.split("=", 1)[1].strip('"')
    return platform.system()


def _ip_addresses() -> list[str]:
    ips = []
    for iface, addrs in psutil.net_if_addrs().items():
        if iface == "lo" or iface.startswith(("docker", "veth", "br-")):
            continue
        for a in addrs:
            if a.family == socket.AF_INET:
                ips.append(f"{a.address} ({iface})")
    return ips


class System:
    """Collects Pi / OS stats. Static info is cached."""

    def __init__(self):
        self.static = {
            "hostname": socket.gethostname(),
            "model": _read("/proc/device-tree/model") or platform.machine(),
            "os": _os_name(),
            "kernel": platform.release(),
            "arch": platform.machine(),
            "cpu_count": psutil.cpu_count(),
            "boot_time": psutil.boot_time(),
        }
        self._net_prev = (time.time(), psutil.net_io_counters())
        psutil.cpu_percent(percpu=True)  # prime the counter

    def read(self) -> dict:
        now = time.time()
        net = psutil.net_io_counters()
        t_prev, n_prev = self._net_prev
        dt = max(now - t_prev, 1e-3)
        self._net_prev = (now, net)
        vm = psutil.virtual_memory()
        sw = psutil.swap_memory()
        freq = psutil.cpu_freq()

        mounts = []
        seen = set()
        for p in psutil.disk_partitions(all=False):
            if p.device in seen or p.fstype in ("squashfs", "tmpfs", "devtmpfs", "overlay", "vfat") and p.mountpoint != "/":
                continue
            if p.mountpoint.startswith(("/snap", "/boot", "/var/lib/docker")):
                continue
            seen.add(p.device)
            try:
                u = psutil.disk_usage(p.mountpoint)
            except Exception:
                continue
            mounts.append(
                {"device": p.device, "mount": p.mountpoint, "fstype": p.fstype, "total": u.total, "used": u.used, "percent": u.percent}
            )

        return {
            **self.static,
            "ips": _ip_addresses(),
            "uptime_s": now - self.static["boot_time"],
            "load": list(os.getloadavg()),
            "cpu_percent": psutil.cpu_percent(),
            "cpu_per_core": psutil.cpu_percent(percpu=True),
            "cpu_freq_mhz": round(freq.current) if freq else None,
            "cpu_temp": cpu_temp(),
            "throttled": throttled(),
            "mem": {"total": vm.total, "used": vm.total - vm.available, "percent": vm.percent},
            "swap": {"total": sw.total, "used": sw.used, "percent": sw.percent},
            "net": {
                "rx_rate": (net.bytes_recv - n_prev.bytes_recv) / dt,
                "tx_rate": (net.bytes_sent - n_prev.bytes_sent) / dt,
                "rx_total": net.bytes_recv,
                "tx_total": net.bytes_sent,
            },
            "mounts": mounts,
        }


class MockSystem:
    """Simulated Raspberry Pi 5, so demo mode looks like a real NAS
    instead of whatever computer it happens to run on."""

    def __init__(self):
        self.t0 = time.time()
        self.boot = time.time() - 3 * 86400 - 5 * 3600

    def read(self) -> dict:
        t = time.time() - self.t0
        load = max(1.0, 6 + 5 * math.sin(t / 90) + random.random() * 6)
        cores = [max(0.0, min(100.0, load + random.uniform(-4, 6))) for _ in range(4)]
        tb = 1_000_000_000_000
        return {
            "hostname": "nas",
            "model": "Raspberry Pi 5 Model B Rev 1.0",
            "os": "Debian GNU/Linux 13 (trixie)",
            "kernel": "6.12.47+rpt-rpi-2712",
            "arch": "aarch64",
            "cpu_count": 4,
            "boot_time": self.boot,
            "ips": ["192.168.1.50 (eth0)"],
            "uptime_s": time.time() - self.boot,
            "load": [round(load / 25, 2), round(load / 28, 2), round(load / 30, 2)],
            "cpu_percent": round(sum(cores) / 4, 1),
            "cpu_per_core": [round(c, 1) for c in cores],
            "cpu_freq_mhz": 2400 if load > 10 else 1500,
            "cpu_temp": round(46 + load / 4 + random.random(), 1),
            "throttled": {"raw": "0x0", "flags": []},
            "mem": {"total": 8 * 1024**3, "used": int((1.6 + 0.1 * math.sin(t / 300)) * 1024**3), "percent": round(20 + math.sin(t / 300), 1)},
            "swap": {"total": 2 * 1024**3, "used": 0, "percent": 0.0},
            "net": {
                "rx_rate": max(0.0, 40e3 + 30e3 * math.sin(t / 40) + random.random() * 20e3),
                "tx_rate": max(0.0, 2.4e6 + 4e5 * math.sin(t / 60) + random.random() * 2e5),
                "rx_total": int(380e9 + t * 5e4),
                "tx_total": int(1.2e12 + t * 2.4e6),
            },
            "mounts": [
                {"device": "/dev/mmcblk0p2", "mount": "/", "fstype": "ext4", "total": 31 * 10**9, "used": int(8.2 * 10**9), "percent": 28.0},
                {"device": "/dev/sdb", "mount": "/srv/backup", "fstype": "btrfs", "total": int(8.0 * tb), "used": int(3.1 * tb), "percent": 38.8},
                {"device": "/dev/sda1", "mount": "/srv/media", "fstype": "ext4", "total": int(4.0 * tb), "used": int(2.9 * tb), "percent": 72.5},
                {"device": "/dev/sdd1", "mount": "/srv/apps", "fstype": "ext4", "total": int(1.0 * tb), "used": int(0.18 * tb), "percent": 18.0},
            ],
        }

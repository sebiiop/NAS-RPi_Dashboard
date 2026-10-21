"""Web server: REST API + static dashboard."""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import hmac
import logging
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .config import BUILTIN_PROFILES, Config, verify_password, validate_profile
from .control import Controller

log = logging.getLogger("nasdash")
STATIC = Path(__file__).parent / "static"
COOKIE = "nasdash_session"
SESSION_SHORT = 12 * 3600       # without "keep me logged in"
SESSION_LONG = 30 * 24 * 3600   # with it


def create_app(config: Config, mock: bool = False) -> FastAPI:
    if mock:
        from .hardware import MockDrives, MockFan
        from .hardware import MockSystem as System

        fan, drives = MockFan(), MockDrives()
    else:
        from .hardware import BrokenFan, Drives, Fan, System

        try:
            fan = Fan()
        except Exception as exc:
            log.exception("Could not open the EMC2101 fan controller")
            fan = BrokenFan(f"{type(exc).__name__}: {exc}")
        drives = Drives()
    controller = Controller(config, fan, drives, System())
    if mock:
        controller.prefill_demo_history(minutes=60)

    @asynccontextmanager
    async def lifespan(app):
        controller.start()
        yield
        controller.stop()

    app = FastAPI(title="NAS Dashboard", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.controller = controller

    # ------------------------------------------------------------------ auth
    # Sessions are signed cookies: "user|expiry|signature". The signing key
    # includes the password hash, so changing the password logs everyone out.
    def signing_key(a: dict) -> bytes:
        if not a.get("secret"):
            def add_secret(d):
                if not d["auth"].get("secret"):
                    d["auth"]["secret"] = secrets.token_hex(32)

            config.update(add_secret)
            a = config.auth()
        return (a["secret"] + a["hash"]).encode()

    def make_token(a: dict, user: str, ttl: int) -> str:
        body = f"{user}|{int(time.time()) + ttl}"
        sig = hmac.new(signing_key(a), body.encode(), hashlib.sha256).hexdigest()
        return base64.urlsafe_b64encode(f"{body}|{sig}".encode()).decode()

    def valid_token(a: dict, token: str | None) -> bool:
        if not token:
            return False
        try:
            user, exp, sig = base64.urlsafe_b64decode(token.encode()).decode().split("|")
        except Exception:
            return False
        good = hmac.new(signing_key(a), f"{user}|{exp}".encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig, good) and user == a.get("user", "admin") and int(exp) > time.time()

    def logged_in(request: Request) -> bool:
        a = config.auth()
        return not a.get("hash") or valid_token(a, request.cookies.get(COOKIE))

    def auth(request: Request):
        if not logged_in(request):
            raise HTTPException(401, "Login required")
        # CSRF guard: browsers can't send this header cross-site without CORS.
        if request.method not in ("GET", "HEAD") and request.headers.get("x-nasdash") != "1":
            raise HTTPException(403, "Missing X-NasDash header")

    protected = [Depends(auth)]

    @app.post("/api/login")
    async def login(body: dict, request: Request, response: Response):
        if request.headers.get("x-nasdash") != "1":
            raise HTTPException(403, "Missing X-NasDash header")
        a = config.auth()
        if not a.get("hash"):
            return {"ok": True}
        user, pw = str(body.get("username", "")), str(body.get("password", ""))
        ok = secrets.compare_digest(user, a.get("user", "admin")) and verify_password(pw, a["hash"])
        if not ok:
            await asyncio.sleep(1.5)  # slows down password guessing
            raise HTTPException(401, "Wrong username or password")
        remember = bool(body.get("remember"))
        ttl = SESSION_LONG if remember else SESSION_SHORT
        response.set_cookie(
            COOKIE, make_token(a, user, ttl), max_age=ttl if remember else None,
            httponly=True, samesite="strict", secure=request.url.scheme == "https",
        )
        return {"ok": True}

    @app.post("/api/logout")
    def logout(response: Response):
        response.delete_cookie(COOKIE)
        return {"ok": True}

    # ------------------------------------------------------------------ pages
    def page(name: str):
        return FileResponse(STATIC / name, headers={"Cache-Control": "no-cache"})

    @app.get("/", include_in_schema=False)
    def index(request: Request):
        return page("index.html") if logged_in(request) else RedirectResponse("/login", 303)

    @app.get("/login", include_in_schema=False)
    def login_page(request: Request):
        if not config.auth().get("hash") or logged_in(request):
            return RedirectResponse("/", 303)
        return page("login.html")

    # Static assets are harmless (no data), so they're served without login.
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    # Make browsers re-check the page's files on every load (a cheap 304 when
    # nothing changed), so an update is never hidden behind an old cached copy.
    @app.middleware("http")
    async def no_stale_assets(request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    # ------------------------------------------------------------------ read
    @app.get("/api/status", dependencies=protected)
    def status():
        snap = controller.snapshot()
        snap["auth_enabled"] = bool(config.auth().get("hash"))
        snap["mock"] = mock
        snap["version"] = __version__
        snap["spindown_min"] = config.get()["control"].get("spindown_min", 0)
        return snap

    @app.get("/api/history", dependencies=protected)
    def history(minutes: int = 60):
        return controller.get_history(max(1, min(minutes, 24 * 60)))

    @app.get("/api/config", dependencies=protected)
    def get_config():
        cfg = config.get()
        return {
            "control": cfg["control"],
            "active_profile": cfg["active_profile"],
            "profiles": cfg["profiles"],
            "builtin": sorted(BUILTIN_PROFILES),
        }

    # ------------------------------------------------------------------ write
    @app.post("/api/profile/active", dependencies=protected)
    def set_active(body: dict):
        key = body.get("key")
        if key not in config.get()["profiles"]:
            raise HTTPException(404, "No such profile")
        config.update(lambda d: d.__setitem__("active_profile", key))
        return {"ok": True}

    @app.put("/api/profiles/{key}", dependencies=protected)
    def save_profile(key: str, body: dict):
        if not re.fullmatch(r"[a-z0-9_]{1,32}", key):
            raise HTTPException(400, "Profile id: lowercase letters, digits, underscore")
        try:
            prof = validate_profile(body)
        except (ValueError, TypeError) as exc:
            raise HTTPException(400, str(exc))
        config.update(lambda d: d["profiles"].__setitem__(key, prof))
        return {"ok": True, "profile": prof}

    @app.delete("/api/profiles/{key}", dependencies=protected)
    def delete_profile(key: str):
        if key in BUILTIN_PROFILES:
            raise HTTPException(400, "Built-in profiles can't be deleted")

        def fn(d):
            d["profiles"].pop(key, None)
            if d["active_profile"] == key:
                d["active_profile"] = "balanced"

        config.update(fn)
        return {"ok": True}

    @app.put("/api/control", dependencies=protected)
    def set_control(body: dict):
        limits = {
            "source": None,
            "interval_s": (1, 60),
            "drive_poll_s": (10, 3600),
            "spindown_min": (0, 600),
            "ramp_step": (1, 100),
            "idle_percent": (0, 100),
            "failsafe_percent": (30, 100),
            "stale_after_s": (30, 7200),
            "history_hours": (1, 24),
        }
        clean = {}
        for k, v in body.items():
            if k not in limits:
                continue
            if k == "source":
                if v not in ("drives_max", "drives_avg", "cpu"):
                    raise HTTPException(400, "Invalid source")
                clean[k] = v
            else:
                lo, hi = limits[k]
                clean[k] = max(lo, min(hi, int(v)))
                if k == "spindown_min" and 0 < clean[k] < 5:
                    clean[k] = 5  # spinning down more often wears the drives
        config.update(lambda d: d["control"].update(clean))
        return {"ok": True, "control": config.get()["control"]}

    @app.post("/api/fan/override", dependencies=protected)
    def fan_override(body: dict):
        controller.set_override(body.get("percent"), body.get("minutes", 15))
        return {"ok": True}

    @app.post("/api/drives/{name}/standby", dependencies=protected)
    def drive_standby(name: str):
        drive = next((d for d in controller.snapshot()["drives"] if d["name"] == name), None)
        if drive is None:
            raise HTTPException(404, "No such drive")
        if drive.get("rotational") is False:
            raise HTTPException(400, "Only hard drives can be spun down")
        err = controller.spin_down(name)
        if err:
            raise HTTPException(500, f"Spin-down failed: {err}")
        return {"ok": True}

    @app.post("/api/drives/refresh", dependencies=protected)
    def drives_refresh():
        controller.poll_now.set()
        return {"ok": True}

    @app.post("/api/power/{action}", dependencies=protected)
    def power(action: str):
        if action not in ("shutdown", "reboot"):
            raise HTTPException(400, "Unknown action")
        if mock:
            log.warning("Mock mode: would %s now", action)
            return {"ok": True, "mock": True}
        verb = "poweroff" if action == "shutdown" else "reboot"
        cmd = ([] if os.geteuid() == 0 else ["sudo", "-n"]) + ["/usr/bin/systemctl" if os.path.exists("/usr/bin/systemctl") else (shutil.which("systemctl") or "systemctl"), verb]
        # Delay slightly so the HTTP response gets out first.
        threading.Timer(1.5, lambda: subprocess.run(cmd)).start()
        return {"ok": True}

    return app


def main():
    parser = argparse.ArgumentParser(description="NAS dashboard and EMC2101 fan controller")
    parser.add_argument("--config", default=os.environ.get("NASDASH_CONFIG", "/etc/nasdash/config.json"))
    parser.add_argument("--mock", action="store_true", help="fake hardware, for testing on any computer")
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config = Config(Path(args.config))
    if not config.path.exists():
        config.save()
    srv = config.get()["server"]
    app = create_app(config, mock=args.mock)
    uvicorn.run(app, host=args.host or srv["host"], port=args.port or srv["port"], log_level="warning")


if __name__ == "__main__":
    main()

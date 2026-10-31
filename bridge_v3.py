"""
bridge_v3.py — local-system-bridge v3.1 · "god mode"
====================================================
Everything from v2 (persistent interactive shell, background jobs,
binary file transfer, screenshot, cron) PLUS:

  🔎 audit log     → every request logged to ~/local-mcp-bridge/audit.log (/admin/audit)
  🐚 multi-shell   → named independent shells: POST /shell/new {"session": "build"}
  🖱 GUI control   → 🔥 v3.1: Wayland-aware /screenshot & /shot (portal, black-screen-proof),
                     click via ydotool (/gui/click), SLOW reliable clicks (/gui/click2,
                     /gui/safeclick — defeats GNOME dock interception), key combos
                     (/gui/keys) and raw evdev codes (/gui/rawkeys), text typing (/gui/type),
                     /gui/open any app — see ISSUES.md for why these exist.
                     BONUS: tools_linux/ff_tabs.py = Firefox Marionette driver (DOM clicks
                     by button TEXT — the reliable escape hatch when pixels lose).

  📦 self-install  → POST /install {"package": "htop", "manager": "apt|pip|snap"} runs as a job
  🧠 hot-load      → POST /tools/add {"name": "...", "code": "def run(p): ..."} adds a LIVE
                     endpoint with NO restart — the agent extends the bridge itself.
                     Saved to hot_tools.json so they survive restarts too.

Setup:  pip install fastapi uvicorn pexpect mss   (Linux GUI extras: ydotool + python3-gi)
Run:    python bridge_v3.py          (or BRIDGE_TOKEN=xxx python bridge_v3.py)
Tunnel: cloudflared tunnel --url http://127.0.0.1:8765     (named tunnel preferred!)
"""

from __future__ import annotations

import asyncio
import json
import os
import platform
import re
import secrets
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path

import uvicorn
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

import server  # v1 tools — reused as plain functions


def _unwrap(tool):
    return getattr(tool, "fn", tool)


read_file_content = _unwrap(server.read_file_content)
list_directory = _unwrap(server.list_directory)
get_system_info = _unwrap(server.get_system_info)
run_command = _unwrap(server.run_command)

TOKEN = os.environ.get("BRIDGE_TOKEN") or secrets.token_urlsafe(24)
HOST, PORT = "127.0.0.1", 8765
STATE_DIR = Path.home() / "local-mcp-bridge"
STATE_DIR.mkdir(exist_ok=True)
AUDIT_LOG = STATE_DIR / "audit.log"
HOT_TOOLS_FILE = STATE_DIR / "hot_tools.json"

app = FastAPI(title="local-system-bridge v3", docs_url=None, redoc_url=None)


async def auth(x_bridge_token: str = Header(default="")):
    if not secrets.compare_digest(x_bridge_token, TOKEN):
        raise HTTPException(status_code=401, detail="invalid token")


# ======================================================================
# 🔎 AUDIT LOG — every request, with a small body snippet, appended to file
# ======================================================================
@app.middleware("http")
async def _audit(request: Request, call_next):
    body = b""
    if request.method in ("POST", "PUT", "PATCH"):
        body = await request.body()

        async def receive():       # re-inject so the route can read it again
            return {"type": "http.request", "body": body, "more_body": False}
        request._receive = receive

    response = await call_next(request)
    try:
        snippet = body[:300].decode("utf-8", "replace").replace("\n", " ") if body else ""
        token_ok = secrets.compare_digest(request.headers.get("x-bridge-token", ""), TOKEN)
        with AUDIT_LOG.open("a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} "
                    f"{request.method} {request.url.path} -> {response.status_code} "
                    f"auth={'ok' if token_ok else 'NO'} {snippet}\n")
    except Exception:
        pass
    return response


@app.get("/admin/audit", dependencies=[Depends(auth)])
def admin_audit(lines: int = 50):
    if not AUDIT_LOG.exists():
        return {"lines": []}
    return {"lines": AUDIT_LOG.read_text(encoding="utf-8", errors="replace")
            .splitlines()[-lines:]}


# ======================================================================
# 🐚 PERSISTENT SHELLS — named sessions, each a living interactive shell
# ======================================================================
_SHELL_MARKER = "==BRIDGE-PROMPT-a91kx7=="
_ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")
_IS_WIN = os.name == "nt"


class Shell:
    """A long-lived interactive shell. cwd / env / aliases persist per session."""

    def __init__(self):
        import pexpect
        if _IS_WIN:
            from pexpect.popen_spawn import PopenSpawn
            self.sh = PopenSpawn("cmd.exe", encoding="utf-8", timeout=30)
            self.sh.sendline(f"prompt {_SHELL_MARKER}$G")
        else:
            self.sh = pexpect.spawn(
                "/bin/bash", ["--norc", "-i"], encoding="utf-8", timeout=30,
                echo=True, cwd=str(Path.home()), dimensions=(40, 220))
            self.sh.sendline(
                f'export PS1="{_SHELL_MARKER}"; unset PROMPT_COMMAND; '
                "bind 'set enable-bracketed-paste off' 2>/dev/null")
        self.lock = threading.Lock()
        self.sh.expect(_SHELL_MARKER)                     # echoed setup line
        self.sh.expect(_SHELL_MARKER, timeout=10)         # fresh prompt
        self.sh.before = ""

    def alive(self) -> bool:
        try:
            return self.sh.isalive()
        except Exception:
            return False

    @staticmethod
    def _clean(text: str) -> str:
        return _ANSI.sub("", text).replace("\r\n", "\n").rstrip("\n")

    def run(self, command: str, timeout: int = 30) -> str:
        with self.lock:
            self.sh.timeout = max(1, min(timeout, 600))
            self.sh.sendline(command)
            try:
                self.sh.expect(_SHELL_MARKER)
            except Exception:
                partial = self._clean(self.sh.before or "")
                msg = ("Error: no prompt within the timeout — command may be "
                       "running or waiting for input. Use /shell/drain, "
                       "/shell/type, or \\x03 (Ctrl+C).")
                return msg + (f"\nPartial output:\n{partial}" if partial else "")
            return self._clean(self.sh.before)

    def type(self, text: str, wait: bool = False, timeout: int = 15) -> str:
        with self.lock:
            self.sh.send(text)
            if wait:
                self.sh.timeout = max(1, min(timeout, 600))
                try:
                    self.sh.expect(_SHELL_MARKER)
                    return self._clean(self.sh.before)
                except Exception:
                    pass
            time.sleep(0.25)
            return self._clean(self.drain())

    def drain(self) -> str:
        chunks = []
        try:
            while True:
                chunks.append(self.sh.read_nonblocking(size=8192, timeout=0.15))
        except Exception:
            pass
        return self._clean("".join(chunks))


_SHELLS: dict[str, Shell] = {}
_SHELLS_LOCK = threading.Lock()


def get_shell(session: str = "main") -> Shell:
    with _SHELLS_LOCK:
        sh = _SHELLS.get(session)
        if sh is None or not sh.alive():
            sh = Shell()
            _SHELLS[session] = sh
        return sh


class ShellRun(BaseModel):
    command: str
    timeout: int = 30
    session: str = "main"


class ShellType(BaseModel):
    text: str
    wait: bool = False
    session: str = "main"


class ShellNew(BaseModel):
    session: str


@app.post("/shell/new", dependencies=[Depends(auth)])
def shell_new(body: ShellNew):
    get_shell(body.session)
    return {"sessions": list(_SHELLS)}


@app.post("/shell/run", dependencies=[Depends(auth)])
def shell_run(body: ShellRun):
    return {"session": body.session,
            "output": get_shell(body.session).run(body.command, body.timeout)}


@app.post("/shell/type", dependencies=[Depends(auth)])
def shell_type(body: ShellType):
    return {"session": body.session,
            "output": get_shell(body.session).type(body.text, body.wait)}


@app.get("/shell/drain", dependencies=[Depends(auth)])
def shell_drain(session: str = "main"):
    return {"session": session, "output": get_shell(session).drain()}


@app.get("/shell/sessions", dependencies=[Depends(auth)])
def shell_sessions():
    return {"sessions": list(_SHELLS)}


# ======================================================================
# ⚙ BACKGROUND JOBS — start, poll live, list, kill
# ======================================================================
JOBS_DIR = Path(tempfile.gettempdir()) / "bridge_jobs"
JOBS_DIR.mkdir(exist_ok=True)
JOBS: dict[str, dict] = {}


class JobStart(BaseModel):
    command: str
    cwd: str | None = None


def _job_view(jid: str, job: dict) -> dict:
    rc = job["proc"].poll()
    return {"job_id": jid, "cmd": job["cmd"], "started": job["started"],
            "running": rc is None, "exit_code": rc}


@app.post("/jobs/start", dependencies=[Depends(auth)])
def jobs_start(body: JobStart):
    cwd = Path(body.cwd).expanduser() if body.cwd else Path.home()
    if not cwd.exists():
        raise HTTPException(400, f"cwd not found: {cwd}")
    jid = uuid.uuid4().hex[:8]
    log = open(JOBS_DIR / f"{jid}.log", "w", buffering=1, encoding="utf-8", errors="replace")
    proc = subprocess.Popen(body.command, shell=True, stdout=log, stderr=subprocess.STDOUT,
                            cwd=str(cwd), text=True, start_new_session=not _IS_WIN)
    JOBS[jid] = {"proc": proc, "log": log, "cmd": body.command,
                 "started": time.strftime("%H:%M:%S")}
    return {"job_id": jid, "hint": f"poll GET /jobs/{jid}?offset=0"}


@app.get("/jobs", dependencies=[Depends(auth)])
def jobs_list():
    """Beacon: one call shows every job's state."""
    return {"jobs": [_job_view(j, jv) for j, jv in JOBS.items()]}


@app.get("/jobs/{jid}", dependencies=[Depends(auth)])
def jobs_status(jid: str, offset: int = 0):
    job = JOBS.get(jid)
    if not job:
        raise HTTPException(404, "unknown job_id")
    log_path = JOBS_DIR / f"{jid}.log"
    size = log_path.stat().st_size if log_path.exists() else 0
    chunk = ""
    if size > offset:
        with open(log_path, "rb") as f:
            f.seek(offset)
            chunk = f.read(size - offset).decode("utf-8", "replace")
    view = _job_view(jid, job)
    view["output_since_offset"] = chunk
    view["next_offset"] = size
    return view


@app.post("/jobs/{jid}/kill", dependencies=[Depends(auth)])
def jobs_kill(jid: str):
    job = JOBS.get(jid)
    if not job:
        raise HTTPException(404, "unknown job_id")
    try:
        if _IS_WIN:
            job["proc"].kill()
        else:
            os.killpg(os.getpgid(job["proc"].pid), signal.SIGKILL)
        return {"killed": jid}
    except Exception as exc:
        return {"killed": False, "detail": str(exc)}


# ======================================================================
# 📁 FILE TRANSFER
# ======================================================================
@app.get("/files/download", dependencies=[Depends(auth)])
def files_download(path: str):
    p = Path(path).expanduser()
    if not p.exists() or not p.is_file():
        raise HTTPException(404, f"file not found: {p}")
    return FileResponse(p, filename=p.name)


@app.post("/files/upload", dependencies=[Depends(auth)])
async def files_upload(path: str = Form(...), file: UploadFile = File(...)):
    dest = Path(path).expanduser()
    dest.parent.mkdir(parents=True, exist_ok=True)
    size = 0
    with open(dest, "wb") as f:
        while chunk := await file.read(1024 * 1024):
            f.write(chunk)
            size += len(chunk)
    return {"saved_to": str(dest), "bytes": size}


# ======================================================================
# 🖱 GUI — screenshot + open apps + click/type (ydotool on Linux)
# ======================================================================
_KNOWN_APPS = {
    "chrome": ["google-chrome", "google-chrome-stable", "chromium-browser", "chromium"],
    "firefox": ["firefox"],
    "files": ["nautilus"],
    "terminal": ["gnome-terminal"],
    "code": ["code"],
}


class GuiOpen(BaseModel):
    target: str   # "chrome", "firefox", any binary name, or a file/URL


@app.post("/gui/open", dependencies=[Depends(auth)])
def gui_open(body: GuiOpen):
    system = platform.system()
    target = body.target
    try:
        if system == "Darwin":
            subprocess.Popen(["open", target], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif _IS_WIN:
            subprocess.Popen(f'start "" "{target}"', shell=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            cmd = target
            for name, candidates in _KNOWN_APPS.items():
                if target.lower() == name:
                    cmd = next((c for c in candidates if shutil.which(c)), target)
                    break
            subprocess.Popen(cmd, shell=True, start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return {"opened": target}
    except Exception as exc:
        raise HTTPException(500, f"could not open {target}: {exc}")


# ------------------------------------------------------------------ #
# SCREENSHOTS — v3.1: Wayland-aware (ISSUES.md #W1).                    #
# On Wayland: mss = black 6 KB frame, gnome-screenshot segfaults.       #
# The working path on GNOME/Wayland = xdg portal screenshot             #
# (tools_linux/portal_shot.py — subscribe-before-call pattern),         #
# invoked with the SYSTEM python3 (needs python3-gi).                   #
# ------------------------------------------------------------------ #
def _is_wayland() -> bool:
    return bool(os.environ.get("WAYLAND_DISPLAY")) or \
        os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland"


def _portal_shot(dest: Path) -> bool:
    helper = Path(__file__).parent / "tools_linux" / "portal_shot.py"
    if not helper.exists():
        helper = Path.home() / "local-mcp-bridge" / "tools_linux" / "portal_shot.py"
    env = dict(os.environ)
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path={env['XDG_RUNTIME_DIR']}/bus")
    try:
        r = subprocess.run(["/usr/bin/python3", str(helper), str(dest)],
                           env=env, capture_output=True, text=True, timeout=40)
        return r.returncode == 0 and dest.exists() and dest.stat().st_size > 10000
    except Exception:
        return False


def _do_screenshot(dest: Path) -> Path:
    # Wayland → portal first, ALWAYS. Legacy tools only as X11 fallbacks.
    if _is_wayland() and _portal_shot(dest):
        return dest
    if not _is_wayland():
        for cmd in (["gnome-screenshot", "-f", str(dest)], ["scrot", str(dest)]):
            try:
                subprocess.run(cmd, check=True, timeout=15, capture_output=True)
                if dest.exists() and dest.stat().st_size > 10000:
                    return dest
            except Exception:
                continue
    # last-resort fallbacks (may black-frame on Wayland — caller checks size)
    if _portal_shot(dest):
        return dest
    try:
        import mss
        with mss.mss() as sct:
            sct.shot(output=str(dest))
        if dest.exists() and dest.stat().st_size > 10000:
            return dest
    except Exception:
        pass
    raise HTTPException(500, "no working screenshot path. On Wayland install "
                             "python3-gi + keep tools_linux/portal_shot.py beside the bridge. "
                             "See ISSUES.md #W1.")


@app.get("/screenshot", dependencies=[Depends(auth)])
@app.get("/gui/screenshot", dependencies=[Depends(auth)])
def screenshot():
    dest = Path(tempfile.gettempdir()) / f"shot_{int(time.time())}.png"
    return FileResponse(_do_screenshot(dest), filename="screenshot.png")


@app.post("/shot", dependencies=[Depends(auth)])
def shot_b64():
    """One-call vision for the agent: returns json with png base64 + byte size."""
    import base64 as _b64
    dest = Path(tempfile.gettempdir()) / f"shot_{int(time.time()*1000)}.png"
    _do_screenshot(dest)
    blob = dest.read_bytes()
    return {"bytes": len(blob), "png_b64": _b64.b64encode(blob).decode()}


# ------------------------------------------------------------------ #
# POINTER/KEYBOARD — via ydotool (v3.1 hardening; see ISSUES.md).      #
# #G1: bare `click 0xC0` press+release bursts get dropped on some      #
# Wayland stacks => click2 = human press-hold-release.                 #
# #G2: the auto-hide GNOME dock EATS clicks near the screen bottom     #
# => safeclick = park mid-screen, let the dock retract, then click.    #
# #D2: ydotool needs YDOTOOL_SOCKET env if the daemon uses a custom    #
# socket — honored here automatically.                                 #
# ------------------------------------------------------------------ #
_YDO_ENV = dict(os.environ)
_YDO_ENV.setdefault("YDOTOOL_SOCKET", "/tmp/.ydotool_socket")


def _ydo(*args: str) -> str:
    try:
        r = subprocess.run(["ydotool", *args], capture_output=True, text=True, timeout=10, env=_YDO_ENV)
    except FileNotFoundError:
        raise HTTPException(500, "ydotool not installed. Run: POST /install "
                                 '{"package":"ydotool","manager":"apt"} '
                                 "(and ensure ydotoold is running — see ISSUES.md #D1)")
    if r.returncode != 0:
        raise HTTPException(500, f"ydotool failed: {r.stderr.strip()} (ydotoold running? stale socket? ISSUES.md #D2)")
    return r.stdout


_BTN_LEFT = 272  # evdev BTN_LEFT


class GuiKeys(BaseModel):
    combo: str    # e.g. "ctrl+shift+t", "alt+f4", "f5", "esc"


class GuiXY(BaseModel):
    x: int
    y: int


# full evdev map — proven in live session
_KEYCODES = {
    "esc": 1, "enter": 28, "tab": 15, "space": 57, "backspace": 14,
    "left": 105, "right": 106, "up": 103, "down": 108,
    "home": 102, "end": 107, "pageup": 104, "pagedown": 109,
    "delete": 111, "insert": 110, "minus": 12, "equal": 13,
    **{c: k for c, k in zip("abcdefghijklmnopqrstuvwxyz",
        [30, 48, 46, 32, 18, 33, 34, 35, 23, 36, 37, 38, 50, 49, 24, 25, 16, 19, 31, 20, 22, 47, 17, 45, 21, 44])},
    **{str(d): k for d, k in enumerate([11, 2, 3, 4, 5, 6, 7, 8, 9, 10])},  # 0..9
    **{f"f{i}": (58 + i if i <= 10 else 86 + (i - 10)) for i in range(1, 13)},  # f1=59..f10=68, f11=87, f12=88
}
_MODS = {"ctrl": 29, "alt": 56, "shift": 42, "super": 125, "meta": 125}


def _send_keycodes(codes: list[int], hold_s: float = 0.08) -> None:
    for c in codes:
        _ydo("key", f"{c}:1")
    time.sleep(hold_s)
    for c in reversed(codes):
        _ydo("key", f"{c}:0")


@app.post("/gui/keys", dependencies=[Depends(auth)])
def gui_keys(body: GuiKeys):
    combo = [k.strip().lower() for k in body.combo.split("+") if k.strip()]
    mods, keys = [], []
    for k in combo:
        if k in _MODS:
            mods.append(_MODS[k])
        elif k in _KEYCODES:
            keys.append(_KEYCODES[k])
        else:
            raise HTTPException(400, f"unknown key: {k} (known mods: {sorted(_MODS)}; keys: {sorted(_KEYCODES)})")
    if not keys:
        raise HTTPException(400, "combo needs at least one non-modifier key")
    _send_keycodes(mods + keys)
    return {"sent": body.combo, "codes": mods + keys}


@app.post("/gui/rawkeys", dependencies=[Depends(auth)])
def gui_rawkeys(body: dict):
    """Arbitrary evdev codes: {"codes": "29:1 12:1 12:0 29:0"} — full-power mode."""
    codes = (body.get("codes") or "").split()
    if not codes:
        raise HTTPException(400, "pass codes like '29:1 12:1 12:0 29:0'")
    for c in codes:
        if not re.fullmatch(r"\d+:[01]", c):
            raise HTTPException(400, f"bad code {c!r} (want N:1/N:0)")
    _ydo("key", *codes)
    return {"sent": codes}


@app.post("/gui/click2", dependencies=[Depends(auth)])
def gui_click2(body: GuiXY):
    """Human-like click: move, settle, press, hold, release."""
    _ydo("mousemove", "-a", str(body.x), str(body.y))
    time.sleep(0.25)
    _ydo("key", f"{_BTN_LEFT}:1")
    time.sleep(0.12)
    _ydo("key", f"{_BTN_LEFT}:0")
    return {"slow_clicked": [body.x, body.y]}


@app.post("/gui/safeclick", dependencies=[Depends(auth)])
def gui_safeclick(body: GuiXY):
    """Dock-proof click: park the pointer mid-screen first so the GNOME
    auto-hide dash retracts, THEN approach the target directly and click."""
    park_y = min(body.y - 140, 300)
    _ydo("mousemove", "-a", str(body.x), str(max(park_y, 60)))
    time.sleep(1.2)                       # let the dock auto-hide
    _ydo("mousemove", "-a", str(body.x), str(body.y))
    time.sleep(0.25)
    _ydo("key", f"{_BTN_LEFT}:1")
    time.sleep(0.12)
    _ydo("key", f"{_BTN_LEFT}:0")
    return {"safe_clicked": [body.x, body.y]}


class GuiType(BaseModel):
    text: str


class GuiClick(BaseModel):
    x: int
    y: int


@app.post("/gui/type", dependencies=[Depends(auth)])
def gui_type(body: GuiType):
    _ydo("type", "--", body.text)
    return {"typed": len(body.text)}


@app.post("/gui/click", dependencies=[Depends(auth)])
def gui_click(body: GuiClick):
    # legacy burst click. If it ever silently no-ops on a Wayland desktop,
    # switch to /gui/click2 (slow) or /gui/safeclick (dock-proof). #G1/#G2
    _ydo("mousemove", "-a", str(body.x), str(body.y))
    _ydo("click", "0xC0")
    return {"clicked": [body.x, body.y], "hint": "if the target ignores this, use /gui/click2 or /gui/safeclick"}


# ======================================================================
# 📦 SELF-INSTALL — agent installs what it needs, as a background job
# ======================================================================
class InstallBody(BaseModel):
    package: str
    manager: str = "pip"   # pip | apt | snap


@app.post("/install", dependencies=[Depends(auth)])
def install_pkg(body: InstallBody):
    import sys
    pkg = body.package.strip()
    if not pkg:
        raise HTTPException(400, "empty package")
    if body.manager == "pip":
        cmd = f"{sys.executable} -m pip install {pkg}"
    elif body.manager == "apt":
        cmd = f"sudo -n apt-get install -y {pkg} || echo NEEDS_SUDO_PASSWORD"
    elif body.manager == "snap":
        cmd = f"sudo -n snap install {pkg} || echo NEEDS_SUDO_PASSWORD"
    else:
        raise HTTPException(400, "manager must be pip | apt | snap")
    return jobs_start(JobStart(command=cmd)) | {
        "note": "it's a background job — poll it. If output says NEEDS_SUDO_PASSWORD, "
                "run it in /shell/run and answer via /shell/type"}


# ======================================================================
# 🧠 HOT-LOAD TOOLS — agent writes NEW endpoints into the running bridge
#    POST /tools/add {"name":"clean_tmp","code":"def run(p):\n    ..."}
#    code must define run(params: dict) — params = JSON body or query args
# ======================================================================
_HOT_TOOLS: dict[str, dict] = {}


class ToolAdd(BaseModel):
    name: str
    code: str
    method: str = "POST"   # POST or GET


def _make_handler(fn):
    async def handler(request: Request):
        params = dict(request.query_params)
        if request.method in ("POST", "PUT", "PATCH"):
            try:
                params.update(await request.json())
            except Exception:
                pass
        try:
            result = fn(params)
            if asyncio.iscoroutine(result):
                result = await result
            return {"result": result}
        except Exception as exc:
            raise HTTPException(500, f"tool error: {exc}")
    handler.__name__ = "hot_tool"
    return handler


def _register_tool(name: str, code: str, method: str) -> str:
    import textwrap
    g: dict = {}
    exec(textwrap.dedent(code), g)
    fn = g.get("run")
    if not callable(fn):
        raise HTTPException(400, "code must define: def run(params: dict)")
    path = f"/tools/{name}"
    app.routes[:] = [r for r in app.routes if getattr(r, "path", None) != path]
    app.add_api_route(path, _make_handler(fn), methods=[method.upper()],
                      dependencies=[Depends(auth)])
    _HOT_TOOLS[name] = {"code": code, "method": method.upper()}
    HOT_TOOLS_FILE.write_text(json.dumps(_HOT_TOOLS, indent=1))
    return path


@app.post("/tools/add", dependencies=[Depends(auth)])
def tools_add(body: ToolAdd):
    if not re.fullmatch(r"[a-zA-Z0-9_\-]+", body.name):
        raise HTTPException(400, "name: letters, digits, _ - only")
    return {"added": _register_tool(body.name, body.code, body.method),
            "call": f"{body.method.upper()} /tools/{body.name} (token header)"}


@app.get("/tools", dependencies=[Depends(auth)])
def tools_list():
    return {"hot_tools": list(_HOT_TOOLS)}


@app.post("/tools/remove", dependencies=[Depends(auth)])
def tools_remove(body: ShellNew):   # reuse {session} shape: session=name
    name = body.session
    _HOT_TOOLS.pop(name, None)
    HOT_TOOLS_FILE.write_text(json.dumps(_HOT_TOOLS, indent=1))
    path = f"/tools/{name}"
    app.routes[:] = [r for r in app.routes if getattr(r, "path", None) != path]
    return {"removed": name}


def _reload_hot_tools():
    try:
        saved = json.loads(HOT_TOOLS_FILE.read_text())
        for name, spec in saved.items():
            _register_tool(name, spec["code"], spec.get("method", "POST"))
    except Exception:
        pass


_reload_hot_tools()          # survive restarts


# ======================================================================
# cron (from v2) + v1 compat endpoints
# ======================================================================
def _crontab_read() -> str:
    try:
        r = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=15)
    except FileNotFoundError:
        raise HTTPException(500, "cron not installed: sudo apt install cron")
    return r.stdout or ""


def _crontab_write(tab: str) -> None:
    r = subprocess.run(["crontab", "-"], input=tab, capture_output=True, text=True, timeout=15)
    if r.returncode != 0:
        raise HTTPException(500, f"crontab rejected it: {r.stderr.strip()}")


@app.get("/cron", dependencies=[Depends(auth)])
def cron_list():
    return {"crontab": _crontab_read()}


class CronLine(BaseModel):
    line: str


@app.post("/cron/add", dependencies=[Depends(auth)])
def cron_add(body: CronLine):
    tab = _crontab_read()
    new = (tab.rstrip("\n") + "\n" if tab.strip() else "") + body.line + "\n"
    _crontab_write(new)
    return {"crontab": _crontab_read()}


@app.post("/cron/remove", dependencies=[Depends(auth)])
def cron_remove(body: CronLine):
    kept = [l for l in _crontab_read().splitlines() if body.line not in l]
    _crontab_write("\n".join(kept) + "\n")
    return {"crontab": _crontab_read()}


@app.get("/health")
def health():
    return {"ok": True, "server": "local-system-bridge v3.1",
            "hot_tools": list(_HOT_TOOLS), "sessions": list(_SHELLS)}


@app.get("/system-info", dependencies=[Depends(auth)])
def system_info():
    return get_system_info()


@app.get("/list", dependencies=[Depends(auth)])
def ls(path: str = "~"):
    return {"result": list_directory(path)}


@app.get("/read", dependencies=[Depends(auth)])
def read(path: str):
    return {"result": read_file_content(path)}


class RunBody(BaseModel):
    command: str
    cwd: str | None = None


@app.post("/run", dependencies=[Depends(auth)])
def run(body: RunBody):
    return {"result": run_command(body.command, body.cwd)}


# ======================================================================
if __name__ == "__main__":
    src = "BRIDGE_TOKEN env" if os.environ.get("BRIDGE_TOKEN") else "random per start"
    print("=" * 66)
    print("  local-system-bridge v3.1 · god mode (Wayland-hardened)")
    print(f"  TOKEN ({src}): {TOKEN}")
    print("  shells (multi) · jobs · files · Wayland screenshot/shot · cron")
    print("  GUI: open/click/click2/safeclick/type/keys/rawkeys · /tools/add")
    print("  audit log:", AUDIT_LOG)
    print("  tunnel: cloudflared tunnel --url http://127.0.0.1:8765")
    print("  READ ISSUES.md FIRST — it encodes a full day of debugging pain.")
    print("=" * 66)
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")

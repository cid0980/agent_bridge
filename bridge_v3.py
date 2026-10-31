"""
bridge_v3.py — local-system-bridge v3.2 · "god mode"
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

  ⚡ v3.2 SPEED  → /gui/batch (N GUI actions + verification shot in ONE request),
                     cropped/JPEG/downscaled screenshots (region=…), in-process
                     Marionette endpoints (/ff/tabs /ff/click /ff/close /ff/js /ff/state),
                     /status one-call dashboard, and a blast-radius guard that REFUSES
                     to kill the bridge or tunnel (/safe-kill for app cleanup).

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
import io
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
    force: bool = False     # bypass the bridge/tunnel self-kill guard (#S6)


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
    warn = _blast_guard(body.command)
    if warn and not getattr(body, "force", False):
        raise HTTPException(409, warn)
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
    force: bool = False     # bypass the bridge/tunnel self-kill guard (#S6)


def _job_view(jid: str, job: dict) -> dict:
    rc = job["proc"].poll()
    return {"job_id": jid, "cmd": job["cmd"], "started": job["started"],
            "running": rc is None, "exit_code": rc}


@app.post("/jobs/start", dependencies=[Depends(auth)])
def jobs_start(body: JobStart):
    warn = _blast_guard(body.command)
    if warn and not getattr(body, "force", False):
        raise HTTPException(409, warn)
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


# ------------------------------------------------------------------ #
# 🖼 VISION — v3.2: CROP / DOWNSCALE / JPEG.                            #
# A full 1920×1080 PNG costs ~2 MB down the tunnel and ~1–2 s. A       #
# 900×40 crop of a tab strip costs ~4 KB and answers the same         #
# question. ALWAYS pass a region when you know where to look.          #
#   POST /shot {"region":"0,0,900,40","format":"jpg","quality":70}    #
#   POST /shot {"max_width":960}          ← whole screen, half bytes  #
#   scale: multiply region coords (HiDPI boxes — this PC = 1.204)     #
# ------------------------------------------------------------------ #
def _img_mod():
    try:
        from PIL import Image  # pillow — optional, degrades gracefully
        return Image
    except Exception:
        return None


class ShotOpts(BaseModel):
    region: str | None = None       # "x,y,w,h" crop
    format: str = "png"             # png | jpg
    quality: int = 75               # jpg quality
    max_width: int | None = None    # downscale to this width
    scale: float = 1.0              # region coord multiplier (HiDPI)


def _capture(opts: ShotOpts | None = None) -> tuple[bytes, str, dict]:
    """Screenshot → (bytes, mime, meta). Crop/resize/re-encode when asked."""
    o = opts or ShotOpts()
    dest = Path(tempfile.gettempdir()) / f"shot_{int(time.time() * 1000)}.png"
    _do_screenshot(dest)
    raw = dest.read_bytes()
    meta: dict = {"captured_bytes": len(raw)}
    want_more = bool(o.region or o.max_width or o.format.lower() in ("jpg", "jpeg"))
    if not want_more:
        return raw, "image/png", meta
    Image = _img_mod()
    if Image is None:
        meta["note"] = "pillow not installed — full PNG returned (pip install pillow)"
        return raw, "image/png", meta
    import io as _io
    img = Image.open(_io.BytesIO(raw)).convert("RGB")
    meta["w"], meta["h"] = img.size
    if o.region:
        try:
            vals = [float(v) for v in o.region.replace(" ", "").split(",")]
            if len(vals) != 4:
                raise ValueError
        except ValueError:
            raise HTTPException(400, 'region must be "x,y,w,h", e.g. "0,0,900,40"')
        sc = o.scale or 1.0
        x, y, w, h = [int(v * sc) for v in vals]
        x, y = max(x, 0), max(y, 0)
        img = img.crop((x, y, min(x + w, img.width), min(y + h, img.height)))
        meta["region"] = [x, y, img.width, img.height]
    if o.max_width and img.width > o.max_width:
        ratio = o.max_width / img.width
        img = img.resize((int(o.max_width), max(1, int(img.height * ratio))))
    buf = _io.BytesIO()
    if o.format.lower() in ("jpg", "jpeg"):
        img.save(buf, "JPEG", quality=max(1, min(o.quality, 95)), optimize=True)
        mime = "image/jpeg"
    else:
        img.save(buf, "PNG", optimize=True)
        mime = "image/png"
    blob = buf.getvalue()
    meta["sent_bytes"] = len(blob)
    return blob, mime, meta


@app.get("/screenshot", dependencies=[Depends(auth)])
@app.get("/gui/screenshot", dependencies=[Depends(auth)])
def screenshot(region: str | None = None, format: str = "png",
               quality: int = 75, max_width: int | None = None, scale: float = 1.0):
    blob, mime, _ = _capture(ShotOpts(region=region, format=format, quality=quality,
                                      max_width=max_width, scale=scale))
    dest = Path(tempfile.gettempdir()) / f"out_{int(time.time() * 1000)}"
    dest = dest.with_suffix(".jpg" if mime == "image/jpeg" else ".png")
    dest.write_bytes(blob)
    return FileResponse(dest, filename=dest.name, media_type=mime)


@app.post("/shot", dependencies=[Depends(auth)])
def shot_b64(opts: ShotOpts | None = None):
    """One-call vision. Returns base64 + size (and 'captured_bytes' before processing,
    so you can SEE how much the crop saved)."""
    import base64 as _b64
    blob, mime, meta = _capture(opts)
    out = {"bytes": len(blob), "mime": mime, "b64": _b64.b64encode(blob).decode(), **meta}
    if mime == "image/png":
        out["png_b64"] = out["b64"]          # v3.1 compat
    return out


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
    r = _slow_click(body.x, body.y)
    return {"slow_clicked": r["clicked"]}


@app.post("/gui/safeclick", dependencies=[Depends(auth)])
def gui_safeclick(body: GuiXY):
    """Dock-proof click: park the pointer mid-screen first so the GNOME
    auto-hide dash retracts, THEN approach the target directly and click."""
    r = _safe_click(body.x, body.y)
    return {"safe_clicked": r["clicked"]}


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
# ⚡ v3.2 SPEED LAYER
# The v3.1 lesson: a whole GUI sequence cost 10+ round trips because each
# action needed a follow-up screenshot to confirm it. Fixes here:
#   /gui/batch  → N actions + a verification shot in ONE request
#   /ff/*       → Firefox DOM control (no pixels, no coordinate drift)
#   /status     → one call tells you everything (incl. tunnel URL)
#   blast guard → /run and friends REFUSE to kill the bridge/tunnel
# ======================================================================

# ---------- shared pointer/keyboard primitives (no HTTP in here) ----------
def _slow_click(x: int, y: int) -> dict:
    """Human-like: move → settle → press → hold → release. Defeats #G1."""
    _ydo("mousemove", "-a", str(x), str(y))
    time.sleep(0.25)
    _ydo("key", f"{_BTN_LEFT}:1")
    time.sleep(0.12)
    _ydo("key", f"{_BTN_LEFT}:0")
    return {"clicked": [x, y], "mode": "slow"}


def _safe_click(x: int, y: int) -> dict:
    """Dock-proof: park mid-screen so the GNOME dash retracts, then click. #G2."""
    park_y = max(min(y - 140, 300), 60)
    _ydo("mousemove", "-a", str(x), str(park_y))
    time.sleep(1.2)
    _ydo("mousemove", "-a", str(x), str(y))
    time.sleep(0.25)
    _ydo("key", f"{_BTN_LEFT}:1")
    time.sleep(0.12)
    _ydo("key", f"{_BTN_LEFT}:0")
    return {"clicked": [x, y], "mode": "safe"}


def _key_combo(combo: str) -> dict:
    parts = [k.strip().lower() for k in combo.split("+") if k.strip()]
    mods, keys = [], []
    for k in parts:
        if k in _MODS:
            mods.append(_MODS[k])
        elif k in _KEYCODES:
            keys.append(_KEYCODES[k])
        else:
            raise HTTPException(400, f"unknown key: {k} (mods: {sorted(_MODS)}; keys: {sorted(_KEYCODES)})")
    if not keys:
        raise HTTPException(400, "combo needs at least one non-modifier key")
    _send_keycodes(mods + keys)
    return {"sent": combo, "codes": mods + keys}


# ---------- 🚀 ONE CALL = A WHOLE GUI SEQUENCE ----------
@app.post("/gui/batch", dependencies=[Depends(auth)])
def gui_batch(body: dict):
    """Run a list of GUI actions in ONE round trip.

    {"stop_on_error": true, "steps": [
        {"op":"click","x":120,"y":40},                  # slow click (default)
        {"op":"click","x":120,"y":40,"safe":true},      # dock-proof
        {"op":"move","x":900,"y":500},
        {"op":"key","combo":"ctrl+w"},
        {"op":"type","text":"hello"},
        {"op":"scroll","dy":-5},                        # negative = down
        {"op":"sleep","s":0.4},
        {"op":"shot","region":"0,0,900,40","format":"jpg"}   # inline verify
    ]}
    Every step returns ok/ms; 'shot' steps return base64 inline.
    RULE: plan the whole sequence, put ONE shot at the end. Not one shot per step.
    """
    steps = body.get("steps") or []
    if not isinstance(steps, list) or not steps:
        raise HTTPException(400, 'need {"steps": [...]} — see endpoint docstring')
    stop = bool(body.get("stop_on_error", True))
    t0, out = time.time(), []
    for i, st in enumerate(steps):
        op = str(st.get("op", "")).lower()
        t = time.time()
        try:
            if op == "move":
                _ydo("mousemove", "-a", str(int(st["x"])), str(int(st["y"])))
                r = {"moved": [int(st["x"]), int(st["y"])]}
            elif op == "click":
                x, y = int(st["x"]), int(st["y"])
                if st.get("safe"):
                    r = _safe_click(x, y)
                elif st.get("slow", True):
                    r = _slow_click(x, y)
                else:
                    _ydo("mousemove", "-a", str(x), str(y))
                    _ydo("click", "0xC0")
                    r = {"clicked": [x, y], "mode": "burst"}
            elif op in ("key", "keys"):
                r = _key_combo(str(st["combo"]))
            elif op == "rawkeys":
                codes = str(st.get("codes", "")).split()
                if not codes or not all(re.fullmatch(r"\d+:[01]", c) for c in codes):
                    raise ValueError("codes must look like '29:1 12:1 12:0 29:0'")
                _ydo("key", *codes)
                r = {"sent": codes}
            elif op == "type":
                _ydo("type", "--", str(st.get("text", "")))
                r = {"typed": len(str(st.get("text", "")))}
            elif op == "sleep":
                s = float(st.get("s", 0.4))
                time.sleep(max(0.0, min(s, 30.0)))
                r = {"slept": s}
            elif op == "scroll":
                dy = int(st.get("dy", -1))     # ydotool wheel: 0x04 up, 0x05 down
                btn = "0x04" if dy > 0 else "0x05"
                for _ in range(abs(dy) or 1):
                    _ydo("click", btn)
                    time.sleep(0.05)
                r = {"scrolled": dy}
            elif op == "shot":
                o = ShotOpts(**{k: v for k, v in st.items() if k in ShotOpts.model_fields})
                blob, mime, meta = _capture(o)
                import base64 as _b64
                r = {"bytes": len(blob), "mime": mime, "b64": _b64.b64encode(blob).decode(), **meta}
            else:
                raise ValueError(f"unknown op {op!r} (move|click|key|rawkeys|type|sleep|scroll|shot)")
            out.append({"i": i, "op": op, "ok": True, "ms": int((time.time() - t) * 1000), **r})
        except Exception as e:
            out.append({"i": i, "op": op, "ok": False, "ms": int((time.time() - t) * 1000),
                        "error": str(e)})
            if stop:
                break
    return {"steps": out, "ran": len(out), "of": len(steps),
            "ms": int((time.time() - t0) * 1000)}


# ---------- 🦊 FIREFOX VIA MARIONETTE (in-process, no subprocess) ----------
_FF_PORT = int(os.environ.get("FF_MARIONETTE_PORT", "2828"))

_FF_STATE_JS = r"""
const heads=[...document.querySelectorAll('h1,h2,h3')].map(e=>(e.textContent||'').trim()).filter(t=>t&&t.length<90).slice(0,4).join(' | ');
const btns=[...document.querySelectorAll('button,a')].filter(e=>e.offsetParent&&e.offsetHeight>4).map(e=>(e.textContent||'').trim()).filter(t=>t&&t.length<70).slice(0,16).join(', ');
return 'HEADS: '+heads+' || BTNS: '+btns;
"""

_FF_CLICK_JS = r"""
const want = arguments[0];
const els = [...document.querySelectorAll('button,a,div[role=button],[class*=btn]')].filter(e=>e.offsetParent && e.offsetHeight>4);
let b = els.find(e => new RegExp(want,'i').test((e.textContent||'').trim()) && (e.textContent||'').trim().length < 80);
if(!b) return 'NOTFOUND';
const rect = b.getBoundingClientRect();
const cx = rect.left + rect.width/2, cy = rect.top + rect.height/2;
for(const t of ['pointerover','pointermove','pointerdown','mousedown','pointerup','mouseup','click']){
  const Ctor = t.startsWith('pointer') ? PointerEvent : MouseEvent;
  b.dispatchEvent(new Ctor(t,{bubbles:true,cancelable:true,clientX:cx,clientY:cy}));
}
b.click();
return 'CLICKED|'+(b.textContent||'').trim().slice(0,50);
"""


class _Marionette:
    def __init__(self, port: int = _FF_PORT):
        import socket as _socket
        self.s = _socket.create_connection(("127.0.0.1", port), 5)
        self.f = self.s.makefile("rb")
        self._i = 0
        self._recv()                       # greeting

    def _recv(self):
        ln = b""
        while True:
            c = self.f.read(1)
            if not c:
                raise IOError("marionette closed")
            if c == b":":
                break
            ln += c
        return json.loads(self.f.read(int(ln)).decode())

    def cmd(self, name: str, params: dict | None = None):
        self._i += 1
        b = json.dumps([0, self._i, name, params or {}]).encode()
        self.s.sendall(str(len(b)).encode() + b":" + b)
        r = self._recv()
        if len(r) > 2 and r[2]:
            raise RuntimeError(f"marionette {name}: {r[2]}")
        d = r[3] if len(r) > 3 else {}
        return d.get("value", d) if isinstance(d, dict) else d

    def close(self):
        try:
            self.s.close()
        except Exception:
            pass


def _ff() -> _Marionette:
    try:
        mc = _Marionette()
    except OSError as e:
        raise HTTPException(503, f"Marionette not listening on 127.0.0.1:{_FF_PORT} — start "
                                 f"Firefox with --marionette (tools_linux/ff_tabs.py marionette-on). "
                                 f"ISSUES.md #F1/#F2. ({e})")
    try:
        mc.cmd("WebDriver:NewSession", {"capabilities": {}})
    except Exception:
        pass                            # session already exists — fine
    return mc


def _ff_tabs(mc: _Marionette) -> list[dict]:
    tabs = []
    for hid in mc.cmd("WebDriver:GetWindowHandles"):
        mc.cmd("WebDriver:SwitchToWindow", {"handle": hid})
        # NOTE: GetCurrentUrl is unreliable (returns None) — titles only. ISSUES.md #F2
        tabs.append({"id": hid, "title": str(mc.cmd("WebDriver:GetTitle") or "")})
    return tabs


def _ff_target(mc: _Marionette, needle: str | None) -> str:
    """Resolve a tab by id-prefix OR title substring; default = current tab."""
    tabs = _ff_tabs(mc)
    if not needle:
        if not tabs:
            raise HTTPException(404, "no tabs open")
        mc.cmd("WebDriver:SwitchToWindow", {"handle": tabs[0]["id"]})
        return tabs[0]["id"]
    n = needle.lower()
    for t in tabs:
        if t["id"].startswith(needle) or n in t["title"].lower():
            mc.cmd("WebDriver:SwitchToWindow", {"handle": t["id"]})
            return t["id"]
    raise HTTPException(404, f"no tab matches {needle!r}. open tabs: "
                             + ", ".join(t["title"][:40] for t in tabs))


@app.get("/ff/tabs", dependencies=[Depends(auth)])
def ff_tabs_list():
    mc = _ff()
    try:
        return {"tabs": _ff_tabs(mc), "count": len(_ff_tabs(mc))}
    finally:
        mc.close()


@app.post("/ff/state", dependencies=[Depends(auth)])
def ff_state(body: dict):
    mc = _ff()
    try:
        _ff_target(mc, body.get("tab"))
        return {"state": mc.cmd("WebDriver:ExecuteScript", {"script": _FF_STATE_JS, "args": []})}
    finally:
        mc.close()


@app.post("/ff/js", dependencies=[Depends(auth)])
def ff_js(body: dict):
    """Arbitrary JS in a tab: {"tab": "gmail", "script": "return document.title"}"""
    script = body.get("script")
    if not script:
        raise HTTPException(400, 'need {"script": "return ..."}')
    mc = _ff()
    try:
        _ff_target(mc, body.get("tab"))
        return {"result": mc.cmd("WebDriver:ExecuteScript",
                                 {"script": script, "args": body.get("args") or []})}
    finally:
        mc.close()


@app.post("/ff/click", dependencies=[Depends(auth)])
def ff_click(body: dict):
    """Click by visible TEXT — immune to dock/focus/zoom. {"tab": "...", "text": "Continue"}"""
    text = str(body.get("text") or "")
    if not text:
        raise HTTPException(400, 'need {"text": "button label"}')
    mc = _ff()
    try:
        _ff_target(mc, body.get("tab"))
        r = mc.cmd("WebDriver:ExecuteScript", {"script": _FF_CLICK_JS, "args": [text]})
        if str(r).startswith("NOTFOUND"):
            return {"ok": False, "result": r, "hint": "check /ff/state for real button labels"}
        return {"ok": True, "result": r}
    finally:
        mc.close()


@app.post("/ff/close", dependencies=[Depends(auth)])
def ff_close(body: dict):
    """Close tabs BY TITLE — one call, many tabs. {"tabs": ["Just a moment", "Domain Portfolio"]}
    or {"tab": "<id-prefix|title>"} for a single one. Never closes the last remaining window."""
    needles = body.get("tabs") or ([body["tab"]] if body.get("tab") else [])
    if not needles:
        raise HTTPException(400, 'need {"tabs": ["title substring", ...]}')
    mc = _ff()
    closed, failed = [], []
    try:
        for needle in needles:
            try:
                tabs = _ff_tabs(mc)
                targets = [t for t in tabs
                           if str(t["id"]).startswith(str(needle))
                           or str(needle).lower() in t["title"].lower()]
                if not targets:
                    failed.append({"needle": needle, "why": "no match"})
                    continue
                if len(tabs) - len(targets) < 1:      # keep at least one window
                    targets = targets[:max(1, len(tabs) - 1)]
                for t in targets:
                    mc.cmd("WebDriver:SwitchToWindow", {"handle": t["id"]})
                    mc.cmd("WebDriver:CloseWindow", {})
                    closed.append(t["title"][:60])
            except Exception as e:
                failed.append({"needle": needle, "why": str(e)})
        remaining = [t["title"][:40] for t in _ff_tabs(mc)]
        return {"closed": closed, "failed": failed, "remaining": remaining}
    finally:
        mc.close()


@app.post("/ff/open", dependencies=[Depends(auth)])
def ff_open(body: dict):
    """Open a URL in a new tab: {"url": "https://..."}"""
    url = str(body.get("url") or "")
    if not url:
        raise HTTPException(400, 'need {"url": "..."}')
    mc = _ff()
    try:
        mc.cmd("WebDriver:NewWindow", {"type": "tab"})
        mc.cmd("WebDriver:NavigateTo", {"url": url})
        return {"opened": url, "title": str(mc.cmd("WebDriver:GetTitle") or "")}
    finally:
        mc.close()


# ---------- 🛡 BLAST RADIUS GUARD ----------
# We killed our own tunnel once with a broad pkill (ISSUES.md #S6).
# These commands are refused unless force=true.
_GUARD_CRITICAL = ("cloudflared", "agent-tunnel", "agent-bridge", "bridge_v3")
_GUARD_VERB = re.compile(r"\b(pkill|killall|kill|systemctl|systemd-run|sudo\s+kill)\b")


def _blast_guard(cmd: str) -> str | None:
    if not cmd or not _GUARD_VERB.search(cmd):
        return None
    low = cmd.lower()
    hits = [n for n in _GUARD_CRITICAL if n in low]
    if not hits:
        return None
    return ("⛔ REFUSED: this command can kill the bridge or the tunnel (" + ", ".join(hits) +
            ") and that severs the agent's only connection — it happened once (ISSUES.md #S6). "
            "Use /safe-kill for app cleanup. Pass force=true if you truly mean it.")


@app.post("/safe-kill", dependencies=[Depends(auth)])
def safe_kill(body: dict):
    """Kill an APP by name, refusing anything that carries the bridge/tunnel."""
    name = str(body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, 'need {"name": "firefox"}')
    if any(n in name.lower() for n in _GUARD_CRITICAL):
        raise HTTPException(403, f"{name!r} is critical to the remote session — refusing.")
    r = subprocess.run(["pkill", "-x", name], capture_output=True, text=True, timeout=10)
    alive = subprocess.run(["pgrep", "-x", name], capture_output=True, text=True, timeout=10)
    return {"killed": name, "rc": r.returncode,
            "still_running": bool(alive.stdout.strip()),
            "stderr": r.stderr.strip()[:200]}


# ---------- 🩺 ONE-CALL STATUS ----------
STARTED = time.time()
BRIDGE_VERSION = "3.2"


def _tunnel_url() -> str | None:
    try:
        r = subprocess.run(["journalctl", "--user", "-u", "agent-tunnel", "-n", "200", "--no-pager"],
                           capture_output=True, text=True, timeout=5)
        found = re.findall(r"https://[a-z0-9-]+\.trycloudflare\.com", r.stdout or "")
        return found[-1] if found else None
    except Exception:
        return None


@app.get("/status", dependencies=[Depends(auth)])
def status():
    """Everything about the box in ONE call — use this first after a reconnect."""
    out: dict = {"version": BRIDGE_VERSION, "uptime_s": int(time.time() - STARTED),
                 "pid": os.getpid(), "session": os.environ.get("XDG_SESSION_TYPE", "?"),
                 "wayland": _is_wayland(), "hot_tools": list(_HOT_TOOLS)}

    def _probe(key, fn):
        try:
            out[key] = fn()
        except Exception as e:
            out[key] = f"ERR: {type(e).__name__}: {e}"

    _probe("tunnel_url", _tunnel_url)
    _probe("ydotoold", lambda: subprocess.run(["pgrep", "-x", "ydotoold"],
                                              capture_output=True, timeout=3).returncode == 0)
    _probe("firefox", lambda: subprocess.run(["pgrep", "-x", "firefox"],
                                             capture_output=True, timeout=3).returncode == 0)
    _probe("disk_free_gb", lambda: round(shutil.disk_usage(str(Path.home())).free / 2 ** 30, 1))

    def _ff_count():
        mc = _ff()
        try:
            tabs = _ff_tabs(mc)
            return {"count": len(tabs), "titles": [t["title"][:45] for t in tabs]}
        finally:
            mc.close()
    _probe("ff_tabs", _ff_count)
    out["jobs_running"] = sum(1 for j in JOBS.values() if j["proc"].poll() is None)
    return out


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
    return {"ok": True, "server": f"local-system-bridge v{BRIDGE_VERSION}",
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
    force: bool = False     # bypass the bridge/tunnel self-kill guard (#S6)


@app.post("/run", dependencies=[Depends(auth)])
def run(body: RunBody):
    warn = _blast_guard(body.command)
    if warn and not body.force:
        raise HTTPException(409, warn)
    return {"result": run_command(body.command, body.cwd)}


# ======================================================================
if __name__ == "__main__":
    src = "BRIDGE_TOKEN env" if os.environ.get("BRIDGE_TOKEN") else "random per start"
    print("=" * 66)
    print(f"  local-system-bridge v{BRIDGE_VERSION} · god mode (Wayland-hardened + speed layer)")
    print(f"  TOKEN ({src}): {TOKEN}")
    print("  shells (multi) · jobs · files · Wayland screenshot/shot · cron")
    print("  GUI: open/click/click2/safeclick/type/keys/rawkeys · /gui/batch")
    print("  FF : /ff/tabs /ff/click /ff/close /ff/js  (Marionette, no pixels)")
    print("  INFO: /status (tunnel url, tabs, daemons) · /safe-kill · /tools/add")
    print("  audit log:", AUDIT_LOG)
    print("  tunnel: cloudflared tunnel --url http://127.0.0.1:8765")
    print("  READ ISSUES.md FIRST — it encodes a full day of debugging pain.")
    print("=" * 66)
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")

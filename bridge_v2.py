"""
bridge_v2.py — local-system-bridge v2
=====================================
Everything v1 had, PLUS fixes for all six limitations:

  ✔ persistent interactive shell   → cd / exports / aliases SURVIVE between calls
  ✔ interactive programs           → answer sudo prompts, send keys (Ctrl+C, q in top)
  ✔ no 60s wall                    → background JOBS you can poll live while they run
  ✔ real file transfer             → binary-safe upload & download, no size cap
  ✔ GUI                            → screenshot endpoint (needs a logged-in desktop)
  ✔ proactivity                    → manage crontab remotely so things run on their own

Run (inside your venv):
    pip install fastapi uvicorn pexpect mss          # 'mss' optional (screenshot)
    python bridge_v2.py

Optional:  BRIDGE_TOKEN=my-fixed-token python bridge_v2.py   (stable token)
Then open the tunnel:  cloudflared tunnel --url http://127.0.0.1:8765
"""

from __future__ import annotations

import os
import re
import secrets
import signal
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path

import uvicorn
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
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

app = FastAPI(title="local-system-bridge v2", docs_url=None, redoc_url=None)


async def auth(x_bridge_token: str = Header(default="")):
    if not secrets.compare_digest(x_bridge_token, TOKEN):
        raise HTTPException(status_code=401, detail="invalid token")


# ======================================================================
# 1+2 · PERSISTENT, INTERACTIVE SHELL  (one living bash session)
# ======================================================================
_SHELL_MARKER = "==BRIDGE-PROMPT-a91kx7=="
_ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")


class Shell:
    """A single long-lived interactive bash in a pty.

    State (cwd, env vars, aliases) persists across calls, and prompts like
    sudo's password can be answered with /shell/type."""

    def __init__(self):
        import pexpect

        self.sh = pexpect.spawn(
            "/bin/bash", ["--norc", "-i"],
            encoding="utf-8", timeout=30, echo=True,
            cwd=str(Path.home()), dimensions=(40, 220),
        )
        self.lock = threading.Lock()
        # clean marker prompt, no prompt-command noise, no bracketed-paste escapes
        self.sh.sendline(
            f'export PS1="{_SHELL_MARKER}"; unset PROMPT_COMMAND; '
            "bind 'set enable-bracketed-paste off' 2>/dev/null"
        )
        self.sh.expect(_SHELL_MARKER)          # matches the echoed command
        self.sh.expect(_SHELL_MARKER, timeout=10)  # matches the new prompt
        self.sh.before = ""                    # discard init noise

    @staticmethod
    def _clean(text: str) -> str:
        text = _ANSI.sub("", text)
        return text.replace("\r\n", "\n").rstrip("\n")

    def run(self, command: str, timeout: int = 30) -> str:
        """Send a line, wait for the shell prompt, return everything printed."""
        with self.lock:
            self.sh.timeout = max(1, min(timeout, 600))
            self.sh.sendline(command)
            try:
                self.sh.expect(_SHELL_MARKER)
            except Exception:
                partial = self._clean(self.sh.before or "")
                msg = ("Error: no prompt within the timeout — the command may "
                       "still be running or waiting for input. Use /shell/drain "
                       "to inspect, /shell/type to send input or \\x03 for Ctrl+C.")
                return msg + (f"\nPartial output so far:\n{partial}" if partial else "")
            return self._clean(self.sh.before)

    def type(self, text: str, wait: bool = False, timeout: int = 15) -> str:
        """Send raw input WITHOUT waiting for a prompt (passwords, keys, Ctrl+C=\\x03)."""
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
        """Read whatever is currently on the 'screen' without sending anything."""
        chunks = []
        try:
            while True:
                chunks.append(self.sh.read_nonblocking(size=8192, timeout=0.15))
        except Exception:
            pass
        return self._clean("".join(chunks))


_SHELL: Shell | None = None


def get_shell() -> Shell:
    global _SHELL
    if _SHELL is None:
        _SHELL = Shell()
    return _SHELL


class ShellRun(BaseModel):
    command: str
    timeout: int = 30


class ShellType(BaseModel):
    text: str
    wait: bool = False


@app.post("/shell/run", dependencies=[Depends(auth)])
def shell_run(body: ShellRun):
    return {"output": get_shell().run(body.command, body.timeout)}


@app.post("/shell/type", dependencies=[Depends(auth)])
def shell_type(body: ShellType):
    return {"output": get_shell().type(body.text, body.wait)}


@app.get("/shell/drain", dependencies=[Depends(auth)])
def shell_drain():
    return {"output": get_shell().drain()}


# ======================================================================
# 3 · BACKGROUND JOBS  (start long tasks, poll live, no timeout wall)
# ======================================================================
JOBS_DIR = Path(tempfile.gettempdir()) / "bridge_jobs"
JOBS_DIR.mkdir(exist_ok=True)
JOBS: dict[str, dict] = {}


class JobStart(BaseModel):
    command: str
    cwd: str | None = None


@app.post("/jobs/start", dependencies=[Depends(auth)])
def jobs_start(body: JobStart):
    cwd = Path(body.cwd).expanduser() if body.cwd else Path.home()
    if not cwd.exists():
        raise HTTPException(400, f"cwd not found: {cwd}")
    jid = uuid.uuid4().hex[:8]
    log = open(JOBS_DIR / f"{jid}.log", "w", buffering=1, encoding="utf-8", errors="replace")
    proc = subprocess.Popen(
        body.command, shell=True, stdout=log, stderr=subprocess.STDOUT,
        cwd=str(cwd), text=True, start_new_session=True,   # own process group
    )
    JOBS[jid] = {"proc": proc, "log": log, "cmd": body.command,
                 "started": time.strftime("%H:%M:%S")}
    return {"job_id": jid, "hint": f"poll with GET /jobs/{jid}?offset=0"}


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
    rc = job["proc"].poll()
    return {
        "running": rc is None,
        "exit_code": rc,
        "cmd": job["cmd"],
        "output_since_offset": chunk,
        "next_offset": size,          # pass this back as ?offset= on the next poll
    }


@app.post("/jobs/{jid}/kill", dependencies=[Depends(auth)])
def jobs_kill(jid: str):
    job = JOBS.get(jid)
    if not job:
        raise HTTPException(404, "unknown job_id")
    try:
        os.killpg(os.getpgid(job["proc"].pid), signal.SIGKILL)
        return {"killed": jid}
    except ProcessLookupError:
        return {"killed": False, "detail": "process already gone"}


# ======================================================================
# 4 · FILE TRANSFER  (binary-safe, no size cap)
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
        while chunk := await file.read(1024 * 1024):   # 1 MB chunks
            f.write(chunk)
            size += len(chunk)
    return {"saved_to": str(dest), "bytes": size}


# ======================================================================
# 5 · SCREENSHOT  (needs a logged-in graphical session on this PC)
# ======================================================================
@app.get("/screenshot", dependencies=[Depends(auth)])
def screenshot():
    dest = Path(tempfile.gettempdir()) / f"shot_{int(time.time())}.png"
    for cmd in (["gnome-screenshot", "-f", str(dest)], ["scrot", str(dest)]):
        try:
            subprocess.run(cmd, check=True, timeout=15,
                           capture_output=True)
            return FileResponse(dest, filename="screenshot.png")
        except FileNotFoundError:
            continue
        except Exception:
            continue
    try:
        import mss
        with mss.mss() as sct:
            sct.shot(output=str(dest))
        return FileResponse(dest, filename="screenshot.png")
    except Exception as exc:
        raise HTTPException(
            500,
            f"No screenshot tool worked ({exc}). "
            "Install one: sudo apt install gnome-screenshot",
        )


# ======================================================================
# 6 · CRON  (proactivity: things that run while you're not chatting)
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
    line: str   # e.g. "*/5 * * * * df -h >> /home/cid/disk.log 2>&1"


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


# ======================================================================
# v1 endpoints kept for compatibility
# ======================================================================
@app.get("/health")
def health():
    return {"ok": True, "server": "local-system-bridge v2"}


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
    src = "from BRIDGE_TOKEN env" if os.environ.get("BRIDGE_TOKEN") else "random (new each start)"
    print("=" * 64)
    print("  local-system-bridge v2")
    print(f"  TOKEN ({src}): {TOKEN}")
    print("  New powers:")
    print("    /shell/*    persistent interactive bash (sudo prompts work!)")
    print("    /jobs/*     background tasks, poll live, no 60s wall")
    print("    /files/*    binary upload & download")
    print("    /screenshot see the desktop")
    print("    /cron/*     schedule things to run on their own")
    print("  Tunnel: cloudflared tunnel --url http://127.0.0.1:8765")
    print("=" * 64)
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")

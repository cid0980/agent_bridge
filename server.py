"""
Local System Bridge — MCP Server
================================
Exposes tools so an AI agent can ask questions and get answers
directly from THIS computer:

  - read_file_content : read a file's contents
  - list_directory    : list folders & files
  - get_system_info   : OS, CPU, RAM, disk, uptime
  - run_command       : run a shell command and return its output

Run it:  python server.py        (or: fastmcp run server.py)
Test it: fastmcp dev server.py
"""

from __future__ import annotations

import os
import platform
import shutil
import socket
import subprocess
import time
from pathlib import Path

from fastmcp import FastMCP

mcp = FastMCP("local-system-bridge")

MAX_CHARS = 20_000   # safety cap so a huge file / output doesn't flood the agent
CMD_TIMEOUT = 60     # seconds


def _truncate(text: str, limit: int = MAX_CHARS) -> str:
    if len(text) > limit:
        return text[:limit] + f"\n\n… [truncated: {len(text) - limit} more chars]"
    return text


# ---------------------------------------------------------------- read file
@mcp.tool()
def read_file_content(path: str, max_chars: int = MAX_CHARS) -> str:
    """Read a text file on this computer and return its contents.

    Args:
        path: Absolute or ~ path to the file.
        max_chars: Cap on returned characters (default 20000).
    """
    file_path = Path(path).expanduser()
    if not file_path.exists():
        return f"Error: file not found -> {file_path}"
    if file_path.is_dir():
        return f"Error: {file_path} is a directory. Use list_directory instead."
    try:
        text = file_path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        return f"Error reading {file_path}: {exc}"
    header = f"# {file_path} ({len(text)} chars)\n\n"
    return _truncate(header + text, max_chars)


# ------------------------------------------------------------ list directory
@mcp.tool()
def list_directory(path: str = "~", max_entries: int = 300) -> str:
    """List files and folders in a directory on this computer.

    Args:
        path: Directory to list (default: user home).
        max_entries: Max entries returned (default 300).
    """
    dir_path = Path(path).expanduser()
    if not dir_path.exists():
        return f"Error: directory not found -> {dir_path}"
    if not dir_path.is_dir():
        return f"Error: {dir_path} is a file. Use read_file_content instead."

    lines = [f"# Contents of {dir_path}\n"]
    try:
        entries = sorted(dir_path.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
    except Exception as exc:
        return f"Error listing {dir_path}: {exc}"

    for i, entry in enumerate(entries):
        if i >= max_entries:
            lines.append(f"... and {len(entries) - max_entries} more entries")
            break
        if entry.is_dir():
            lines.append(f"[dir]  {entry.name}/")
        else:
            try:
                size = entry.stat().st_size
            except OSError:
                size = -1
            lines.append(f"       {entry.name}  ({size} bytes)")
    return "\n".join(lines)


# -------------------------------------------------------------- system info
@mcp.tool()
def get_system_info() -> dict:
    """Return basic info about this computer: OS, CPU, RAM, disk, network."""
    info: dict = {
        "os": f"{platform.system()} {platform.release()} ({platform.version()})",
        "machine": platform.machine(),
        "cpu_cores": os.cpu_count(),
        "hostname": socket.gethostname(),
        "user": os.environ.get("USERNAME") or os.environ.get("USER") or "unknown",
        "python": platform.python_version(),
        "uptime_hours": _uptime_hours(),
        "current_working_dir": str(Path.cwd()),
    }

    # RAM (uses psutil if installed, otherwise /proc on Linux)
    info["ram"] = _ram_info()

    # Disk for the home drive
    try:
        usage = shutil.disk_usage(str(Path.home()))
        info["disk"] = {
            "total_gb": round(usage.total / 1024**3, 1),
            "used_gb": round(usage.used / 1024**3, 1),
            "free_gb": round(usage.free / 1024**3, 1),
        }
    except Exception:
        info["disk"] = "unavailable"
    return info


def _ram_info():
    try:
        import psutil  # optional dependency

        vm = psutil.virtual_memory()
        return {"total_gb": round(vm.total / 1024**3, 1),
                "available_gb": round(vm.available / 1024**3, 1),
                "percent_used": vm.percent}
    except ImportError:
        pass
    try:  # Linux fallback
        meminfo = Path("/proc/meminfo").read_text()
        total = next(int(l.split()[1]) for l in meminfo.splitlines() if l.startswith("MemTotal"))
        avail = next(int(l.split()[1]) for l in meminfo.splitlines() if l.startswith("MemAvailable"))
        return {"total_gb": round(total / 1024**2, 1), "available_gb": round(avail / 1024**2, 1)}
    except Exception:
        return "unavailable (install psutil for RAM stats: pip install psutil)"


def _uptime_hours():
    try:
        if hasattr(time, "boottime"):
            return round((time.time() - time.boottime()) / 3600, 1)
        with open("/proc/uptime") as f:
            return round(float(f.read().split()[0]) / 3600, 1)
    except Exception:
        return "unavailable"


# --------------------------------------------------------------- run command
@mcp.tool()
def run_command(command: str, cwd: str | None = None) -> str:
    """Run a shell command on this computer and return its output.

    Args:
        command: The command to run (e.g. "ipconfig", "dir", "python --version").
        cwd: Optional working directory for the command.
    """
    workdir = Path(cwd).expanduser() if cwd else Path.home()
    if not workdir.exists():
        return f"Error: working directory not found -> {workdir}"
    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=CMD_TIMEOUT,
            cwd=str(workdir),
        )
        output = ""
        if result.stdout:
            output += f"STDOUT:\n{result.stdout}\n"
        if result.stderr:
            output += f"STDERR:\n{result.stderr}\n"
        output += f"(exit code: {result.returncode})"
        return _truncate(output)
    except subprocess.TimeoutExpired:
        return f"Error: command timed out after {CMD_TIMEOUT}s (increase CMD_TIMEOUT in server.py if needed)"
    except Exception as exc:
        return f"Error running command: {exc}"


# ------------------------------------------------------------------- entry
if __name__ == "__main__":
    mcp.run()  # stdio transport — the client (Claude Desktop / Cursor) launches this

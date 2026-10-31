"""
bridge_server.py — HTTP bridge so the Arena agent can reach THIS PC.

Reuses the 4 tools from server.py (your MCP server) and exposes them
as simple HTTP endpoints behind a random access token, so the agent
can call them with curl through a Cloudflare Tunnel.

Setup (inside your venv):
    pip install fastapi uvicorn
    python bridge_server.py

It prints a TOKEN at startup — together with your tunnel URL, that is
what you paste to the agent in chat. Nothing works without the token.
"""

import secrets

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel

import server  # your MCP server module — we reuse its tool logic


def _unwrap(tool):
    """FastMCP wraps functions in Tool objects; get the raw callable."""
    return getattr(tool, "fn", tool)


read_file_content = _unwrap(server.read_file_content)
list_directory = _unwrap(server.list_directory)
get_system_info = _unwrap(server.get_system_info)
run_command = _unwrap(server.run_command)

TOKEN = secrets.token_urlsafe(24)
HOST, PORT = "127.0.0.1", 8765

app = FastAPI(title="local-system-bridge", docs_url=None, redoc_url=None)


async def auth(x_bridge_token: str = Header(default="")):
    if not secrets.compare_digest(x_bridge_token, TOKEN):
        raise HTTPException(status_code=401, detail="invalid token")


class RunBody(BaseModel):
    command: str
    cwd: str | None = None


@app.get("/health")
def health():
    return {"ok": True, "server": "local-system-bridge"}   # public, no token


@app.get("/system-info", dependencies=[Depends(auth)])
def system_info():
    return get_system_info()


@app.get("/list", dependencies=[Depends(auth)])
def ls(path: str = "~"):
    return {"result": list_directory(path)}


@app.get("/read", dependencies=[Depends(auth)])
def read(path: str):
    return {"result": read_file_content(path)}


@app.post("/run", dependencies=[Depends(auth)])
def run(body: RunBody):
    return {"result": run_command(body.command, body.cwd)}


if __name__ == "__main__":
    print("=" * 62)
    print("  local-system-bridge (HTTP mode) is starting")
    print(f"  TOKEN: {TOKEN}")
    print("  1. Keep this terminal open")
    print("  2. In a SECOND terminal run:")
    print(f"       cloudflared tunnel --url http://{HOST}:{PORT}")
    print("  3. Paste the https://....trycloudflare.com URL and this")
    print("     TOKEN to your agent in the chat")
    print("=" * 62)
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")

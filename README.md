# 🌉 agent_bridge — give an AI agent hands on your machine

A small bridge that lets a cloud AI agent (Arena Agent Mode, or any tool-using agent)
**read, run, watch, and control things on YOUR computer** — files, shell, apps, screen,
scheduled tasks — through one HTTPS URL + one token.

Built and battle-tested live: file written onto a real desktop through a Cloudflare
tunnel, git pushed, and the bridge hot-swapped itself — all by the agent, from chat.

---

## ✨ What's inside

| File | What it is |
|---|---|
| `bridge_v3.py` | **The main server. God mode.** Everything below runs from this one file. |
| `bridge_server.py` / `bridge_v2.py` | Earlier generations, kept for history/learning |
| `server.py` | The **MCP-native** version — use this one with Cursor / VS Code / Claude |
| `test_client.py` | Local smoke test |
| `install.sh` / `install.ps1` | One-command installers (Linux/macOS, Windows) |
| `agent/bridge.sh` | Helper functions an agent sources to drive the bridge |

## 🧠 v3 superpowers

- `/shell/*` — **persistent interactive shells** (multi-session): cwd, exports, sudo prompts survive. `/shell/type` answers prompts; `Ctrl+C` = `\x03`
- `/jobs/*` — **background jobs** with live-offset polling — builds run for hours, no timeouts
- `/files/*` — **binary-safe** upload/download, any size
- `/screenshot` + `/gui/open` + `/gui/click` + `/gui/type` — **see the desktop, open apps, click and type** (click/type need `ydotool` on Linux)
- `/cron/*` — **schedule** things that run while nobody's chatting
- `/install` — the agent **installs what it needs** (pip/apt/snap) instead of waiting for you
- `/tools/add` — **the agent writes NEW endpoints into the live bridge** — no restart, no redeploy. It literally extends itself and they persist across reboots (`hot_tools.json`)
- `/admin/audit` — every request logged to `audit.log`. Trust, but verify.
- v1 compat: `/run` `/read` `/list` `/system-info`

## 🚀 Quick install — any PC

**Linux / macOS**
```bash
git clone https://github.com/cid0980/agent_bridge.git
cd agent_bridge
./install.sh          # venv + deps + token + cloudflared + autostart (systemd user)
```

**Windows** (PowerShell)
```powershell
git clone https://github.com/cid0980/agent_bridge.git
cd agent_bridge
powershell -ExecutionPolicy Bypass -File install.ps1
```

Then (any OS):
```bash
cloudflared tunnel --url http://127.0.0.1:8765        # prints your https://…trycloudflare.com URL
```
**Paste the URL + token to your agent. Done — it now has hands on your PC.**

> Repo is private right now → clone with a GitHub PAT:
> `git clone https://<your-PAT>@github.com/cid0980/agent_bridge.git`

### Named tunnel (optional, recommended)
Quick-tunnel URLs change every restart. For a **permanent** URL:
```bash
cloudflared tunnel login            # opens browser, links your CF account
cloudflared tunnel create agent-bridge
cloudflared tunnel route dns agent-bridge bridge.<your-domain>
# update the systemd unit / run command accordingly — see cloudflared docs
```

## 🤖 Agent cheat-sheet (curl)

```bash
H="X-Bridge-Token: <token>"; U="https://<url>.trycloudflare.com"

curl -H "$H" "$U/system-info"                                   # machine vitals
curl -H "$H" -X POST -H 'Content-Type: application/json' \
     -d '{"command":"cd ~/projects && ls"}' "$U/shell/run"      # persistent shell
curl -H "$H" -X POST -H 'Content-Type: application/json' \
     -d '{"command":"flutter build apk"}' "$U/jobs/start"       # background job
curl -H "$H" "$U/jobs/<id>?offset=0"                            # live output
curl -H "$H" "$U/screenshot" -o shot.png                        # see the desktop
curl -H "$H" -X POST -H 'Content-Type: application/json' \
     -d '{"target":"firefox"}' "$U/gui/open"                    # open an app
curl -H "$H" -X POST -H 'Content-Type: application/json' \
     -d '{"name":"disk_alert","code":"def run(p):\n import shutil\n u=shutil.disk_usage(\"/\")\n return {\"free_gb\": round(u.free/1e9,1)}"}' \
     "$U/tools/add"                                             # hot-load new power
curl -H "$H" -X POST "$U/tools/disk_alert"                      # call it, immediately live
```

Agents: `source agent/bridge.sh` after exporting `BRIDGE_URL` + `BRIDGE_TOKEN` and
you get `bshell`, `bjob`, `bshot`, `bimg`, `bapp`, `binstall` …

## 🔒 Security notes (read these)

- The token is the **only** lock on the door. Anyone with URL + token gets a shell as your user — including `sudo` via the interactive shell. Never post them publicly; close the tunnel when done.
- Every request is logged to `~/local-mcp-bridge/audit.log`.
- Quick-tunnel traffic terminates at Cloudflare's edge. For stricter setups use a named tunnel + **Cloudflare Access**, or restrict to localhost-only and skip the tunnel entirely for LAN use.
- `/tools/add` runs arbitrary Python — that's the point (god mode), and it's why the token matters.
- Secrets typed into `/shell/type` (e.g. sudo passwords) appear in `audit.log` — it's chmod-600-worthy on multi-user machines.

## 🗺 Roadmap / known limits
- Named tunnel not configured automatically (needs your Cloudflare login once)
- `ydotool` needs its daemon for click/type: `sudo apt install ydotool && ydotoold &`
- Job registry is in-memory (log files survive restarts; tracking doesn't)
- The remote PC cannot initiate contact with the chat — it can only leave you files/logs

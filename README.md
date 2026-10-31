# 🌉 agent_bridge — give an AI agent hands on your machine

A small bridge that lets a cloud AI agent (Arena Agent Mode, or any tool-using agent)
**read, run, watch, and control things on YOUR computer** — files, shell, apps, screen,
scheduled tasks — through one HTTPS URL + one token.

Built and battle-tested live: on its proving day it hot-loaded tools into itself, debugged
Wayland screenshots, tamed a polkit daemon, completed a multi-step Zoho wizard through
**both** pixel-clicks and DOM-level Marionette automation, and survived a Firefox restart
with full session rescue. Every scar from that day is documented in **[ISSUES.md](ISSUES.md)** —
read it before deploying anywhere new, and the next machine skips the entertainment.

---

## ✨ What's inside

| File | What it is |
|---|---|
| `bridge_v3.py` | **The main server (v3.1). God mode.** Everything below runs from this one file. |
| `bridge_server.py` / `bridge_v2.py` | Earlier generations, kept for history/learning |
| `server.py` | The **MCP-native** version — use this one with Cursor / VS Code / Claude |
| `test_client.py` | Local smoke test |
| `install.sh` / `install.ps1` | One-command installers (Linux/macOS, Windows) |
| `agent/bridge.sh` | Helper functions an agent sources to drive the bridge |
| **`ISSUES.md`** | **The field manual:** every bug/footgun from battle-testing + the fix for each. |
| `tools_linux/portal_shot.py` | Wayland-safe screenshot helper (xdg portal, subscribe-first pattern) |
| `tools_linux/ff_tabs.py` | **Firefox Marionette driver** — click buttons by TEXT, close/restore tabs, sessionstore rescue |

## 🧠 v3.1 superpowers

- `/shell/*` — **persistent interactive shells** (multi-session): cwd, exports, sudo prompts survive. `/shell/type` answers prompts; `Ctrl+C` = `\x03`
- `/jobs/*` — **background jobs** with live-offset polling — builds run for hours; also the fix for quick-tunnel ~100 s request drops (ISSUES.md #T1)
- `/files/*` — **binary-safe** upload/download, any size
- `/screenshot` / `POST /shot` — **Wayland-aware** (portal path = black-screen-proof on GNOME/Wayland; ISSUES.md #W1). `/shot` returns base64 JSON for one-call vision loops
- `/gui/open` `/gui/click` — plus **`/gui/click2`** (human-like slow click; zero-latency bursts get dropped on Wayland — ISSUES.md #G1) and **`/gui/safeclick`** (dock-proof: parks the pointer mid-screen so the auto-hide GNOME dash retracts before clicking — #G2)
- `/gui/keys` + `/gui/rawkeys` — full keyboard chords (ctrl/shift/alt/super combos, f1–f12, nav) and raw evdev codes. Always verify each step with `/shot` (#G3)
- `tools_linux/ff_tabs.py` — **the escape hatch:** `list / state / click <tab-title> <button-text> / close / restore-tabs / marionette-on` — DOM-level Firefox control, immune to pixel problems (#F1)
- `/cron/*` — **schedule** things that run while nobody's chatting
- `/install` — the agent **installs what it needs** (pip/apt/snap) instead of waiting for you
- `/tools/add` — **the agent writes NEW endpoints into the live bridge** — no restart, no redeploy. It literally extends itself; tools persist across reboots (`hot_tools.json`)
- `/admin/audit` — every request logged to `audit.log`. Trust, but verify.
- v1 compat: `/run` `/read` `/list` `/system-info`

## 🚀 Quick install — any PC

**Linux / macOS**
```bash
git clone https://github.com/cid0980/agent_bridge.git
cd agent_bridge
./install.sh          # venv + deps + token + cloudflared + autostart (systemd user)
                      # + ydotool/python3-gi + input group on Linux → then LOG OUT & BACK IN once
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

After install on Linux, do the one thing no installer can skip: **log out and back in** so
the `input` group membership applies (ISSUES.md #D1). GUI control then works prompt-free.

### Named tunnel (optional, recommended)
Quick-tunnel URLs change every restart (ISSUES.md #T2). For a **permanent** URL:
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
     -d '{"command":"flutter build apk"}' "$U/jobs/start"       # background job (long ops!)
curl -H "$H" "$U/jobs/<id>?offset=0"                            # live output
curl -H "$H" -X POST "$U/shot"                                  # one-call vision (base64 png)
curl -H "$H" -X POST -H 'Content-Type: application/json' \
     -d '{"x":400,"y":300}' "$U/gui/safeclick"                  # dock-proof click (Wayland!)
curl -H "$H" -X POST -H 'Content-Type: application/json' \
     -d '{"combo":"ctrl+shift+t"}' "$U/gui/keys"                # keyboard chords
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
- While `firefox --marionette` runs, 127.0.0.1:2828 is an unauthenticated browser remote — restart Firefox to close it (ISSUES.md #F4).

## 🗺 Roadmap / known limits
- Named tunnel not configured automatically (needs your Cloudflare login once)
- Job registry is in-memory (log files survive restarts; tracking doesn't)
- The remote PC cannot initiate contact with the chat — it can only leave you files/logs

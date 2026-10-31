# ISSUES.md — agent_bridge field manual

Everything that went wrong setting up and driving this bridge on a real
GNOME/Wayland Ubuntu desktop (Oct 31, 2026), why it went wrong, what fixed it,
and how to avoid every one of these traps on a fresh PC.

> **TL;DR golden path:** run `install.sh` as the desktop user (it now installs
> `ydotool` + `python3-gi`, puts you in the `input` group, enables
> `ydotool.service`), **log out & back in once**, prefer a **named Cloudflare
> tunnel** over quick tunnels, and for any stubborn web UI use
> `tools_linux/ff_tabs.py <cmd>` (Marionette DOM clicks) instead of pixel-clicking.
> If you do exactly that, none of the issues below can bite you.

---

## 0. Symptom → first-aid table

| Symptom | Jump to |
|---|---|
| `ModuleNotFoundError: mcp.server.fastmcp` | #C1 |
| sudo installs hang / password prompt via bridge | #S1, #S2 |
| Tunnel responds with empty body / request hangs | #T1 |
| Old tunnel URL dead (curl exit 6 / HTTP 000) | #T2 |
| Screenshot is a 6 KB black PNG on Wayland | #W1 |
| Bridge went dark right after a cleanup command | #S6 (you killed cloudflared) |
| gnome-screenshot hangs then segfaults | #W2 |
| Screenshot D-Bus call = `AccessDenied` | #W3 |
| Portal screenshot call hangs ~60 s then times out | #W4 |
| `ydotoold` exits silently / socket EADDRINUSE | #D2 |
| `systemd-run` ydotoold unit keeps flapping inactive | #D3 |
| User-mode ydotoold: `Permission denied` on /dev/uinput | #D4 |
| pkexec'd daemon dies the moment pkexec returns | #D5 |
| `pgrep ydotoold` matches my own probe → wrong kill | #S3 |
| Clicks "work" (hover/tooltip visible) but buttons ignore them | #G1 |
| Agent takes a screenshot per action (slow) | #G8 (use /gui/batch + region crop) |
| GNOME dock pops up and eats clicks near screen bottom | #G2 |
| Ctrl+1..8 / Ctrl+PageUp sometimes do nothing in Firefox | #G3 |
| Tab-strip clicks land on the *wrong* tab | #G4 |
| Agent closed the WRONG tab with Ctrl+W | #G5 |
| Cursor invisible in portal screenshots | #G6 |
| Ctrl+F + Enter doesn't press a `<button>` | #F5 |
| Firefox restart lost all tabs | #F3 |
| Marionette `GetCurrentUrl` returns `None` | #F2 |
| Red striped bar under the address bar | #F4 |
| Token/secret got pasted into the address bar | #P1 |

---

## 1. 🔧 Core bridge issues (#C)

### #C1 — `mcp.server.fastmcp` import broken
- **Symptom:** v1 server crashed on import on the current python-mcp builds.
- **Root cause:** `fastmcp` lives in its own package now; the path flipped across releases.
- **Fix:** `requirements.txt` pins `fastmcp>=4.0` (tested 4.1.4). `server.py` imports plain `fastmcp`.
- **Prevention:** `pip install -r requirements.txt` — do not hand-pick versions.

### #C2 — One bridge per port (8765), and "ghost" bridge processes
- **Symptom:** second bridge instance fails to bind 8765; agent flaps between two stale processes.
- **Root cause:** uvicorn single-binds; old bridge survived with the old token.
- **Fix (worked in the field):** kill with a **bracket self-guard** so your probe doesn't match itself:
  `pkill -f "bridge_v3\.py"` used carefully, or `pkill -f "bridge_v[3].py"`. Same for daemons: `pkill -f "ydotoo[l]d"` (the `[l]` keeps pgrep/pkill from matching the pkill command line itself).
- **Prevention:** systemd unit (`agent-bridge.service`) owns the lifecycle — never start a second bridge by hand; use `systemctl --user restart agent-bridge`.

### #C3 — JSON shell-ingestion mangles scripts routed through `/run`
- **Symptom:** multi-line sed/python one-liners arrive corrupted; `printf`/`echo` double-escape hell.
- **Root cause:** command text travels JSON → `/run` → `sh -c` → bash — every layer re-escapes quotes/backslashes.
- **Fix:** never paste scripts into `/run` bodies. Write the script locally, **base64 it**, decode on target:
  ```bash
  B64=$(base64 -w0 myscript.py)
  curl -X POST ... /run -d "{\"command\":\"echo $B64 | base64 -d > /tmp/myscript.py && python3 /tmp/myscript.py\"}"
  ```
  The bridge also has native `/files/upload` for larger payloads.
- **Prevention (agents):** `agent/bridge.sh` already ships `brun`/`bup` helpers doing exactly this.

### #C4 — `test_client.py` / sandbox probes mislead on Wayland boxes
- **Symptom:** dev laptop (headless) screenshot "works"; user's Wayland desktop shows black.
- **Lesson:** GUI endpoints only make sense on the *user's actual desktop*. Validate there, not in the build sandbox.

### #C5 — Never restart `agent-bridge.service` from inside the bridge
- **Symptom:** `systemctl --user restart agent-bridge` via `/run` → request dies with `exit code -15` (SIGTERM) and the reply never comes.
- **Root cause:** the command runs as a child of the service — restarting the unit kills your own caller.
- **Fix:** it *did* restart (systemd brought v3.1 back 3 s later); just run the follow-up `/health` check as a SEPARATE request. Rule: service lifecycle commands are fire-and-forget — put the verification in the next call.

---

## 2. 🌐 Transport / tunnel issues (#T)

### #T1 — Cloudflare **quick** tunnel drops requests held >~100 s (empty-body responses)
- **Symptom:** long `/shell/run` (apt, builds) returns an EMPTY response around 100–120 s; command actually kept running server-side.
- **Root cause:** quick tunnels (trycloudflare.com) enforce an edge timeout on open responses.
- **Fix:** anything long-running goes through **`/jobs/start`** (background) + poll `/jobs/{id}?offset=N` — response never stays open >100 s.
- **Pattern that always works:** `/jobs/start → poll → read till exit_code`.
- **Prevention:** keep quick tunnels for interactive ops only. Long builds/installs = **jobs API**, every time.

### #T2 — Quick-tunnel URL churn + DNS goes stale
- **Symptom:** agent calls yesterdays's URL → `curl exit 6` (DNS), HTTP 000 forever.
- **Root cause:** quick-tunnel subdomains are released when the process restarts (reboot, service restart); the agent's saved URL points into the void.
- **Fix:** get the new URL from the machine: `journalctl --user -u agent-tunnel | grep trycloudflare` — or use TWO URLs in parallel only while both resolve (seen live: one tunnel dead, the older one still answering).
- **Prevention (recommended):** use a **named tunnel** (README → "Named tunnel") so the URL is permanent: `cloudflared tunnel login; cloudflared tunnel create agent-bridge; cloudflared tunnel route dns agent-bridge bridge.<your-domain>`. Optional Cloudflare Access rule on top.

---

## 3. 🐚 Shell / jobs issues (#S)

### #S1 — sudo/apt through the bridge hang forever
- **Symptom:** `apt install ydotool` via `/shell/run` stalls.
- **Root cause:** apt waits on a password prompt; pexpect shell has no `sudo -S` pipeline by default.
- **Fix:** route privilege escalations as **jobs**: `/jobs/start pkexec <cmd>` → polkit popup appears on the user's screen → user (or agent, careful!) approves → job completes. Never `sudo` inline.
- **Prevention:** installer needs sudo only once (`install.sh` calls `sudo apt-get` interactively on your own keyboard). After that, `input`-group membership removes most escalation needs (#D-series).

### #S2 — Foreground `pkexec` wedges the persistent shell
- **Symptom:** after `pkexec something` in `/shell/run`, the shell never returns to prompt; next commands land nowhere.
- **Root cause:** pkexec grabs the tty; pexpect's marker prompt vanishes.
- **Fix / recovery:** inject Ctrl+C via `/shell/type` text `"\x03"` **twice**, then `/shell/drain`.
- ⚠️ **COLLATERAL DAMAGE WARNING:** that Ctrl+C also kills the shell's *job children* — it terminated the pkexec-owned daemon once (#D5). Avoid Ctrl+C'ing a shell that parents a pkexec-held daemon; if possible, recover with `/shell/drain` first, Ctrl+C last.

### #S3 — `pgrep ydotoold` (and similar) matches the probe process itself
- **Symptom:** "is it running?" checks return a PID — yours. Kill scripts kill nothing/everything wrong.
- **Fix:** bracket trick: `pgrep -af "ydotoo[l]d"`. The `[l]` regex still matches the real string but not the literal probe command line.
- **Prevention:** apply the bracket trick to **any** pgrep/pkill from automation.

### #S4 — `/shell/type wait=true` swallowed prompts (v2 bug)
- **Symptom:** typed input appeared to vanish; follow-up reads empty.
- **Fix (shipped in v3):** `type()` sleeps + drains instead of `expect`-wait when `wait=false`; v3.1 keeps this. If you revive v2, expect the bug — don't reintroduce.

### #S5 — Panel/crash dialogs (VS Code crash reporter etc.) steal click focus
- **Symptom:** clicks mysteriously do nothing while a modal crash dialog hides behind.
- **Fix:** screenshot FIRST ({POST /shot}) before input injection lands anywhere; dialogs are visible instantly in the frame.

---

### #S6 — I killed my own tunnel with a "cleanup" `pkill` (v3.2 fix)
- **Symptom:** the agent asks for a desktop cleanup; Firefox and terminals
  close, then **the whole bridge goes dark** — `curl /health` now returns a
  Cloudflare `Error 1033 · unable to resolve it` page. Nothing on the PC is
  broken; the *channel* is gone.
- **Root cause:** `cloudflared` was being run **manually in one of those
  terminal windows**. `pkill -x gnome-terminal-server` closed every terminal —
  including the process group hosting the tunnel. The bridge survived, but
  there was no tunnel left to reach it.
- **Rule:** the tunnel and the bridge are the agent's **oxygen**. Never let a
  generic cleanup pattern (`pkill <app>`, `killall`, `systemctl stop`, logout)
  reach them.
- **v3.2 fix:** `/run`, `/shell/run` and `/jobs/start` return **HTTP 409**
  when a command contains a kill verb *and* one of
  `cloudflared|agent-tunnel|agent-bridge|bridge_v3`. Override with
  `"force": true`. App cleanup goes through `POST /safe-kill {"name":"firefox"}`,
  which refuses those four names outright.
- **Prevention:** run the tunnel under systemd (`agent-tunnel.service` —
  `install.sh` sets it up), never in a terminal window; and order any
  "close everything" as **apps → daemons → services last**
  (`systemctl --user disable --now agent-bridge agent-tunnel`).

## 4. 🖼️ Wayland screenshots (#W) — the big one

### #W1 — `mss` returns a silent black 6 KB frame on Wayland
- **Symptom:** `/screenshot` "works" (200 OK), image is pure black.
- **Root cause:** X11 screen-grab APIs read the void under Wayland. mss has no portal integration.
- **Fix (v3.1):** `_is_wayland()` detection → **portal screenshot first** (`tools_linux/portal_shot.py`). mss is the last-ditch fallback and its output is size-validated (>10 KB) so a black frame can't pass silently.
- **Prevention:** ensure `python3-gi` is installed (`install.sh` does it). Portal needs session env — the bridge sets `XDG_RUNTIME_DIR` + `DBUS_SESSION_BUS_ADDRESS` automatically.

### #W2 — `gnome-screenshot` hangs ~30 s then segfaults
- **Symptom:** exactly that. It also blocks while a polkit popup is up.
- **Root cause:** gnome-screenshot is X11-era tooling; under Wayland+snap confinement it wedges on screen access.
- **v3.1 behavior:** skipped entirely on Wayland.
- **Prevention:** don't install it as a workaround — the portal path is strictly better.

### #W3 — direct `org.gnome.Shell.Screenshot` D-Bus call → `GDBus.Error: AccessDenied`
- **Symptom:** `Screenshot is not allowed` from gdbus.
- **Root cause:** GNOME restricted the shell screenshot API to internal callers; apps must go through xdg-desktop-portal.
- **Lesson:** portal APIs only; shell bus is a dead end for arbitrary callers.

### #W4 — portal Screenshot hangs ~60 s then TIMEOUT (the afternoon-eater)
- **Symptom:** portal API called from python-gi: call returns, but nothing ever responds; every variant times out.
- **Root cause (root of the whole mess):** the code subscribed to the portal `Response` signal **after** making the call. The portal round-trips in tens of ms → the response fired before the listener existed → client waited forever.
- **Fix (in `portal_shot.py`):** `signal_subscribe()` FIRST, `call_sync("Screenshot")` SECOND. That single ordering flip made non-interactive portal capture 100% reliable.
- **Prevention:** reuse `portal_shot.py` verbatim; don't reimplement.

---

## 5. 🎹 Input daemon issues (#D) — ydotool/ydotoold

### #D1 — "just run ydotoold" is four failure modes in a trench coat
- The reliable end-state:
  1. `sudo apt install ydotool` (install.sh does this),
  2. `sudo usermod -aG input $USER` (install.sh does this),
  3. `systemctl enable --now ydotool.service` (install.sh does this),
  4. **LOG OUT AND BACK IN once.** Group membership is frozen into the session at login — no reload short of relogin picks it up. `sg`/`newgrp` are **not** available on stock Ubuntu to dodge this; don't try.
- After relogin: `/gui/*` endpoints work with zero pkexec prompts, forever.

### #D2 — `ydotoold` silently dies: stale socket (`EADDRINUSE`)
- **Symptom:** daemon "starts", exits instantly, no error printed anywhere obvious; `/gui/click` → CONNECTION_REFUSED.
- **Root cause:** ydotoold does NOT unlink a stale `/tmp/.ydotool_socket` on start-up — bind fails, daemon exits without logging.
- **Fix:** `rm -f /tmp/.ydotool_socket` before EVERY ydotoold (re)start. Non-negotiable.
- **Prevention:** `ydotool.service` uses its own socket path; on manual restarts always `rm -f` first.

### #D3 — `systemd-run --user ydotoold` transient unit flaps `inactive(dead)`
- **Symptom:** unit bounces between active and dead; daemon gone each check.
- **Root cause:** ydotoold forks; systemd transient units see the intermediate exit and reap the service.
- **Lesson:** don't re-launch ydotoold via systemd-run; use the stock service.

### #D4 — `chmod 0666 /dev/uinput` is NOT enough for a user-mode daemon
- **Symptom:** even with world-writable uinput, my user-owned ydotoold got "Permission denied".
- **Root cause:** uinput access for ydotool's virtual device path depends on **input group credentials**, which are captured at session start (see #D1 step 4). chmod changes the node, not your process credentials.
- **Fix:** input group + relogin. There is no shortcut; three workarounds died on this hill.

### #D5 — pkexec kills ALL descendants when its direct command exits (polkit scope reaping)
- **Symptom:** pkexec'd `ydotoold &` (or `setsid nohup ydotoold &`) dies *silently* moments later — daemon vanished while logs said it "started".
- **Root cause:** polkit's systemd scope teardown: when pkexec's command exits, the entire scope (including detached children) is SIGKILLed. `(ydotoold &)` and even `setsid nohup` both die.
- **Fix (golden daemon pattern — only needed if you must go around the input group):**
  keep pkexec **in the foreground with a held child**:
  ```bash
  rm -f /tmp/.ydotool_socket
  pkexec sh -c 'ydotoold --socket-path=/tmp/.ydotool_socket & P=$!; sleep 2; chmod 0666 /tmp/.ydotool_socket; wait $P'
  # run as a /jobs/start job so the tunnel doesn't drop it (#T1); it runs forever BY DESIGN
  ```
- **Daemon dies at reboot anyway → the stock service + input group (#D1) is the durable path.**

### #D6 — root-daemon race: foreground ydotoold blocked the chmod step
- **Symptom:** socket exists but 0666 never applied (owner root, socket 0600 root-only).
- **Root cause:** inside the pkexec chain, running ydotoold in the *foreground* blocked the script from reaching `chmod`.
- **Fix:** `ydotoold & P=$!; sleep 2; chmod 0666 ...; wait $P` — background the daemon, chmod while it binds, then hold pkexec open on `wait`.

---

## 6. 🖱️ GUI / vision-loop issues (#G)

### #G1 — Bare `ydotool click 0xC0` bursts get silently dropped
- **Symptom:** `/gui/click` returns 200, tooltip confirms the pointer is ON the button, the button ignores the click. Repeatedly.
- **Root cause:** press+release with nanosecond separation race the compositor/app input pipeline on this Wayland stack. Pointer *motion* arrives; *button* events vanish.
- **Fix (v3.1):** `/gui/click2` = move → settle 250 ms → press → hold 120 ms → release. The moment we switched, sidebar clicks started working (confirmed by Zoho's reaction dialogs).
- **Prevention:** default to `click2` everywhere on Wayland; keep `/gui/click` only for compatibility.

### #G2 — GNOME auto-hide dash EATS clicks anywhere near the screen bottom
- **Symptom:** a click aimed at a bottom-row button does nothing; screenshot then shows the dash overlay sitting over the footer; the page never advanced (three consecutive attempts!).
- **Root cause:** pointer resting/approaching lower strip re-reveals the auto-hide dock; the dock surface captures the press that lands during/after its slide-up.
- **Fix (v3.1):** `/gui/safeclick` — park the pointer mid-screen (letting the dock retract), wait 1.2 s, then move directly to the target and slow-click. Also valid: fullscreen the window first (`/gui/keys` f11) — the dock cannot overlay a fullscreen client.
- **Prevention:** for any bottom-strip target, use safeclick or F11 + safeclick.

### #G3 — Keyboard chords are delivered but apps ignore them without a real focus-state
- **Symptom:** Ctrl+1 accepted by daemon (379 ms RTT) but Firefox never switched tabs; Ctrl+W then closed *whatever happened to be active* (user's Claude tab died as collateral once).
- **Root cause:** ydotool injects at evdev level; whether the app *reacts* depends on which window/pane has keyboard focus, which is unobservable from evdev alone.
- **Fix:** never chain keyboard actions without a `/shot` verify frame between steps. `Ctrl+PageUp`/`Ctrl+PageDown` tab-walks proved reliable once the *window* was clicked-for-focus first.
- **Prevention:** feedback loop: action → `/shot` → confirm → next action.

### #G4 — Compact tab-strip coordinates drift (hits the *neighbor* tab)
- **Symptom:** clicks on a tab × landed on the adjacent tab's body, selecting it instead.
- **Root cause:** compact tab bars compress shifts (~15 px swerve); stale coordinates from an old screenshot kill precision.
- **Fix rule:** for any destructive click (×), re-locate from a FRESH frame immediately before pressing — never press from memorized coordinates. Better: tab ops via marionette (`/tmp close` via ff_tabs.py) — zero pixels.

### #G5 — Two-pilot hazard: human + agent injecting input at the same time
- **Symptom:** agent's click lands when the user mid-types/mid-pastes → state changes misattributed (the wrong tab got closed while the user was literally pasting a token into the omnibar).
- **Fix rule:** request a hands-off window before any multi-step GUI run ("Hands off — go do it!" mode). Also always report to the user what's about to be clicked.

### #G6 — The cursor is invisible in portal screenshots
- **Symptom:** you cannot visually verify pointer position from frames.
- **Consequence:** never confirm "pointer is at X" by screenshot; confirm by *hover side effects* (tooltips) or circumstantial frame diffs.
- **Better:** skip pointer calibration entirely → Marionette (#F).

### #G7 — Page zooming is NOT a click-solver, it re-positions worst-case elements
- **Symptom:** Ctrl+minus×2 moved the footer button *into* the dock's reach, worsening clicks.
- **Lesson:** zoom shifts fixed-position elements toward the viewport edge; don't reach for zoom to dodge overlay zones.
- **Cleanup note:** reset with Ctrl+0 when you're done (we reset the user's browser at the end).

---

### #G8 — Screenshot spam: a 3-click job cost 12 full frames (v3.2 fix)
- **Symptom:** the user notices the agent taking screenshot after screenshot
  to close a couple of tabs. Each frame is a full 1920×1080 PNG (~0.1–2 MB)
  and a ~1–2 s round trip, so a trivial job crawls.
- **Root cause (three, all fixable):**
  1. `ydotool` is **write-only** — it reports the codes it sent, never what the
     app did. So every action seems to need a visual confirmation.
  2. Failures (#G1 dock eating clicks, #G4 tab drift, #G3 focus races) are
     invisible until the next frame, and each failure costs another frame.
  3. Full-screen frames were sent to answer a 900×40 question.
- **v3.2 fix:**
  - `POST /gui/batch` — N actions **plus one `shot` step** in a single request.
  - `/shot` accepts `region` (crop), `max_width` (downscale), `format=jpg`
    → measured on one frame: tab-strip crop **69× smaller**, dialog crop
    **57×**, half-width JPEG **4.6×**.
  - Browser work needs no pixels at all: `/ff/tabs`, `/ff/close`, `/ff/click`,
    `/ff/js` talk to the DOM through Marionette.
- **Rule:** plan the sequence, fire it as **one batch**, verify with **one**
  cropped shot. Trust return values (`{"closed": true}`) instead of
  re-photographing the screen.

## 7. 🦊 Firefox / Marionette automation (#F) — the escape hatch that ended the war

### #F1 — Why marionette is the final boss tier
Pixel clicking on this desktop = unreliable (dock eats presses, scale drift, focus races, invisible cursor). **Marionette clicks DOM buttons by their text**: zero coordinates, immune to all overlay/focus/dock/zoom problems. It marched through a 3-step Zoho wizard in ~8 s after pointer attempts failed for ~25 minutes.

### #F2 — Protocol landmines (all baked into `tools_linux/ff_tabs.py`)
- HTTP-alike framing: `<bytelen>:<json>` packets, each is `[type, id, error, data]`; `data` is sometimes `{"value": ...}` and sometimes the raw list — unpack **both** ways.
- `WebDriver:GetCurrentUrl` returned `None` on Firefox 154 (snap build) — **address tabs by TITLE** (`WebDriver:GetTitle`), not URL.
- One `WebDriver:NewSession` per connection.
- To click: dispatch the full event cascade (`pointerover/pointerdown/mousedown/pointerup/mouseup/click`) then `el.click()`; plain `.click()` sometimes misses React/Ember handlers.
- `WebDriver:CloseWindow` on the **last tab kills Firefox entirely** — ff_tabs guards for it.

### #F3 — Restarting Firefox can silently nuke the tab session (and re-writes the evidence!)
- **Symptom:** after `pkill firefox` + relaunch, only "New Tab" came back; `SessionStore.restoreLastSession()` no-op'd.
- **Root cause (critical):** the *running* Firefox continuously overwrites `recovery.jsonlz4` with the CURRENT session — within seconds the old 14-tab state in `recovery.jsonlz4` was destroyed. The survivor was **`recovery.baklz4`** (timestamped before the kill).
- **Fix (recovery recipe):**
  1. **Immediately snapshot the sessionstore files before restarting Firefox:** `cp ~/snap/firefox/common/.mozilla/firefox/*/sessionstore-backups/* /tmp/ss_save/`.
  2. If lost: decode `recovery.baklz4` (pure-python jsonlz4 decoder inside `ff_tabs.py restore-tabs`) → extract tab URLs → reopen all via Marionette.
  3. `(pkill TERM → wait → relaunch with --marionette --marionette-port 2828)` is `ff_tabs.py marionette-on`.
- **Prevention:** set Firefox → Settings → "Open previous windows and tabs" to always-restore, so NORMAL restarts auto-recover; still snapshot before any script-kill.

### #F4 — Red striped bar under the address bar
- **Symptom:** after `firefox --marionette`, a red-striped banner under the toolbar.
- **Meaning:** firefox's "controlled by automation" indicator — normal while marionette is enabled; any local process can drive the browser via 127.0.0.1:2828 while it's on.
- **Handling:** fine while working; when done, restart Firefox normally (banner + listener both disappear). The URL on the bar: tabs restore via History → Recently Closed Windows if "restore previous session" was off (#F3).

### #F5 — Ctrl+F + Enter doesn't press `<button>` elements
- **Symptom:** find-bar says "1 of 1 match", button text highlights green, Esc + Enter does nothing.
- **Root cause:** Enter-after-find only *activates* anchors (`<a>`); Zoho's wizard buttons are real `<button>`s.
- **Lesson:** find-text is a good *locator* (status bar shows match counts — useful for diagnostics!) but not a clicker. Marionette clicks them by text instead.

### #F6 — Reconnecting arena-clients
- When Firefox is restarted, other tabs like chat UIs reconnect on reload; nothing to do. But don't restart Firefox while your agent session materially depends on page state (forms!) that isn't server-side-backed.

---

## 8. 🧭 Agent-tactic failures to never repeat (#A)

1. **Never blind-close a tab with chords.** Ctrl+W kills whatever is active — after a failed Ctrl+1 "select", it closed the user's *Claude* tab instead of the target. Recovery existed (Ctrl+Shift+T) but trust paid the price. Use Marionette close-by-title or a fresh-frame vision click.
2. **Verify EVERY step with a screenshot.** Each unverified assumption in this session cost 5–15 minutes of correction (assumed scale factor, assumed tooltips, assumed focus states).
3. **Esc before Enter on any modal** — Enter presses whatever the default action is (in one dialog it was "Yes, quit setup" — exactly what we didn't want).
4. **Hands-off consent before multi-step GUI runs** (ask the user!). Two pilots crashed two operations before consent was taken.
5. **Announce side effects.** The user watched our run live in the Arena chat tab — the screenshots and button presses were visible; say what you'll do before you do it.
6. **Don't retry dead ends.** mss-black, gnome-screenshot segfault, systemd-run flapping — each wasted a round-trip because earlier failures weren't consulted. That's what this file is for.

---

## 9. 🔐 Security lessons (#P)

### #P1 — A GitHub PAT got pasted into the Firefox address bar
- **Symptom:** the fine-grained token sat in the omnibox with "Search with Google" under it — pressing Enter would have pushed the token to a search engine.
- **Response:** Esc to clear, then **revoke the token** (Settings → Developer settings → Fine-grained tokens → Delete). Any token that crossed AI chats/screenshots/omnibars counts as burned.
- **Prevention:** never paste tokens into browser UI; the bridge stores its token at `~/.bridge_token` with `chmod 600` (install.sh does this), and clients pass it as the `X-Bridge-Token` header.

### #P2 — Bridge token = full shell
- The bridge is a root of this machine (shell + file transfer + sudo-adjacent flows). The token in the wrong hands = your machine.
- `audit.log` records every request — review it: `/admin/audit`.
- Quick tunnels are unauthenticated beyond the token; consider Cloudflare **Access** on the named tunnel (#T2) for production.

### #P3 — Marionette listening port
- While `firefox --marionette` runs, localhost:2828 is an unauthenticated remote control of the browser. Local-only (127.0.0.1) — but restart Firefox when your operation is done (#F4).

---

## 10. ✅ Fresh-PC checklist (do these and never meet the issues above)

On the target Linux (Ubuntu/GNOME Wayland tested):

1. Clone + install:
   ```bash
   git clone https://github.com/cid0980/agent_bridge.git && cd agent_bridge
   ./install.sh            # venv + deps + ydotool + python3-gi + input group + services
   ```
2. **Log out & back in** (input group binds to the login session — #D1).
3. Prefer a **named tunnel** for a permanent URL (README section) — #T2.
4. Agent-side: `source agent/bridge.sh` for `bshell/bjob/bshot/bsc...` helpers — base64-encapsulation and jobs-polling built in (#C3, #T1).
5. Enable Firefox "restore previous windows on startup" (Settings → General → Startup) — #F3.
6. For GUI work: default to `/gui/click2` / `/gui/safeclick` (never bare click) and `/shot`-verify every step (#G1, #G2, #G3).
7. For web-app flows inside Firefox: use `tools_linux/ff_tabs.py` (marionette) instead of pixels (#F1).
8. Before any script restarts Firefox: `cp -r ~/snap/firefox/common/.mozilla/firefox/*/sessionstore-backups /tmp/ss_save/` — #F3.
9. Keep secrets out of browser UI; regenerate anything that leaked — #P1.
10. When in doubt: look in this file first.

### 11. ✅ v3.2 fast-operating rules (whoever drives next)
1. `GET /status` first — tunnel URL, daemons, tabs, disk in ONE call.
2. Browser → Marionette (`/ff/*`); pixels only for non-browser UI.
3. One `POST /gui/batch` per job, with `{"op":"shot","region":…}` at the end.
4. Crop before you send: `region`, `max_width`, `format=jpg`.
5. Apps get `POST /safe-kill`; never a broad `pkill` (#S6).
6. Long commands → `/jobs/start` + poll, never a held request (#T1).
7. Shut down in order: apps → daemons → **services last**.

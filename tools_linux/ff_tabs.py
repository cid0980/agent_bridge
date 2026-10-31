#!/usr/bin/env python3
"""
ff_tabs.py — Firefox remote control via Marionette (the DOM-level escape hatch).

Why this exists (see ISSUES.md #F1): on this GNOME/Wayland desktop, emulated
pointer clicks get eaten by overlays/docks, coordinates drift, and focus races.
Marionette talks to the DOM directly — clicks buttons *by their text*, zero
pixel math, immune to dock, focus and zoom issues.

Firefox must be started once with:  firefox --marionette --marionette-port 2828
(This tool can do that restart for you:  `ff_tabs.py marionette-on`.)

USAGE ("needle" = case-insensitive substring of a tab's TITLE):
    ff_tabs.py list                      list every open tab:  id | title
    ff_tabs.py state <needle>            dump headings + visible buttons of the tab
    ff_tabs.py click <needle> <text>     click a button/link whose text matches
    ff_tabs.py close <needle>            close matching tabs (never the last window)
    ff_tabs.py marionette-on             restart Firefox with marionette enabled
    ff_tabs.py restore-tabs              reopen tabs saved in sessionstore backups

Protocol notes baked in from the field (ISSUES.md #F2):
  - packets are  [type, id, error, data]; unwrap data["value"] OR the raw list.
  - WebDriver:GetCurrentUrl may return None; address tabs BY TITLE instead.
"""
import json
import os
import socket
import subprocess
import sys
import time
import glob

PORT = 2828


class Marionette:
    def __init__(self, port: int = PORT):
        self.s = socket.create_connection(("127.0.0.1", port), 20)
        self.f = self.s.makefile("rb")
        self._m = 0
        self._recv()  # hello

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

    def cmd(self, name, params=None):
        self._m += 1
        b = json.dumps([0, self._m, name, params or {}]).encode()
        self.s.sendall(str(len(b)).encode() + b":" + b)
        r = self._recv()
        if len(r) > 2 and r[2]:
            raise RuntimeError(f"marionette error on {name}: {r[2]}")
        d = r[3] if len(r) > 3 else {}
        return d.get("value", d) if isinstance(d, dict) else d


STATE_JS = r"""
const heads=[...document.querySelectorAll('h1,h2,h3')].map(e=>(e.textContent||'').trim()).filter(t=>t&&t.length<90).slice(0,4).join(' | ');
const btns=[...document.querySelectorAll('button,a')].filter(e=>e.offsetParent&&e.offsetHeight>4).map(e=>(e.textContent||'').trim()).filter(t=>t&&t.length<70).slice(0,16).join(', ');
return 'HEADS: '+heads+' || BTNS: '+btns;
"""

CLICK_JS = r"""
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


def _tabs(mc: Marionette):
    mc.cmd("WebDriver:NewSession", {"capabilities": {}})
    out = []
    for hid in mc.cmd("WebDriver:GetWindowHandles"):
        mc.cmd("WebDriver:SwitchToWindow", {"handle": hid})
        out.append((hid, str(mc.cmd("WebDriver:GetTitle") or "")))
    return out


def _pick(mc: Marionette, needle: str):
    for hid, title in _tabs(mc):
        if needle.lower() in title.lower():
            return hid
    raise SystemExit(f"no tab title contains {needle!r}")


def _jsonlz4_load(path):
    """Read Mozilla's sessionstore .jsonlz4 (magic + per-block 4-byte LE length + LZ4 blocks)."""
    def block_dec(data):
        out = bytearray(); i = 0
        while i < len(data):
            token = data[i]; i += 1
            lit = token >> 4
            if lit == 15:
                while True:
                    b = data[i]; i += 1; lit += b
                    if b != 255: break
            out += data[i:i+lit]; i += lit
            if i >= len(data): break
            off = data[i] | (data[i+1] << 8); i += 2
            mlen = token & 15
            if mlen == 15:
                while True:
                    b = data[i]; i += 1; mlen += b
                    if b != 255: break
            mlen += 4
            st = len(out) - off
            for k in range(mlen): out.append(out[st+k])
        return bytes(out)
    raw = open(path, "rb").read()
    if raw[:8] != b"mozLz40\x00":
        return json.loads(raw)
    pos = 8; buf = bytearray()
    while pos < len(raw):
        clen = int.from_bytes(raw[pos:pos+4], "little"); pos += 4
        buf += block_dec(raw[pos:pos+clen]); pos += clen
    return json.loads(bytes(buf))


def main():
    op = sys.argv[1] if len(sys.argv) > 1 else "list"

    if op == "marionette-on":
        subprocess.run(["pkill", "-TERM", "-x", "firefox"], capture_output=True)
        for _ in range(20):
            if not subprocess.run(["pgrep", "-x", "firefox"], capture_output=True).stdout:
                break
            time.sleep(1)
        env = dict(os.environ)
        env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
        env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path={env['XDG_RUNTIME_DIR']}/bus")
        env.setdefault("WAYLAND_DISPLAY", "wayland-0")
        env.setdefault("MOZ_ENABLE_WAYLAND", "1")
        subprocess.Popen(["setsid", "nohup", "firefox", "--marionette",
                          "--marionette-port", str(PORT)],
                         env=env, stdout=open("/tmp/ff_mario.log", "w"),
                         stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
        print("restarting firefox with marionette on :2828 (give it ~8s)")
        return

    if op == "restore-tabs":
        # IMPORTANT (ISSUES.md #F3): a freshly running Firefox OVERWRITES
        # recovery.jsonlz4 with the new session. recovery.baklz4 is the survivor.
        cands = sorted(glob.glob(os.path.expanduser(
            "~/snap/firefox/common/.mozilla/firefox/*/sessionstore-backups/recovery.baklz4"))
            or glob.glob(os.path.expanduser(
                "~/.mozilla/firefox/*/sessionstore-backups/recovery.baklz4")),
            key=os.path.getmtime)
        if not cands:
            raise SystemExit("no recovery.baklz4 found")
        urls = []
        for w in _jsonlz4_load(cands[-1]).get("windows", []):
            for t in w.get("tabs", []):
                ent = t.get("entries", [])
                if not ent: continue
                u = ent[min(t.get("index", 1) - 1, len(ent) - 1)].get("url", "")
                if u.startswith("http") and u not in urls:
                    urls.append(u)
        mc = Marionette()
        for u in urls:
            r = mc.cmd("WebDriver:NewWindow", {"type": "tab", "focus": False})
            hid = r.get("handle") if isinstance(r, dict) else None
            if hid: mc.cmd("WebDriver:SwitchToWindow", {"handle": hid})
            mc.cmd("WebDriver:Navigate", {"url": u})
            print("reopened:", u[:80])
            time.sleep(0.4)
        print(f"{len(urls)} tabs restored from {os.path.basename(cands[-1])}")
        return

    mc = Marionette()

    if op == "list":
        for hid, title in _tabs(mc):
            print(f"{hid[:10]} | {title[:70]}")
    elif op == "state":
        hid = _pick(mc, sys.argv[2]); mc.cmd("WebDriver:SwitchToWindow", {"handle": hid})
        print(mc.cmd("WebDriver:ExecuteScript", {"script": STATE_JS, "args": []}))
    elif op == "click":
        hid = _pick(mc, sys.argv[2]); mc.cmd("WebDriver:SwitchToWindow", {"handle": hid})
        print(mc.cmd("WebDriver:ExecuteScript", {"script": CLICK_JS, "args": [sys.argv[3]]}))
    elif op == "close":
        needle = sys.argv[2].lower()
        for hid, title in _tabs(mc):
            if needle in title.lower():
                mc.cmd("WebDriver:SwitchToWindow", {"handle": hid})
                mc.cmd("WebDriver:CloseWindow")
                print("closed:", title[:70])
                if not (mc.cmd("WebDriver:GetWindowHandles") or []):
                    print("(that was the last window — firefox exited)")
                    return
    else:
        print(__doc__)


if __name__ == "__main__":
    main()

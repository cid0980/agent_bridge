#!/usr/bin/env python3
"""
portal_shot.py — Wayland-safe screenshot via the GNOME screenshot portal.

Why this exists (see ISSUES.md #W1): on Wayland, mss returns a black frame
and gnome-screenshot segfaults. The *correct* path on GNOME/Wayland is the
org.freedesktop.portal.Screenshot API over D-Bus.

The ONE subtlety that took an afternoon to find:
    Subscribe to the Response signal BEFORE calling Screenshot().
    Subscribing after the call races the portal response and the call
    just sits there until TIMEOUT — every single time.

Usage:
    XDG_RUNTIME_DIR=... DBUS_SESSION_BUS_ADDRESS=... python3 portal_shot.py /tmp/out.png

Exit codes: 0 = wrote the file, 1 = no python-gi, 2 = other error.
"""
import sys

DEST = sys.argv[1] if len(sys.argv) > 1 else "/tmp/portal_shot.png"

try:
    import gi
    gi.require_version("Gio", "2.0")
    from gi.repository import Gio, GLib
except Exception as e:  # python3-gi missing or D-Bus env missing
    print(f"portal_shot: python3-gi/GLib not usable: {e}", file=sys.stderr)
    sys.exit(1)

# NOTE — field-proven details (do NOT "simplify"):
#   * subscribe BEFORE calling (else the Response races past the listener)
#   * sender = org.freedesktop.portal.Desktop
#   * path = None → the Response is emitted on the REQUEST object path
#   * pass a handle_token (some portal backends require it)
#   * signal flags NONE (NO_MATCH_RULE breaks delivery)
done_path = [None]
loop_main = GLib.MainLoop()
bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)


def _on_response(conn, sender, path, iface, signal, params, *_):
    if signal != "Response":
        return
    response, results = params.unpack()
    if response == 0:
        uri = results.get("uri")
        done_path[0] = uri.get_string() if uri is not None and not isinstance(uri, str) else uri
    loop_main.quit()


# 1) SUBSCRIBE FIRST (this is the whole trick)
tag = bus.signal_subscribe(
    "org.freedesktop.portal.Desktop", "org.freedesktop.portal.Request",
    "Response", None, None, Gio.DBusSignalFlags.NONE, _on_response)

# 2) only then ask for the screenshot
reply = bus.call_sync(
    "org.freedesktop.portal.Desktop",
    "/org/freedesktop/portal/desktop",
    "org.freedesktop.portal.Screenshot", "Screenshot",
    GLib.Variant("(sa{sv})", ("", {"handle_token": GLib.Variant("s", "ab2"),
                                   "interactive": GLib.Variant("b", False)})),
    GLib.VariantType("(o)"), Gio.DBusCallFlags.NONE, 15000, None)

timer = GLib.timeout_add_seconds(25, lambda: (loop_main.quit(), False)[1])
loop_main.run()
try:
    GLib.source_remove(timer)
    bus.signal_unsubscribe(tag)
except Exception:
    pass

uri = done_path[0]
if not uri:
    print("portal_shot: no response URI (user cancelled or portal timed out)", file=sys.stderr)
    sys.exit(2)

src = uri.replace("file://", "")
import shutil
shutil.copyfile(src, DEST)
print(DEST)

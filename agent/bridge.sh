#!/usr/bin/env bash
# ==============================================================================
#  bridge.sh — the AGENT side of the bridge.
#  Source this in any sandbox/shell to control the remote PC ergonomically:
#
#    export BRIDGE_URL="https://xxxx.trycloudflare.com"
#    export BRIDGE_TOKEN="your-token"
#    source bridge.sh
#
#    bshell 'ls ~/Desktop'            # run in the persistent shell
#    bjob   'flutter build apk'       # background job -> prints job_id
#    bjobtail <id>                    # live output since last poll
#    bshot                            # screenshot -> /tmp/remote_shot.png (then view it)
#    bimg   ~/Pictures/cat.png        # download any image -> /tmp (then view it)
#    bup    ./local.bin ~/Downloads/  # upload a file TO the pc
#    bapp   firefox                   # open an app on the remote desktop
# ==============================================================================

bget()  { curl -s --max-time 60 -H "X-Bridge-Token: $BRIDGE_TOKEN" "$BRIDGE_URL$1"; }
bpost() { curl -s --max-time 120 -X POST -H "X-Bridge-Token: $BRIDGE_TOKEN" \
               -H "Content-Type: application/json" -d "$2" "$BRIDGE_URL$1"; }
_jqtext() { python3 -c "import sys,json;d=json.load(sys.stdin);print(d.get(sys.argv[1], d))" "$1"; }

# persistent interactive shell (session=main by default)
bshell() { bpost /shell/run "{\"command\":$(python3 -c 'import json,sys;print(json.dumps(sys.argv[1]))' "$1"),\"session\":\"${2:-main}\"}" | _jqtext output; }
btype()  { bpost /shell/type "{\"text\":$(python3 -c 'import json,sys;print(json.dumps(sys.argv[1]))' "$1"),\"wait\":true}" | _jqtext output; }

# quick one-shot command (non-persistent)
brun() { bpost /run "{\"command\":$(python3 -c 'import json,sys;print(json.dumps(sys.argv[1]))' "$1")}" | _jqtext result; }

# background jobs
bjob()     { bpost /jobs/start "{\"command\":$(python3 -c 'import json,sys;print(json.dumps(sys.argv[1]))' "$1")}" | tee /dev/stderr | _jqtext job_id; }
bjobtail() { bget "/jobs/$1?offset=${2:-0}" | python3 -c "
import sys, json
d = json.load(sys.stdin)
print(d['output_since_offset'], end='')
print(f'--- running={d[\"running\"]} exit={d[\"exit_code\"]} next_offset={d[\"next_offset\"]}')"; }
bjobs() { bget /jobs; }

# files
bdown() { local f; f="$(basename "$1")"; curl -s -H "X-Bridge-Token: $BRIDGE_TOKEN" "$BRIDGE_URL/files/download?path=$(python3 -c 'import urllib.parse,sys;print(urllib.parse.quote(sys.argv[1]))' "$1")" -o "/tmp/$f" && echo "saved: /tmp/$f"; }
bup()   { curl -s -H "X-Bridge-Token: $BRIDGE_TOKEN" -F "path=$2$(basename "$1")" -F "file=@$1" "$BRIDGE_URL/files/upload"; }

# images: fetch raw bytes, then view the saved file with your file-viewer tool
bshot() { curl -s -H "X-Bridge-Token: $BRIDGE_TOKEN" "$BRIDGE_URL/screenshot" -o /tmp/remote_shot.png && echo "saved: /tmp/remote_shot.png  <- now open it with read_file"; }
bimg()  { bdown "$1"; }

# GUI
bapp()   { bpost /gui/open "{\"target\":\"$1\"}"; }
bclick() { bpost /gui/click "{\"x\":$1,\"y\":$2}"; }
bkeys()  { bpost /gui/type "{\"text\":$(python3 -c 'import json,sys;print(json.dumps(sys.argv[1]))' "$1")}"; }

# introspection
binfo()  { bget /system-info; }
baudit() { bget "/admin/audit?lines=${1:-30}" | _jqtext lines; }
btools() { bget /tools; }

# install something the agent needs, on the remote PC
binstall() { bpost /install "{\"package\":\"$1\",\"manager\":\"${2:-pip}\"}"; }

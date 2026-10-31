#!/usr/bin/env bash
# ==============================================================================
#  agent-bridge — one-command installer for Linux & macOS
#  Usage:  cd into the cloned repo, then:   ./install.sh
# ==============================================================================
set -e

BOLD="\033[1m"; DIM="\033[2m"; GREEN="\033[32m"; YELLOW="\033[33m"; RST="\033[0m"
say()  { echo -e "${BOLD}$1${RST}"; }
note() { echo -e "${DIM}$1${RST}"; }
warn() { echo -e "${YELLOW}$1${RST}"; }

INSTALL_DIR="$HOME/local-mcp-bridge"
OS="$(uname -s)"          # Linux | Darwin

say "▶ agent-bridge v3 installer ($OS)"

# --- 1. files ------------------------------------------------------------------
mkdir -p "$INSTALL_DIR"
if [ -f "$(dirname "$0")/bridge_v3.py" ]; then
  cp "$(dirname "$0")"/*.py "$(dirname "$0")/requirements.txt" "$INSTALL_DIR/"
  [ -d "$(dirname "$0")/tools_linux" ] && cp -r "$(dirname "$0")/tools_linux" "$INSTALL_DIR/"
  [ -f "$(dirname "$0")/ISSUES.md" ] && cp "$(dirname "$0")/ISSUES.md" "$(dirname "$0")/README.md" "$INSTALL_DIR/"
  note "copied repo files -> $INSTALL_DIR"
else
  warn "bridge_v3.py not found next to install.sh — copy repo files into $INSTALL_DIR first"
  exit 1
fi
cd "$INSTALL_DIR"

# --- 1b. Linux GUI support (ISSUES.md #D1/#W1) ---------------------------------
#     ydotool = pointer/keyboard injection · python3-gi = Wayland screenshots.
if [ "$OS" = "Linux" ] && command -v apt-get >/dev/null 2>&1; then
  if command -v ydotool >/dev/null 2>&1 && python3 -c "import gi" 2>/dev/null; then
    echo -e "${GREEN}✓${RST} ydotool + python3-gi already present"
  else
    warn "installing GUI support packages (ydotool, python3-gi)..."
    sudo apt-get install -y ydotool python3-gi || warn "couldn't install — apt them manually later"
  fi
  # the user must be in the 'input' group for the ydotool daemon to accept its
  # sockets without pkexec. Membership only applies on NEW logins.
  if ! id -nG "$USER" | grep -qw input; then
    warn "adding $USER to the 'input' group (needed by ydotoold)..."
    sudo usermod -aG input "$USER" && warn ">>> LOG OUT & BACK IN once so this group sticks <<<"
  fi
  # enable the distro daemon (it runs as root; its socket is fine for the
  # input group). v3.0 fought this for hours — see ISSUES.md #D1-#D4.
  systemctl enable --now ydotool.service 2>/dev/null \
    && echo -e "${GREEN}✓${RST} ydotoold enabled (socket is ready immediately)" \
    || warn "ydotool.service not available — start ydotoold manually (ISSUES.md #D1)"
fi

# --- 2. python venv ------------------------------------------------------------
if ! python3 -m venv .venv 2>/dev/null; then
  warn "venv module missing — on Ubuntu: sudo apt install python3-venv python3-full"
  exit 1
fi
PYBIN="$INSTALL_DIR/.venv/bin/python"
"$PYBIN" -m pip install --quiet --upgrade pip
"$PYBIN" -m pip install --quiet -r requirements.txt
echo -e "${GREEN}✓${RST} dependencies installed into .venv"

# --- 3. token ------------------------------------------------------------------
TOKEN_FILE="$INSTALL_DIR/.bridge_token"
if [ -s "$TOKEN_FILE" ]; then
  TOKEN="$(cat "$TOKEN_FILE")"
else
  TOKEN="$(head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n' | head -c 32)"
  echo "$TOKEN" > "$TOKEN_FILE"; chmod 600 "$TOKEN_FILE"
fi
note "bridge token: $TOKEN   (stored in $TOKEN_FILE)"

# --- 4. cloudflared -------------------------------------------------------------
if ! command -v cloudflared >/dev/null 2>&1; then
  say "installing cloudflared..."
  if [ "$OS" = "Linux" ] && [ "$(uname -m)" = "x86_64" ]; then
    curl -fsSL -o /tmp/cf.deb https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb \
      && sudo dpkg -i /tmp/cf.deb
  elif [ "$OS" = "Darwin" ]; then
    brew install cloudflared || warn "no brew? get it: https://brew.sh"
  else
    warn "install cloudflared manually: https://github.com/cloudflare/cloudflared"
  fi
fi
command -v cloudflared >/dev/null 2>&1 && echo -e "${GREEN}✓${RST} cloudflared ready"

# --- 5. autostart (Linux: systemd user services; macOS: launchd hint) -----------
if [ "$OS" = "Linux" ] && command -v systemctl >/dev/null 2>&1; then
  UNIT_DIR="$HOME/.config/systemd/user"
  mkdir -p "$UNIT_DIR"

  cat > "$UNIT_DIR/agent-bridge.service" <<EOF
[Unit]
Description=agent-bridge v3 (local system bridge for AI agents)
After=network-online.target

[Service]
WorkingDirectory=$INSTALL_DIR
Environment=BRIDGE_TOKEN=$TOKEN
ExecStart=$PYBIN $INSTALL_DIR/bridge_v3.py
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
EOF

  cat > "$UNIT_DIR/agent-tunnel.service" <<EOF
[Unit]
Description=cloudflared quick tunnel for agent-bridge
After=network-online.target agent-bridge.service

[Service]
ExecStart=$(command -v cloudflared || echo /usr/bin/cloudflared) tunnel --no-autoupdate --url http://127.0.0.1:8765
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
EOF

  systemctl --user daemon-reload
  systemctl --user enable --now agent-bridge.service && echo -e "${GREEN}✓${RST} bridge autostarts at boot"
  systemctl --user enable agent-tunnel.service      && echo -e "${GREEN}✓${RST} tunnel autostarts at boot"
  loginctl enable-linger "$USER" 2>/dev/null || warn "run 'sudo loginctl enable-linger $USER' so services survive logout"

  warn "NOTE: quick-tunnel URL changes each boot. Find it with:"
  echo "      journalctl --user -u agent-tunnel -f | grep trycloudflare"
  warn "For a PERMANENT url + Access rules, use a named tunnel (see README)."
elif [ "$OS" = "Darwin" ]; then
  warn "macOS: autostart via launchd — see README (start manually for now):"
  echo "      BRIDGE_TOKEN=$TOKEN $PYBIN $INSTALL_DIR/bridge_v3.py &"
fi

# --- 6. go ----------------------------------------------------------------------
echo
say "✅ install complete."
echo "   start bridge : BRIDGE_TOKEN=$TOKEN $PYBIN $INSTALL_DIR/bridge_v3.py"
echo "   open tunnel  : cloudflared tunnel --url http://127.0.0.1:8765"
echo "   then paste the https://…trycloudflare.com URL + token to your agent."

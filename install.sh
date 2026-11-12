#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# install.sh — Obsidian Sticky TODO widget (Linux + macOS)
#
#   ./install.sh                         install / upgrade, start on login
#   ./install.sh --todo-file PATH        also set todo_file in a new config
#   ./install.sh --uninstall             stop and remove (keeps your config)
# ──────────────────────────────────────────────────────────────────────────────
set -euo pipefail

APP=obsidian-sticky
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN="$HOME/.local/bin/$APP"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/$APP"
CONFIG="$CONFIG_DIR/config.toml"
UNIT="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/$APP.service"
LABEL="local.$APP"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG_DIR="$HOME/Library/Logs"

TODO_FILE=""
UNINSTALL=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --todo-file) TODO_FILE="${2:?--todo-file needs a path}"; shift 2 ;;
    --uninstall) UNINSTALL=1; shift ;;
    -h|--help) sed -n '3,7p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done

OS="$(uname -s)"

stop_service() {
  if [[ "$OS" == Darwin ]]; then
    launchctl unload "$PLIST" 2>/dev/null || true
  else
    systemctl --user disable --now "$APP.service" 2>/dev/null || true
  fi
  pkill -f "$BIN" 2>/dev/null || true
}

if [[ $UNINSTALL == 1 ]]; then
  stop_service
  rm -f "$BIN" "$UNIT" "$PLIST"
  [[ "$OS" != Darwin ]] && systemctl --user daemon-reload 2>/dev/null || true
  echo "✅  Removed. Your config is still in $CONFIG_DIR"
  exit 0
fi

echo "🔧  Installing Obsidian Sticky…"

# ── 1. Find a Python ≥ 3.11 with tkinter ──────────────────────────────────────
PYTHON=""
for candidate in python3.14 python3.13 python3.12 python3.11 python3 \
                 /opt/homebrew/bin/python3.13 /opt/homebrew/bin/python3.12; do
  path="$(command -v "$candidate" 2>/dev/null)" || continue
  if "$path" -c 'import sys, tkinter, tomllib; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
    PYTHON="$path"; break
  fi
done
if [[ -z "$PYTHON" ]]; then
  echo "❌  Need Python 3.11+ with tkinter."
  if [[ "$OS" == Darwin ]]; then
    echo "    Fix: brew install python@3.13 python-tk@3.13"
  elif command -v pacman >/dev/null; then
    echo "    Fix: sudo pacman -S tk      (Omarchy: omarchy pkg add tk)"
  else
    echo "    Fix: install your distro's python3-tk package"
  fi
  exit 1
fi
echo "✅  Python: $PYTHON"

# macOS: PyObjC keeps the note above other apps on every Space.
if [[ "$OS" == Darwin ]] && ! "$PYTHON" -c "from AppKit import NSApp" 2>/dev/null; then
  echo "📦  Installing pyobjc-framework-Cocoa…"
  "$PYTHON" -m pip install --user pyobjc-framework-Cocoa -q \
    || "$PYTHON" -m pip install pyobjc-framework-Cocoa --break-system-packages -q
fi

# ── 2. Install the script ─────────────────────────────────────────────────────
mkdir -p "$(dirname "$BIN")"
install -m 755 "$HERE/obsidian_todos_sync.py" "$BIN"
echo "✅  Script → $BIN"

# ── 3. Config (never overwritten) ─────────────────────────────────────────────
mkdir -p "$CONFIG_DIR"
if [[ ! -f "$CONFIG" ]]; then
  if [[ -z "$TODO_FILE" && -t 0 ]]; then
    read -r -p "Path to your Obsidian TODO.md [~/Obsidian/TODO.md]: " TODO_FILE
  fi
  cp "$HERE/config.example.toml" "$CONFIG"
  if [[ -n "$TODO_FILE" ]]; then
    TODO_FILE="${TODO_FILE/#\~/$HOME}"
    TODO_ESCAPED="$(printf '%s' "$TODO_FILE" | sed 's/[\\"]/\\&/g; s/[&|]/\\&/g')"
    sed -i.bak "s|^todo_file = .*|todo_file = \"$TODO_ESCAPED\"|" "$CONFIG"
    rm -f "$CONFIG.bak"
  fi
  echo "✅  Config → $CONFIG"
else
  echo "✅  Keeping existing config $CONFIG"
  [[ -n "$TODO_FILE" ]] && echo "    (--todo-file ignored; edit todo_file there instead)"
fi

# ── 4. Start on login ─────────────────────────────────────────────────────────
stop_service
if [[ "$OS" == Darwin ]]; then
  mkdir -p "$(dirname "$PLIST")" "$LOG_DIR"
  cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array><string>$PYTHON</string><string>$BIN</string></array>
  <key>RunAtLoad</key><true/>
  <!-- Restart after a crash, but let the × button close it. -->
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>StandardOutPath</key><string>$LOG_DIR/$APP.log</string>
  <key>StandardErrorPath</key><string>$LOG_DIR/$APP-error.log</string>
</dict>
</plist>
EOF
  launchctl load "$PLIST"
  echo "✅  LaunchAgent → $PLIST"
  echo ""
  echo "  Restart:  launchctl kickstart -k gui/\$(id -u)/$LABEL"
  echo "  Stop:     launchctl unload $PLIST"
  echo "  Logs:     tail -f $LOG_DIR/$APP-error.log"
else
  mkdir -p "$(dirname "$UNIT")"
  cat > "$UNIT" <<EOF
[Unit]
Description=Obsidian Sticky TODO widget
PartOf=graphical-session.target
After=graphical-session.target

[Service]
ExecStart=$PYTHON $BIN
# Restart after a crash, but let the × button close it.
Restart=on-failure
RestartSec=5

[Install]
WantedBy=graphical-session.target
EOF
  systemctl --user daemon-reload
  systemctl --user enable --now "$APP.service"
  echo "✅  systemd user service → $UNIT"
  echo ""
  echo "  Restart:  systemctl --user restart $APP"
  echo "  Stop:     systemctl --user stop $APP"
  echo "  Logs:     journalctl --user -u $APP -f"
fi

echo ""
echo "The note appears top-right. Drag it anywhere; it remembers where."
echo "Edit $CONFIG and restart to change paths, size, colours or fonts."

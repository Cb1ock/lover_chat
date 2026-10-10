#!/bin/zsh
# Run bot.py as a macOS launchd daemon: restarted if it crashes, started again at login.
#
#   ./botctl.sh install    write the LaunchAgent and start it
#   ./botctl.sh uninstall  stop it and remove the LaunchAgent
#   ./botctl.sh start | stop | restart | status | log
#
# "stop" lets the bot exit cleanly (exit 0), which launchd doesn't restart; a crash is.
set -e
LABEL=com.lover_chat.bot
DIR=${0:A:h}
PLIST=$HOME/Library/LaunchAgents/$LABEL.plist
DOMAIN=gui/$UID

write_plist() {
  local env=""
  for var in HTTP_PROXY HTTPS_PROXY NO_PROXY http_proxy https_proxy no_proxy; do
    [[ -n ${(P)var} ]] && env+="    <key>$var</key><string>${(P)var}</string>"$'\n'
  done
  mkdir -p "$DIR/data" "${PLIST:h}"
  cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$DIR/.venv/bin/python</string><string>-u</string><string>$DIR/bot.py</string>
  </array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>StandardOutPath</key><string>$DIR/data/bot.log</string>
  <key>StandardErrorPath</key><string>$DIR/data/bot.log</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PYTHONUNBUFFERED</key><string>1</string>
$env  </dict>
</dict>
</plist>
EOF
}

case "$1" in
  install)
    write_plist
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    launchctl bootstrap "$DOMAIN" "$PLIST"
    echo "installed and started: $LABEL" ;;
  uninstall)
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    rm -f "$PLIST"
    echo "removed: $LABEL" ;;
  start)   launchctl kickstart "$DOMAIN/$LABEL" ;;
  stop)    launchctl kill SIGINT "$DOMAIN/$LABEL" ;;
  restart) launchctl kickstart -k "$DOMAIN/$LABEL" ;;
  status)  launchctl print "$DOMAIN/$LABEL" | grep -E "^\s*(state|pid|last exit code|runs) =" ;;
  log)     tail -n 50 -f "$DIR/data/bot.log" ;;
  *) echo "usage: $0 install|uninstall|start|stop|restart|status|log"; exit 1 ;;
esac

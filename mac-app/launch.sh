#!/bin/bash
# What the Film Look Studio app does (the app itself only calls this, so fixes here need no rebuild).
#   launch.sh start   start the server in the background if it isn't running; wait until it answers
#   launch.sh status  exit 0 if the app answers on port 8765
#   launch.sh stop    stop the server this launcher started
cd "$(dirname "$0")/.." || exit 1
URL="http://127.0.0.1:8765/"
LOG="$HOME/Library/Logs/Film Look Studio.log"
PIDFILE="$HOME/Library/Application Support/Film Look Studio/server.pid"
answers() { /usr/bin/curl -s -o /dev/null --max-time 2 "$URL"; }

case "$1" in
  status) answers ;;
  start)
    answers && exit 0
    mkdir -p "$(dirname "$PIDFILE")"
    { echo; echo "=== started $(date) ==="; } >> "$LOG"
    nohup ./studio serve --no-browser --auto-stop >> "$LOG" 2>&1 < /dev/null &
    echo $! > "$PIDFILE"
    for _ in $(seq 1 240); do answers && exit 0; sleep 0.5; done   # up to 2 minutes
    exit 1 ;;
  stop)
    if [ -f "$PIDFILE" ]; then kill "$(cat "$PIDFILE")" 2>/dev/null; rm -f "$PIDFILE"; fi ;;
esac

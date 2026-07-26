#!/usr/bin/env bash
# Self-healing Cloudflare quick tunnel for Stockly.
#
# cloudflared "quick" tunnels have no uptime guarantee and exit whenever their
# edge connection blips (common after Wi-Fi changes / sleep). This supervisor
# relaunches it automatically and always records the *current* public URL to
# a stable file, since a quick tunnel gets a new URL on each restart.
#
#   Current URL:  cat /tmp/stockly_url.txt
#   Tunnel log:   /tmp/stockly_tunnel.log
#
# Usage: tunnel-keepalive.sh [PORT]   (defaults to 5001)
set -u

PORT="${1:-5001}"
URL_FILE=/tmp/stockly_url.txt
LOG=/tmp/stockly_tunnel.log

while true; do
  : > "$LOG"
  cloudflared tunnel --url "http://127.0.0.1:${PORT}" \
      --protocol http2 --edge-ip-version 4 >> "$LOG" 2>&1 &
  CF_PID=$!

  # Capture the assigned quick-tunnel URL once it appears.
  for _ in $(seq 1 40); do
    U=$(grep -Eo "https://[a-z0-9-]+\.trycloudflare\.com" "$LOG" | head -1)
    if [ -n "$U" ]; then
      echo "$U" > "$URL_FILE"
      echo "$(date '+%Y-%m-%d %H:%M:%S') up: $U" >> /tmp/stockly_keepalive.log
      break
    fi
    sleep 1
  done

  wait "$CF_PID"   # block until cloudflared exits
  echo "$(date '+%Y-%m-%d %H:%M:%S') tunnel exited, restarting in 3s" >> /tmp/stockly_keepalive.log
  sleep 3
done

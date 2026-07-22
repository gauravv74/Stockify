#!/usr/bin/env bash
# Run this on your HOME machine (a residential/mobile connection).
#
# It opens a reverse SOCKS proxy on the EC2 host so the deployed Stockly app can
# make its scraper requests through YOUR home internet instead of the AWS
# datacenter IP (which grocery apps block). Keep this running while you want the
# scrapers to work; it auto-reconnects if the connection drops.
#
# Usage:
#   ./deploy/home-proxy.sh [EC2_HOST] [ssh_user] [pem_file]
# Defaults: host=65.1.45.28 user=ubuntu key=../stockifyy.pem
#
# On the server, the app must have:  STOCKLY_PROXY_SERVER=socks5://host.docker.internal:1080
set -euo pipefail

HOST="${1:-65.1.45.28}"
USER="${2:-ubuntu}"
HERE="$(cd "$(dirname "$0")/.." && pwd)"
KEY="${3:-$HERE/stockifyy.pem}"
PORT="${PROXY_PORT:-1080}"

if [ ! -f "$KEY" ]; then
  echo "PEM key not found: $KEY"
  exit 1
fi
chmod 400 "$KEY" 2>/dev/null || true

echo "Home proxy -> ${USER}@${HOST}  (remote SOCKS on :${PORT}, exits via this machine)"
echo "Keep this terminal open. Ctrl-C to stop."

while true; do
  ssh -N \
    -o ExitOnForwardFailure=yes \
    -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
    -o StrictHostKeyChecking=accept-new \
    -i "$KEY" \
    -R "0.0.0.0:${PORT}" \
    "${USER}@${HOST}" || true
  echo "$(date '+%H:%M:%S') tunnel dropped — reconnecting in 5s..."
  sleep 5
done

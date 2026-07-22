#!/usr/bin/env bash
# Deploy Stockly to EC2 from your Mac.
# Usage: ./deploy/deploy-to-ec2.sh <EC2_PUBLIC_IP> [ssh_user] [pem_file] [domain]
set -euo pipefail

IP="${1:?Usage: $0 <EC2_PUBLIC_IP> [ubuntu|ec2-user] [pem_file] [domain]}"
USER="${2:-ubuntu}"
DOMAIN="${4:-${STOCKLY_DOMAIN:-}}"
HERE="$(cd "$(dirname "$0")/.." && pwd)"
KEY="${3:-}"
if [ -z "$KEY" ]; then
  for candidate in "$HERE/stockify-3.pem" "$HERE/stockify.pem"; do
  if [ -f "$candidate" ]; then KEY="$candidate"; break; fi
  done
fi
BOOT="$HERE/deploy/ec2-bootstrap.sh"

if [ -z "$KEY" ] || [ ! -f "$KEY" ]; then
  echo "Missing PEM key. Pass it as the 3rd argument."
  exit 1
fi
chmod 400 "$KEY"

echo "==> Connecting to ${USER}@${IP}..."
if [ -n "$DOMAIN" ]; then
  echo "==> Domain: ${DOMAIN} (Caddy + HTTPS)"
fi
ssh -o StrictHostKeyChecking=accept-new -i "$KEY" "${USER}@${IP}" \
  "STOCKLY_DOMAIN='${DOMAIN}' bash -s" < "$BOOT"

if [ -n "$DOMAIN" ]; then
  echo ""
  echo "Done. Open: https://${DOMAIN}/"
else
  echo ""
  echo "Done. Open: http://${IP}/"
fi

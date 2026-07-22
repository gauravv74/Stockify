#!/usr/bin/env bash
# Deploy Stockly to EC2 from your Mac.
# Usage: ./deploy/deploy-to-ec2.sh <EC2_PUBLIC_IP> [ssh_user]
set -euo pipefail

IP="${1:?Usage: $0 <EC2_PUBLIC_IP> [ubuntu|ec2-user]}"
USER="${2:-ubuntu}"
HERE="$(cd "$(dirname "$0")/.." && pwd)"
KEY="$HERE/stockify.pem"
BOOT="$HERE/deploy/ec2-bootstrap.sh"

if [ ! -f "$KEY" ]; then
  echo "Missing PEM key: $KEY"
  exit 1
fi
chmod 400 "$KEY"

echo "==> Connecting to ${USER}@${IP}..."
ssh -o StrictHostKeyChecking=accept-new -i "$KEY" "${USER}@${IP}" "bash -s" < "$BOOT"

echo ""
echo "Done. Open: http://${IP}/"

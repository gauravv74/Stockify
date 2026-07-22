#!/usr/bin/env bash
# Run on a fresh Ubuntu EC2 instance to install Docker and start Stockly.
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/gauravv74/Stockify.git}"
APP_DIR="${APP_DIR:-$HOME/Stockify}"

echo "==> Installing Docker..."
sudo apt-get update -qq
sudo apt-get install -y -qq docker.io docker-compose-v2 git curl
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER" || true

echo "==> Cloning Stockly..."
if [ -d "$APP_DIR/.git" ]; then
  cd "$APP_DIR" && git pull
else
  git clone "$REPO_URL" "$APP_DIR"
  cd "$APP_DIR"
fi

echo "==> Configuring environment..."
if [ ! -f .env ]; then
  SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")
  ADMIN_PASS=$(python3 -c "import secrets; print(secrets.token_urlsafe(16))")
  cat > .env <<EOF
STOCKLY_ENV=production
STOCKLY_HOST=0.0.0.0
STOCKLY_PORT=5001
STOCKLY_WORKERS=2
STOCKLY_THREADS=4
STOCKLY_TIMEOUT=300
STOCKLY_SECRET_KEY=${SECRET}
STOCKLY_COOKIE_SECURE=0
STOCKLY_COOKIE_SAMESITE=Lax
STOCKLY_TRUST_PROXY=1
STOCKLY_SESSION_DAYS=14
STOCKLY_DATA_DIR=/app/data
STOCKLY_ADMIN_USER=admin
STOCKLY_ADMIN_PASS=${ADMIN_PASS}
EOF
  echo ""
  echo "============================================"
  echo "  Admin login: admin / ${ADMIN_PASS}"
  echo "  Save this password — shown once only."
  echo "============================================"
  echo ""
fi

echo "==> Building and starting containers..."
sudo docker compose up -d --build

echo "==> Waiting for health check..."
for i in $(seq 1 30); do
  if curl -sf http://127.0.0.1/api/health >/dev/null 2>&1; then
    echo "Stockly is up."
    curl -s http://127.0.0.1/api/health
    exit 0
  fi
  sleep 5
done

echo "Health check timed out — check logs: sudo docker compose logs"
exit 1

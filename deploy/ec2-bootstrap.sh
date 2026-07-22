#!/usr/bin/env bash
# Run on a fresh Ubuntu EC2 instance to install Docker and start Stockly.
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/gauravv74/Stockify.git}"
APP_DIR="${APP_DIR:-$HOME/Stockify}"
STOCKLY_DOMAIN="${STOCKLY_DOMAIN:-}"

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
  COOKIE_SECURE=0
  if [ -n "$STOCKLY_DOMAIN" ]; then
    COOKIE_SECURE=1
  fi
  cat > .env <<EOF
STOCKLY_ENV=production
STOCKLY_HOST=0.0.0.0
STOCKLY_PORT=5001
STOCKLY_WORKERS=2
STOCKLY_THREADS=4
STOCKLY_TIMEOUT=300
STOCKLY_SECRET_KEY=${SECRET}
STOCKLY_COOKIE_SECURE=${COOKIE_SECURE}
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

if [ -n "$STOCKLY_DOMAIN" ]; then
  grep -q '^STOCKLY_DOMAIN=' .env 2>/dev/null || echo "STOCKLY_DOMAIN=${STOCKLY_DOMAIN}" >> .env
  if grep -q '^STOCKLY_COOKIE_SECURE=' .env; then
    sed -i "s/^STOCKLY_COOKIE_SECURE=.*/STOCKLY_COOKIE_SECURE=1/" .env
  else
    echo "STOCKLY_COOKIE_SECURE=1" >> .env
  fi
  export STOCKLY_DOMAIN
  COMPOSE_FILES=(-f docker-compose.caddy.yml)
else
  COMPOSE_FILES=(-f docker-compose.yml)
fi

echo "==> Building and starting containers..."
sudo -E docker compose "${COMPOSE_FILES[@]}" up -d --build

echo "==> Waiting for health check..."
HEALTH_URL="http://127.0.0.1/api/health"
if [ -n "$STOCKLY_DOMAIN" ]; then
  HEALTH_URL="https://${STOCKLY_DOMAIN}/api/health"
fi
for i in $(seq 1 40); do
  if curl -skf "$HEALTH_URL" >/dev/null 2>&1 || curl -sf http://127.0.0.1/api/health >/dev/null 2>&1; then
    echo "Stockly is up."
    curl -sk "$HEALTH_URL" 2>/dev/null || curl -s http://127.0.0.1/api/health
    exit 0
  fi
  sleep 5
done

echo "Health check timed out — check logs: sudo docker compose logs"
exit 1

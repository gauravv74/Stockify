# Stockly production deploy

## Quick start (Docker)

```bash
cd "/Users/gauravg/Desktop/Agent Creation"
cp .env.example .env
# edit .env — set a real STOCKLY_SECRET_KEY
python3 -c "import secrets; print(secrets.token_hex(32))"

docker compose up -d --build
```

Open http://YOUR_SERVER/ and sign in:
- Default: `admin` / `admin123`
- You **must change password** on first login

## What production mode includes

- Gunicorn (multi-thread workers) instead of Flask dev server
- SQLite user DB under `/app/data` (persisted Docker volume)
- Secure session cookies behind HTTPS proxy
- ProxyFix for nginx `X-Forwarded-*`
- Health check: `GET /api/health`
- Playwright Chromium baked into the image (Instamart / Zepto)

## HTTPS (recommended)

Terminate TLS at nginx/caddy/cloud LB, keep `STOCKLY_COOKIE_SECURE=1` and
`STOCKLY_TRUST_PROXY=1`. Point DNS at the host and mount certificates into nginx.

## Bare metal (no Docker)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python3 -m playwright install --with-deps chromium
export STOCKLY_ENV=production
export STOCKLY_SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))")
export STOCKLY_COOKIE_SECURE=1
export STOCKLY_TRUST_PROXY=1
gunicorn -c gunicorn.conf.py wsgi:app
```

Put nginx in front (see `deploy/nginx.conf`).

## Ops notes

- Back up `data/stockly.db` regularly
- Scrapers can break when Blinkit/Swiggy/Zepto change APIs — expect maintenance
- Prefer 1–2 workers; Playwright is memory-heavy
- Change the default admin password immediately

# Self-hosted WhatsApp bridge (whatsapp-web.js). Uses Puppeteer's bundled
# Chromium; the base image ships the shared libs Chromium needs on Debian.
FROM node:20-bookworm-slim

ENV NODE_ENV=production \
    WA_BRIDGE_HOST=0.0.0.0 \
    WA_BRIDGE_PORT=3001 \
    WA_AUTH_DIR=/data/.wwebjs_auth

# Chromium runtime dependencies (Puppeteer downloads the browser binary itself).
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates fonts-liberation libasound2 libatk-bridge2.0-0 \
      libatk1.0-0 libatspi2.0-0 libcups2 libdbus-1-3 libdrm2 libgbm1 \
      libglib2.0-0 libgtk-3-0 libnspr4 libnss3 libpango-1.0-0 libx11-6 \
      libxcb1 libxcomposite1 libxdamage1 libxext6 libxfixes3 libxkbcommon0 \
      libxrandr2 wget \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY package.json ./
RUN npm install --omit=dev

COPY server.js ./
RUN mkdir -p /data

EXPOSE 3001
CMD ["node", "server.js"]

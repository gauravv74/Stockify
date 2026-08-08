'use strict';

/**
 * Stockly WhatsApp bridge — self-hosted, free.
 *
 * Owns a single WhatsApp Web session (whatsapp-web.js) and exposes a tiny local
 * HTTP API the Python worker calls to send messages:
 *
 *   GET  /health          -> { ready: bool, state }
 *   POST /send            -> { to: "919...", message: "..." }   (X-Auth-Token header)
 *
 * First run prints a QR code in the terminal. Open WhatsApp on your phone ->
 * Settings -> Linked Devices -> Link a Device, and scan it. The session is
 * persisted under .wwebjs_auth/ (LocalAuth), so you only scan once; restarts
 * reconnect automatically.
 *
 * NOTE: whatsapp-web.js is an unofficial library that automates WhatsApp Web.
 * Use a number you're comfortable linking; heavy/abusive sending can get a
 * number flagged. Stock alerts on a 20-min cadence are well within normal use.
 */

const path = require('path');
const express = require('express');
const qrcode = require('qrcode-terminal');
const QRImage = require('qrcode');
const { Client, LocalAuth } = require('whatsapp-web.js');

process.on('unhandledRejection', (reason) => {
  console.error('UNHANDLED REJECTION:', reason);
});
process.on('uncaughtException', (err) => {
  console.error('UNCAUGHT EXCEPTION:', err);
});

const PORT = parseInt(process.env.WA_BRIDGE_PORT || '3001', 10);
const HOST = process.env.WA_BRIDGE_HOST || '127.0.0.1';
const TOKEN = process.env.WA_BRIDGE_TOKEN || '';
const AUTH_DIR = process.env.WA_AUTH_DIR || path.join(__dirname, '.wwebjs_auth');

let ready = false;
let lastState = 'starting';

// Reuse an existing Chrome/Chromium instead of Puppeteer's bundled download.
// Set WA_CHROME_PATH explicitly, else fall back to common macOS/Linux locations.
function resolveChrome() {
  if (process.env.WA_CHROME_PATH) return process.env.WA_CHROME_PATH;
  const fs = require('fs');
  const candidates = [
    '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
    '/Applications/Chromium.app/Contents/MacOS/Chromium',
    '/usr/bin/google-chrome',
    '/usr/bin/chromium',
    '/usr/bin/chromium-browser',
  ];
  for (const c of candidates) {
    try { if (fs.existsSync(c)) return c; } catch (_) { /* ignore */ }
  }
  return undefined; // let Puppeteer use its bundled Chromium (e.g. in Docker)
}

const CHROME_PATH = resolveChrome();
console.log('Chrome executable:', CHROME_PATH || '(Puppeteer bundled)');

const puppeteerOpts = {
  headless: true,
  args: [
    '--no-sandbox',
    '--disable-setuid-sandbox',
    '--disable-dev-shm-usage',
    '--disable-gpu',
  ],
};
if (CHROME_PATH) puppeteerOpts.executablePath = CHROME_PATH;

const client = new Client({
  authStrategy: new LocalAuth({ dataPath: AUTH_DIR }),
  puppeteer: puppeteerOpts,
});

const QR_PNG = path.join(__dirname, 'qr.png');
client.on('qr', (qr) => {
  lastState = 'qr';
  ready = false;
  console.log('\n📱  Scan this QR in WhatsApp → Settings → Linked Devices → Link a Device:\n');
  qrcode.generate(qr, { small: true });
  // Also write a scannable PNG so it can be viewed outside the terminal.
  QRImage.toFile(QR_PNG, qr, { width: 320, margin: 2 })
    .then(() => console.log(`(QR image saved to ${QR_PNG})`))
    .catch((e) => console.error('QR png write failed:', e));
  console.log('\n(Waiting for you to scan…)\n');
});

client.on('loading_screen', (percent, message) => {
  lastState = `loading ${percent}%`;
  console.log(`… loading ${percent}% ${message || ''}`);
});

client.on('authenticated', () => {
  lastState = 'authenticated';
  console.log('🔐  Authenticated — session saved, no need to scan again.');
});

client.on('auth_failure', (msg) => {
  ready = false;
  lastState = 'auth_failure';
  console.error('❌  Auth failure:', msg, '\n    Run `npm run logout` and restart to re-scan.');
});

client.on('ready', () => {
  ready = true;
  lastState = 'ready';
  // Linked now — drop the stale QR so /qr stops serving it.
  try { require('fs').unlinkSync(QR_PNG); } catch (_) { /* ignore */ }
  console.log('✅  WhatsApp bridge ready — you can send messages now.');
});

client.on('disconnected', (reason) => {
  ready = false;
  lastState = `disconnected:${reason}`;
  console.warn('⚠️   Disconnected:', reason, '— attempting to reconnect…');
  // Re-initialise so a transient disconnect self-heals.
  client.initialize().catch((e) => console.error('re-init failed:', e));
});

client.initialize().catch((e) => {
  lastState = 'init_error';
  console.error('initialize() failed:', e);
});

// ---------------------------------------------------------------------------
// HTTP API (bound to localhost by default; use a token if you expose it).
// ---------------------------------------------------------------------------
const app = express();
app.use(express.json({ limit: '64kb' }));

function authOk(req) {
  if (!TOKEN) return true;
  return req.get('X-Auth-Token') === TOKEN;
}

function statusPayload() {
  let me = null;
  try { me = ready && client.info ? client.info.wid.user : null; } catch (_) { me = null; }
  const fs = require('fs');
  let hasQr = false;
  try { hasQr = !ready && lastState === 'qr' && fs.existsSync(QR_PNG); } catch (_) { hasQr = false; }
  return { ready, state: lastState, me, has_qr: hasQr };
}

app.get('/health', (_req, res) => res.json(statusPayload()));

// Same as /health; the admin UI polls this for link status.
app.get('/status', (_req, res) => res.json(statusPayload()));

// Current pairing QR as a PNG (only while unlinked). 204 when already linked.
app.get('/qr', (_req, res) => {
  const fs = require('fs');
  if (ready || !fs.existsSync(QR_PNG)) return res.status(204).end();
  res.type('png');
  res.setHeader('Cache-Control', 'no-store');
  return res.sendFile(QR_PNG);
});

// Unlink the current WhatsApp account and re-initialise so a fresh QR appears.
app.post('/logout', async (req, res) => {
  if (!authOk(req)) return res.status(401).json({ error: 'unauthorized' });
  try {
    ready = false;
    lastState = 'logging_out';
    try { await client.logout(); } catch (_) { /* may already be unlinked */ }
    try { require('fs').unlinkSync(QR_PNG); } catch (_) { /* ignore */ }
    // Re-initialise to trigger a new 'qr' event.
    client.initialize().catch((e) => console.error('re-init after logout failed:', e));
    return res.json({ ok: true });
  } catch (e) {
    return res.status(500).json({ error: String((e && e.message) || e) });
  }
});

app.post('/send', async (req, res) => {
  if (!authOk(req)) return res.status(401).json({ error: 'unauthorized' });
  if (!ready) return res.status(503).json({ error: `bridge not ready (state=${lastState}); scan the QR` });

  const { to, message } = req.body || {};
  if (!to || !message) return res.status(400).json({ error: 'to and message are required' });

  const num = String(to).replace(/\D/g, '');
  if (!num) return res.status(400).json({ error: 'invalid phone number' });

  // Try resolving the proper chat id, but tolerate getNumberId failing.
  let chatId = `${num}@c.us`;
  try {
    const numberId = await client.getNumberId(num);
    if (numberId && numberId._serialized) chatId = numberId._serialized;
  } catch (e) {
    console.error('getNumberId failed (using default chatId):', e && e.message);
  }
  try {
    const sent = await client.sendMessage(chatId, String(message));
    const id = sent && sent.id ? sent.id._serialized : null;
    return res.json({ ok: true, id });
  } catch (e) {
    console.error('sendMessage failed:\n', (e && e.stack) || e);
    return res.status(500).json({ error: String((e && e.message) || e) });
  }
});

app.listen(PORT, HOST, () => {
  console.log(`Stockly WhatsApp bridge listening on http://${HOST}:${PORT}`);
  if (!TOKEN) console.log('   (no WA_BRIDGE_TOKEN set — fine for localhost-only use)');
});

// Graceful shutdown so the Chromium child process doesn't linger.
for (const sig of ['SIGINT', 'SIGTERM']) {
  process.on(sig, async () => {
    console.log(`\n${sig} received — shutting down bridge…`);
    try { await client.destroy(); } catch (_) { /* ignore */ }
    process.exit(0);
  });
}

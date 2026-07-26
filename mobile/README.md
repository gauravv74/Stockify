# Stockly Mobile (Expo / React Native)

A cross-platform (iOS + Android) mobile client for the Stockly availability
checker. It talks to the existing Flask backend — no separate API needed.

## What it does

- Sign in with your Stockly account (session-cookie auth, same as the web app)
- Forced password change on first login (mirrors the backend rule)
- Pick a platform (Blinkit / Instamart / Zepto / BigBasket, or **All**)
- Choose cities and/or type pincodes, enter one or more products
- Runs `/api/check` and streams results **live** into a list as each
  pincode × product × platform is checked

## Requirements

- Node 18+ (Node 20 recommended)
- The **Expo Go** app on your phone (App Store / Play Store), or an
  iOS Simulator / Android emulator
- A reachable Stockly backend over **https** (the backend sets `Secure`
  session cookies in production, so a plain-http address won't stay signed in)

## Run it

```bash
cd mobile
npm install          # first time only
npm start            # starts the Metro dev server + QR code
```

Then:

- **Phone:** open Expo Go and scan the QR code shown in the terminal
- **iOS simulator:** press `i`   •   **Android emulator:** press `a`

On first launch, tap **Server** and enter your backend URL, e.g.
`https://your-domain`. It's saved on the device, so you only do this once
(tap **Change** to point at a different server later).

Default first-boot login is `admin` / `admin123` (you'll be forced to change it).

## How it connects

- **Auth:** React Native's native networking keeps a shared cookie jar, so the
  `stockly_session` cookie from `/api/login` is replayed automatically on later
  requests — including in Expo Go. No token handling required.
- **Streaming:** `/api/check` returns newline-delimited JSON. RN's `fetch`
  can't read a body incrementally, so the client uses `XMLHttpRequest` and
  parses each complete line out of the growing response (`src/api.js`).

## Project layout

```
App.js                      # routes to Login / ChangePassword / Check by auth state
src/
  api.js                    # API client + streaming check
  AuthContext.js            # session state (login/logout/me/change-password)
  config.js                 # persisted, editable server URL
  theme.js                  # colors + status styling
  components/ui.js          # Button / Field / Chip / Card
  components/ResultRow.js    # one availability result
  screens/LoginScreen.js
  screens/ChangePasswordScreen.js
  screens/CheckScreen.js
```

## Building installable apps (later)

Use EAS Build (no local Xcode/Android Studio toolchain needed):

```bash
npm install -g eas-cli
eas build -p android --profile preview   # APK
eas build -p ios --profile preview        # needs an Apple Developer account
```

## Notes

- BigBasket results depend on the backend reaching BigBasket successfully.
  From datacenter IPs BigBasket's Akamai edge blocks scraping, so run the
  backend behind your residential proxy (see the repo's `deploy/home-proxy.sh`).

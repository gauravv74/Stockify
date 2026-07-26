import AsyncStorage from "@react-native-async-storage/async-storage";

// The backend base URL. Because the server issues `Secure` session cookies in
// production, this MUST be an https:// origin (your Caddy/nginx domain), e.g.
// https://stockly.example.com — a plain-http IP will drop the login cookie.
//
// It is user-editable on the login screen and persisted here so you never need
// to rebuild the app to point at a different server.
const STORAGE_KEY = "stockly.apiBase";

// Optional compile-time default. Leave blank to force entry on first launch.
export const DEFAULT_API_BASE = "";

function normalize(url) {
  if (!url) return "";
  let u = url.trim();
  if (!/^https?:\/\//i.test(u)) u = "https://" + u;
  return u.replace(/\/+$/, "");
}

export async function loadApiBase() {
  try {
    const saved = await AsyncStorage.getItem(STORAGE_KEY);
    return normalize(saved || DEFAULT_API_BASE);
  } catch {
    return normalize(DEFAULT_API_BASE);
  }
}

export async function saveApiBase(url) {
  const clean = normalize(url);
  await AsyncStorage.setItem(STORAGE_KEY, clean);
  return clean;
}

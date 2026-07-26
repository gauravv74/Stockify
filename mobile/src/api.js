// Stockly API client for React Native.
//
// Auth uses the Flask session cookie. React Native's native networking layer
// (fetch + XMLHttpRequest) keeps a shared cookie jar automatically, so once
// /api/login sets `stockly_session` it is replayed on later requests without us
// managing headers manually — this works in Expo Go too.

let API_BASE = "";

export function setApiBase(url) {
  API_BASE = (url || "").replace(/\/+$/, "");
}

export function getApiBase() {
  return API_BASE;
}

class ApiError extends Error {
  constructor(message, status) {
    super(message || "Request failed");
    this.status = status;
  }
}

async function request(path, { method = "GET", body } = {}) {
  if (!API_BASE) throw new ApiError("No server URL configured", 0);
  let res;
  try {
    res = await fetch(API_BASE + path, {
      method,
      headers: body ? { "Content-Type": "application/json" } : undefined,
      body: body ? JSON.stringify(body) : undefined,
    });
  } catch (e) {
    throw new ApiError("Can't reach the server. Check the URL and your connection.", 0);
  }
  let data = null;
  try {
    data = await res.json();
  } catch {
    data = null;
  }
  if (!res.ok) {
    const msg = (data && (data.error || data.message)) || `HTTP ${res.status}`;
    throw new ApiError(msg, res.status);
  }
  return data;
}

export const api = {
  login: (username, password) =>
    request("/api/login", { method: "POST", body: { username, password } }),
  logout: () => request("/api/logout", { method: "POST" }),
  me: () => request("/api/me"),
  changePassword: (current_password, new_password) =>
    request("/api/change-password", {
      method: "POST",
      body: { current_password, new_password },
    }),
  cities: () => request("/api/cities"),
  health: () => request("/api/health"),
};

// Streaming availability check.
//
// /api/check streams newline-delimited JSON (meta -> result rows -> done).
// RN's fetch cannot read a body incrementally, so we use XMLHttpRequest and
// parse each complete line out of the growing responseText.
//
// Returns an abort() function.
export function streamCheck(payload, handlers = {}) {
  const { onMeta, onRow, onError, onDone } = handlers;
  const xhr = new XMLHttpRequest();
  let consumed = 0;
  let finished = false;

  const dispatch = (obj) => {
    if (!obj || typeof obj !== "object") return;
    if (obj.type === "meta") onMeta && onMeta(obj);
    else if (obj.type === "done") onDone && onDone(obj);
    else if (obj.type === "error") onError && onError(obj.message || "Error");
    else onRow && onRow(obj);
  };

  const drain = () => {
    const text = xhr.responseText || "";
    let nl;
    while ((nl = text.indexOf("\n", consumed)) !== -1) {
      const line = text.slice(consumed, nl).trim();
      consumed = nl + 1;
      if (!line) continue;
      try {
        dispatch(JSON.parse(line));
      } catch {
        /* partial or non-JSON line; ignore */
      }
    }
  };

  xhr.open("POST", API_BASE + "/api/check");
  xhr.setRequestHeader("Content-Type", "application/json");

  xhr.onreadystatechange = () => {
    if (xhr.readyState === XMLHttpRequest.LOADING) {
      if (xhr.status === 200) drain();
    } else if (xhr.readyState === XMLHttpRequest.DONE) {
      if (finished) return;
      finished = true;
      if (xhr.status === 200) {
        drain();
        onDone && onDone({ type: "done" });
      } else {
        let msg = `HTTP ${xhr.status}`;
        try {
          const j = JSON.parse(xhr.responseText || "{}");
          if (j.error) msg = j.error;
        } catch {}
        if (xhr.status === 0) msg = "Can't reach the server.";
        onError && onError(msg, xhr.status);
      }
    }
  };
  xhr.onerror = () => {
    if (finished) return;
    finished = true;
    onError && onError("Network error.", 0);
  };

  xhr.send(JSON.stringify(payload));

  return () => {
    finished = true;
    try {
      xhr.abort();
    } catch {}
  };
}

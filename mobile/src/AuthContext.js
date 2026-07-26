import React, { createContext, useContext, useEffect, useState, useCallback } from "react";
import { api, setApiBase } from "./api";
import { loadApiBase, saveApiBase } from "./config";

const AuthContext = createContext(null);

export function AuthProvider({ children }) {
  const [booting, setBooting] = useState(true);
  const [apiBase, setBase] = useState("");
  const [user, setUser] = useState(null);
  const [platforms, setPlatforms] = useState([]);
  const [mustChange, setMustChange] = useState(false);

  const applySession = useCallback((data) => {
    setUser(data.user || null);
    setPlatforms(data.platforms || []);
    setMustChange(!!data.must_change_password);
  }, []);

  const clearSession = useCallback(() => {
    setUser(null);
    setPlatforms([]);
    setMustChange(false);
  }, []);

  // On launch: load saved server URL, then probe an existing session.
  useEffect(() => {
    (async () => {
      const base = await loadApiBase();
      setBase(base);
      setApiBase(base);
      if (base) {
        try {
          const me = await api.me();
          applySession(me);
        } catch {
          clearSession();
        }
      }
      setBooting(false);
    })();
  }, [applySession, clearSession]);

  const setServer = useCallback(async (url) => {
    const clean = await saveApiBase(url);
    setBase(clean);
    setApiBase(clean);
    return clean;
  }, []);

  const login = useCallback(
    async (username, password) => {
      const data = await api.login(username, password);
      applySession(data);
      return data;
    },
    [applySession]
  );

  const changePassword = useCallback(
    async (current, next) => {
      const data = await api.changePassword(current, next);
      applySession(data);
      return data;
    },
    [applySession]
  );

  const logout = useCallback(async () => {
    try {
      await api.logout();
    } catch {}
    clearSession();
  }, [clearSession]);

  return (
    <AuthContext.Provider
      value={{
        booting,
        apiBase,
        user,
        platforms,
        mustChange,
        isAuthed: !!user,
        setServer,
        login,
        logout,
        changePassword,
      }}
    >
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within AuthProvider");
  return ctx;
}

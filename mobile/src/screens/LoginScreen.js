import React, { useState } from "react";
import {
  View,
  Text,
  StyleSheet,
  KeyboardAvoidingView,
  Platform,
  ScrollView,
  TouchableOpacity,
} from "react-native";
import { useAuth } from "../AuthContext";
import { Button, Field } from "../components/ui";
import { colors } from "../theme";

export default function LoginScreen() {
  const { apiBase, setServer, login } = useAuth();
  const [server, setServerInput] = useState(apiBase || "");
  const [editingServer, setEditingServer] = useState(!apiBase);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const onSaveServer = async () => {
    setError("");
    if (!server.trim()) {
      setError("Enter your server URL (e.g. https://your-domain).");
      return;
    }
    try {
      const clean = await setServer(server);
      setServerInput(clean);
      setEditingServer(false);
    } catch (e) {
      setError(String(e.message || e));
    }
  };

  const onLogin = async () => {
    setError("");
    if (!apiBase) {
      setEditingServer(true);
      setError("Set your server URL first.");
      return;
    }
    if (!username || !password) {
      setError("Enter username and password.");
      return;
    }
    setBusy(true);
    try {
      await login(username.trim(), password);
    } catch (e) {
      setError(String(e.message || e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <KeyboardAvoidingView
      style={styles.flex}
      behavior={Platform.OS === "ios" ? "padding" : undefined}
    >
      <ScrollView
        contentContainerStyle={styles.container}
        keyboardShouldPersistTaps="handled"
      >
        <View style={styles.logoWrap}>
          <Text style={styles.logo}>Stockly</Text>
          <Text style={styles.tagline}>Multi-platform availability checker</Text>
        </View>

        {editingServer ? (
          <>
            <Field
              label="Server URL"
              placeholder="https://your-domain"
              autoCapitalize="none"
              autoCorrect={false}
              keyboardType="url"
              value={server}
              onChangeText={setServerInput}
              hint="Use your https:// domain. A plain-http address won't keep you signed in."
            />
            <Button title="Save server" onPress={onSaveServer} />
          </>
        ) : (
          <TouchableOpacity onPress={() => setEditingServer(true)} style={styles.serverPill}>
            <Text style={styles.serverPillLabel}>Server</Text>
            <Text style={styles.serverPillValue} numberOfLines={1}>
              {apiBase}
            </Text>
            <Text style={styles.serverPillEdit}>Change</Text>
          </TouchableOpacity>
        )}

        {!editingServer && (
          <View style={{ marginTop: 8 }}>
            <Field
              label="Username"
              placeholder="admin"
              autoCapitalize="none"
              autoCorrect={false}
              value={username}
              onChangeText={setUsername}
            />
            <Field
              label="Password"
              placeholder="••••••••"
              secureTextEntry
              value={password}
              onChangeText={setPassword}
            />
            <Button title="Sign in" onPress={onLogin} loading={busy} />
          </View>
        )}

        {error ? <Text style={styles.error}>{error}</Text> : null}
      </ScrollView>
    </KeyboardAvoidingView>
  );
}

const styles = StyleSheet.create({
  flex: { flex: 1, backgroundColor: colors.bg },
  container: { padding: 24, paddingTop: 90, flexGrow: 1 },
  logoWrap: { alignItems: "center", marginBottom: 40 },
  logo: { color: colors.primary, fontSize: 40, fontWeight: "800", letterSpacing: -1 },
  tagline: { color: colors.textMuted, fontSize: 14, marginTop: 6 },
  serverPill: {
    flexDirection: "row",
    alignItems: "center",
    backgroundColor: colors.surface,
    borderWidth: 1,
    borderColor: colors.border,
    borderRadius: 12,
    padding: 14,
    marginBottom: 8,
  },
  serverPillLabel: { color: colors.textMuted, fontSize: 13, fontWeight: "700", marginRight: 10 },
  serverPillValue: { color: colors.text, fontSize: 14, flex: 1 },
  serverPillEdit: { color: colors.primary, fontSize: 13, fontWeight: "700", marginLeft: 10 },
  error: { color: colors.danger, marginTop: 16, textAlign: "center", fontSize: 14 },
});

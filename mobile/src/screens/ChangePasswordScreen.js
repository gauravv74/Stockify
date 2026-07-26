import React, { useState } from "react";
import {
  View,
  Text,
  StyleSheet,
  KeyboardAvoidingView,
  Platform,
  ScrollView,
} from "react-native";
import { useAuth } from "../AuthContext";
import { Button, Field } from "../components/ui";
import { colors } from "../theme";

export default function ChangePasswordScreen() {
  const { changePassword, logout } = useAuth();
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const onSubmit = async () => {
    setError("");
    if (next.length < 8) return setError("New password must be at least 8 characters.");
    if (next !== confirm) return setError("New passwords don't match.");
    setBusy(true);
    try {
      await changePassword(current, next);
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
      <ScrollView contentContainerStyle={styles.container} keyboardShouldPersistTaps="handled">
        <Text style={styles.title}>Change your password</Text>
        <Text style={styles.subtitle}>
          You must set a new password before continuing.
        </Text>

        <Field
          label="Current password"
          secureTextEntry
          value={current}
          onChangeText={setCurrent}
        />
        <Field
          label="New password"
          secureTextEntry
          value={next}
          onChangeText={setNext}
          hint="At least 8 characters."
        />
        <Field
          label="Confirm new password"
          secureTextEntry
          value={confirm}
          onChangeText={setConfirm}
        />
        <Button title="Update password" onPress={onSubmit} loading={busy} />
        <Button title="Sign out" variant="ghost" onPress={logout} style={{ marginTop: 12 }} />

        {error ? <Text style={styles.error}>{error}</Text> : null}
      </ScrollView>
    </KeyboardAvoidingView>
  );
}

const styles = StyleSheet.create({
  flex: { flex: 1, backgroundColor: colors.bg },
  container: { padding: 24, paddingTop: 80, flexGrow: 1 },
  title: { color: colors.text, fontSize: 26, fontWeight: "800", marginBottom: 8 },
  subtitle: { color: colors.textMuted, fontSize: 14, marginBottom: 28 },
  error: { color: colors.danger, marginTop: 16, textAlign: "center", fontSize: 14 },
});

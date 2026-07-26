import React from "react";
import {
  Text,
  TextInput,
  TouchableOpacity,
  View,
  StyleSheet,
  ActivityIndicator,
} from "react-native";
import { colors } from "../theme";

export function Button({ title, onPress, loading, disabled, variant = "primary", style }) {
  const isDisabled = disabled || loading;
  const bg =
    variant === "ghost" ? "transparent" : variant === "danger" ? colors.danger : colors.primary;
  return (
    <TouchableOpacity
      activeOpacity={0.85}
      onPress={onPress}
      disabled={isDisabled}
      style={[
        styles.btn,
        { backgroundColor: bg, opacity: isDisabled ? 0.5 : 1 },
        variant === "ghost" && styles.btnGhost,
        style,
      ]}
    >
      {loading ? (
        <ActivityIndicator color={variant === "ghost" ? colors.text : "#04140a"} />
      ) : (
        <Text style={[styles.btnText, variant === "ghost" && { color: colors.text }]}>{title}</Text>
      )}
    </TouchableOpacity>
  );
}

export function Field({ label, hint, ...props }) {
  return (
    <View style={{ marginBottom: 16 }}>
      {label ? <Text style={styles.label}>{label}</Text> : null}
      <TextInput
        placeholderTextColor={colors.textMuted}
        style={styles.input}
        {...props}
      />
      {hint ? <Text style={styles.hint}>{hint}</Text> : null}
    </View>
  );
}

export function Chip({ label, active, onPress }) {
  return (
    <TouchableOpacity
      activeOpacity={0.8}
      onPress={onPress}
      style={[styles.chip, active && styles.chipActive]}
    >
      <Text style={[styles.chipText, active && styles.chipTextActive]}>{label}</Text>
    </TouchableOpacity>
  );
}

export function Card({ children, style }) {
  return <View style={[styles.card, style]}>{children}</View>;
}

const styles = StyleSheet.create({
  btn: {
    height: 50,
    borderRadius: 12,
    alignItems: "center",
    justifyContent: "center",
    paddingHorizontal: 18,
  },
  btnGhost: { borderWidth: 1, borderColor: colors.border },
  btnText: { color: "#04140a", fontSize: 16, fontWeight: "700" },
  label: { color: colors.textMuted, fontSize: 13, marginBottom: 6, fontWeight: "600" },
  hint: { color: colors.textMuted, fontSize: 12, marginTop: 6 },
  input: {
    backgroundColor: colors.surface,
    borderWidth: 1,
    borderColor: colors.border,
    borderRadius: 12,
    color: colors.text,
    paddingHorizontal: 14,
    paddingVertical: 12,
    fontSize: 16,
  },
  chip: {
    paddingHorizontal: 14,
    paddingVertical: 8,
    borderRadius: 999,
    backgroundColor: colors.chip,
    marginRight: 8,
    marginBottom: 8,
    borderWidth: 1,
    borderColor: colors.border,
  },
  chipActive: { backgroundColor: colors.chipActive, borderColor: colors.chipActive },
  chipText: { color: colors.text, fontSize: 14, fontWeight: "600" },
  chipTextActive: { color: "#04140a" },
  card: {
    backgroundColor: colors.surface,
    borderRadius: 14,
    padding: 16,
    borderWidth: 1,
    borderColor: colors.border,
  },
});

import React from "react";
import { View, Text, StyleSheet } from "react-native";
import { colors, statusMeta, PLATFORM_LABELS } from "../theme";

function money(v) {
  if (v === "" || v === null || v === undefined) return null;
  return "₹" + v;
}

export default function ResultRow({ row }) {
  const meta = statusMeta(row.status);
  const price = money(row.price);
  const mrp = money(row.mrp);
  const showMrp = mrp && mrp !== price;

  return (
    <View style={styles.row}>
      <View style={[styles.dot, { backgroundColor: meta.color }]} />
      <View style={styles.body}>
        <View style={styles.topLine}>
          <Text style={styles.platform}>
            {PLATFORM_LABELS[row.platform] || row.platform}
          </Text>
          <Text style={styles.pincode}>{row.pincode}</Text>
          <View style={{ flex: 1 }} />
          <Text style={[styles.status, { color: meta.color }]}>{meta.label}</Text>
        </View>

        <Text style={styles.product} numberOfLines={1}>
          {row.name || row.product}
        </Text>

        {(row.variant || price) && (
          <View style={styles.metaLine}>
            {row.variant ? <Text style={styles.metaText}>{row.variant}</Text> : null}
            {price ? (
              <Text style={styles.price}>
                {price}
                {showMrp ? <Text style={styles.mrp}>  {mrp}</Text> : null}
              </Text>
            ) : null}
          </View>
        )}

        {(row.eta || row.location || row.detail) && (
          <Text style={styles.sub} numberOfLines={1}>
            {row.eta ? `⏱ ${row.eta}   ` : ""}
            {row.detail ? row.detail : row.location}
          </Text>
        )}
      </View>
    </View>
  );
}

const styles = StyleSheet.create({
  row: {
    flexDirection: "row",
    backgroundColor: colors.surface,
    borderRadius: 12,
    padding: 14,
    marginBottom: 10,
    borderWidth: 1,
    borderColor: colors.border,
  },
  dot: { width: 10, height: 10, borderRadius: 5, marginTop: 5, marginRight: 12 },
  body: { flex: 1 },
  topLine: { flexDirection: "row", alignItems: "center", marginBottom: 4 },
  platform: { color: colors.text, fontSize: 13, fontWeight: "800", marginRight: 8 },
  pincode: { color: colors.textMuted, fontSize: 12 },
  status: { fontSize: 12, fontWeight: "700" },
  product: { color: colors.text, fontSize: 15, fontWeight: "600" },
  metaLine: { flexDirection: "row", alignItems: "center", marginTop: 4 },
  metaText: { color: colors.textMuted, fontSize: 13, marginRight: 10 },
  price: { color: colors.primary, fontSize: 14, fontWeight: "700" },
  mrp: { color: colors.textMuted, fontSize: 12, textDecorationLine: "line-through", fontWeight: "400" },
  sub: { color: colors.textMuted, fontSize: 12, marginTop: 4 },
});

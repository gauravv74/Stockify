import React, { useEffect, useMemo, useRef, useState } from "react";
import {
  View,
  Text,
  StyleSheet,
  FlatList,
  ActivityIndicator,
  TouchableOpacity,
  TextInput,
} from "react-native";
import { SafeAreaView } from "react-native";
import { useAuth } from "../AuthContext";
import { api, streamCheck } from "../api";
import { Button, Chip, Card } from "../components/ui";
import ResultRow from "../components/ResultRow";
import { colors, PLATFORM_LABELS } from "../theme";

export default function CheckScreen() {
  const { user, platforms, logout } = useAuth();

  const platformOptions = useMemo(
    () => (platforms.length > 1 ? ["all", ...platforms] : platforms),
    [platforms]
  );

  const [platform, setPlatform] = useState(platformOptions[0] || "blinkit");
  const [cities, setCities] = useState([]);
  const [citiesErr, setCitiesErr] = useState("");
  const [loadingCities, setLoadingCities] = useState(true);
  const [selected, setSelected] = useState(() => new Set());
  const [pincodes, setPincodes] = useState("");
  const [products, setProducts] = useState("");

  const [running, setRunning] = useState(false);
  const [results, setResults] = useState([]);
  const [meta, setMeta] = useState(null);
  const [error, setError] = useState("");
  const abortRef = useRef(null);

  useEffect(() => {
    (async () => {
      try {
        const data = await api.cities();
        setCities(data.cities || []);
      } catch (e) {
        setCitiesErr(String(e.message || e));
      } finally {
        setLoadingCities(false);
      }
    })();
  }, []);

  const toggleCity = (id) => {
    setSelected((prev) => {
      const next = new Set(prev);
      next.has(id) ? next.delete(id) : next.add(id);
      return next;
    });
  };

  const stop = () => {
    if (abortRef.current) abortRef.current();
    abortRef.current = null;
    setRunning(false);
  };

  const run = () => {
    setError("");
    const productList = products.trim();
    if (!productList) return setError("Enter at least one product.");
    if (selected.size === 0 && !pincodes.trim())
      return setError("Pick a city or enter a pincode.");

    setResults([]);
    setMeta(null);
    setRunning(true);

    const payload = {
      platform,
      cities: Array.from(selected),
      pincodes: pincodes.trim(),
      products: productList,
    };

    abortRef.current = streamCheck(payload, {
      onMeta: (m) => setMeta(m),
      onRow: (row) => setResults((prev) => [...prev, row]),
      onError: (msg) => {
        setError(msg);
        setRunning(false);
      },
      onDone: () => {
        setRunning(false);
        abortRef.current = null;
      },
    });
  };

  useEffect(() => () => abortRef.current && abortRef.current(), []);

  const done = results.length;
  const total = meta?.total || 0;
  const progress = total ? Math.min(1, done / total) : 0;

  const header = (
    <View>
      <View style={styles.headerRow}>
        <View>
          <Text style={styles.hi}>Signed in as</Text>
          <Text style={styles.username}>{user?.username}</Text>
        </View>
        <TouchableOpacity onPress={logout}>
          <Text style={styles.logout}>Sign out</Text>
        </TouchableOpacity>
      </View>

      <Card style={{ marginBottom: 16 }}>
        <Text style={styles.section}>Platform</Text>
        <View style={styles.chipWrap}>
          {platformOptions.map((p) => (
            <Chip
              key={p}
              label={PLATFORM_LABELS[p] || p}
              active={platform === p}
              onPress={() => setPlatform(p)}
            />
          ))}
        </View>

        <Text style={styles.section}>Products</Text>
        <TextInput
          style={styles.input}
          placeholder="e.g. amul milk, maggi"
          placeholderTextColor={colors.textMuted}
          value={products}
          onChangeText={setProducts}
          autoCapitalize="none"
        />
        <Text style={styles.hint}>Separate multiple products with commas or new lines.</Text>

        <Text style={styles.section}>Cities</Text>
        {loadingCities ? (
          <ActivityIndicator color={colors.primary} style={{ marginVertical: 8 }} />
        ) : citiesErr ? (
          <Text style={styles.err}>{citiesErr}</Text>
        ) : (
          <View style={styles.chipWrap}>
            {cities.map((c) => (
              <Chip
                key={c.id}
                label={`${c.name} (${c.count})`}
                active={selected.has(c.id)}
                onPress={() => toggleCity(c.id)}
              />
            ))}
          </View>
        )}

        <Text style={styles.section}>Pincodes (optional)</Text>
        <TextInput
          style={styles.input}
          placeholder="e.g. 560001, 400001"
          placeholderTextColor={colors.textMuted}
          value={pincodes}
          onChangeText={setPincodes}
          keyboardType="numbers-and-punctuation"
        />

        <View style={{ height: 16 }} />
        {running ? (
          <Button title="Stop" variant="danger" onPress={stop} />
        ) : (
          <Button title="Check availability" onPress={run} />
        )}
        {error ? <Text style={styles.err}>{error}</Text> : null}
      </Card>

      {(running || meta) && (
        <View style={styles.progressWrap}>
          <View style={styles.progressBarBg}>
            <View style={[styles.progressBar, { width: `${progress * 100}%` }]} />
          </View>
          <Text style={styles.progressText}>
            {done}
            {total ? ` / ${total}` : ""} checked
            {running ? " …" : ""}
          </Text>
        </View>
      )}
    </View>
  );

  return (
    <SafeAreaView style={styles.safe}>
      <FlatList
        data={results}
        keyExtractor={(item, i) => `${item.platform}-${item.pincode}-${item.product}-${i}`}
        renderItem={({ item }) => <ResultRow row={item} />}
        ListHeaderComponent={header}
        contentContainerStyle={styles.list}
        keyboardShouldPersistTaps="handled"
        ListEmptyComponent={
          !running && !meta ? (
            <Text style={styles.empty}>
              Pick a platform, enter products, choose a city, and run a check.
            </Text>
          ) : null
        }
      />
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  safe: { flex: 1, backgroundColor: colors.bg },
  list: { padding: 16, paddingBottom: 40 },
  headerRow: {
    flexDirection: "row",
    justifyContent: "space-between",
    alignItems: "center",
    marginBottom: 16,
    marginTop: 8,
  },
  hi: { color: colors.textMuted, fontSize: 12 },
  username: { color: colors.text, fontSize: 18, fontWeight: "800" },
  logout: { color: colors.info, fontSize: 14, fontWeight: "700" },
  section: { color: colors.textMuted, fontSize: 13, fontWeight: "700", marginTop: 12, marginBottom: 8 },
  chipWrap: { flexDirection: "row", flexWrap: "wrap" },
  input: {
    backgroundColor: colors.bg,
    borderWidth: 1,
    borderColor: colors.border,
    borderRadius: 12,
    color: colors.text,
    paddingHorizontal: 14,
    paddingVertical: 12,
    fontSize: 16,
  },
  hint: { color: colors.textMuted, fontSize: 12, marginTop: 6 },
  err: { color: colors.danger, fontSize: 13, marginTop: 12 },
  progressWrap: { marginBottom: 14 },
  progressBarBg: {
    height: 6,
    borderRadius: 3,
    backgroundColor: colors.surfaceAlt,
    overflow: "hidden",
  },
  progressBar: { height: 6, backgroundColor: colors.primary },
  progressText: { color: colors.textMuted, fontSize: 12, marginTop: 6 },
  empty: { color: colors.textMuted, fontSize: 14, textAlign: "center", marginTop: 40, paddingHorizontal: 20 },
});

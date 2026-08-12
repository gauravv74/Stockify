// Shared design tokens for the Stockly mobile app.
export const colors = {
  bg: "#0f172a",
  surface: "#1e293b",
  surfaceAlt: "#334155",
  border: "#334155",
  text: "#f1f5f9",
  textMuted: "#94a3b8",
  primary: "#22c55e",
  primaryDark: "#16a34a",
  danger: "#ef4444",
  warning: "#f59e0b",
  info: "#38bdf8",
  chip: "#334155",
  chipActive: "#22c55e",
};

// Maps a check status to a color + friendly label.
export const STATUS_META = {
  available: { color: "#22c55e", label: "Available" },
  out_of_stock: { color: "#f59e0b", label: "Out of stock" },
  not_serviceable: { color: "#94a3b8", label: "Not serviceable" },
  not_found: { color: "#94a3b8", label: "Not found" },
  geocode_failed: { color: "#ef4444", label: "Geocode failed" },
  error: { color: "#ef4444", label: "Error" },
};

export function statusMeta(status) {
  if (!status) return { color: colors.textMuted, label: "—" };
  if (STATUS_META[status]) return STATUS_META[status];
  if (status.startsWith("error")) return { color: colors.danger, label: "Error" };
  return { color: colors.textMuted, label: status };
}

export const PLATFORM_LABELS = {
  blinkit: "Blinkit",
  instamart: "Instamart",
  zepto: "Zepto",
  bigbasket: "BigBasket",
  flipkart: "Flipkart Minutes",
  flipkart_com: "Flipkart",
  amazon: "Amazon",
  jiomart: "JioMart",
  apple: "Apple",
  croma: "Croma",
  all: "All",
};

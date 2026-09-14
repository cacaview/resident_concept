// Cognitive route → visual identity. One distinct low-saturation hue per route.

export interface RouteMeta {
  color: string;
  label: string;
}

export const ROUTE_META: Record<string, RouteMeta> = {
  continuity: { color: "#6f9fd8", label: "延续" },
  revisit: { color: "#cfa468", label: "重访" },
  distant: { color: "#a48fd8", label: "远距" },
  serendipity: { color: "#6fc9a8", label: "偶遇" },
  self: { color: "#d888a8", label: "自省" },
  personal: { color: "#d89a6f", label: "私人" },
  rest: { color: "#8b93a3", label: "安歇" },
  // Routes that may appear beyond the v0.1 core set:
  world: { color: "#74c2c9", label: "世界" },
};

export const ROUTE_FALLBACK: RouteMeta = { color: "#9aa3b2", label: "未知" };

export function routeMeta(route: string | null | undefined): RouteMeta {
  if (!route) return ROUTE_FALLBACK;
  return ROUTE_META[route] ?? ROUTE_FALLBACK;
}

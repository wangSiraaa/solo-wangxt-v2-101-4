const BASE = "/api";

async function request(path, options = {}) {
  const resp = await fetch(`${BASE}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const text = await resp.text();
  const data = text ? JSON.parse(text) : null;
  if (!resp.ok) {
    const detail =
      typeof data === "object" && data
        ? Object.entries(data)
            .map(([k, v]) => `${k}: ${[].concat(v).join("；")}`)
            .join("｜")
        : String(data);
    throw new Error(detail || `HTTP ${resp.status}`);
  }
  return data;
}

// includeLineage=true → 「包含前身/后继」视图；缺省 → 「仅当前刊名」
const lineageParam = (includeLineage) =>
  includeLineage ? "&include_lineage=1" : "";

export const api = {
  listTitles: (search = "", includeLineage = false) =>
    request(
      `/titles/?${search ? `search=${encodeURIComponent(search)}` : ""}` +
        (includeLineage ? `&include_lineage=1` : ""),
    ),
  createTitle: (payload) =>
    request("/titles/", { method: "POST", body: JSON.stringify(payload) }),
  updateTitle: (id, payload) =>
    request(`/titles/${id}/`, {
      method: "PATCH",
      body: JSON.stringify(payload),
    }),

  timeline: (titleId, includeLineage = false) =>
    request(`/timeline/?title=${titleId}${lineageParam(includeLineage)}`),
  listNumbers: (titleId) => request(`/numbers/?title=${titleId}`),

  createNumber: (payload) =>
    request("/numbers/", { method: "POST", body: JSON.stringify(payload) }),
  createIssue: (payload) =>
    request("/issues/", { method: "POST", body: JSON.stringify(payload) }),
  createItem: (payload) =>
    request("/items/", { method: "POST", body: JSON.stringify(payload) }),
  setItemStatus: (id, status) =>
    request(`/items/${id}/`, {
      method: "PATCH",
      body: JSON.stringify({ status }),
    }),

  locate: (params, includeLineage = false) => {
    const qs = new URLSearchParams(
      Object.entries(params).filter(([, v]) => v !== "" && v != null),
    ).toString();
    return request(
      `/items/locate/?${qs}${lineageParam(includeLineage)}`,
    );
  },

  listBindings: (titleId) =>
    request(titleId ? `/bindings/?title=${titleId}` : "/bindings/"),
  bind: (payload) =>
    request("/bindings/", { method: "POST", body: JSON.stringify(payload) }),
  unbind: (bindingId) =>
    request("/bindings/unbind/", {
      method: "POST",
      body: JSON.stringify({ binding_id: bindingId }),
    }),

  // 刊名沿革（版本化关系 + 审计）
  listSuccessions: (titleId, active = "") => {
    const qs = new URLSearchParams();
    if (titleId) qs.set("title", titleId);
    if (active !== "") qs.set("active", active ? "1" : "0");
    const tail = qs.toString();
    return request(`/successions/${tail ? `?${tail}` : ""}`);
  },
  createSuccession: (payload) =>
    request("/successions/", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  revokeSuccession: (successionId, reason = "") =>
    request("/successions/revoke/", {
      method: "POST",
      body: JSON.stringify({ succession: successionId, reason }),
    }),
  successionAudit: (titleId) =>
    request(titleId ? `/successions/audit/?title=${titleId}` : "/successions/audit/"),
  replaySuccessions: (titleId) =>
    request(titleId ? `/successions/replay/?title=${titleId}` : "/successions/replay/"),
};

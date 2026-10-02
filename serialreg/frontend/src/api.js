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

const monthParam = (m) => (m ? (m.length === 7 ? `${m}-01` : m) : null);

export const api = {
  listTitles: (q = "", lineage = false) =>
    request(
      `/titles/?${new URLSearchParams(
        Object.entries({ q, lineage: lineage ? 1 : "" }).filter(
          ([, v]) => v !== "",
        ),
      )}`,
    ),
  titleLineage: (id) => request(`/titles/${id}/lineage/`),
  createTitle: (payload) =>
    request("/titles/", { method: "POST", body: JSON.stringify(payload) }),
  updateTitle: (id, payload) =>
    request(`/titles/${id}/`, {
      method: "PATCH",
      body: JSON.stringify(payload),
    }),

  timeline: (titleId, lineage = false) =>
    request(`/timeline/?title=${titleId}${lineage ? "&lineage=1" : ""}`),
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

  locate: (params) => {
    const qs = new URLSearchParams(
      Object.entries(params).filter(([, v]) => v !== "" && v != null),
    ).toString();
    return request(`/items/locate/?${qs}`);
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

  // ---- 刊名沿革（版本化关系 + 审计）----
  listSuccessions: (titleId, state = "") =>
    request(
      `/successions/?${new URLSearchParams(
        Object.entries({ title: titleId ?? "", state }).filter(
          ([, v]) => v !== "",
        ),
      )}`,
    ),
  createSuccession: ({ predecessor, successor, effective_month, detail }) =>
    request("/successions/", {
      method: "POST",
      body: JSON.stringify({
        predecessor,
        successor,
        effective_month: monthParam(effective_month),
        detail: detail || "",
      }),
    }),
  correctSuccession: (id, payload) =>
    request(`/successions/${id}/correct/`, {
      method: "POST",
      body: JSON.stringify({
        successor: payload.successor,
        effective_month: payload.effective_month
          ? monthParam(payload.effective_month)
          : undefined,
        detail: payload.detail || "",
      }),
    }),
  revokeSuccession: (id, reason) =>
    request(`/successions/${id}/revoke/`, {
      method: "POST",
      body: JSON.stringify({ reason: reason || "" }),
    }),
  successionAudits: (titleId, replay = false) =>
    request(
      `/successions/audits/?${new URLSearchParams(
        Object.entries({
          title: titleId ?? "",
          replay: replay ? 1 : "",
        }).filter(([, v]) => v !== ""),
      )}`,
    ),
};

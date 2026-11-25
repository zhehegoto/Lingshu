// 模型目录：自动扫描 models/ 后由后端返回 chat / draw 两组候选。
// 页面只负责渲染，不再假定“只有一条固定模型”。
const $ = (id) => document.getElementById(id);

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

export function shortLabel(label) {
  const text = String(label || "").replace(/\s+/g, " ").trim();
  if (!text) return "未选择模型";
  const base = text.split("·")[0].trim();
  const parts = base.split("-").filter(Boolean);
  return parts.slice(0, 3).join("-") || base;
}

export async function loadCatalog() {
  const resp = await fetch("/api/models", { cache: "no-store" });
  if (!resp.ok) throw new Error(`models ${resp.status}`);
  return resp.json();
}

export async function chooseModel(role, path) {
  const resp = await fetch("/api/models/select", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ role, path }),
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    const err = new Error(data.detail || data.error || `HTTP ${resp.status}`);
    err.code = data.error || "switch_failed";
    err.status = resp.status;
    throw err;
  }
  return data;
}

function sizeText(mib) {
  const n = Number(mib);
  if (!n) return "";
  return n >= 1024 ? `${(n / 1024).toFixed(1)} GB` : `${Math.round(n)} MB`;
}

// emptyText：目录里一个模型都没有时显示的话。这里要写"接下来该做什么"，
// 而不是"未找到"——第一次打开软件的人看到的应该是引导。
export function fillSelect(select, items, selectedPath, emptyText) {
  if (!select) return;
  const list = Array.isArray(items) ? items : [];
  if (!list.length) {
    select.innerHTML = `<option value="">${esc(emptyText || "models 目录里还没有模型")}</option>`;
    return;
  }
  const current = selectedPath || select.value;
  select.innerHTML = list.map((m) => {
    const picked = m.path === current || m.key === current;
    const size = sizeText(m.size_mib);
    // 下拉框宽度有限，全名会被 ellipsis 吃掉、连带把体积也吃掉；
    // 这里用短名 + 体积，完整名字留在 title 里悬停可看。
    const name = shortLabel(m.label);
    return `<option value="${esc(m.path)}"${picked ? " selected" : ""} title="${esc(m.label)}（${esc(m.path)}）">${esc(name)}${size ? ` · ${esc(size)}` : ""}</option>`;
  }).join("");
  if (!list.some((m) => m.path === current || m.key === current) && list[0]) {
    select.value = list[0].path;
  }
}

export function selectedLabel(items, path) {
  const hit = (items || []).find((m) => m.path === path);
  return hit?.label || "";
}

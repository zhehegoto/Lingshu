// Agent 页：极简输入 → 用户任务 → 计划/执行/答复。
// 工作目录与权限收进输入框上方的小菜单，不再占一整张卡片。
import { api, human } from "./api.js?v=36";
import { loadCatalog, chooseModel, shortLabel } from "./model-picker.js?v=2";

const $ = (id) => document.getElementById(id);
let busy = false;
let rootDir = "";
let scope = "root";
let agentChatItems = [];
let agentModelPath = "";

function pill(text, cls = "") {
  const p = $("agent-pill");
  p.textContent = text;
  p.className = `pill ${cls}`;
}

function bubble(role, text = "") {
  const wrap = document.createElement("div");
  wrap.className = `msg ${role}`;
  const b = document.createElement("div");
  b.className = "bubble";
  b.textContent = text;
  wrap.appendChild(b);
  $("agent-messages").appendChild(wrap);
  $("agent-messages").scrollTop = $("agent-messages").scrollHeight;
  return b;
}

function renderPlan(steps) {
  const box = $("agent-plan");
  box.innerHTML = "";
  if (!steps?.length) {
    box.innerHTML = `<div class="empty small">输入任务后自动生成计划</div>`;
    return;
  }
  steps.forEach((s, i) => {
    const row = document.createElement("div");
    row.className = "plan-step";
    row.innerHTML = `<b>${i + 1}</b><span>${s}</span>`;
    box.appendChild(row);
  });
}

function markPlanDone() {
  [...$("agent-plan").querySelectorAll(".plan-step")].forEach((r) => r.classList.add("done"));
}

function renderTrace(trace) {
  const box = $("agent-trace");
  if (!trace?.length) {
    box.innerHTML = `<div class="empty small">还没有执行记录</div>`;
    return;
  }
  box.innerHTML = "";
  for (const t of trace) {
    const row = document.createElement("div");
    row.className = `trace-row ${t.ok ? "ok" : "err"}`;
    const args = JSON.stringify(t.args || {});
    row.innerHTML = `<b>${t.tool}</b> · ${t.ok ? "成功" : "失败"}<br><span class="muted">${args.length > 90 ? args.slice(0, 90) + "…" : args}</span><br>${t.detail || ""}`;
    box.appendChild(row);
  }
}

function setBusy(v) {
  busy = v;
  $("agent-send").disabled = v;
  $("agent-input").disabled = v;
  $("agent-scope").disabled = v;
  $("agent-perm").disabled = v;
  $("agent-model-label").disabled = v;
  if (v) {
    $("agent-scope-menu").hidden = true;
    $("agent-model-menu").hidden = true;
    $("agent-scope").setAttribute("aria-expanded", "false");
    $("agent-model-label").setAttribute("aria-expanded", "false");
  }
}

function showEmpty() {
  $("agent-thread").hidden = true;
  $("agent-empty").hidden = false;
}

function showThread() {
  $("agent-empty").hidden = true;
  $("agent-thread").hidden = false;
}

function resetAgent() {
  if (busy) return;
  $("agent-messages").innerHTML = "";
  renderPlan([]);
  renderTrace([]);
  $("agent-details").hidden = true;
  $("agent-details").open = false;
  $("agent-hint").textContent = "";
  $("agent-input").value = "";
  showEmpty();
  pill("待命");
}

function setScope(kind) {
  const perm = $("agent-perm");
  if (kind === "all") {
    scope = "all";
    $("agent-workdir").value = "C:/";
    $("agent-scope-text").textContent = "整台电脑";
    perm.value = "full";
  } else if (kind === "root") {
    scope = "root";
    $("agent-workdir").value = rootDir || "C:/";
    $("agent-scope-text").textContent = "默认工作区";
    perm.value = "workspace";
  }
  $("agent-scope-menu").hidden = true;
  $("agent-scope").setAttribute("aria-expanded", "false");
}

function paintAgentModel(label) {
  $("agent-model-label").textContent = shortLabel(label || "对话模型");
}

function renderAgentModelOptions(data) {
  agentChatItems = Array.isArray(data.chat) ? data.chat : [];
  agentModelPath = data.selected?.chat?.path || "";
  const list = $("agent-model-options");
  list.innerHTML = "";
  if (!agentChatItems.length) {
    list.innerHTML = `<div class="empty">models 目录里没有识别到对话模型</div>`;
    return;
  }
  for (const item of agentChatItems) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.dataset.path = item.path;
    if (item.path === agentModelPath) {
      btn.classList.add("on");
      btn.setAttribute("aria-current", "true");
    }
    const check = document.createElement("span");
    check.className = "check";
    check.textContent = item.path === agentModelPath ? "✓" : "";
    const label = document.createElement("span");
    label.textContent = item.label;
    btn.append(check, label);
    list.appendChild(btn);
  }
}

async function openAgentModelMenu() {
  if (busy) return;
  const menu = $("agent-model-menu");
  const open = menu.hidden;
  $("agent-scope-menu").hidden = true;
  $("agent-scope").setAttribute("aria-expanded", "false");
  menu.hidden = !open;
  $("agent-model-label").setAttribute("aria-expanded", open ? "true" : "false");
  if (!open) return;
  try {
    renderAgentModelOptions(await loadCatalog());
  } catch (e) {
    $("agent-hint").textContent = human(e);
  }
}

async function pickAgentModel(path) {
  const item = agentChatItems.find((m) => m.path === path);
  $("agent-model-menu").hidden = true;
  $("agent-model-label").setAttribute("aria-expanded", "false");
  if (!item || busy || path === agentModelPath) return;
  const prev = agentChatItems.find((m) => m.path === agentModelPath);
  $("agent-model-label").textContent = "切换中…";
  try {
    const r = await chooseModel("chat", path);
    agentModelPath = path;
    paintAgentModel(item.label);
    $("agent-hint").textContent = r.restarted
      ? "对话模型已切换，服务已重启。"
      : "对话模型已切换，下次启动对话服务时生效。";
    window.dispatchEvent(new CustomEvent("lingshu:model"));
  } catch (e) {
    paintAgentModel(prev?.label || "");
    $("agent-hint").textContent = human(e);
  }
}

async function chooseCustomFolder() {
  $("agent-scope-menu").hidden = true;
  $("agent-scope").setAttribute("aria-expanded", "false");
  try {
    const resp = await fetch("/api/system/pick-folder", { method: "POST" });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data?.error || `HTTP ${resp.status}`);
    if (data.canceled || !data.path) return;
    scope = "custom";
    $("agent-workdir").value = data.path;
    $("agent-perm").value = "full";
    $("agent-scope-text").textContent = data.path.split(/[\\/]/).filter(Boolean).pop() || "自定义目录";
  } catch (e) {
    $("agent-hint").textContent = human(e);
  }
}

async function runTask(text) {
  if (busy || !text.trim()) return;
  const workdir = $("agent-workdir").value.trim();
  const permission = $("agent-perm").value;
  showThread();
  bubble("user", text);
  $("agent-input").value = "";
  setBusy(true);
  $("agent-details").hidden = false;
  renderTrace([]);
  pill("规划中", "warn");
  $("agent-hint").textContent = "";
  try {
    const plan = await api.agentPlan(text, workdir);
    renderPlan(plan.plan);
    pill("执行中", "warn");
    $("agent-hint").textContent = `权限：${{ view: "仅限查看", workspace: "工作区内修改", full: "完全权限" }[permission]} · 目录：${workdir}`;
    const r = await api.agentRun(text, workdir, permission);
    renderTrace(r.trace || []);
    bubble("assistant", r.answer || "（没有返回正文）");
    markPlanDone();
    pill("完成", "ok");
    $("agent-hint").textContent = `执行完成：${r.trace?.length || 0} 次工具调用`;
  } catch (e) {
    pill("出错", "err");
    $("agent-hint").textContent = human(e);
    if (e?.code === "chat_stopped") {
      $("agent-hint").textContent = "对话模型已关闭：去「运行」页启动后再试";
    }
  } finally {
    setBusy(false);
  }
}

export function initAgent() {
  $("agent-form").addEventListener("submit", (e) => {
    e.preventDefault();
    runTask($("agent-input").value);
  });
  $("agent-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      runTask($("agent-input").value);
    }
  });
  $("agent-scope").addEventListener("click", (e) => {
    e.stopPropagation();
    $("agent-model-menu").hidden = true;
    $("agent-model-label").setAttribute("aria-expanded", "false");
    const menu = $("agent-scope-menu");
    menu.hidden = !menu.hidden;
    $("agent-scope").setAttribute("aria-expanded", menu.hidden ? "false" : "true");
  });
  $("agent-scope-menu").addEventListener("click", (e) => {
    e.stopPropagation();
    const b = e.target.closest("button[data-scope]");
    if (!b) return;
    if (b.dataset.scope === "custom") {
      chooseCustomFolder();
      return;
    }
    setScope(b.dataset.scope);
  });
  $("agent-model-label").addEventListener("click", (e) => {
    e.stopPropagation();
    openAgentModelMenu();
  });
  $("agent-model-options").addEventListener("click", (e) => {
    e.stopPropagation();
    const b = e.target.closest("button[data-path]");
    if (b) pickAgentModel(b.dataset.path);
  });
  document.addEventListener("click", () => {
    $("agent-scope-menu").hidden = true;
    $("agent-model-menu").hidden = true;
    $("agent-scope").setAttribute("aria-expanded", "false");
    $("agent-model-label").setAttribute("aria-expanded", "false");
  });
  $("agent-clear").addEventListener("click", resetAgent);
  $("agent-new").addEventListener("click", () => {
    if (!busy) resetAgent();
    $("agent-input").focus();
  });

  api.overview().then((o) => {
    rootDir = o?.app?.dir || "";
    if (!$("agent-workdir").value.trim()) $("agent-workdir").value = rootDir;
  }).catch(() => {});

  loadCatalog().then((data) => {
    agentChatItems = Array.isArray(data.chat) ? data.chat : [];
    agentModelPath = data.selected?.chat?.path || "";
    paintAgentModel(data.selected?.chat?.label || "");
  }).catch(() => {});

  showEmpty();
  pill("待命");
}

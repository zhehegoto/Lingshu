// 运行：本机硬件（自动识别）+ 自动参数 + 模型启动 + 显存/队列监测 + 日志
// 2026-11 改版：概览、服务开关、监测三块合并到这一页；模型默认关闭。
import { api, human } from "./api.js?v=36";
import { loadCatalog, chooseModel, fillSelect } from "./model-picker.js?v=2";
import { initModelDrop } from "./model-import.js?v=1";

const $ = (id) => document.getElementById(id);
let lastSeq = 0;
let logTimer = null;
let monTimer = null;
let logOpen = true;
let status = null;
let hw = null;
let modelsLoaded = false;
let modelCatalog = null;

function pct(used, total) {
  if (!total) return 0;
  return Math.max(0, Math.min(100, (used / total) * 100));
}

function shortCpu(name) {
  const m = String(name || "").match(/i[3579][- ]?\d{4,5}[A-Z]{0,3}/i);
  return m ? m[0].replace(/\s/g, "") : String(name || "CPU").replace(/\(R\)|\(TM\)/g, "").trim();
}

function hwCard(k, v, s = "") {
  return `<div class="ov-card"><div class="k">${k}</div><div class="v" title="${v}">${v}</div>${s ? `<div class="s">${s}</div>` : ""}</div>`;
}

function renderModelSelects(force = false) {
  if (modelsLoaded && !force) return Promise.resolve();
  return loadCatalog().then((data) => {
    modelCatalog = data;
    fillSelect($("run-chat-model"), data.chat || [], data.selected?.chat?.path,
      "还没有对话模型——把 .gguf 拖到下面的框里");
    fillSelect($("run-sd-model"), data.draw || [], data.selected?.draw?.path,
      "还没有画图模型——把 qwen-image 的 .gguf 拖进来");
    modelsLoaded = true;
    return data;
  }).catch(() => {});
}

async function switchModel(role) {
  const sel = $(role === "chat" ? "run-chat-model" : "run-sd-model");
  const note = $(role === "chat" ? "run-model-note" : "run-sd-model-note");
  if (!sel) return;
  note.textContent = "正在切换模型，服务若已启动会自动重启…";
  try {
    const r = await chooseModel(role, sel.value);
    await renderModelSelects(true);
    window.dispatchEvent(new CustomEvent("lingshu:model"));
    const data = modelCatalog || {};
    const current = role === "chat" ? data.selected?.chat : data.selected?.draw;
    note.textContent = r.restarted
      ? `已切换：${current?.label || ""}；服务已重启`
      : `已选择：${current?.label || ""}；启动服务时生效`;
  } catch (e) {
    note.textContent = human(e);
    await renderModelSelects(true);
  }
}

function renderHardware() {
  if (!hw) return;
  const h = hw.hardware || {};
  const gpu = h.gpu || {};
  const cpu = h.cpu || {};
  const ram = h.ram || {};
  const disk = h.disk || {};
  const chat = h.chat || h;
  const sd = h.sd || h;
  const guard = h.guard || h;
  const live = hw.vram || {};
  const free = live.gpu_free_mib ?? gpu.free_mib ?? 0;
  const vramTotal = gpu.vram_mib || 0;
  const ramTotal = ram.total_mib || 0;
  const ramAvail = live.ram_avail_mib ?? ram.avail_mib ?? 0;

  $("ov-sub").textContent = `本机 · 探测于 ${new Date().toLocaleString("zh-CN", { hour12: false })}`;
  $("ov-title").textContent = "灵枢";
  $("ov-desc").textContent = gpu
    ? `${gpu.name} · ${cpu.physical || "?"} 核 ${cpu.logical || "?"} 线程 · ${Math.round((ram.total_mib || 0) / 1024 * 10) / 10} GB 内存`
    : "未检测到 NVIDIA 显卡：纯 CPU 会明显变慢，参数已按最保守档位选。";

  const chips = [];
  for (const key of ["chat", "sd"]) {
    const s = hw.services?.[key];
    if (!s) continue;
    const cls = s.ready ? "ok" : (s.loading ? "warn" : "");
    const text = s.ready ? "就绪" : (s.loading ? "启动中" : "已关闭");
    chips.push(`<span class="pill ${cls}"><span class="dot ${s.ready ? "ok" : (s.loading ? "busy" : "")}"></span>${s.name} · ${text} · ${s.port}</span>`);
  }
  $("ov-service-chips").innerHTML = chips.join("");

  $("ov-ctx").textContent = chat.ctx ?? "—";
  $("ov-steps").textContent = sd.steps ?? "—";
  $("ov-tile").textContent = sd.vae_tile ?? "—";

  // 「启动全部模型」只在显存装得下两条权重链时出现。
  // 硬点它只会让出图模型在加载途中被护栏杀掉，用户看到的却是"点了没反应"。
  const allBtn = $("run-start-all");
  const both = (hw.summary && hw.summary.both) || {};
  if (allBtn) {
    const fits = both.fits !== false;
    const hasModels = !!both.vram_mib;
    allBtn.hidden = hasModels ? !fits : false;
    allBtn.title = fits
      ? ""
      : `显存不够同时装下对话与画图（约需 ${Math.round((both.need_mib || 0) / 1024 * 10) / 10} GB / 共 ${Math.round((both.vram_mib || 0) / 1024 * 10) / 10} GB），请分别启动`;
  }

  $("ov-hardware").innerHTML = [
    hwCard("显卡", gpu.name ? gpu.name.replace("NVIDIA ", "") : "无 NVIDIA 显卡",
      gpu ? `驱动 ${gpu.driver || "?"} · 显存 ${Math.round(gpu.vram_mib / 1024 * 10) / 10} GB · 空闲 ${Math.round((live.gpu_free_mib ?? gpu.free_mib ?? 0) / 1024 * 10) / 10} GB` : "纯 CPU 模式"),
    hwCard("CPU", shortCpu(cpu.name), `${cpu.physical || "?"} 核 / ${cpu.logical || "?"} 线程`),
    hwCard("内存", `${Math.round((ram.total_mib || 0) / 1024 * 10) / 10} GB`, `可用 ${Math.round((live.ram_avail_mib ?? ram.avail_mib ?? 0) / 1024 * 10) / 10} GB`),
    hwCard("磁盘", `${disk.free_gib ?? "?"} GB 可用`, `共 ${disk.total_gib ?? "?"} GB`),
  ].join("");

  const params = [
    ["对话", `-ngl ${chat.ngl ?? "?"} · 上下文 ${chat.ctx ?? "?"}`],
    ["速度", `线程 ${chat.threads ?? "?"} · 批量 ${chat.batch ?? "?"}/${chat.ubatch ?? "?"}`],
    ["画图", `VAE tile ${sd.vae_tile ?? "?"} · ${sd.steps ?? "?"} 步`],
    ["护栏", `预算 ${guard.budget_mib ?? "?"} MiB · 护栏 ${guard.guard_mib ?? "?"} MiB`],
  ];
  $("ov-params").innerHTML = params.map(([k, v]) => `<div class="kv"><span>${k}</span><b>${v}</b></div>`).join("");
  $("ov-fit").textContent = chat.fits ? "整块进显存" : "分层/CPU 兜底";
  $("ov-fit").className = `pill ${chat.fits ? "ok" : "warn"}`;
  $("ov-why").textContent = (h.notes && h.notes[0]) || "参数由硬件探测自动决定。";

  renderModelSelects();
  $("run-fa").textContent = chat.flash_attn ? "开" : "关";
  $("run-kv").textContent = "f16 · 锁定";
  $("run-threads").textContent = `${chat.threads ?? "?"} 线程 · ${chat.batch ?? "?"}/${chat.ubatch ?? "?"}`;
  $("run-guard").textContent = `${guard.guard_mib ?? "?"} / ${guard.budget_mib ?? "?"} MiB`;
  $("run-think-budget").textContent = `${chat.ctx ?? 4096} tokens 上下文`;

  syncCtxChips();
  $("run-thinking").checked = !!hw.settings?.thinking;

  // 显存实时空闲单独刷一次，避免只有概览时能看到"空闲"而运行页过期
  $("run-gpu-live")?.replaceChildren();
}

function syncCtxChips() {
  const ctx = hw?.settings?.n_ctx || 4096;
  [...$("run-ctx-chips").children].forEach((b) => {
    b.classList.toggle("active", b.dataset.ctx !== "auto" && Number(b.dataset.ctx) === ctx);
  });
  const est = hw?.estimate;
  if (est) {
    $("run-ctx-note").textContent =
      `按 ${est.ctx} 估算：KV ${est.kv_mib} MB，模型 + KV + 余量 = ${est.total_mib} MB（护栏 ${est.guard_mib} MB），${est.fits_guard ? "装得下" : "会超护栏"}`;
    $("run-ctx-note").classList.toggle("err", !est.fits_guard);
  }
}

function renderState(st) {
  if (!st) return;
  const chatReady = !!st.ready?.chat;
  const sdReady = !!st.ready?.sd;
  const busy = ["PREPARING", "GENERATING", "RELOADING"].includes(st.state);
  $("run-state").textContent = busy ? (st.state === "GENERATING" ? "出图中" : "运行中") : (chatReady || sdReady ? "已启动" : "待机");
  $("run-state").className = `pill ${chatReady || sdReady ? "ok" : "warn"}`;
  $("run-ready").textContent = chatReady ? "就绪" : (st.services?.chat?.want ? "启动中" : "已关闭");
  $("run-ready").className = `pill ${chatReady ? "ok" : "warn"}`;
}

async function refresh() {
  try {
    hw = await api.overview();
    renderHardware();
    status = await api.status();
    renderState(status);
  } catch (e) {
    $("ov-sub").textContent = human(e);
  }
}

function paintLog(items) {
  const box = $("run-log");
  if (!box || !logOpen) return;
  const stick = box.scrollHeight - box.scrollTop - box.clientHeight < 24;
  for (const it of items) {
    const line = document.createElement("div");
    line.className = `lv-${it.level}`;
    line.textContent = `${it.time}  ${it.msg}`;
    box.appendChild(line);
  }
  while (box.childElementCount > 400) box.removeChild(box.firstElementChild);
  if (stick) box.scrollTop = box.scrollHeight;
  $("run-log-meta").textContent = `实时 · ${box.childElementCount} 条`;
}

function setLogOpen(start = true) {
  logOpen = true;
  const card = $("run-log-card");
  const view = $("run-log");
  const lower = $("run-lower");
  if (!card || !view || !lower) return;
  view.hidden = false;
  card.classList.add("open");
  lower.classList.add("log-open");
  $("run-log-meta").textContent = "实时 · 0 条";
  clearInterval(logTimer);
  logTimer = null;
  lastSeq = 0;
  view.innerHTML = "";
  if (!start) return;
  pollLogs();
  logTimer = setInterval(pollLogs, 1500);
}

async function pollLogs() {
  try {
    const r = await api.logs(lastSeq, 200);
    if (r.items?.length) paintLog(r.items);
    lastSeq = r.last ?? lastSeq;
  } catch { /* 轮询失败下次再取 */ }
}

async function saveCtx(ctx) {
  if (!ctx || ctx === "auto") return;
  try {
    $("run-ctx-note").textContent = "正在保存并重启对话服务（约 20s）…";
    const r = await api.saveSettings({ n_ctx: Number(ctx) });
    if (r.restart_chat_now) $("run-ctx-note").textContent = "上下文已保存，对话服务正在重启（约 20s）";
    else $("run-ctx-note").textContent = "上下文已保存，下次启动对话模型时生效";
    await refresh();
  } catch (e) {
    $("run-ctx-note").textContent = human(e);
  }
}

// —— 合并进来的监测 ——
function drawChart(samples, total) {
  const c = $("vram-chart");
  if (!c) return;
  const dpr = window.devicePixelRatio || 1;
  const w = c.clientWidth || 500;
  const h = 150;
  c.width = Math.round(w * dpr);
  c.height = Math.round(h * dpr);
  const ctx = c.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  const isDark = document.documentElement.dataset.theme === "dark";
  const accent = getComputedStyle(document.documentElement).getPropertyValue("--accent").trim() || "#0e9f85";
  ctx.clearRect(0, 0, w, h);
  ctx.fillStyle = isDark ? "#070d11" : "#fbfdfd";
  ctx.fillRect(0, 0, w, h);
  const pad = 8;
  const usable = h - pad * 2;
  const max = total || 8188;
  ctx.strokeStyle = isDark ? "#20343d" : "#d9e4e6";
  ctx.setLineDash([4, 4]);
  for (const frac of [0.5, 0.8]) {
    const y = pad + usable * (1 - frac);
    ctx.beginPath();
    ctx.moveTo(pad, y);
    ctx.lineTo(w - pad, y);
    ctx.stroke();
  }
  ctx.setLineDash([]);
  if (!samples.length) {
    ctx.fillStyle = isDark ? "#7f959e" : "#6f858c";
    ctx.font = "12px Microsoft YaHei";
    ctx.fillText("还没有采样", pad + 6, h / 2);
    return;
  }
  ctx.beginPath();
  samples.forEach((s, i) => {
    const x = pad + (i / Math.max(1, samples.length - 1)) * (w - pad * 2);
    const y = pad + usable * (1 - Math.min(1, s.used / max));
    if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
  });
  ctx.strokeStyle = accent;
  ctx.lineWidth = 2;
  ctx.stroke();
  const last = samples[samples.length - 1];
  ctx.fillStyle = isDark ? "#b9cbd0" : "#5b7279";
  ctx.font = "12px Microsoft YaHei";
  ctx.fillText(`${last.used} / ${max} MiB`, w - 120, 18);
}

function renderServices(st) {
  const box = $("mon-services");
  if (!box) return;
  const rows = [];
  for (const key of ["chat", "sd"]) {
    const s = st?.services?.[key];
    if (!s) continue;
    const state = s.ready ? "就绪" : (s.loading ? "启动中" : "已关闭");
    rows.push(`<div class="info-row"><span><span class="dot ${s.ready ? "ok" : (s.loading ? "busy" : "")}"></span> ${s.name} · ${s.port}</span><b>${state}${s.pid ? ` · pid ${s.pid}` : ""}</b></div>`);
  }
  box.innerHTML = rows.join("") || `<div class="muted small">没有服务信息</div>`;
  const q = st?.queue || {};
  $("mon-queue").textContent = `队列 ${q.depth ?? 0} / ${q.max ?? 4}` + (q.running ? ` · 正在跑 ${q.running}` : "");
}

function renderJobs(items) {
  const box = $("mon-jobs");
  if (!box) return;
  if (!items?.length) {
    box.innerHTML = `<div class="empty small">还没有作业</div>`;
    return;
  }
  box.innerHTML = "";
  for (const j of items.slice(0, 10)) {
    const row = document.createElement("div");
    row.className = "job-row";
    const t = new Date((j.created_at || 0) * 1000).toLocaleTimeString("zh-CN", { hour12: false });
    row.innerHTML = `<span class="muted small">${t}</span><b>${j.width}×${j.height}</b><span>${(j.prompt || "").slice(0, 46)}${(j.prompt || "").length > 46 ? "…" : ""}</span><span class="status">${j.status}${j.error ? ` · ${j.error}` : ""}</span>`;
    box.appendChild(row);
  }
}

async function refreshMonitor() {
  try {
    const st = await api.status();
    status = st;
    renderServices(st);
    const v = await api.vram(300);
    drawChart(v.samples || [], v.total_mib);
    $("mon-vram-now").textContent = v.last ? `当前 ${v.last.used} MiB · ${Math.round(v.last.used / (v.total_mib || 8188) * 100)}%` : "无采样";
  } catch (e) {
    $("mon-vram-note").textContent = human(e);
  }
}

function escText(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function gbText(mib) {
  const n = Number(mib) || 0;
  return n >= 1024 ? `${(n / 1024).toFixed(1)} GB` : `${n} MB`;
}

function closeModelManager() {
  $("model-mgr-modal").hidden = true;
  $("model-mgr-scrim").hidden = true;
}

async function openModelManager() {
  const modal = $("model-mgr-modal");
  const body = $("model-mgr-body");
  if (!modal || !body) return;
  $("model-mgr-scrim").hidden = false;
  modal.hidden = false;
  body.innerHTML = `<div class="muted small">读取中…</div>`;
  try {
    renderModelManager(await loadCatalog());
  } catch (e) {
    body.innerHTML = `<div class="muted small err">${escText(human(e))}</div>`;
  }
}

function renderModelManager(data) {
  const body = $("model-mgr-body");
  const files = (data && data.files) || [];
  if (!files.length) {
    body.innerHTML = `<div class="muted small">models 目录还是空的：把 .gguf / .safetensors 拖到运行页的拖入框就行。</div>`;
    return;
  }
  const total = files.reduce((s, f) => s + (f.size_mib || 0), 0);
  body.innerHTML = `<div class="muted small">共 ${files.length} 个文件 · ${gbText(total)}；磁盘上占的就是这些</div>`
    + files.map((f) => `<div class="model-mgr-row">
      <div class="model-mgr-main">
        <b title="${escText(f.path)}">${escText(f.name)}</b>
        <span class="muted small">${gbText(f.size_mib)}${f.in_use ? " · 正在使用" : ""}</span>
      </div>
      <button class="ghost danger" data-del="${escText(f.path)}"${f.in_use ? ` disabled title="当前配置正在用它，删了下次启动会缺文件"` : ""}>删除</button>
    </div>`).join("");
  body.querySelectorAll("button[data-del]").forEach((btn) => {
    btn.addEventListener("click", () => deleteModel(btn.dataset.del, btn));
  });
}

async function deleteModel(path, btn) {
  const name = String(path).split(/[\\/]/).pop();
  if (!confirm(`删除 ${name}？\n删掉的文件会进回收站，之后可以还原。`)) return;
  btn.disabled = true;
  btn.textContent = "删除中…";
  try {
    const resp = await fetch("/api/models/delete", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path }),
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error(data.detail || data.error || `HTTP ${resp.status}`);
    modelsLoaded = false;
    const fresh = await renderModelSelects(true);
    renderModelManager(fresh || await loadCatalog());
    window.dispatchEvent(new CustomEvent("lingshu:model"));
  } catch (e) {
    btn.disabled = false;
    btn.textContent = "删除";
    alert(human(e));
  }
}

export function initRun() {
  setLogOpen(false);
  $("run-chat-model")?.addEventListener("change", () => switchModel("chat"));
  $("run-sd-model")?.addEventListener("change", () => switchModel("sd"));
  $("btn-model-manager")?.addEventListener("click", openModelManager);
  for (const id of ["model-mgr-close", "model-mgr-ok"]) {
    $(id)?.addEventListener("click", closeModelManager);
  }
  $("model-mgr-scrim")?.addEventListener("click", closeModelManager);
  // 拖入模型后：下拉列表按新扫的结果重填；正在跑的服务不受影响，选完即切换。
  initModelDrop(() => {
    modelsLoaded = false;
    renderModelSelects(true);
    window.dispatchEvent(new CustomEvent("lingshu:model"));
  });
  $("run-ctx-chips")?.addEventListener("click", (e) => {
    const b = e.target.closest("button[data-ctx]");
    if (b) saveCtx(b.dataset.ctx);
  });
  $("run-thinking")?.addEventListener("change", async (e) => {
    try {
      await api.saveSettings({ thinking: e.target.checked });
      await refresh();
    } catch (err) {
      e.target.checked = !e.target.checked;
      $("run-ctx-note").textContent = human(err);
    }
  });
  $("ov-open-output")?.addEventListener("click", async () => {
    try { await api.openFolder("output"); } catch (e) { $("ov-sub").textContent = human(e); }
  });
  $("ov-redetect")?.addEventListener("click", async () => {
    $("ov-why").textContent = "正在重新检测本机硬件…";
    try {
      await api.redetectHardware();
      await refresh();
      $("ov-why").textContent = "已重新检测；推荐参数已按当前硬件刷新。";
    } catch (e) {
      $("ov-why").textContent = human(e);
    }
  });
  $("mon-refresh")?.addEventListener("click", () => {
    refreshMonitor();
    renderModelSelects(true);
  });
  window.addEventListener("lingshu:page", (e) => {
    if (e.detail.name !== "run") {
      clearInterval(logTimer);
      clearInterval(monTimer);
      return;
    }
    setLogOpen();
    refresh();
    refreshMonitor();
    renderModelSelects(true);
    clearInterval(monTimer);
    monTimer = setInterval(refreshMonitor, 5000);
  });
  window.addEventListener("lingshu:status", (e) => {
    status = e.detail;
    renderState(status);
    renderServices(status);
  });
  window.addEventListener("lingshu:model", () => refresh());
  window.addEventListener("lingshu:params", () => refresh());
  window.addEventListener("resize", () => refreshMonitor());
  // 首次进入由 app.js 在 PAGES.run() 之后统一发 lingshu:page，
  // 这里不重复拉一遍，否则 /api/logs 的 since=0 会被请求两次、日志每行重复两次。
}

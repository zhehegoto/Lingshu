// 画布模式：底图 + 框选 → mask 重绘（img2img）
// 语义：白=重绘、黑=保留（二期实测）。strength 固定 0.95，前端不给改。
import { api } from "./api.js?v=36";
import { jobEvents } from "./sse.js?v=28";
import { loadCatalog, chooseModel, shortLabel } from "./model-picker.js?v=2";

const state = { scale: 0.5, dragging: null, sel: null, base: null, jobId: null, sawSampling: false };

const cv = () => document.getElementById("canvas");
const stage = () => document.getElementById("canvas-stage");
const box = () => document.getElementById("sel-box");
const hint = () => document.getElementById("cv-hint");

function canvasModelName(label) {
  const short = shortLabel(label);
  return /^qwen-image-2\.1/i.test(short) ? "Qwen-Image 2.1" : short;
}

async function paintDrawModel(data) {
  const sel = document.getElementById("cv-model");
  if (!sel) return;
  try {
    const catalog = data || await loadCatalog();
    const items = catalog.draw || [];
    sel.innerHTML = "";
    if (!items.length) {
      const o = document.createElement("option");
      o.textContent = "未识别到画图模型";
      o.value = "";
      sel.appendChild(o);
      sel.disabled = true;
      return;
    }
    sel.disabled = false;
    items.forEach((m) => {
      const o = document.createElement("option");
      o.value = m.path;
      o.textContent = canvasModelName(m.label);
      o.title = m.label;
      sel.appendChild(o);
    });
    const current = catalog.selected?.draw?.path;
    if (current) sel.value = current;
    const cur = items.find((m) => m.path === sel.value);
    sel.title = cur?.label || "";
  } catch {
    // 模型目录暂不可读时保留原状态，不影响画布操作。
  }
}

async function switchCanvasModel(path) {
  const sel = document.getElementById("cv-model");
  if (!sel || !path) return;
  if (state.jobId) {
    hint().className = "hint";
    hint().textContent = "正在出图，等这张完成后再换画图模型。";
    await paintDrawModel();
    return;
  }
  const previous = sel.value;
  sel.disabled = true;
  hint().className = "hint";
  hint().textContent = "切换画图模型…";
  try {
    const r = await chooseModel("draw", path);
    const data = await loadCatalog();
    await paintDrawModel(data);
    const formSel = document.getElementById("draw-model");
    if (formSel) formSel.value = data.selected?.draw?.path || path;
    const cur = (data.draw || []).find((m) => m.path === data.selected?.draw?.path);
    const note = document.getElementById("draw-model-note");
    if (note) {
      note.textContent = r.restarted
        ? `已切换到 ${cur?.label || "新模型"}，画图服务已重启`
        : `当前使用 ${cur?.label || "新模型"}；启动画图模型时生效`;
    }
    window.dispatchEvent(new CustomEvent("lingshu:model"));
    hint().className = "hint";
    hint().textContent = `画图模型已切换为 ${canvasModelName(cur?.label)}。`;
  } catch (e) {
    sel.value = previous;
    await paintDrawModel();
    hint().className = "hint err";
    hint().textContent = `切换画图模型失败：${e.message}`;
  } finally {
    sel.disabled = false;
  }
}

function drawGrid() {
  const c = cv();
  const ctx = c.getContext("2d");
  ctx.clearRect(0, 0, c.width, c.height);
  ctx.fillStyle = "#f1ece3";          // 与页面底色同族，别再用冷灰
  ctx.fillRect(0, 0, c.width, c.height);
  ctx.fillStyle = "#d7cdbe";
  const gap = 32;
  for (let y = gap; y < c.height; y += gap) {
    for (let x = gap; x < c.width; x += gap) {
      ctx.fillRect(x, y, 1, 1);
    }
  }
  ctx.strokeStyle = "#e6ded2";
  ctx.strokeRect(0.5, 0.5, c.width - 1, c.height - 1);
}

function applyScale() {
  cv().style.width = `${Math.round(1024 * state.scale)}px`;
  cv().style.height = `${Math.round(1024 * state.scale)}px`;
}

function toCanvas(ev) {
  const r = cv().getBoundingClientRect();
  const x = (ev.clientX - r.left) / state.scale;
  const y = (ev.clientY - r.top) / state.scale;
  return { x: Math.max(0, Math.min(1024, x)), y: Math.max(0, Math.min(1024, y)) };
}

// 白=重绘、黑=保留
function buildMask() {
  const m = document.createElement("canvas");
  m.width = m.height = 1024;
  const ctx = m.getContext("2d");
  ctx.fillStyle = "#000";
  ctx.fillRect(0, 0, 1024, 1024);
  if (state.sel) {
    const s = state.sel;
    ctx.fillStyle = "#fff";
    ctx.fillRect(s.x, s.y, s.w, s.h);
  }
  return m.toDataURL("image/png");
}

function refreshButton() {
  const btn = document.getElementById("cv-generate");
  const ok = Boolean(state.base && state.sel && state.sel.w > 8 && state.sel.h > 8 &&
    document.getElementById("cv-prompt").value.trim());
  btn.disabled = !ok || Boolean(state.jobId);
}

function showSel(a, b) {
  const x = Math.min(a.x, b.x), y = Math.min(a.y, b.y);
  const w = Math.abs(a.x - b.x), h = Math.abs(a.y - b.y);
  const bx = box();
  bx.hidden = false;
  bx.style.left = `${x * state.scale}px`;
  bx.style.top = `${y * state.scale}px`;
  bx.style.width = `${w * state.scale}px`;
  bx.style.height = `${h * state.scale}px`;
  state.sel = { x, y, w, h };
  document.getElementById("cv-sel").textContent =
    `${Math.round(x)},${Math.round(y)} · ${Math.round(w)}×${Math.round(h)}`;
  if (w > 8 && h > 8) {
    hint().className = "hint";
    hint().textContent = `框内重绘、框外保留（denoising_strength 固定 0.95）。${state.base ? "" : "先载入一张底图。"}`;
  }
  refreshButton();
}

async function loadBaseFromJob() {
  try {
    const { items } = await api.jobs();
    const withImg = items.find((j) => j.status === "done" && (j.images || []).length);
    if (!withImg) {
      hint().className = "hint err";
      hint().textContent = "还没有出过图。先去表单模式生成一张，再回来做局部重绘。";
      return;
    }
    await loadBase(withImg.images[0]);
  } catch (e) {
    hint().className = "hint err";
    hint().textContent = `载入底图失败：${e.message}`;
  }
}

function loadBase(url) {
  return new Promise((resolve) => {
    const img = new Image();
    img.onload = () => {
      const c = cv();
      const ctx = c.getContext("2d");
      ctx.clearRect(0, 0, 1024, 1024);
      ctx.drawImage(img, 0, 0, 1024, 1024);
      state.base = url;
      box().hidden = true;           // 换了底图，旧框选作废（否则框还画在图上，容易误解）
      state.sel = null;
      document.getElementById("cv-sel").textContent = "未选中区域";
      hint().className = "hint";
      hint().textContent = `底图已载入：${url.split("/").pop()}。框选要改的区域，填描述，点生成。`;
      refreshButton();
      resolve();
    };
    img.onerror = () => {
      hint().className = "hint err";
      hint().textContent = "底图加载失败（/output 路径不通）";
      resolve();
    };
    img.src = url;
  });
}

async function generate() {
  if (!state.base || !state.sel) return;
  const prompt = document.getElementById("cv-prompt").value.trim();
  if (!prompt) return;

  const btn = document.getElementById("cv-generate");
  btn.disabled = true;
  state.sawSampling = false;
  hint().textContent = "提交中…";

  try {
    const r = await api.createJob({
      prompt,
      width: 1024,
      height: 1024,
      kind: "mask",
      init_image: cv().toDataURL("image/png"),
      mask: buildMask(),
    });
    state.jobId = r.id;

    jobEvents(r.id, (evt) => {
      if (evt.type === "snapshot") {
        hint().textContent = `状态 ${evt.status}…`;
      } else if (evt.type === "progress") {
        const st = evt.state || {};
        const step = st.sampling_step ?? 0;
        const total = st.sampling_steps ?? 8;
        // 刚提交时 sd.progress 可能还留着上一单的 8/8，先见到小于总数的步数才算真在采样
        if (step < total) state.sawSampling = true;
        hint().textContent = (state.sawSampling && step >= total)
          ? "采样完成，解码中…"
          : `采样 ${step}/${total}`;
      } else if (evt.type === "status" && evt.status === "preparing") {
        hint().textContent = "准备中（卸载对话模型）…";
      } else if (evt.type === "done") {
        finish(evt.error ? null : r.id, evt.error || null);
      }
    });
  } catch (e) {
    state.jobId = null;
    refreshButton();
    hint().className = "hint err";
    hint().textContent = e.code === "gpu_busy"
      ? "出图服务正忙（对话模型还占着显存），稍后重试。"
      : `提交失败：${e.message}`;
  }
}

async function finish(id, err) {
  state.jobId = null;
  if (err) {
    refreshButton();
    hint().className = "hint err";
    hint().textContent = `重绘失败：${err}`;
    return;
  }
  try {
    const job = await api.job(id);
    if (job.images && job.images.length) {
      await loadBase(job.images[0]);
      hint().textContent = "重绘完成，成品已载入画布（可以继续框选下一处）。";
    }
  } catch {
    hint().className = "hint err";
    hint().textContent = "重绘完成但取结果失败，去作品文件夹看。";
  }
  refreshButton();
}

export function initCanvas() {
  drawGrid();
  applyScale();
  refreshButton();
  paintDrawModel();
  document.getElementById("cv-model")?.addEventListener("change", (e) => {
    switchCanvasModel(e.target.value);
  });
  window.addEventListener("lingshu:model", () => { paintDrawModel(); });
  // 「历史 → 去画布」：把某张作品当底图载进来。切到画布标签由触发方负责。
  window.addEventListener("lingshu:use-base", async (e) => {
    const url = e.detail?.url;
    if (!url) return;
    try {
      await loadBase(url);
    } catch (err) {
      const h = document.getElementById("cv-hint");
      if (h) h.textContent = String(err?.message || err);
    }
  });

  cv().addEventListener("pointerdown", (e) => {
    state.dragging = toCanvas(e);
    showSel(state.dragging, state.dragging);
    cv().setPointerCapture(e.pointerId);
  });
  cv().addEventListener("pointermove", (e) => {
    if (!state.dragging) return;
    showSel(state.dragging, toCanvas(e));
  });
  cv().addEventListener("pointerup", () => { state.dragging = null; });

  document.getElementById("cv-prompt").addEventListener("input", refreshButton);
  document.getElementById("cv-load-last").addEventListener("click", loadBaseFromJob);
  document.getElementById("cv-fit").addEventListener("click", () => {
    const avail = stage().clientWidth - 4;
    state.scale = Math.min(1, avail / 1024);
    applyScale();
  });
  document.getElementById("cv-100").addEventListener("click", () => {
    state.scale = 1;
    applyScale();
  });
  document.getElementById("cv-generate").addEventListener("click", generate);
}

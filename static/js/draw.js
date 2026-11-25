// 画图页（表单模式）：创建作业 → EventSource 推进度 → 落图显示
// 进度条只表示采样：step==steps 之后变灰显示"解码中"（sd-server 的解码不在 progress 里，约 8s）
import { api, human } from "./api.js?v=36";
import { jobEvents } from "./sse.js?v=28";
import { loadCatalog, chooseModel, fillSelect } from "./model-picker.js?v=2";

const state = {
  jobId: null, cancel: null, ratio: "1:1", res: "1K", images: [], selected: new Set(),
  caps: null,               // 后端实测过的尺寸表：{sizes: {"1K": {"1:1": [w,h]}}}
  modelReady: true,        // 出图模型（8081）在不在。顶栏关掉后为 false，生成要拦住
  modelWant: true,         // 用户意图：true 但没 ready = 正在加载，按钮说“启动中”而不是“已关闭”
};

const el = (id) => document.getElementById(id);

function hint(text, isErr = false) {
  const h = el("draw-hint");
  h.textContent = text || "";
  h.className = `hint${isErr ? " err" : ""}`;
}

// ---- 一句话出图：让对话模型把中文口语变成作业参数 ----
// 只做"填参"，不直接提交作业：模型给的 prompt 常常需要人看一眼再改。
const PLAN_TOOLS = [{
  type: "function",
  function: {
    name: "generate_image",
    description: "用户要求画图/出图/生成图片时调用，把画面描述整理成英文提示词。",
    parameters: {
      type: "object",
      properties: {
        prompt: { type: "string", description: "英文画面描述，具体一些" },
        steps: { type: "integer", description: "采样步数，只能填 8 到 20 之间的整数，通常 8" },
        seed: { type: "integer", description: "随机种子，-1 表示随机" },
      },
      required: ["prompt"],
    },
  },
}];

function planHint(text, isErr = false) {
  const h = el("plan-hint");
  h.textContent = text || "";
  h.className = `muted small${isErr ? " err" : ""}`;
}

// 失败路径：AI 没填成，就把原话放进描述框，用户直接在表单里改，不掉进死胡同
function degradePlan(text, why) {
  let filled = false;
  if (!el("prompt").value.trim()) {
    el("prompt").value = text;
    filled = true;
  }
  el("prompt").focus();
  const sizeWords = /(竖|横|比例|尺寸|宽高|宽屏|竖版|横版|portrait|landscape|ratio|aspect)/i.test(text);
  // 模型说的话不转述给用户——它可能编一个"已设置 3:4，默认 1080×1440"。
  // 这里只说明实际发生了什么，以及尺寸这类东西该去哪儿点。
  planHint(
    `AI 填参失败（${why}）。` +
    (filled ? "已把你这句话放进下面的描述框，" : "下面的描述框保留了你原来的内容，") +
    "改完点「生成」就能出图。" +
    (sizeWords ? " 尺寸不由 AI 代选：用下方比例按钮自己选。" : ""),
    true
  );
}

async function planFromText() {
  const text = (el("nl-prompt").value || "").trim();
  if (!text) {
    planHint("先写一句想要什么画面，例如：画一个红苹果放在木桌上", true);
    el("nl-prompt").focus();
    return;
  }
  const btn = el("btn-plan");
  btn.disabled = true;
  const t0 = Date.now();
  const tick = setInterval(() => {
    btn.textContent = `思考中… ${Math.round((Date.now() - t0) / 1000)}s`;
  }, 200);
  btn.textContent = "思考中… 0s";
  planHint("正在让模型把这句话变成出图参数（实测约 13s，不是卡住）…");
  try {
    const r = await api.chatOnce({
      messages: [{ role: "user", content: text }],
      tools: PLAN_TOOLS,
      tool_choice: "auto",
    });
    const call = (r.message?.tool_calls || [])[0];
    if (!call) return degradePlan(text, "模型这次没有调用出图工具");
    let args = {};
    try {
      args = JSON.parse(call.function?.arguments || "{}");
    } catch {
      return degradePlan(text, "模型给的参数不是合法 JSON");
    }
    const prompt = String(args.prompt || "").trim();
    if (!prompt) return degradePlan(text, "模型没给出画面描述");

    // 步数必须自己说了算：实测模型会随手给 30（越界）或 16（能跑但翻倍耗时）。
    // 只有用户原话里明确要"多少步/精细/高质量"时才采用模型给的步数，否则按默认 8 步。
    const wanted = /(\d+\s*步|步数|精细|高质量|高清)/.test(text);
    let steps = Number(args.steps);
    let why = "";
    if (!Number.isFinite(steps) || steps < 8 || steps > 20) {
      steps = 8; why = "模型给的步数越界，按默认 8 步";
    } else if (!wanted && steps !== 8) {
      why = `模型给的是 ${Math.round(steps)}，你没要求更高步数，按默认 8 步`;
      steps = 8;
    }
    steps = Math.round(steps);

    el("prompt").value = prompt;
    el("steps").value = String(steps);

    // seed：必须是整数（模型可能给字符串或小数）；-1 表示随机
    const notes = [];
    const seed = Number(args.seed);
    const seedOk = Number.isFinite(seed) && Number.isInteger(seed);
    if (seedOk) el("seed").value = String(seed);
    else if (args.seed !== undefined) notes.push(`模型给的 seed「${args.seed}」不是整数，已忽略`);

    // 尺寸：本工具只支持 1024×1024，模型给的宽高一律不采用（实测它会回 1920×1080 / 768×1024，
    // 甚至在没有 tool_call 的纯文本里谎称"已设置 3:4，默认 1080×1440"）。
    const sizeWords = /(竖|横|比例|尺寸|宽高|宽屏|竖版|横版|portrait|landscape|ratio|aspect)/i.test(text);
    const onlySquare = "比例请用下方按钮自己选（1:1 / 4:3 / 3:4 / 16:9 / 9:16）";
    if (args.width || args.height) {
      const wh = `${args.width ?? "?"}×${args.height ?? "?"}`;
      if (!(Number(args.width) === 1024 && Number(args.height) === 1024)) {
        notes.push(`尺寸只支持 1024×1024，模型建议的 ${wh} 已忽略；${onlySquare}`);
      }
    } else if (sizeWords) {
      notes.push(`尺寸不由 AI 代选；${onlySquare}`);
    }

    planHint(
      `已填参：${steps} 步${why ? `（${why}）` : ""}` +
      `${seedOk ? ` · seed ${seed}` : ""}。` +
      (notes.length ? `${notes.join("；")}。` : "") +
      "检查描述后点「生成」。"
    );
    el("prompt").scrollIntoView({ block: "center" });
  } catch (e) {
    return degradePlan(text, human(e));
  } finally {
    clearInterval(tick);
    btn.disabled = false;
    btn.textContent = "让 AI 填";
  }
}

function setProgress({ step, total, label, decoding = false }) {
  const box = el("progress");
  const fill = el("bar-fill");
  const text = el("progress-text");
  box.hidden = false;
  box.querySelector(".bar").classList.toggle("decoding", decoding);
  const pct = total ? Math.min(100, Math.round((step / total) * 100)) : 0;
  fill.style.width = `${decoding ? 100 : pct}%`;
  text.textContent = label || (decoding ? "解码中（进度不含解码段）" : `采样 ${step}/${total} · ${pct}%`);
}

function showImage(url) {
  el("gallery").innerHTML = `<img src="${url}" alt="生成结果">`;
}

// —— 最近 4 张：最新在左，旧图右移，空格用虚线占位 ——
function renderRecent(items) {
  const box = el("recent4");
  if (!box) return;
  const live = items;
  box.innerHTML = "";
  for (let i = 0; i < 4; i++) {
    const it = live[i];
    const cell = document.createElement("button");
    if (!it) {
      cell.className = "cell empty";
      cell.textContent = live.length ? "空" : "还没有作品";
      cell.disabled = true;
      box.appendChild(cell);
      continue;
    }
    cell.className = "cell filled";
    cell.dataset.path = it.path;
    cell.title = it.title;

    const wrap = document.createElement("div");
    wrap.className = "thumb-wrap";
    const img = new Image();
    img.src = it.url;
    img.alt = it.title;
    wrap.appendChild(img);
    cell.appendChild(wrap);

    const cap = document.createElement("span");
    cap.className = "caption";
    cap.textContent = it.title;         // 改名后这里就是新名字，不用刷新页面
    cell.appendChild(cap);

    cell.addEventListener("click", () => showImage(it.url));
    box.appendChild(cell);
  }
}

function dayOf(ts) {
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return "未知日期";
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

function timeOf(ts) {
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return "";
  const p = (n) => String(n).padStart(2, "0");
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

// —— 历史：按日期分组，不是一长串平铺 ——
function renderHistory(items) {
  const body = el("history-body");
  body.innerHTML = "";
  if (!items.length) {
    body.innerHTML = `<div class="muted small">还没有作品。生成第一张后会出现在这里。</div>`;
    syncSelectUI();
    return;
  }
  const groups = new Map();
  items.forEach((it) => {
    const d = dayOf(it.created_at);
    if (!groups.has(d)) groups.set(d, []);
    groups.get(d).push(it);
  });
  [...groups.keys()].sort().reverse().forEach((day) => {
    const wrap = document.createElement("div");
    wrap.className = "hist-group";
    const h4 = document.createElement("h4");
    const n = groups.get(day).length;
    h4.textContent = `${day} · ${n} 张`;
    wrap.appendChild(h4);

    groups.get(day).forEach((it) => {
      const row = document.createElement("div");
      row.className = "hist-item";

      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.className = "hist-cb";
      cb.checked = state.selected.has(it.path);
      cb.title = "选中后可批量删除";
      cb.addEventListener("change", () => {
        if (cb.checked) state.selected.add(it.path);
        else state.selected.delete(it.path);
        syncSelectUI();
      });
      row.appendChild(cb);

      const img = new Image();
      img.className = "thumb";
      img.src = it.url;
      img.alt = it.name;
      img.addEventListener("click", () => showImage(it.url));
      row.appendChild(img);

      const meta = document.createElement("div");
      meta.className = "meta";

      const titleRow = document.createElement("div");
      titleRow.className = "title-row";
      const title = document.createElement("span");
      title.className = "title";
      title.textContent = it.title;
      titleRow.appendChild(title);
      const rn = document.createElement("button");
      rn.className = "link-btn";
      rn.textContent = "改名";
      rn.addEventListener("click", () => startRename(it, title));
      titleRow.appendChild(rn);
      meta.appendChild(titleRow);

      const p = document.createElement("p");
      p.className = "prompt";
      p.textContent = it.prompt || "(无描述)";
      const t = document.createElement("div");
      t.className = "time";
      t.textContent = `${timeOf(it.created_at)} · ${it.name}`;
      meta.append(p, t);

      const acts = document.createElement("div");
      acts.className = "hist-acts";
      const again = document.createElement("button");
      again.className = "link-btn";
      again.textContent = "再来一张";
      again.title = "用同样的描述、步数、种子再生成一次";
      again.addEventListener("click", () => repeatPrompt(it));
      acts.appendChild(again);
      const toCanvas = document.createElement("button");
      toCanvas.className = "link-btn";
      toCanvas.textContent = "去画布";
      toCanvas.title = "把这张当底图，涂抹局部后重绘";
      toCanvas.addEventListener("click", () => {
        closeHistory();
        window.dispatchEvent(new CustomEvent("lingshu:use-base", { detail: { url: it.url } }));
        showView("canvas");
      });
      acts.appendChild(toCanvas);
      meta.appendChild(acts);
      row.appendChild(meta);

      row.appendChild(deleteButton(it));
      wrap.appendChild(row);
    });
    body.appendChild(wrap);
  });

  // 全选与批量删除条在抽屉顶部（HTML 里固定一份），这里只同步它的状态
  syncSelectUI();
}

// 全选 / 批量删除条的状态：总数、已选数、全选框的三态
function syncSelectUI() {
  const total = state.images.length;
  const n = state.selected.size;
  const all = el("sel-all");
  const btn = el("btn-del-selected");
  if (!all || !btn) return;
  all.checked = total > 0 && n === total;
  all.indeterminate = n > 0 && n < total;
  btn.disabled = n === 0;
  if (!btn.classList.contains("armed")) btn.textContent = `删除所选（${n}）`;
}

async function refreshImages() {
  try {
    const { items } = await api.images();
    state.images = items;
    // 选中的东西里，已经不存在了的要摘掉——否则"删除所选（N）"会带着幽灵计数
    const alive = new Set(items.map((it) => it.path));
    [...state.selected].forEach((p) => { if (!alive.has(p)) state.selected.delete(p); });
    renderRecent(items);
    renderHistory(items);
  } catch (e) {
    const recent = el("recent4");
    if (recent) recent.innerHTML = `<div class="cell empty">读取失败</div>`;
    el("history-body").innerHTML = `<div class="muted small">作品列表读取失败：${human(e)}</div>`;
    syncSelectUI();
  }
}

// 行内改名：Enter 保存、Esc 取消、失焦也保存（保存后统一重取列表，四格和历史一起变）
function startRename(it, titleEl) {
  const input = document.createElement("input");
  input.className = "rename-input";
  input.value = it.display_name || "";
  input.placeholder = it.name;
  input.maxLength = 50;
  titleEl.replaceWith(input);
  input.focus();
  input.select();

  let done = false;
  const finish = async (save) => {
    if (done) return;
    done = true;
    const val = input.value.trim();
    if (!save || val === (it.display_name || "")) {
      await refreshImages();
      return;
    }
    try {
      const r = await api.renameImage(it.path, val);
      hint(`已改名为「${r.display_name}」（只改显示名，文件名仍是 ${it.name}）`);
    } catch (e) {
      hint(human(e), true);
    }
    await refreshImages();
  };
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); finish(true); }
    if (e.key === "Escape") { e.preventDefault(); finish(false); }
  });
  input.addEventListener("blur", () => finish(true));
}

// 两段式确认：第一下只把按钮改成"再点一次"，4 秒内再点才真执行——不做原生 confirm。
// 原生 confirm 是浏览器正中的模态框：打断看图的连贯性，也让自动化验证卡在同步弹窗上。
//
// 但"两段式"有个致命细节：第一下和第二下如果落在同一个双击里（间隔几十毫秒），
// 用户以为只是点了一下，实际已经删了。所以确认点击必须晚于 ANTI_DOUBLE 毫秒才算数——
// 这是实测代价换来的：一次"全选 + 连点"把 27 张作品一次性删光了（没有回收站，删了回不来）。
const ANTI_DOUBLE_MS = 400;

function twoStage(btn, idleText, armedText, run) {
  let timer = null;
  let armedAt = 0;
  const reset = () => {
    clearTimeout(timer);
    timer = null;
    armedAt = 0;
    btn.textContent = idleText();
    btn.classList.remove("armed");
  };
  btn.addEventListener("click", async () => {
    if (!timer) {
      btn.textContent = armedText;
      btn.classList.add("armed");
      armedAt = Date.now();
      timer = setTimeout(reset, 4000);
      return;
    }
    if (Date.now() - armedAt < ANTI_DOUBLE_MS) return;   // 双击的第二下不算确认
    reset();
    try {
      await run();
    } catch (e) {
      hint(human(e), true);
    }
  });
  return btn;
}

// 单行删除 = 真删（png + 同名 json 一起 unlink）。没有回收站，也就没有"恢复"这一步。
function deleteButton(it) {
  const b = document.createElement("button");
  b.className = "link-btn danger";
  b.textContent = "删除";
  twoStage(b, () => "删除", "再点一次", async () => {
    await api.deleteImage(it.path);
    state.selected.delete(it.path);
    hint(`已删除 ${it.name}（文件已移除，不可恢复）`);
    await refreshImages();
  });
  return b;
}

// 批量删除：只删勾中的那些。返回体里的 count 是"真的删掉的文件数"（png + json 各算一个），
// 所以在提示里用勾选张数，免得用户以为删了两倍的图。
async function deleteSelected() {
  const paths = [...state.selected];
  if (!paths.length) return;
  await api.deleteImages(paths);
  hint(`已删除 ${paths.length} 张（文件已移除，不可恢复）`);
  state.selected.clear();
  await refreshImages();
}

function enableGenerate(on) {
  const b = el("btn-generate");
  b.disabled = on || !state.modelReady;
  b.textContent = on
    ? "生成中…"
    : (!state.modelReady
        ? (state.modelWant ? "模型启动中…" : "模型已关闭")
        : "生成");
}

async function generate() {
  if (!state.modelReady) {
    return hint(
      state.modelWant
        ? "出图模型还在启动，稍等十几秒再生成"
        : "出图模型已关闭，先在顶栏点「启动出图模型」",
      true,
    );
  }
  const prompt = el("prompt").value.trim();
  if (!prompt) return hint("先写描述", true);
  const wh = state.caps?.sizes?.[state.res]?.[state.ratio];
  if (!wh) return hint(`${state.ratio} · ${state.res} 这个组合没开放，换一个比例或清晰度`, true);
  const steps = Number(el("steps").value || 8);
  if (steps < 8 || steps > 20) return hint("步数只能是 8–20", true);

  enableGenerate(true);
  hint("提交中…");
  try {
    const job = await api.createJob({
      prompt,
      negative: (el("negative")?.value || "").trim(),
      width: wh[0],
      height: wh[1],
      steps,
      seed: Number(el("seed").value || -1),
      batch_count: Number(el("batch").value || 1),
    });
    state.jobId = job.id;
    hint(`已排队 ${job.id}`);
    setProgress({ step: 0, total: steps, label: "排队中…" });
    watch(job.id, steps);
  } catch (e) {
    enableGenerate(false);
    hint(human(e), true);
  }
}

function watch(jobId, steps) {
  state.cancel?.close();
  const started = Date.now();
  const elapsed = () => `${Math.round((Date.now() - started) / 1000)}s`;

  state.cancel = jobEvents(jobId, (evt) => {
    if (evt.type === "snapshot") {
      setProgress({ step: 0, total: steps, label: `状态 ${evt.status} · ${elapsed()}` });
    } else if (evt.type === "progress") {
      const st = evt.state || {};
      const step = st.sampling_step ?? Math.round((evt.progress || 0) * steps);
      const total = st.sampling_steps ?? steps;
      if (step >= total && total > 0) {
        setProgress({ step, total, decoding: true, label: `采样完成，解码中 · ${elapsed()}` });
      } else {
        setProgress({ step, total, label: `采样 ${step}/${total} · ${elapsed()}` });
      }
    } else if (evt.type === "status") {
      if (evt.status === "preparing") hint(`准备中（卸载对话模型）· ${elapsed()}`);
      if (evt.status === "cancelling") hint("取消中…");
    } else if (evt.type === "done") {
      enableGenerate(false);
      const err = evt.error;
      const cancelBtn = document.getElementById("cancel-btn");
      if (cancelBtn) cancelBtn.hidden = true;      // 结束了就别再挂着"取消"
      if (err) {
        hint(human({ code: err, message: err }), true);
        setProgress({ step: 0, total: steps, label: `失败：${err}` });
      } else {
        hint(`完成 · ${elapsed()}`);
        setProgress({ step: steps, total: steps, decoding: true, label: `完成 · ${elapsed()}` });
        api.job(jobId).then(async (j) => {
          const imgs = j.images || [];
          await refreshImages();                   // 先刷列表：四格按时间重排
          if (imgs.length) showImage(imgs[0]);
        }).catch(() => {});
      }
    }
  }, () => {
    // EventSource 断开：改轮询，直到作业进入终态
    pollUntilDone(jobId, steps);
  });
}

async function pollUntilDone(jobId, steps) {
  enableGenerate(true);
  hint("事件流断开，改为轮询…");
  for (let i = 0; i < 240; i++) {
    await new Promise((r) => setTimeout(r, 1500));
    try {
      const j = await api.job(jobId);
      if (["done", "failed", "cancelled"].includes(j.status)) {
        enableGenerate(false);
        if (j.status === "done" && j.images?.length) {
          hint("完成（轮询）");
          setProgress({ step: steps, total: steps, decoding: true, label: "完成" });
          await refreshImages();
          showImage(j.images[0]);
        } else {
          hint(human({ code: j.error, message: j.status }), true);
        }
        return;
      }
      hint(`状态 ${j.status}…`);
    } catch { /* 继续轮询 */ }
  }
  enableGenerate(false);
  hint("轮询超时", true);
}

async function cancel() {
  if (!state.jobId) return;
  hint("取消中（会重启出图服务）…");
  try {
    await api.cancelJob(state.jobId);
  } catch (e) {
    hint(human(e), true);
  }
}

// —— 尺寸目录：发布版对所有机器开放，按本机显存选一档推荐 ——
async function loadCaps() {
  try {
    state.caps = await api.drawCaps();
    const supported = state.caps.resolutions || [];
    [...el("res-group").children].forEach((b) => {
      const ok = supported.includes(b.dataset.res);
      b.disabled = !ok;
      b.title = ok ? (state.caps.notes?.[b.dataset.res] || "") : "未开放";
    });
    const rec = state.caps.recommended;
    if (rec && supported.includes(rec)) {
      state.res = rec;
      [...el("res-group").children].forEach((b) => b.classList.toggle("active", b.dataset.res === rec));
    }
    paintSize();
  } catch (e) {
    hint(`尺寸表读取失败：${human(e)}`, true);
    el("btn-generate").disabled = true;
  }
}

function paintSize() {
  const wh = state.caps?.sizes?.[state.res]?.[state.ratio];
  const note = el("draw-size-note");
  if (!wh) {
    note.textContent = `${state.ratio} · ${state.res}：这个组合没开放`;
    note.classList.add("err");
    return;
  }
  let text = `实际尺寸 ${wh[0]}×${wh[1]}`;
  if (state.caps.recommended) text += ` · 本机推荐 ${state.caps.recommended}`;
  if (state.res === "4K") text += ` · ${state.caps.notes?.["4K"] || "很吃显存"}`;
  note.textContent = text;
  note.classList.toggle("err", state.res === "4K");
}

let noteTimer = null;

// 模型名已经在旁边下拉里了，这行只负责「刚才发生了什么」——切换/失败/重启。
// 说完就收，否则页面上永远挂着一句"当前使用 xx"，等于把下拉重写了一遍。
function flashNote(text, ms = 4500) {
  const note = el("draw-model-note");
  if (!note) return;
  note.textContent = text || "";
  clearTimeout(noteTimer);
  if (text) noteTimer = setTimeout(() => { note.textContent = ""; }, ms);
}

function closeHistory() {
  const drawer = el("history-drawer");
  const scrim = el("history-scrim");
  if (drawer) drawer.hidden = true;
  if (scrim) scrim.hidden = true;
}

function showView(view) {
  el("draw-subtabs")?.querySelector(`button[data-view="${view}"]`)?.click();
}

// 按作品的宽高反查「比例 + 清晰度」组合；caps 里没有这个尺寸就返回 false，让调用方自己兜底
function matchSize(width, height) {
  const sizes = state.caps?.sizes;
  if (!sizes || !width || !height) return false;
  for (const res of Object.keys(sizes)) {
    for (const ratio of Object.keys(sizes[res] || {})) {
      const wh = sizes[res][ratio];
      if (!wh || wh[0] !== width || wh[1] !== height) continue;
      const rb = [...(el("ratio-group")?.children || [])].find((b) => b.dataset.ratio === ratio);
      const sb = [...(el("res-group")?.children || [])].find((b) => b.dataset.res === res);
      if (!rb || !sb || rb.disabled || sb.disabled) return false;
      rb.click();
      sb.click();
      paintSize();
      return true;
    }
  }
  return false;
}

// 「再来一张（同种子）」：把这张图的参数搬回表单，让用户自己点生成。
// 不自动提交——他多半想改一两个字，自动跑掉只会白烧一遍显存。
function repeatPrompt(it) {
  const full = String(it.prompt_full || it.prompt || "").trim();
  if (!full) return hint("这张图没记下描述，没法复现", true);
  el("prompt").value = full;
  if (el("negative")) el("negative").value = it.negative || "";
  if (it.steps) el("steps").value = it.steps;
  const seed = Number(it.seed);
  const hasSeed = Number.isFinite(seed) && seed >= 0;
  el("seed").value = hasSeed ? seed : -1;
  if (el("batch")) el("batch").value = "1";
  const sized = matchSize(Number(it.width), Number(it.height));
  showView("form");
  closeHistory();
  hint(hasSeed
    ? `已填好描述、步数、种子 ${seed}${sized ? "" : "；尺寸沿用当前比例"}，点「生成」即可复现`
    : "已填好描述与步数；这张原图的种子是随机生成的，再生成会是另一张");
}

async function initModelPicker() {
  const sel = el("draw-model");
  const note = el("draw-model-note");
  if (!sel || !note) return;
  try {
    const data = await loadCatalog();
    fillSelect(sel, data.draw, data.selected?.draw?.path,
      "还没有画图模型——把 qwen-image 的 .gguf 拖进 models");
    const cur = (data.draw || []).find((m) => m.path === data.selected?.draw?.path);
    flashNote(cur ? "" : "models 目录里没找到画图模型，先往下拉一个进来");
  } catch {
    flashNote("读取模型目录失败", 8000);
  }
  sel.addEventListener("change", async () => {
    if (state.jobId) {
      flashNote("正在出图，等这张完成后再换画图模型");
      return;
    }
    flashNote("切换中…", 8000);
    try {
      const r = await chooseModel("draw", sel.value);
      const data = await loadCatalog();
      fillSelect(sel, data.draw, data.selected?.draw?.path,
        "还没有画图模型——把 qwen-image 的 .gguf 拖进 models");
      const cur = (data.draw || []).find((m) => m.path === data.selected?.draw?.path);
      flashNote(r.restarted
        ? `已切换到 ${cur?.label || "新模型"}，画图服务已重启`
        : `已选择 ${cur?.label || "新模型"}，启动画图模型时生效`);
      window.dispatchEvent(new CustomEvent("lingshu:model"));
    } catch (e) {
      flashNote(human(e), 8000);
      loadCatalog().then((data) => fillSelect(sel, data.draw, data.selected?.draw?.path)).catch(() => {});
    }
  });
}

async function init() {
  await initModelPicker();

  // 步数默认值跟配置走
  try {
    const s = await api.settings();
    el("steps").value = s.values.steps;
  } catch { /* 用页面默认 */ }

  // 比例 / 清晰度：全部按后端实测表放开，没测过的组合禁止选中
  el("ratio-group").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-ratio]");
    if (!b || b.disabled) return;
    state.ratio = b.dataset.ratio;
    [...el("ratio-group").children].forEach((x) => x.classList.toggle("active", x === b));
    paintSize();
  });
  el("res-group").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-res]");
    if (!b || b.disabled) return;
    state.res = b.dataset.res;
    [...el("res-group").children].forEach((x) => x.classList.toggle("active", x === b));
    paintSize();
  });

  el("btn-generate").addEventListener("click", generate);
  await loadCaps();

  // 表单 / 画布切换
  el("draw-subtabs").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-view]");
    if (!b) return;
    [...el("draw-subtabs").querySelectorAll(".subtab")].forEach((x) => x.classList.toggle("active", x === b));
    const canvas = b.dataset.view === "canvas";
    el("draw-form-view").hidden = canvas;
    el("draw-canvas-view").hidden = !canvas;
  });
  el("draw-open-folder").addEventListener("click", async () => {
    try { await api.openFolder("output"); } catch (e) { hint(human(e), true); }
  });
  el("cv-open-folder").addEventListener("click", async () => {
    try { await api.openFolder("output"); } catch (e) { el("cv-hint").textContent = human(e); }
  });
  const p = el("progress");
  const cancelBtn = document.createElement("button");
  cancelBtn.className = "icon-btn";
  cancelBtn.id = "cancel-btn";
  cancelBtn.textContent = "取消";
  cancelBtn.style.marginTop = "8px";
  cancelBtn.hidden = true;                     // 只在生成中显示
  cancelBtn.addEventListener("click", cancel);
  p.appendChild(cancelBtn);

  // 历史抽屉
  const drawer = el("history-drawer");
  const scrim = el("history-scrim");
  const closeHist = () => { drawer.hidden = true; scrim.hidden = true; };
  el("btn-history").addEventListener("click", async () => {
    drawer.hidden = false;
    scrim.hidden = false;
    await refreshImages();
  });
  el("btn-close-history").addEventListener("click", closeHist);
  scrim.addEventListener("click", closeHist);

  // 全选：一次勾上当前列表里的全部作品；取消则全清
  el("sel-all").addEventListener("change", () => {
    if (el("sel-all").checked) state.images.forEach((it) => state.selected.add(it.path));
    else state.selected.clear();
    renderHistory(state.images);
    syncSelectUI();
  });
  twoStage(el("btn-del-selected"),
    () => `删除所选（${state.selected.size}）`,
    "再点一次确认（不可恢复）",
    deleteSelected);

  // 首屏：四格 + 历史一起取一次
  await refreshImages();

  // 打开页面时把最近一张放进大图区（没有就保留空状态）
  const first = state.images[0];
  if (first) showImage(first.url);
}

export { init as initDraw };

// 顶栏开关把它调过来：模型关掉时「生成」直接置灰，不是等提交后才 409
// want=true 但 ready=false 是「正在加载」，文案不能说成「已关闭」
export function setModelReady(ok, want = true, announce = true) {
  const next = !!ok;
  const wantNext = !!want;
  const loading = !next && wantNext;
  if (next === state.modelReady && wantNext === state.modelWant) return;
  state.modelReady = next;
  state.modelWant = wantNext;
  enableGenerate(false);
  if (!announce) return;             // 首屏同步：只对齐状态，不往提示区塞话
  if (next) hint("出图模型已启动");
  else if (loading) hint("出图模型正在启动…");
  else hint("出图模型已关闭，要用先在顶栏启动", true);
}

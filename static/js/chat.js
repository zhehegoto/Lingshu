// 对话页：会话列表 + 切换 + 新建 + 改名 + 软删 + 流式 SSE
// 在飞策略（本批选定）：生成中禁止切换——侧边栏置灰，要切先点「停止」。
// 理由：单卡上同时只能有一条生成流，切走也发不出新消息（只会 503 排队），
// “能走但走了看不见结果”比“先停下再走”更容易被误解。
import { api, human, ERROR_TEXT } from "./api.js?v=36";
import { streamSSE } from "./sse.js?v=28";
import { loadCatalog, chooseModel, shortLabel } from "./model-picker.js?v=2";

const PARAM_SPECS = [
  ["temperature", "温度", "越高越发散，越低越稳", "0–2", "0.7"],
  ["top_p", "Top P", "只从累计概率最高的词里选", "0–1", "0.9"],
  ["top_k", "Top K", "每步只考虑前 K 个词", "1–200", "40"],
  ["min_p", "Min P", "过滤掉概率太低的词", "0–1", "0.05"],
  ["repeat_penalty", "重复惩罚", "大于 1 会抑制复读", "0.1–3", "1.1"],
  ["presence_penalty", "话题新鲜度", "鼓励聊新话题，负值更集中", "-2–2", "0"],
  ["max_tokens", "最大回复长度", "一次最多生成多少 token", "≥1", "1024"],
  ["seed", "随机种子", "固定后同参数更容易复现", "0–2³¹-1", ""],
];

const state = {
  messages: [],          // {role, content}
  streaming: false,
  pending: null,         // 等 CHAT_READY 期间暂存的消息
  pollTimer: null,
  chatId: null,          // 当前会话 id；null 表示还没落盘的新对话
  controller: null,      // 当前流的 AbortController，「停止」就是 abort
  items: [],             // 会话列表
  showDeleted: false,
  modelReady: false,     // 对话模型（8099）在不在。默认关闭，启动后 app.js 会同步
  modelWant: false,      // 用户意图：true 但没 ready，就是正在加载，文案要说“启动中”而不是“已关闭”
  settings: { system: "", params: {} },
  attachments: [],       // {id,name,url,text,kind,size}
  tools: { master: true, time: true, calc: true, file: false, folder: false, write: false, web: false, page: false },
};

const el = {
  list: () => document.getElementById("messages"),
  input: () => document.getElementById("chat-input"),
  form: () => document.getElementById("chat-form"),
  send: () => document.getElementById("btn-send"),
  stop: () => document.getElementById("btn-stop"),
  hint: () => document.getElementById("chat-hint"),
  side: () => document.getElementById("chat-list"),
  newBtn: () => document.getElementById("btn-new-chat"),
  showDel: () => document.getElementById("show-deleted-chats"),
};

function bubble(role, text = "") {
  const wrap = document.createElement("div");
  wrap.className = `msg ${role}`;
  const b = document.createElement("div");
  b.className = "bubble";
  b.textContent = text;
  wrap.appendChild(b);
  el.list().appendChild(wrap);
  el.list().scrollTop = el.list().scrollHeight;
  return b;
}

function clearBubbles() {
  el.list().innerHTML = "";
}

function setHint(text, isErr = false) {
  el.hint().textContent = text || "";
  el.hint().className = `hint${isErr ? " err" : ""}`;
}

function setBusy(busy) {
  state.streaming = busy;
  syncSend();
  el.stop().hidden = !busy;
  renderList();                       // 生成中列表置灰，语义是“先停再切”
}

// 发送按钮的可用性只有一个来源：没有正在生成 + 对话模型是开着的
// 「开不起来「已关闭」是两件事」：want 为真但还没 ready = 正在加载，那 10–20 秒不该告诉用户
// 「模型已关闭」（否则用户去点顶栏启动，其实已经在起了）。
function syncSend() {
  const off = !state.modelReady;
  const loading = off && state.modelWant;
  el.send().disabled = state.streaming || off;
  el.input().placeholder = loading
    ? "对话模型正在启动，通常十几秒…"
    : (off ? "对话模型已关闭，去「运行」页启动" : "发消息或创建任务，/ 调用指令，@ 文件或对话");
  const pill = document.getElementById("chat-model-pill");
  if (pill) {
    pill.textContent = loading ? "启动中" : (off ? "已关闭" : "对话就绪");
    pill.className = `pill ${off ? "warn" : "ok"}`;
  }
}

function closeModelMenu() {
  const menu = document.getElementById("chat-model-menu");
  const tag = document.getElementById("chat-model-tag");
  if (menu) menu.hidden = true;
  if (tag) tag.setAttribute("aria-expanded", "false");
}

function paintChatModel(label) {
  const tag = document.getElementById("chat-model-tag");
  if (tag) tag.textContent = shortLabel(label);
}

async function openModelMenu() {
  const menu = document.getElementById("chat-model-menu");
  const options = document.getElementById("chat-model-options");
  const tag = document.getElementById("chat-model-tag");
  if (!menu || !options) return;
  try {
    const data = await loadCatalog();
    const chat = data.chat || [];
    const selected = data.selected?.chat?.path || "";
    if (!chat.length) {
      options.innerHTML = `<div class="empty">models 目录里没找到对话模型</div>`;
    } else {
      options.innerHTML = chat.map((m) => {
        const on = m.path === selected;
        return `<button type="button" data-path="${m.path.replace(/"/g, "&quot;")}" data-label="${String(m.label).replace(/"/g, "&quot;")}"${on ? ' class="on"' : ""}>
          <span class="check">${on ? "✓" : ""}</span><span>${m.label}</span>
        </button>`;
      }).join("");
    }
    paintChatModel(data.selected?.chat?.label || "");
    menu.hidden = false;
    tag?.setAttribute("aria-expanded", "true");
  } catch (e) {
    options.innerHTML = `<div class="empty">读取模型目录失败</div>`;
    menu.hidden = false;
    tag?.setAttribute("aria-expanded", "true");
  }
}

function initModelPicker() {
  const tag = document.getElementById("chat-model-tag");
  const menu = document.getElementById("chat-model-menu");
  const options = document.getElementById("chat-model-options");
  if (!tag || !menu || !options) return;

  loadCatalog().then((data) => paintChatModel(data.selected?.chat?.label || "")).catch(() => {});

  tag.addEventListener("click", (e) => {
    e.stopPropagation();
    if (menu.hidden) openModelMenu(); else closeModelMenu();
  });
  menu.addEventListener("click", (e) => e.stopPropagation());
  options.addEventListener("click", async (e) => {
    const btn = e.target.closest("button[data-path]");
    if (!btn) return;
    if (state.streaming) {
      setHint("正在生成回复，先点「停止」再换模型", true);
      closeModelMenu();
      return;
    }
    const path = btn.dataset.path;
    const label = btn.dataset.label || "";
    closeModelMenu();
    tag.textContent = "切换中…";
    setHint("正在切换对话模型，服务若已启动会自动重启（约 20s）…");
    try {
      const r = await chooseModel("chat", path);
      paintChatModel(label);
      setHint(r.restarted ? "模型已切换，对话服务已重启" : "模型已切换，下次启动对话模型时生效");
    } catch (err) {
      setHint(human(err), true);
      loadCatalog().then((data) => paintChatModel(data.selected?.chat?.label || "")).catch(() => {});
    }
  });
  document.addEventListener("click", closeModelMenu);
}

function timeOf(ts) {
  const d = new Date(typeof ts === "number" ? ts * 1000 : ts);
  if (Number.isNaN(d.getTime())) return "";
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

// —— 会话列表 ——
function renderList() {
  const box = el.side();
  box.innerHTML = "";
  const q = String(document.getElementById("chat-search")?.value || "").trim().toLowerCase();
  const visible = q
    ? state.items.filter((it) => String(it.title || "").toLowerCase().includes(q))
    : state.items;
  if (!state.items.length) {
    const empty = document.createElement("div");
    empty.className = "side-empty";
    empty.textContent = "还没有会话";
    box.appendChild(empty);
    return;
  }
  if (!visible.length) {
    const empty = document.createElement("div");
    empty.className = "side-empty";
    empty.textContent = `没有标题含「${q}」的会话`;
    box.appendChild(empty);
    return;
  }
  for (const it of visible) {
    const row = document.createElement("div");
    row.className = `chat-row${it.id === state.chatId ? " active" : ""}${it.deleted ? " deleted" : ""}`;

    const main = document.createElement("button");
    main.className = "chat-item";
    main.dataset.id = it.id;
    const t = document.createElement("span");
    t.className = "ci-title";
    t.textContent = it.title;
    const m = document.createElement("span");
    m.className = "ci-meta";
    if (it.deleted) {
      // 标签放 meta 行最前面：标题行要给名字让位，放后面会被 ellipsis 吃掉
      const tag = document.createElement("span");
      tag.className = "tag-deleted";
      tag.textContent = "已删除";
      m.appendChild(tag);
    }
    m.appendChild(document.createTextNode(`${it.messages} 条 · ${timeOf(it.updated_at)}`));
    main.append(t, m);
    if (state.streaming && it.id !== state.chatId) {
      main.classList.add("locked");
      main.setAttribute("aria-disabled", "true");
      main.title = "当前消息生成中，先点「停止」再切换";
    }
    main.addEventListener("click", () => {
      if (state.streaming) {
        setHint("当前消息生成中：先点「停止」，再切换会话。", true);
        return;
      }
      if (it.deleted) {
        setHint("这是已删除的会话，点「恢复」后可继续。", true);
        return;
      }
      switchTo(it.id);
    });
    row.appendChild(main);

    const actions = document.createElement("div");
    actions.className = "ci-actions";
    if (it.deleted) {
      const rest = document.createElement("button");
      rest.className = "link-btn";
      rest.textContent = "恢复";
      rest.addEventListener("click", async () => {
        try {
          await api.restoreChat(it.id);
          await refreshChats();
          switchTo(it.id);
        } catch (e) { setHint(human(e), true); }
      });
      actions.appendChild(rest);
      actions.appendChild(purgeChatButton(it));
    } else {
      const rn = document.createElement("button");
      rn.className = "link-btn";
      rn.textContent = "改名";
      rn.addEventListener("click", () => startRename(it, t));
      const del = document.createElement("button");
      del.className = "link-btn danger-text";
      del.textContent = "删除";
      del.addEventListener("click", () => removeChat(it));
      const ex = document.createElement("button");
      ex.className = "link-btn";
      ex.textContent = "导出";
      ex.title = "导出为 Markdown，存到 chats/exports";
      ex.addEventListener("click", () => exportChat(it));
      actions.append(rn, ex, del);
    }
    row.appendChild(actions);
    box.appendChild(row);
  }

  // 整批清空会话回收站：只在勾了"显示已删除"且确实有已删除项时出现
  const trashed = state.items.filter((i) => i.deleted).length;
  if (trashed) {
    const bar = document.createElement("div");
    bar.className = "row-acts";
    bar.style.marginTop = "10px";
    bar.appendChild(purgeAllChatsButton(trashed));
    box.appendChild(bar);
  }
}

async function refreshChats() {
  try {
    const { items } = await api.chats(state.showDeleted);
    state.items = items;
  } catch { /* 网络抖动保留旧列表 */ }
  renderList();
}

async function exportChat(it) {
  try {
    const r = await api.exportChat(it.id);
    setHint(`已导出「${it.title}」→ ${r.name}（在 chats/exports 目录）`, false);
  } catch (e) {
    setHint(human(e), true);
  }
}

function startRename(it, titleEl) {
  const input = document.createElement("input");
  input.className = "rename-input";
  input.value = it.title;
  input.maxLength = 40;
  titleEl.replaceWith(input);
  input.focus();
  input.select();
  let done = false;
  const finish = async (save) => {
    if (done) return;
    done = true;
    if (!save || input.value.trim() === it.title) { await refreshChats(); return; }
    try {
      await api.renameChat(it.id, input.value.trim());
    } catch (e) { setHint(human(e), true); }
    await refreshChats();
  };
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); finish(true); }
    if (e.key === "Escape") { e.preventDefault(); finish(false); }
  });
  input.addEventListener("blur", () => finish(true));
}

async function removeChat(it) {
  if (state.streaming) { setHint("生成中不能删除会话，先点「停止」。", true); return; }
  if (!confirm(`删除这个会话？\n${it.title}\n\n软删除：挪到 chats/_trash，可恢复。`)) return;
  try {
    await api.deleteChat(it.id);
    if (it.id === state.chatId) {                   // 删掉的是当前会话 → 回到空白新对话
      state.chatId = null;
      state.messages = [];
      clearBubbles();
      setHint("当前会话已删除（可勾「显示已删除」再恢复）");
    }
    await refreshChats();
  } catch (e) { setHint(human(e), true); }
}

// 彻底删除会话（回收站的真出口）。两段式确认，不用原生 confirm
function purgeAllChatsButton(count) {
  const b = document.createElement("button");
  b.className = "link-btn danger";
  b.textContent = `清空会话回收站（${count} 条）`;
  let timer = null;
  b.addEventListener("click", async () => {
    if (!timer) {
      b.textContent = "再点一次确认（不可恢复）";
      b.classList.add("armed");
      timer = setTimeout(() => {
        b.textContent = `清空会话回收站（${count} 条）`;
        b.classList.remove("armed");
        timer = null;
      }, 4000);
      return;
    }
    clearTimeout(timer);
    timer = null;
    try {
      const r = await api.purgeAllChats();
      setHint(`已清空会话回收站，移除 ${r.count} 条`);
      await refreshChats();
    } catch (e) {
      setHint(human(e), true);
      b.textContent = `清空会话回收站（${count} 条）`;
      b.classList.remove("armed");
    }
  });
  return b;
}

// 彻底删除会话（回收站的真出口）。两段式确认，不用原生 confirm
function purgeChatButton(it) {
  const b = document.createElement("button");
  b.className = "link-btn danger";
  b.textContent = "彻底删除";
  let timer = null;
  b.addEventListener("click", async () => {
    if (!timer) {
      b.textContent = "再点一次确认";
      b.classList.add("armed");
      timer = setTimeout(() => {
        b.textContent = "彻底删除";
        b.classList.remove("armed");
        timer = null;
      }, 4000);
      return;
    }
    clearTimeout(timer);
    timer = null;
    try {
      await api.purgeChat(it.id);
      setHint(`已彻底删除会话「${it.title}」（文件已从 chats/_trash 移除）`);
      await refreshChats();
    } catch (e) {
      setHint(human(e), true);
      b.textContent = "彻底删除";
      b.classList.remove("armed");
    }
  });
  return b;
}

async function switchTo(id) {
  if (state.streaming) { setHint("生成中不能切换，先点「停止」。", true); return; }
  try {
    const doc = await api.chat(id);
    state.chatId = doc.id;
    state.messages = doc.messages || [];
    state.settings = {
      system: doc.settings?.system || "",
      params: doc.settings?.params || {},
    };
    state.attachments = [];
    renderAttachments();
    clearBubbles();
    state.messages.forEach((m) => bubble(m.role === "user" ? "user" : "assistant", m.content));
    setHint(`已切到「${doc.title}」（${state.messages.length} 条消息）`);
    renderList();
  } catch (e) { setHint(human(e), true); }
}

function newChat() {
  if (state.streaming) { setHint("生成中不能新建会话，先点「停止」。", true); return; }
  state.chatId = null;
  state.messages = [];
  state.settings = { system: "", params: {} };
  state.attachments = [];
  renderAttachments();
  clearBubbles();
  setHint("新对话：发第一条消息后自动保存");
  renderList();
  el.input().focus();
}

// —— 发送 ——
// —— 发送前的上下文拼装：系统提示词 + 工具事实 + 附件正文 ——
function nowText() {
  return new Date().toLocaleString("zh-CN", { hour12: false, weekday: "long" });
}

function calcFacts(text) {
  // 只认纯算术片段，用 Function 也只会碰到数字和四则运算符，不执行用户代码
  const out = [];
  const re = /(\d[\d\s.+\-*/%()]{3,}\d)/g;
  let m;
  while ((m = re.exec(text))) {
    const expr = m[1].trim();
    if (!/[+\-*/%]/.test(expr)) continue;
    try {
      const v = Function(`"use strict";return (${expr})`)();
      if (typeof v === "number" && Number.isFinite(v)) out.push(`${expr} = ${v}`);
    } catch { /* 不是合法算式就跳过 */ }
    if (out.length >= 3) break;
  }
  return out;
}

function apiMessages(userText) {
  const msgs = state.messages.slice(0, -1).map((m) => ({ role: m.role, content: m.content }));
  const sys = [];
  if (state.settings.system.trim()) sys.push(state.settings.system.trim());
  if (state.tools.master && state.tools.time) sys.push(`当前本机时间：${nowText()}。回答时间问题以此为准。`);
  if (state.tools.master && state.tools.calc) {
    const facts = calcFacts(userText);
    if (facts.length) sys.push(`本地计算器已算好：${facts.join("；")}。直接使用这个结果，不要重算。`);
  }
  if (sys.length) msgs.unshift({ role: "system", content: sys.join("\n\n") });

  let content = userText;
  if (state.attachments.length) {
    const parts = state.attachments.map((a) => {
      if (a.kind === "text" && a.text) return `【附件 ${a.name}】\n${a.text}`;
      return `【附件 ${a.name}】${a.kind === "image" ? "（图片已保存在本机，当前对话模型不读图）" : "（二进制文件，未读取内容）"}`;
    });
    content = `${parts.join("\n\n")}\n\n${userText}`;
  }
  msgs.push({ role: "user", content });
  return msgs;
}

// —— 附件 ——
function renderAttachments() {
  const box = document.getElementById("chat-attach-chips");
  if (!box) return;
  box.innerHTML = "";
  for (const a of state.attachments) {
    const chip = document.createElement("span");
    chip.className = "attach-chip";
    if (a.kind === "image") {
      const img = document.createElement("img");
      img.src = a.url;
      chip.appendChild(img);
    }
    const nm = document.createElement("span");
    nm.className = "nm";
    nm.textContent = a.name;
    chip.appendChild(nm);
    const x = document.createElement("button");
    x.type = "button";
    x.textContent = "×";
    x.title = "移除";
    x.addEventListener("click", () => {
      state.attachments = state.attachments.filter((it) => it.id !== a.id);
      renderAttachments();
    });
    chip.appendChild(x);
    box.appendChild(chip);
  }
}

async function uploadFiles(files) {
  for (const f of files) {
    if (f.size > 10 * 1024 * 1024) { setHint(`${f.name} 超过 10 MB，没上传`, true); continue; }
    const data = await new Promise((resolve, reject) => {
      const r = new FileReader();
      r.onload = () => resolve(String(r.result || ""));
      r.onerror = () => reject(r.error);
      r.readAsDataURL(f);
    });
    try {
      const r = await api.uploadAttachment(f.name, data);
      state.attachments.push(r);
      renderAttachments();
      setHint(`已添加附件 ${r.name}`);
    } catch (e) {
      setHint(human(e), true);
    }
  }
}

// —— 工具面板 ——
const TOOL_ROWS = [
  ["time", "◷", "当前时间", "回答今天几号、现在几点", true],
  ["calc", "=", "计算器", "先在本机算好算式，再让模型解释", true],
  ["file", "▤", "读取文件", "读取「灵枢」目录里的文本文件（最多 4000 字）", true],
  ["folder", "▣", "查看文件夹", "列出「灵枢」目录里的文件和子目录", true],
  ["write", "✎", "写入文件", "在「灵枢」目录里新建/覆盖文本文件", true],
  ["web", "◎", "联网搜索", "按关键词联网搜索网页摘要", true],
  ["page", "▭", "读取网页", "读取 http/https 网页正文（最多 4000 字）", true],
  ["shell", ">_", "执行命令", "本机完全访问下风险太高，不开放", false],
];

function renderTools() {
  const box = document.getElementById("tools-list");
  box.innerHTML = "";
  for (const [key, ic, title, desc, usable] of TOOL_ROWS) {
    const row = document.createElement("label");
    row.className = `tool-row${usable ? "" : " disabled"}`;
    row.innerHTML = `<span class="ic">${ic}</span><span class="tx"><b>${title}</b><span>${desc}</span></span>`;
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.disabled = !usable || !state.tools.master;
    cb.checked = usable && !!state.tools[key];
    cb.addEventListener("change", () => { state.tools[key] = cb.checked; });
    row.appendChild(cb);
    box.appendChild(row);
  }
  document.getElementById("tools-master").checked = state.tools.master;
  document.getElementById("tools-note").textContent = state.tools.master
    ? "当前可用：当前时间、计算器、读取文件、查看文件夹、写入文件、联网搜索、读取网页。执行命令不开放。"
    : "总开关已关：模型不会拿到任何工具信息。";
}

function openTools() {
  const pop = document.getElementById("tools-popover");
  renderTools();
  const btn = document.getElementById("btn-tools");
  const r = btn.getBoundingClientRect();
  pop.hidden = false;
  const h = pop.offsetHeight;
  pop.style.left = `${Math.min(r.left, window.innerWidth - 360)}px`;
  pop.style.top = `${Math.max(8, r.top - h - 8)}px`;
  btn.classList.add("on");
}

function closeTools() {
  document.getElementById("tools-popover").hidden = true;
  document.getElementById("btn-tools")?.classList.remove("on");
}

// —— 对话设置 ——
function renderChatSettings() {
  const grid = document.getElementById("cs-params");
  if (!grid.childElementCount) {
    for (const [key, label, desc, range, ph] of PARAM_SPECS) {
      const div = document.createElement("div");
      div.className = "field";
      div.innerHTML = `<div class="field-head"><label>${label}</label><span class="muted small">${range}</span></div>`;
      const input = document.createElement("input");
      input.dataset.param = key;
      input.placeholder = `默认${ph ? `（${ph}）` : ""}`;
      input.value = state.settings.params[key] ?? "";
      if (key === "max_tokens" || key === "top_k" || key === "seed") input.type = "number";
      div.appendChild(input);
      const note = document.createElement("div");
      note.className = "muted small";
      note.textContent = desc;
      div.appendChild(note);
      grid.appendChild(div);
    }
  } else {
    for (const input of grid.querySelectorAll("input[data-param]")) {
      input.value = state.settings.params[input.dataset.param] ?? "";
    }
  }
  document.getElementById("cs-system").value = state.settings.system || "";
}

function openChatSettings() {
  renderChatSettings();
  document.getElementById("chat-settings-scrim").hidden = false;
  document.getElementById("chat-settings-modal").hidden = false;
}

function closeChatSettings() {
  document.getElementById("chat-settings-scrim").hidden = true;
  document.getElementById("chat-settings-modal").hidden = true;
}

function openChatParams() {
  closeChatSettings();
  closeTools();
  closeModelMenu();
  document.getElementById("chat-params-scrim").hidden = false;
  document.getElementById("chat-params-modal").hidden = false;
  window.dispatchEvent(new CustomEvent("lingshu:params"));
}

function closeChatParams() {
  document.getElementById("chat-params-scrim").hidden = true;
  document.getElementById("chat-params-modal").hidden = true;
}

async function saveChatSettings() {
  const system = document.getElementById("cs-system").value.trim();
  const params = {};
  for (const input of document.querySelectorAll("#cs-params input[data-param]")) {
    const v = input.value.trim();
    if (v === "") continue;
    const n = Number(v);
    if (Number.isFinite(n)) params[input.dataset.param] = n;
  }
  state.settings = { system, params };
  closeChatSettings();
  if (state.chatId) {
    try {
      await api.saveChatSettings(state.chatId, state.settings);
      setHint("对话设置已保存到这个会话");
    } catch (e) {
      setHint(`设置保存失败：${human(e)}`, true);
    }
  } else {
    setHint("对话设置已生效；发第一条消息后跟着会话一起保存");
  }
}

async function send(text) {
  if (!text.trim() || state.streaming) return;
  if (!state.modelReady) {
    setHint(
      state.modelWant
        ? "对话模型还在启动，稍等十几秒再发"
        : "对话模型已关闭，去「设置」页点「启动对话模型」",
      true,
    );
    return;
  }
  state.messages.push({ role: "user", content: text });
  bubble("user", text);
  el.input().value = "";
  const sentAttachments = state.attachments;
  state.attachments = [];
  renderAttachments();
  setBusy(true);
  setHint("");

  const reply = bubble("assistant", "");
  let reasoningEl = null;
  let got = "";
  const controller = new AbortController();
  state.controller = controller;

  try {
    const payload = apiMessages(text);
    const anyTools = state.tools.master && (state.tools.file || state.tools.folder || state.tools.write || state.tools.web || state.tools.page);
    if (anyTools) {
      const flags = {
        file: !!state.tools.file, folder: !!state.tools.folder, write: !!state.tools.write,
        web: !!state.tools.web, page: !!state.tools.page,
      };
      const r = await api.chatTools({
        messages: payload,
        tools: flags,
        params: state.settings.params,
        max_tokens: state.settings.params.max_tokens,
      });
      got = r.content || "";
      reply.textContent = got || "（模型这次没有返回文字）";
      state.messages.push({ role: "assistant", content: got });
      const names = (r.tool_trace || []).map((t) => t.name).filter(Boolean);
      setHint(names.length ? `已调用工具：${names.join("、")}` : "完成（工具已开启）");
      persist();
      setBusy(false);
      el.list().scrollTop = el.list().scrollHeight;
      return;
    }
    const resp = await api.chatStream(payload, controller.signal, state.settings.params);
    if (resp.status === 503) {
      const body = await resp.json().catch(() => ({}));
      state.messages.pop();                    // 没发出去，撤回这轮
      reply.parentElement?.remove();
      state.attachments = sentAttachments;
      renderAttachments();
      if (body.error === "chat_stopped") {
        setHint(ERROR_TEXT.chat_stopped, true);
        setBusy(false);
        return;
      }
      state.pending = text;
      setHint(body.state === "RELOADING"
        ? "模型正在重载（刚出完图/刚被停止），消息已排队，就绪后自动发出"
        : ERROR_TEXT.gpu_busy + "（已排队）");
      watchReady();
      setBusy(false);
      return;
    }
    await streamSSE(resp, ({ data }) => {
      if (data.type === "reasoning") {
        if (!reasoningEl) {
          reasoningEl = document.createElement("div");
          reasoningEl.className = "reasoning";
          reply.prepend(reasoningEl);
        }
        reasoningEl.textContent += data.text;
      } else if (data.type === "content") {
        got += data.text;
        reply.textContent = got;
      } else if (data.type === "done") {
        state.messages.push({ role: "assistant", content: got });
        if (data.degraded === "thinking_truncated") {
          setHint(`回复被截断（思考吃满 ${data.max_tokens} tokens，这里回填的是思考内容，不是正式回复）`, true);
        } else if (data.degraded === "truncated") {
          setHint(`回答没写完就停了（上限 ${data.max_tokens} tokens）——发「继续」让它接着写`, true);
        } else {
          setHint(`完成 · ${data.finish_reason || "stop"} · 上限 ${data.max_tokens} tokens`);
        }
        persist();
      } else if (data.type === "error") {
        setHint(`出错：${data.error}`, true);
      }
    });
    el.list().scrollTop = el.list().scrollHeight;
  } catch (e) {
    if (e.name === "AbortError") {
      if (got) {
        state.messages.push({ role: "assistant", content: `${got}\n\n（已停止）` });
        reply.textContent = got;
      }
      await new Promise((r) => setTimeout(r, 1200));
      for (let i = 0; i < 3; i++) {
        if (await probeIdle()) break;
        await new Promise((r) => setTimeout(r, 500));
      }
      setHint("已停止生成（可以切换会话了）");
      persist();
      return;
    }
    setHint(`请求失败：${e.message}`, true);
  } finally {
    state.controller = null;
    setBusy(false);
  }
}

function stop() {
  if (!state.controller) return;
  state.controller.abort();
  setHint("停止中…（等 llama 释放这一路）");
}

// 读 llama 的真实忙数（不是猜时长）。探测失败返回 false 交给下一轮，最多 3 轮后放行。
async function probeIdle() {
  try {
    const r = await api.chatIdle();
    return r.idle === true;
  } catch {
    return false;
  }
}

// 落盘：每条回复结束后写一次（失败不影响对话，只提示一句）
async function persist() {
  if (!state.messages.length) return;
  try {
    const r = await api.saveChat({ id: state.chatId, messages: state.messages, settings: state.settings });
    state.chatId = r.id;
  } catch {
    setHint("聊天记录保存失败（对话仍可继续）", true);
  }
  await refreshChats();
}

// 打开页面：列表 + 默认打开最近一次会话（不再只认 items[0] 却不给切换入口）
async function restore() {
  await refreshChats();
  const first = state.items.find((it) => !it.deleted);
  if (first) await switchTo(first.id);
  else setHint("输入内容开始对话；出图中会自动排队。");
}

// 503 后：轮询 /api/status，等 CHAT_READY 自动把暂存消息发出去
function watchReady() {
  clearInterval(state.pollTimer);
  state.pollTimer = setInterval(async () => {
    try {
      const st = await api.status();
      if (st.state === "CHAT_READY" && state.pending) {
        const text = state.pending;
        state.pending = null;
        clearInterval(state.pollTimer);
        state.pollTimer = null;
        setHint("对话模型已就绪，自动发送…");
        send(text);
      }
    } catch { /* 网络抖动继续等 */ }
  }, 1500);
}

export function initChat() {
  el.form().addEventListener("submit", (e) => {
    e.preventDefault();
    send(el.input().value);
  });
  el.input().addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      send(el.input().value);
    }
  });
  el.newBtn().addEventListener("click", newChat);
  el.stop().addEventListener("click", stop);
  el.showDel().addEventListener("change", async (e) => {
    state.showDeleted = e.target.checked;
    await refreshChats();
  });
  // 标题搜索：只过滤当前已加载的列表，不重新拉接口（会话就那几十条，够用且不打服务器）
  document.getElementById("chat-search")?.addEventListener("input", () => renderList());

  document.getElementById("btn-attach").addEventListener("click", () => {
    document.getElementById("chat-file").click();
  });
  document.getElementById("chat-file").addEventListener("change", async (e) => {
    await uploadFiles([...e.target.files]);
    e.target.value = "";
  });
  document.getElementById("btn-tools").addEventListener("click", (e) => {
    e.stopPropagation();
    if (document.getElementById("tools-popover").hidden) openTools(); else closeTools();
  });
  document.getElementById("tools-master").addEventListener("change", (e) => {
    state.tools.master = e.target.checked;
    renderTools();
  });
  document.getElementById("tools-popover").addEventListener("click", (e) => e.stopPropagation());
  document.addEventListener("click", () => closeTools());

  document.getElementById("btn-chat-settings").addEventListener("click", openChatSettings);
  document.getElementById("btn-chat-params").addEventListener("click", openChatParams);
  document.getElementById("btn-close-chat-params").addEventListener("click", closeChatParams);
  document.getElementById("btn-chat-params-close").addEventListener("click", closeChatParams);
  document.getElementById("chat-params-scrim").addEventListener("click", closeChatParams);
  document.getElementById("btn-close-chat-settings").addEventListener("click", closeChatSettings);
  document.getElementById("cs-cancel").addEventListener("click", closeChatSettings);
  document.getElementById("chat-settings-scrim").addEventListener("click", closeChatSettings);
  document.getElementById("cs-reset").addEventListener("click", () => {
    state.settings = { system: "", params: {} };
    document.getElementById("cs-system").value = "";
    for (const input of document.querySelectorAll("#cs-params input[data-param]")) input.value = "";
    setHint("已恢复默认（点保存才会写入会话）");
  });
  document.getElementById("cs-save").addEventListener("click", saveChatSettings);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { closeChatSettings(); closeChatParams(); closeTools(); closeModelMenu(); }
  });
  window.addEventListener("lingshu:page", (e) => {
    if (e.detail.name !== "chat") { closeChatSettings(); closeChatParams(); closeTools(); closeModelMenu(); }
  });
  window.addEventListener("lingshu:model", () => {
    loadCatalog().then((data) => paintChatModel(data.selected?.chat?.label || "")).catch(() => {});
  });

  initModelPicker();
  renderAttachments();
  setHint("输入内容开始对话；出图中会自动排队。");
  syncSend();
  restore();
}

// 顶栏开关把它调过来：模型关掉时按钮和输入框都要立刻反映，而不是等下一次点击才失败
// want=true 但 ready=false 是「正在加载」，不能报成「已关闭」
export function setModelReady(ok, want = true, announce = true) {
  const next = !!ok;
  const wantNext = !!want;
  const loading = !next && wantNext;
  if (next === state.modelReady && wantNext === state.modelWant) return;
  state.modelReady = next;
  state.modelWant = wantNext;
  syncSend();
  if (!announce) return;             // 首屏同步：只对齐状态，不往提示区塞话
  setHint(
    next ? "对话模型已启动" : (loading ? "对话模型正在启动…" : "对话模型已关闭，要用先在顶栏启动"),
    !next && !loading,
  );
}

export function chatBusy() {
  return state.streaming;
}

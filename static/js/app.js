// 入口：五个页面的 hash 路由 + 顶部导航 + 全局状态 + 服务启停
// 页面各自 init 一次；状态通过 window 事件广播，避免每个模块自己轮询 /api/status。
import { api, human } from "./api.js?v=36";
import { initChat, chatBusy, setModelReady as setChatModelReady } from "./chat.js?v=44";
import { initDraw, setModelReady as setDrawModelReady } from "./draw.js?v=43";
import { initCanvas } from "./canvas.js?v=35";
import { initRun } from "./run.js?v=18";
import { initAgent } from "./agent.js?v=10";
import { initSettingsPage } from "./settings-page.js?v=9";

const THEME_KEY = "lingshu-theme";
function applyTheme(mode, persist = true) {
  const dark = mode === "dark";
  document.documentElement.dataset.theme = dark ? "dark" : "light";
  const light = document.getElementById("theme-light");
  if (light) light.disabled = dark;
  const sel = document.getElementById("set-theme");
  if (sel) sel.value = dark ? "dark" : "light";
  if (persist) {
    try { localStorage.setItem(THEME_KEY, dark ? "dark" : "light"); } catch (e) {}
  }
}
try { applyTheme(localStorage.getItem(THEME_KEY) || "light", false); } catch (e) { applyTheme("light", false); }
document.getElementById("set-theme")?.addEventListener("change", (e) => applyTheme(e.target.value));


const PAGES = {
  run: initRun,
  chat: initChat,
  agent: initAgent,
  draw: () => { initDraw(); initCanvas(); },
  settings: initSettingsPage,
};
const inited = new Set();
const models = { chat: null, sd: null };
const toggling = { chat: false, sd: false };
let activePage = "run";
let notice = { text: "", at: 0, err: false };

const STATE_TEXT = {
  CHAT_READY: "对话就绪",
  PREPARING: "准备中（卸载对话模型）",
  GENERATING: "出图中",
  RELOADING: "正在恢复对话模型",
  DEGRADED: "出图服务异常，恢复中",
  FAILED: "故障，需重启 gateway",
};

function setNotice(text, err = false) {
  notice = { text, at: Date.now(), err };
  paintNotice();
}

function paintNotice() {
  const box = document.getElementById("nav-vram");
  if (!box) return;
  const fresh = notice.text && Date.now() - notice.at < 4000;
  box.textContent = fresh ? notice.text : (box.dataset.live || "");
  box.classList.toggle("err", !!fresh && notice.err);
}

function route() {
  // 概览/监测已合并进「运行」；旧的 #/overview、#/monitor 链接自动落到运行页。
  const raw = location.hash.replace(/^#\/?/, "") || "run";
  const alias = { overview: "run", monitor: "run" }[raw] || raw;
  const name = PAGES[alias] ? alias : "run";
  if (raw !== name && location.hash !== `#/${name}`) {
    history.replaceState(null, "", `#/${name}`);
  }
  activePage = name;
  Object.keys(PAGES).forEach((k) => {
    const sec = document.getElementById(`page-${k}`);
    if (sec) sec.hidden = k !== name;
  });
  [...document.querySelectorAll(".nav-item")].forEach((a) => {
    a.classList.toggle("active", a.dataset.page === name);
  });
  if (!inited.has(name)) {
    inited.add(name);
    PAGES[name]();
  }
  window.dispatchEvent(new CustomEvent("lingshu:page", { detail: { name } }));
}

function renderModelButtons() {
  // 各页面的按钮按状态改字：开着 = 关闭，关着 = 启动，want 但没 ready = 启动中
  const pairs = [
    ["run-chat-toggle", "chat", "对话模型"],
    ["run-sd-toggle", "sd", "画图模型"],
  ];
  for (const [id, key, label] of pairs) {
    const btn = document.getElementById(id);
    if (!btn) continue;
    const svc = models[key];
    const ready = !!svc?.ready;
    const loading = !ready && (!svc || !!svc.want);
    const busy = toggling[key];
    btn.disabled = busy || loading || (ready && key === "chat" && chatBusy());
    btn.textContent = busy ? `${label}启停中…` : (loading ? `${label}启动中…` : (ready ? `关闭${label}` : `启动${label}`));
    btn.classList.toggle("stopped", !ready && !loading);
  }
  const sdReady = document.getElementById("run-sd-ready");
  if (sdReady) {
    const ready = !!models.sd?.ready;
    const loading = !ready && !!models.sd?.want;
    sdReady.textContent = ready ? "就绪" : (loading ? "启动中" : "已关闭");
    sdReady.className = `pill ${ready ? "ok" : "warn"}`;
  }
  const all = document.getElementById("run-start-all");
  if (all) {
    const anyOff = !models.chat?.ready || !models.sd?.ready;
    all.textContent = anyOff ? "启动全部模型" : "全部已启动";
    all.disabled = !anyOff;
  }
}

async function toggleModel(key) {
  if (toggling[key]) return;
  const wasReady = !!models[key]?.ready;
  toggling[key] = true;
  renderModelButtons();
  setNotice(wasReady ? `${key === "chat" ? "对话" : "出图"}模型关闭中…` : `正在启动${key === "chat" ? "对话" : "出图"}模型…`);
  try {
    const r = wasReady ? await api.stopService(key) : await api.startService(key);
    if (r?.items?.[key]) models[key] = r.items[key];
    syncModelFlags(true);
    setNotice(wasReady ? "已关闭（端口已释放）" : "已启动（端口已打开）");
  } catch (e) {
    setNotice(human(e), true);
  } finally {
    toggling[key] = false;
    renderModelButtons();
    pollStatus();
  }
}

function syncModelFlags(announce = false) {
  setChatModelReady(!!models.chat?.ready, !!models.chat?.want, announce && false);
  setDrawModelReady(!!models.sd?.ready, !!models.sd?.want, false);
}

async function pollStatus() {
  const dot = document.getElementById("nav-dot");
  const text = document.getElementById("nav-state");
  const vram = document.getElementById("nav-vram");
  try {
    const st = await api.status();
    if (st.state === "CHAT_READY") dot.className = st.ready?.chat ? "dot ok" : "dot";
    else if (["GENERATING", "PREPARING", "RELOADING"].includes(st.state)) dot.className = "dot busy";
    else dot.className = "dot err";
    // 就绪时顶栏只留一个绿点：正常状态不需要占一行字，只有"没就绪/出错"才说话
    const ready = st.state === "CHAT_READY" && !!st.ready?.chat;
    text.textContent = ready
      ? ""
      : (st.state === "CHAT_READY"
          ? (st.services?.chat?.want === false ? "对话已关闭" : "对话加载中")
          : (STATE_TEXT[st.state] || st.state));
    const parts = [];
    if (st.progress && st.state === "GENERATING") parts.push(`采样 ${st.progress.step}/${st.progress.total}`);
    if (st.queue?.depth) parts.push(`排队 ${st.queue.depth}`);
    if (st.vram_guard_tripped) parts.push("显存护栏触发");
    vram.dataset.live = parts.join(" · ");
    paintNotice();
    if (st.services) {
      models.chat = st.services.chat;
      models.sd = st.services.sd;
      syncModelFlags(false);
    }
    window.dispatchEvent(new CustomEvent("lingshu:status", { detail: st }));
    renderModelButtons();
  } catch {
    dot.className = "dot err";
    text.textContent = "gateway 未连接";
    vram.dataset.live = "";
    paintNotice();
  }
}

window.addEventListener("hashchange", route);
window.addEventListener("DOMContentLoaded", () => {
  route();
  pollStatus();
  setInterval(pollStatus, 3000);
  document.getElementById("run-chat-toggle")?.addEventListener("click", () => toggleModel("chat"));
  document.getElementById("run-sd-toggle")?.addEventListener("click", () => toggleModel("sd"));
  document.getElementById("run-start-all")?.addEventListener("click", async () => {
    if (!models.chat?.ready) await toggleModel("chat");
    if (!models.sd?.ready) await toggleModel("sd");
  });
});

export { models, toggleModel };

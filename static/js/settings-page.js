// 设置页：生成参数（后端 FieldSpec 驱动）+ 部署路径 + 引擎/界面/关于
import { api, human } from "./api.js?v=36";

const LABELS = {
  steps: ["步数", "采样步数，8 步是实测下限"],
  max_tokens: ["回复上限", "回复的 token 上限，填多少算多少（实际生成仍受上下文长度限制）"],
  thinking: ["思考模式", "关掉后更快，上限不变"],
  n_ctx: ["上下文长度", "上下文窗口（512–32768）：越大记得越多，KV 缓存也越占显存"],
  vae_tile: ["VAE tile", "VAE 解码时每一块的大小：块小更省显存但更慢，块大更快但更吃显存"],
  batch_count_default: ["默认张数", "1 张最快；2–4 张连续出图，更占显存和时间"],
};

const $ = (id) => document.getElementById(id);
let model = null;

function hwCard(k, v, s = "") {
  return `<div class="ov-card"><div class="k">${k}</div><div class="v" title="${v}">${v}</div>${s ? `<div class="s">${s}</div>` : ""}</div>`;
}

function renderHardware(o) {
  const h = o?.hardware || {};
  const gpu = h.gpu || {};
  const cpu = h.cpu || {};
  const ram = h.ram || {};
  const disk = h.disk || {};
  const live = o?.vram || {};
  $("hw-drawer-cards").innerHTML = [
    hwCard("显卡", gpu.name ? gpu.name.replace("NVIDIA ", "") : "无 NVIDIA 显卡",
      gpu ? `显存 ${Math.round((gpu.vram_mib || 0) / 1024 * 10) / 10} GB · 空闲 ${Math.round((live.gpu_free_mib ?? gpu.free_mib ?? 0) / 1024 * 10) / 10} GB` : ""),
    hwCard("CPU", cpu.name || "—", `${cpu.physical || "?"} 核 / ${cpu.logical || "?"} 线程`),
    hwCard("内存", `${Math.round((ram.total_mib || 0) / 1024 * 10) / 10} GB`, `可用 ${Math.round((live.ram_avail_mib ?? ram.avail_mib ?? 0) / 1024 * 10) / 10} GB`),
    hwCard("磁盘", `${disk.free_gib ?? "?"} GB 可用`, `共 ${disk.total_gib ?? "?"} GB`),
  ].join("");
  const chat = h.chat || h;
  const sd = h.sd || h;
  const guard = h.guard || h;
  const rows = [
    ["对话", `-ngl ${chat.ngl ?? "?"} · 上下文 ${chat.ctx ?? "?"}`],
    ["速度", `线程 ${chat.threads ?? "?"} · 批量 ${chat.batch ?? "?"}/${chat.ubatch ?? "?"}`],
    ["出图", `VAE tile ${sd.vae_tile ?? "?"} · ${sd.steps ?? "?"} 步`],
    ["护栏", `预算 ${guard.budget_mib ?? "?"} MiB · 护栏 ${guard.guard_mib ?? "?"} MiB`],
  ];
  $("hw-drawer-params").innerHTML = rows.map(([k, v]) => `<div class="kv"><span>${k}</span><b>${v}</b></div>`).join("");
  $("hw-drawer-why").textContent = (h.notes && h.notes[0]) || "参数由硬件探测自动决定。";
}

function openHardware() {
  $("hardware-drawer").hidden = false;
  $("hw-scrim").hidden = false;
  api.overview().then(renderHardware).catch((e) => { $("hw-drawer-why").textContent = human(e); });
}

function closeHardware() {
  $("hardware-drawer").hidden = true;
  $("hw-scrim").hidden = true;
}

function field(key, value, spec, rec) {
  const [label, desc] = LABELS[key] || [key, spec?.note || ""];
  const wrap = document.createElement("div");
  wrap.className = key === "thinking" ? "field field-check" : "field";
  const head = document.createElement("div");
  head.className = "field-head";
  const l = document.createElement("label");
  l.textContent = label;
  head.appendChild(l);
  let ctrl;
  if (key === "thinking") {
    ctrl = document.createElement("input");
    ctrl.type = "checkbox";
    ctrl.checked = !!value;
    head.appendChild(ctrl);
  }
  wrap.appendChild(head);
  if (key !== "thinking") {
    ctrl = document.createElement(spec?.allowed?.length ? "select" : "input");
    ctrl.id = `setf-${key}`;
    if (ctrl.tagName === "INPUT") {
      ctrl.type = "number";
      if (spec?.min != null) ctrl.min = spec.min;
      if (spec?.max != null) ctrl.max = spec.max;
      ctrl.value = value;
    } else {
      spec.allowed.forEach((v) => {
        const o = document.createElement("option");
        o.value = v;
        o.textContent = String(v);
        if (v === value) o.selected = true;
        ctrl.appendChild(o);
      });
    }
    wrap.appendChild(ctrl);
  } else {
    ctrl.id = `setf-${key}`;
  }
  const n = document.createElement("div");
  n.className = "muted small";
  n.textContent = desc;
  wrap.appendChild(n);
  if (typeof rec === "number" && key !== "thinking") {
    const line = document.createElement("div");
    line.className = "field-rec";
    const v = document.createElement("span");
    v.textContent = `本机推荐 ${rec}`;
    const use = document.createElement("button");
    use.type = "button";
    use.className = "mini-btn";
    use.textContent = "用推荐";
    use.addEventListener("click", () => { ctrl.value = String(rec); });
    line.append(v, use);
    wrap.appendChild(line);
  }
  return wrap;
}

function render(data) {
  model = data;
  const box = $("settings-fields");
  box.innerHTML = "";
  const rec = data.recommended || {};
  Object.keys(data.values || {}).forEach((key) => {
    box.appendChild(field(key, data.values[key], data.fields?.[key], rec[key]));
  });
  if (data.ctx_estimate?.gpu) {
    const note = document.createElement("div");
    note.className = data.ctx_estimate.fits_guard ? "muted small" : "muted small err";
    note.textContent = `按 ${data.ctx_estimate.ctx} 估算：KV ${data.ctx_estimate.kv_mib} MB，合计 ${data.ctx_estimate.total_mib} MB / 护栏 ${data.ctx_estimate.guard_mib} MB`;
    box.appendChild(note);
  }
  if (data.paths?.values) {
    $("set-output").value = data.paths.values["output.dir"] || "";
    $("set-logs").value = data.paths.values["logs.dir"] || "";
    $("set-chats").value = data.paths.values["chats.dir"] || "";
  }
  $("set-llama").textContent = "随包本地 · 内置";
  $("set-sd").textContent = "随包本地 · 内置";
  fillPorts();
}

// 端口从后端读，不再在 HTML 里写死：改了 config.yaml 的端口，界面还能说真话
async function fillPorts() {
  try {
    const st = await api.status();
    const p = st?.ports || {};
    if ($("set-port-chat")) $("set-port-chat").textContent = `127.0.0.1:${p.chat ?? "—"}`;
    if ($("set-port-sd")) $("set-port-sd").textContent = `127.0.0.1:${p.sd ?? "—"}`;
    if ($("set-port-gateway")) $("set-port-gateway").textContent = `127.0.0.1:${p.gateway ?? "—"}`;
    const conflict = (st?.port_conflicts || [])[0];
    if (conflict && $("set-engine-badge")) {
      $("set-engine-badge").textContent = "端口冲突";
      $("set-engine-badge").title = `端口 ${conflict.port} 被 ${conflict.name}（PID ${conflict.pid}）占用`;
    }
  } catch (_) { /* 网关还没起来时忽略 */ }
}

async function load() {
  try {
    render(await api.settings());
  } catch (e) {
    $("settings-fields").innerHTML = `<div class="hint err">${human(e)}</div>`;
  }
}

async function saveFields() {
  const payload = {};
  Object.keys(model?.values || {}).forEach((key) => {
    const c = $(`setf-${key}`);
    if (!c) return;
    if (key === "thinking") payload[key] = c.checked;
    else if (c.value !== "") payload[key] = Number(c.value);
  });
  try {
    const r = await api.saveSettings(payload);
    const changed = Object.keys(r.changed || []);
    $("set-rec-pill").textContent = changed.length ? `已保存 ${changed.length} 项` : "本机推荐";
    render({ ...model, values: r.values, fields: r.fields, locked: r.locked,
      recommended: r.recommended, ctx_estimate: r.ctx_estimate,
      paths: model.paths });
    if (r.restart_chat_now) $("set-rec-pill").textContent = "设置已保存 · 对话服务重启中";
  } catch (e) {
    $("set-rec-pill").textContent = human(e);
  }
}

async function savePaths() {
  try {
    const r = await api.savePaths({
      "output.dir": $("set-output").value.trim(),
      "logs.dir": $("set-logs").value.trim(),
      "chats.dir": $("set-chats").value.trim(),
    });
    $("set-rec-pill").textContent = r.restart_gateway
      ? "路径已保存 · 重启 gateway 后生效"
      : "路径没有改动";
  } catch (e) {
    $("set-rec-pill").textContent = human(e);
  }
}

export function initSettingsPage() {
  $("set-save").addEventListener("click", saveFields);
  $("set-paths-save").addEventListener("click", savePaths);
  $("set-open-output").addEventListener("click", () => api.openFolder("output").catch(() => {}));
  $("set-open-logs").addEventListener("click", () => api.openFolder("logs").catch(() => {}));
  $("set-open-chats").addEventListener("click", () => api.openFolder("chats").catch(() => {}));
  $("open-hw-drawer").addEventListener("click", openHardware);
  $("btn-close-hw-drawer").addEventListener("click", closeHardware);
  $("hw-scrim").addEventListener("click", closeHardware);
  $("hw-redetect").addEventListener("click", async () => {
    $("hw-drawer-why").textContent = "正在重新检测本机硬件…";
    try {
      await api.redetectHardware();
      renderHardware(await api.overview());
      $("hw-drawer-why").textContent = "已重新检测；推荐参数已按当前硬件刷新。";
    } catch (e) { $("hw-drawer-why").textContent = human(e); }
  });
  window.addEventListener("lingshu:page", (e) => { if (e.detail.name === "settings") load(); });
  api.overview().then((o) => {
    renderHardware(o);
  }).catch(() => {});
  load();
}

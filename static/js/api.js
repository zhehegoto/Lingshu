// 统一 fetch 封装：错误语义只在这里解释一次，页面不再各写一遍。
export class ApiError extends Error {
  constructor(status, body) {
    super(body?.detail || body?.error || `HTTP ${status}`);
    this.status = status;
    this.code = body?.error || "http_error";
    this.body = body;
  }
}

// 错误码 → 给用户看的一句话。后端只给机器可读的 error，人话在前端定。
export const ERROR_TEXT = {
  gpu_busy: "出图服务正忙，已自动排队，等它就绪后重发",
  sd_connection_lost: "出图服务重启了，重试即可",
  sd_restarted: "出图服务重启，请重新提交",
  vram_guard_tripped: "显存异常，已中止作业，请检查系统占用",
  queue_full: "队列已满（4 个），先等前面的跑完",
  unverified_batch: "多张出图还没验证，先用 1 张",
  unverified_option: "这个选项还没验证，暂不可用",
  invalid_steps: "步数只能是 8–20",
  unsupported_resolution: "这个尺寸不支持，换一个比例或清晰度",
  locked: "这是锁定项，不能改",
  too_large: "附件超过 10 MB",
  empty_file: "附件是空的",
  invalid_workdir: "工作目录只能是「灵枢」文件夹内的路径",
  plan_failed: "计划生成失败，看模型是否就绪",
  toolcall_truncated: "模型输出被截断，请改用表单填参",
  invalid_image: "文件已损坏，无法恢复（它不是一个能打开的 png）",
  not_in_trash: "这张图不在回收站里，不能恢复",
  exists: "原位已有同名文件，先处理它再恢复",
  invalid_path: "路径不合法，已拒绝",
  not_found: "文件已经不存在了",
  chat_stopped: "对话模型已关闭，去「设置」页点「启动对话模型」",
  sd_stopped: "画图模型已关闭，去「设置」页点「启动画图模型」",
  start_failed: "模型没起来，看 logs 里的 *.err.log",
  unknown_service: "没有这个服务",
};

export function human(err) {
  return ERROR_TEXT[err?.code] || err?.message || "请求失败";
}

async function request(url, { method = "GET", body, headers = {}, signal } = {}) {
  const opts = { method, signal, headers: { ...headers } };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const resp = await fetch(url, opts);
  const text = await resp.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = null; }
  if (!resp.ok) throw new ApiError(resp.status, data || { detail: text.slice(0, 120) });
  return data;
}

export const api = {
  status: () => request("/api/status"),
  settings: () => request("/api/settings"),
  models: () => request("/api/models"),
  // 模型服务的启停（8099 / 8081）。start 会阻塞到探测就绪（看门狗 20–35s）
  services: () => request("/api/services"),
  startService: (key) => request(`/api/services/${key}/start`, { method: "POST" }),
  stopService: (key) => request(`/api/services/${key}/stop`, { method: "POST" }),
  saveSettings: (values) => request("/api/settings", { method: "PUT", body: values }),
  savePaths: (values) => request("/api/settings/paths", { method: "PUT", body: values }),
  chats: (includeDeleted = false) =>
    request(`/api/chats?include_deleted=${includeDeleted ? "true" : "false"}`),
  chat: (id) => request(`/api/chats/${encodeURIComponent(id)}`),
  saveChat: (doc) => request("/api/chats", { method: "POST", body: doc }),
  renameChat: (id, title) =>
    request(`/api/chats/${encodeURIComponent(id)}`, { method: "PATCH", body: { title } }),
  deleteChat: (id) => request(`/api/chats/${encodeURIComponent(id)}`, { method: "DELETE" }),
  restoreChat: (id) => request(`/api/chats/${encodeURIComponent(id)}/restore`, { method: "POST" }),
  purgeChat: (id) => request("/api/chats/purge", { method: "POST", body: { id } }),
  // 导出成 Markdown 落到 chats/exports/：WebView 里弹下载框各平台不一，写文件更稳
  exportChat: (id) => request(`/api/chats/${encodeURIComponent(id)}/export`, { method: "POST" }),
  createJob: (payload) => request("/api/jobs", { method: "POST", body: payload }),
  job: (id) => request(`/api/jobs/${id}`),
  jobs: () => request("/api/jobs"),
  images: () => request("/api/images?limit=120"),
  // 单张删除 = 删资源本身（DELETE + relpath）；批量 = 对多张做同一件事，走动作段 + body
  deleteImage: (relpath) =>
    request(`/api/images/${relpath.split("/").map(encodeURIComponent).join("/")}`, { method: "DELETE" }),
  deleteImages: (relpaths) =>
    request("/api/images/delete", { method: "POST", body: { relpaths } }),
  purgeAllChats: () => request("/api/chats/purge", { method: "POST", body: { all: true } }),
  renameImage: (relpath, displayName) =>
    request(`/api/images/${relpath.split("/").map(encodeURIComponent).join("/")}`,
      { method: "PATCH", body: { display_name: displayName } }),
  cancelJob: (id) => request(`/api/jobs/${id}/cancel`, { method: "POST" }),
  // 停止后的解禁依据：llama 当前有几路在生成（不是猜时长）
  chatIdle: () => request("/api/chat/idle"),
  // 硬件自动识别：GET 读启动时的快照；redetect 强制重采（换显卡/改显存上限后用）
  hardware: () => request("/api/hardware"),
  redetectHardware: () => request("/api/hardware/redetect", { method: "POST" }),
  // 一次性非流式对话（带 tools 时走这条，用来把"一句话"变成出图参数）
  chatOnce: (payload) => request("/api/chat", { method: "POST", body: payload }),
  chatTools: (payload) => request("/api/chat/tools", { method: "POST", body: payload }),
  chatStream: (messages, signal, params) =>    fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ messages, params: params || {} }),
      signal,
    }),
  // —— 新壳：概览 / 日志 / 附件 / Agent / 出图能力 ——
  overview: () => request("/api/overview"),
  logs: (since = 0, limit = 200) => request(`/api/logs?since=${since}&limit=${limit}`),
  openFolder: (kind) => request("/api/open-folder", { method: "POST", body: { kind } }),
  uploadAttachment: (name, data) =>
    request("/api/attachments", { method: "POST", body: { name, data } }),
  drawCaps: () => request("/api/draw/capabilities"),
  vram: (seconds = 300) => request(`/api/vram?seconds=${seconds}`),
  saveChatSettings: (id, settings) =>
    request(`/api/chats/${encodeURIComponent(id)}/settings`, { method: "PATCH", body: { settings } }),
  agentPlan: (task, workdir) =>
    request("/api/agent/plan", { method: "POST", body: { task, workdir } }),
  agentRun: (task, workdir, permission) =>
    request("/api/agent/run", { method: "POST", body: { task, workdir, permission } }),
};

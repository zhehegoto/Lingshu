// 两类事件流，机制不同：
// - POST /api/chat：EventSource 只支持 GET，必须用 fetch + reader 手动解析 SSE 帧
// - GET /api/jobs/{id}/events：可以用 EventSource（但作业结束后要主动 close，否则它会自动重连）

// 解析一个 SSE 帧块。多行 data 用 \n 拼接，注释行（: 开头）忽略。
function parseChunk(chunk) {
  const lines = chunk.split(/\r?\n/);
  const data = [];
  let event = "message";
  for (const line of lines) {
    if (!line || line.startsWith(":")) continue;
    if (line.startsWith("event:")) event = line.slice(6).trim();
    else if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
  }
  if (!data.length) return null;
  const payload = data.join("\n");
  try {
    return { event, data: JSON.parse(payload) };
  } catch {
    return { event, data: { raw: payload } };
  }
}

// 消费 fetch 响应的字节流，逐帧回调。返回一个 abort 函数。
export async function streamSSE(resp, onEvent) {
  if (!resp.ok) {
    let body = null;
    try { body = await resp.json(); } catch { /* 忽略 */ }
    throw Object.assign(new Error("stream_http_error"), { status: resp.status, body });
  }
  const reader = resp.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "";
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      // 帧之间用空行分隔；\r\n 也要兼容
      buffer = buffer.replace(/\r\n/g, "\n");
      let idx;
      while ((idx = buffer.indexOf("\n\n")) >= 0) {
        const chunk = buffer.slice(0, idx);
        buffer = buffer.slice(idx + 2);
        const evt = parseChunk(chunk);
        if (evt) onEvent(evt);
      }
    }
    if (buffer.trim()) {
      const evt = parseChunk(buffer);
      if (evt) onEvent(evt);
    }
  } finally {
    try { reader.releaseLock(); } catch { /* 忽略 */ }
  }
}

// 作业事件：EventSource 自带重连，但作业结束时服务端会关流，重连会把旧快照再推一遍，
// 所以收到 done/end/error 必须主动关。
export function jobEvents(jobId, onEvent, onError) {
  const es = new EventSource(`/api/jobs/${jobId}/events`);
  let closed = false;
  const close = () => {
    if (closed) return;
    closed = true;
    es.close();
  };
  es.onmessage = (e) => {
    let evt = null;
    try { evt = JSON.parse(e.data); } catch { return; }
    onEvent(evt);
    if (["done", "end", "error"].includes(evt.type)) close();
  };
  es.onerror = () => {
    // 流被服务端正常关闭时也会触发 onerror；只在明确关闭前上报一次
    if (!closed) onError?.();
    close();
  };
  return { close };
}

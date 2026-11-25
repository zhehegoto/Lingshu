// 模型导入：拖进运行页的框里 → 原生 body 流式上传到 models → 问要不要删掉本机那份原文件。
// 用 XHR 而不是 fetch，只为了拿到上传进度；几 GB 的文件没有进度条会让人以为卡死。
const $ = (id) => document.getElementById(id);
const ACCEPT = [".gguf", ".safetensors"];

function fmtMB(mib) {
  if (!mib || mib < 0) return "";
  return mib >= 1024 ? `${(mib / 1024).toFixed(2)} GB` : `${Math.round(mib)} MB`;
}

function extOk(name) {
  const low = String(name || "").toLowerCase();
  return ACCEPT.some((e) => low.endsWith(e));
}

function send(file, overwrite, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `/api/models/upload${overwrite ? "?overwrite=1" : ""}`);
    xhr.setRequestHeader("X-Filename", encodeURIComponent(file.name));
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) onProgress(e.loaded / e.total);
    };
    xhr.onload = () => {
      let data = {};
      try { data = JSON.parse(xhr.responseText || "{}"); } catch (_) { data = {}; }
      resolve({ status: xhr.status, data });
    };
    xhr.onerror = () => reject(new Error("上传中断，检查 gateway 是否还在运行"));
    xhr.send(file);
  });
}

function setBar(show, pct, text) {
  $("model-drop-bar").hidden = !show;
  $("model-drop-fill").style.width = `${Math.round((pct || 0) * 100)}%`;
  $("model-drop-status").textContent = text || "";
}

function addLine(text, cls) {
  const li = document.createElement("li");
  li.className = cls || "";
  li.textContent = text;
  $("model-drop-list").appendChild(li);
  return li;
}

function clearLines() {
  $("model-drop-list").replaceChildren();
}

let asked = null;          // 待删候选（上传响应里带回来的）
let modalOpen = false;

function openSourceDialog(results) {
  const list = $("model-src-list");
  list.replaceChildren();
  $("model-src-input").value = "";
  const dups = [];
  const seen = new Set();
  for (const r of results) {
    for (const d of (r.duplicates || [])) {
      if (seen.has(d.path)) continue;
      seen.add(d.path);
      dups.push(d);
    }
  }
  if (dups.length) {
    for (const d of dups) {
      const row = document.createElement("label");
      row.className = "model-src-row";
      row.innerHTML = `<input type="checkbox" checked><span class="mono small">${d.path}</span>` +
        `<span class="muted small">${fmtMB(d.size_mib)}</span>`;
      row.dataset.path = d.path;
      list.appendChild(row);
    }
    $("model-src-text").textContent =
      `已导入 ${results.length} 个模型。在桌面/下载/文档里找到了同名同大小的原文件，删掉它们能立刻腾出空间：`;
  } else {
    $("model-src-text").textContent =
      `模型已经放进 models。浏览器拖拽拿不到原路径，没在桌面/下载/文档里找到同名副本——` +
      `如果原文件还在别的盘（比如 D:\下载），可以手动把完整路径填进下面，删掉能省一份空间。`;
  }
  $("model-src-modal").hidden = false;
  $("model-src-scrim").hidden = false;
  modalOpen = true;
  asked = results;
}

function closeSourceDialog() {
  $("model-src-modal").hidden = true;
  $("model-src-scrim").hidden = true;
  modalOpen = false;
  asked = null;
}

async function deletePaths(paths) {
  const done = [];
  for (const p of paths) {
    try {
      const r = await fetch("/api/models/delete_source", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: p }),
      });
      const data = await r.json().catch(() => ({}));
      if (r.ok) done.push(`已移入回收站：${p}（释放 ${fmtMB(data.freed_mib)}）`);
      else done.push(`没删：${p} —— ${data.detail || r.status}`);
    } catch (e) {
      done.push(`没删：${p} —— ${e.message || e}`);
    }
  }
  for (const t of done) addLine(t, t.startsWith("已移入") ? "ok" : "warn");
}

async function uploadFiles(files, onDone) {
  const usable = [...files].filter((f) => extOk(f.name));
  const skipped = [...files].length - usable.length;
  if (skipped) addLine(`跳过 ${skipped} 个文件：只接受 .gguf / .safetensors`, "warn");
  if (!usable.length) return;

  const results = [];
  for (let i = 0; i < usable.length; i++) {
    const file = usable[i];
    const label = usable.length > 1 ? `${i + 1}/${usable.length} · ` : "";
    setBar(true, 0, `${label}${file.name} · 准备中…`);
    try {
      let r = await send(file, false, (p) => setBar(true, p, `${label}${file.name} · ${Math.round(p * 100)}%`));
      if (r.status === 409 && r.data?.error === "exists") {
        const ok = window.confirm(
          `models 里已经有 ${file.name}（${fmtMB(r.data.size_mib)}）。\n覆盖它吗？正在被模型进程使用时不能覆盖。`,
        );
        if (!ok) { addLine(`跳过 ${file.name}：models 里已有同名文件`, "warn"); continue; }
        r = await send(file, true, (p) => setBar(true, p, `${label}${file.name} · ${Math.round(p * 100)}%`));
      }
      if (r.status >= 200 && r.status < 300) {
        results.push(r.data);
        addLine(`已导入 ${r.data.name}（${fmtMB(r.data.size_mib)}）`, "ok");
      } else {
        addLine(`没导入 ${file.name}：${r.data?.detail || `HTTP ${r.status}`}`, "warn");
      }
    } catch (e) {
      addLine(`没导入 ${file.name}：${e.message || e}`, "warn");
    }
  }
  setBar(false, 0, "");
  if (results.length) {
    onDone && onDone();
    openSourceDialog(results);
  }
}

export function initModelDrop(onDone) {
  const drop = $("model-drop");
  const input = $("model-drop-input");
  if (!drop || !input) return;

  const stop = (e) => { e.preventDefault(); e.stopPropagation(); };
  drop.addEventListener("click", (e) => {
    if (modalOpen) return;
    if (e.target.closest("input")) return;
    input.click();
  });
  input.addEventListener("change", () => {
    const files = [...input.files];
    input.value = "";
    if (files.length) uploadFiles(files, onDone);
  });
  for (const ev of ["dragenter", "dragover"]) {
    drop.addEventListener(ev, (e) => { stop(e); drop.classList.add("over"); });
  }
  for (const ev of ["dragleave", "drop"]) {
    drop.addEventListener(ev, (e) => { stop(e); if (ev === "dragleave" && e.relatedTarget) return; drop.classList.remove("over"); });
  }
  drop.addEventListener("drop", (e) => {
    const files = e.dataTransfer?.files;
    if (files?.length) uploadFiles(files, onDone);
  });

  $("model-src-close")?.addEventListener("click", closeSourceDialog);
  $("model-src-scrim")?.addEventListener("click", closeSourceDialog);
  $("model-src-keep")?.addEventListener("click", () => {
    addLine("原文件保留；想清理时回到这个框再拖一次同名文件就会重新检测。", "");
    closeSourceDialog();
  });
  $("model-src-delete")?.addEventListener("click", async () => {
    const paths = [...$("model-src-list").querySelectorAll(".model-src-row")]
      .filter((row) => row.querySelector("input[type=checkbox]").checked)
      .map((row) => row.dataset.path);
    const manual = $("model-src-input").value.trim();
    if (manual) paths.push(manual);
    if (!paths.length) {
      $("model-src-note").textContent = "没勾任何文件，也没填路径；要么先勾上，要么点「原文件留着」。";
      return;
    }
    closeSourceDialog();
    await deletePaths(paths);
  });
}

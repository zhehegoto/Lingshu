"""顶栏两个模型开关的验收：副作用断言，不是"接口回了 200"。

覆盖：
  A 前置       两个服务就绪（8099 / 8081 都在听）
  B 关出图     → 8081 真的不能连、pid 真的没了、/api/jobs 真的被 409 拒
  C 开出图     → 8081 真的能连、ready 真的回 True
  D 关对话     → 8099 真的不能连、/api/chat 真的回 503 chat_stopped
  E 开对话     → 8099 真的能连、health 真的通
  F 作业中关   → 先取消再关：作业终态是 cancelled，sd 也真的停了（不是"关了又被取消路径拉起来"）
  G 意图        → 关掉对话模型后出一张图，出完 8099 仍是关的（作业流程不许偷偷加载）
  H 复位       两个都启动回原状，脚本可重复运行

用法：先起 gateway（8000），再 `python gateway\\tools\\services_check.py`
"""

from __future__ import annotations

import json
import socket
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8000"
SD_PORT, CHAT_PORT = 8081, 8099
ROOT = Path(__file__).resolve().parents[2]
LOG = ROOT / "logs" / "services_check.out"

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'✓' if ok else '✗'} {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        failures.append(name)


def req(path: str, method: str = "GET", body: dict | None = None, timeout: float = 130.0):
    data = None if body is None else json.dumps(body).encode("utf-8")
    r = urllib.request.Request(BASE + path, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"raw": raw[:200]}


def port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def wait_for(fn, timeout: float, interval: float = 0.5) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            if fn():
                return True
        except Exception:
            pass
        time.sleep(interval)
    return False


def services() -> dict:
    _, body = req("/api/services")
    return body.get("items", {})


def status() -> dict:
    _, body = req("/api/status")
    return body


def stop(key: str) -> dict:
    code, body = req(f"/api/services/{key}/stop", "POST")
    check(f"stop {key} 返回 200", code == 200, f"HTTP {code} {body.get('detail', '')}")
    return body


def start(key: str) -> dict:
    code, body = req(f"/api/services/{key}/start", "POST")
    check(f"start {key} 返回 200", code == 200, f"HTTP {code} {body.get('detail', '')}")
    return body


def main() -> int:
    print(f"被测 gateway：{BASE}")
    # —— A 前置 ——
    ok = wait_for(lambda: port_open(SD_PORT) and port_open(CHAT_PORT), 70)
    check("A 前置：8099 与 8081 都在听", ok, f"sd={port_open(SD_PORT)} chat={port_open(CHAT_PORT)}")
    if not ok:
        print("前置不满足，后面的断言没有意义，先起 gateway 或看 logs")
        return 1
    check("A 前置：/api/services 报两个都 ready",
          all(services().get(k, {}).get("ready") for k in ("chat", "sd")), str(services()))

    # —— B 关出图模型 ——
    body = stop("sd")
    pid_before = (body.get("items", {}).get("sd") or {}).get("pid")
    check("B 8081 真的关了（端口不能连）", not port_open(SD_PORT))
    check("B /api/services 报 sd 没在跑", not services()["sd"]["running"], str(services()["sd"]))
    check("B /api/status 的 ready.sd 变 False", status()["ready"]["sd"] is False)
    code, body = req("/api/jobs", "POST", {"prompt": "services check：模型关闭时应被拒", "steps": 8})
    check("B 关着模型时提交出图被 409 拒（sd_stopped）",
          code == 409 and body.get("error") == "sd_stopped", f"HTTP {code} {body}")

    # —— C 开出图模型 ——
    body = start("sd")
    check("C 8081 真的开了（端口能连）", port_open(SD_PORT))
    check("C ready.sd 变回 True", services()["sd"]["ready"] is True, str(services()["sd"]))
    check("C /api/status 的 pids 里有 sd", "sd" in status().get("pids", {}), str(status().get("pids")))

    # —— D 关对话模型 ——
    stop("chat")
    check("D 8099 真的关了", not port_open(CHAT_PORT))
    check("D /api/chat 回 503 chat_stopped",
          req("/api/chat", "POST", {"messages": [{"role": "user", "content": "在吗"}]})[0] == 503)
    code, body = req("/api/chat", "POST", {"messages": [{"role": "user", "content": "在吗"}]})
    check("D 503 的 error 是 chat_stopped（不是 gpu_busy，前端才不会反复重发）",
          body.get("error") == "chat_stopped", str(body))

    # —— E 开对话模型 ——
    start("chat")
    check("E 8099 真的开了", port_open(CHAT_PORT))
    check("E ready.chat 变回 True", services()["chat"]["ready"] is True, str(services()["chat"]))

    # —— F 作业中关出图模型：先取消再关 ——
    code, job = req("/api/jobs", "POST", {"prompt": "services check：出图中关闭", "steps": 8})
    check("F 作业已受理（202）", code == 202, f"HTTP {code} {job}")
    job_id = job.get("id")
    if job_id:
        began = wait_for(lambda: status()["state"] == "GENERATING", 40)
        check("F 作业进了 GENERATING", began, status()["state"])
        stop("sd")
        check("F 关完之后 8081 是关的（取消路径没有把它偷偷重启）", not port_open(SD_PORT))
        ended = wait_for(lambda: (req(f"/api/jobs/{job_id}")[1] or {}).get("status") in
                         ("cancelled", "failed", "done"), 90)
        job_now = req(f"/api/jobs/{job_id}")[1]
        check("F 作业进入终态（不是一直挂 GENERATING）", ended, str(job_now.get("status")))
        check("F 作业终态是 cancelled / cancelled_by_user",
              job_now.get("status") == "cancelled", f"{job_now.get('status')} / {job_now.get('error')}")
    check("F 状态机回到 CHAT_READY", wait_for(lambda: status()["state"] == "CHAT_READY", 30),
          status()["state"])

    # —— G 意图：关掉对话模型后出一张图，出完它不许自己回来 ——
    start("sd")                                     # F 里关掉了，先恢复出图能力
    stop("chat")
    code, job = req("/api/jobs", "POST", {"prompt": "services check intent: a plain gray square", "steps": 8})
    check("G 作业已受理", code == 202, f"HTTP {code} {job}")
    job_id = job.get("id")
    if job_id:
        done = wait_for(lambda: (req(f"/api/jobs/{job_id}")[1] or {}).get("status") in ("done", "failed"), 120)
        job_now = req(f"/api/jobs/{job_id}")[1]
        check("G 作业跑完（出图不受「对话模型关着」影响）", done and job_now.get("status") == "done",
              f"{job_now.get('status')} / {job_now.get('error')}")
        check("G 出完图后 8099 仍是关的（作业流程没偷偷加载对话模型）", not port_open(CHAT_PORT))
        check("G ready.chat 仍是 False", status()["ready"]["chat"] is False)

    # —— H 复位 ——
    start("chat")
    start("sd")
    ok = wait_for(lambda: port_open(SD_PORT) and port_open(CHAT_PORT), 60)
    check("H 收尾：两个端口都恢复在听（脚本可重复运行）", ok,
          f"sd={port_open(SD_PORT)} chat={port_open(CHAT_PORT)}")

    print()
    if failures:
        print(f"验收: 失败（{len(failures)} 项）" + "：" + "；".join(failures))
        return 1
    print("验收: 通过")
    return 0


if __name__ == "__main__":
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG, "w", encoding="utf-8") as f:
        real = sys.stdout

        class Tee:
            def write(self, s):
                real.write(s)
                f.write(s)

            def flush(self):
                real.flush()
                f.flush()

        sys.stdout = Tee()
        try:
            code = main()
        finally:
            sys.stdout = real
    sys.exit(code)

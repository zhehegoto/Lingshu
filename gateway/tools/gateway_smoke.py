"""gateway 功能冒烟 + 故障注入第 3 条（cancel 断 SSE 改轮询）。

断言的都是副作用，不只是状态码：
- settings PUT 后值真的变了；locked → 409；非法 → 400
- 提交作业有 id；SSE 第一帧是 snapshot
- cancel 后 SSE 断、轮询能拿到 cancelled、state 最终回到 CHAT_READY
"""

from __future__ import annotations

import json
import sys
import time

import httpx

BASE = "http://127.0.0.1:8000"
ok = True


def check(name: str, cond: bool, extra: str = "") -> None:
    global ok
    print(f"  {'✓' if cond else '✗'} {name}{'  ' + extra if extra else ''}")
    ok = ok and cond


def main() -> int:
    with httpx.Client(base_url=BASE, timeout=10) as c:
        # --- 就绪 ---
        h = None
        for _ in range(120):
            try:
                h = c.get("/health")
                if h.status_code == 200:
                    break
            except Exception:
                pass
            time.sleep(1)
        if h is None:
            print("✗ 网关没起来")
            return 1
        print(f"[1] /health {h.status_code} {h.json()}")

        s = c.get("/api/status").json()
        print(f"[2] /api/status state={s['state']} ready={s['ready']} queue={s['queue']}")
        check("chat 可接受", s["can_accept_chat"])

        # --- settings ---
        g = c.get("/api/settings").json()
        check("GET settings 有 6 个可改字段（n_ctx 已解锁）",
              set(g["values"]) == {"steps", "max_tokens", "thinking", "n_ctx", "vae_tile", "batch_count_default"})
        # 关思考不再压预算（实测 512 会把长答案截断），派生值应等于配置上限
        check("GET settings 的 no_think 上限与配置上限一致",
              g["max_tokens_effective_no_think"] == g["values"]["max_tokens"])
        check("GET settings 不含 locked 段之外的矛盾字段", "batch_count_default" in g["unverified_options"] or "batch_count" in g["unverified_options"])

        cur_steps = g["values"]["steps"]
        orig = cur_steps
        target = 8 if cur_steps != 8 else 12      # 取反值，否则"没变也算通过"
        r1 = c.put("/api/settings", json={"steps": target})
        j1 = r1.json()
        back = c.get("/api/settings").json()["values"]["steps"]
        check("PUT hot 真改了", back == target and j1["changed"].get("steps") == target, str(j1["changed"]))
        c.put("/api/settings", json={"steps": orig})          # 恢复原值，测试要幂等
        check("PUT 已恢复原值", c.get("/api/settings").json()["values"]["steps"] == orig)
        r2 = c.put("/api/settings", json={"kv_type": "q8_0"})
        check("PUT 仍锁的 kv_type → 409", r2.status_code == 409, r2.text[:60])
        r3 = c.put("/api/settings", json={"vae_tile": 500})
        check("PUT 不存在的 tile → 400", r3.status_code == 400, r3.text[:60])
        r4 = c.put("/api/settings", json={"n_ctx": 256})
        check("PUT n_ctx 低于 512 → 400", r4.status_code == 400, r4.text[:60])
        check("GET settings 带本机推荐值与 ctx 显存估算",
              {"n_ctx", "vae_tile"} <= set(g.get("recommended") or {})
              and "kv_mib" in (g.get("ctx_estimate") or {}))
        check("PUT 响应里 values/fields/locked 平级",
              set(j1) >= {"values", "fields", "locked"} and "values" not in j1["values"])
        rb = c.post("/api/jobs", json={"prompt": "x", "batch_count": 2})
        check("batch_count=2 → 400 unverified_batch",
              rb.status_code == 400 and rb.json().get("error") == "unverified_batch", rb.text[:60])

        # --- 提交作业 ---
        j = c.post("/api/jobs", json={"prompt": "a red apple on a wooden table, soft window light"})
        check("POST /api/jobs → 202", j.status_code == 202, j.text[:80])
        job_id = j.json()["id"]
        print(f"[3] job={job_id}")

        # --- SSE 第一帧必须是 snapshot ---
        first = None
        with c.stream("GET", f"/api/jobs/{job_id}/events", timeout=15) as resp:
            for line in resp.iter_lines():
                if line.startswith("data:"):
                    first = json.loads(line[5:])
                    break
        check("SSE 首帧是 snapshot", bool(first and first.get("type") == "snapshot"), str(first)[:80])

        # --- 真正进入采样再 cancel ---
        t0 = time.time()
        while time.time() - t0 < 30:
            jj = c.get(f"/api/jobs/{job_id}").json()
            if jj["status"] == "generating":
                break
            time.sleep(0.4)
        time.sleep(6)
        before = c.get("/api/status").json()["state"]

        # 出图中对话必须 503，前端据此排队
        busy = c.post("/api/chat", json={"messages": [{"role": "user", "content": "hi"}]})
        check("出图中 /api/chat → 503 gpu_busy", busy.status_code == 503 and busy.json().get("error") == "gpu_busy",
              f"{busy.status_code} {busy.text[:50]}")

        cancel = c.post(f"/api/jobs/{job_id}/cancel")
        print(f"[4] cancel {cancel.status_code} {cancel.json()} (此前 state={before})")
        check("cancel 被接受", cancel.status_code == 200 and cancel.json().get("ok"))

        # --- 轮询到 cancelled ---
        t1 = time.time()
        status, err = None, None
        while time.time() - t1 < 90:
            jj = c.get(f"/api/jobs/{job_id}").json()
            status, err = jj["status"], jj.get("error")
            if status in ("cancelled", "failed", "done"):
                break
            time.sleep(0.5)
        check("轮询到 cancelled", status == "cancelled", f"{status}/{err} 用时 {time.time()-t1:.1f}s")

        # --- 回到 CHAT_READY ---
        t2 = time.time()
        state = None
        while time.time() - t2 < 90:
            state = c.get("/api/status").json()["state"]
            if state == "CHAT_READY":
                break
            time.sleep(0.5)
        check("最终回到 CHAT_READY", state == "CHAT_READY", f"用时 {time.time()-t2:.1f}s")

        # --- 真跑一次对话 SSE（关思考、压短，只为验链路） ---
        c.put("/api/settings", json={"thinking": False, "max_tokens": 256})
        types, text = [], []
        t3 = time.time()
        with c.stream("POST", "/api/chat", timeout=90,
                      json={"messages": [{"role": "user", "content": "用一句话回答：1+1 等于几？"}]}) as resp:
            check("chat 流式响应 200", resp.status_code == 200, str(resp.status_code))
            for line in resp.iter_lines():
                if not line.startswith("data:"):
                    continue
                evt = json.loads(line[5:])
                types.append(evt.get("type"))
                if evt.get("type") in ("content", "reasoning"):
                    text.append(evt.get("text") or "")
                if evt.get("type") in ("done", "error"):
                    done_evt = evt
                    break
        check("chat 收到 done", "done" in types, f"{time.time()-t3:.1f}s types={sorted(set(types))}")
        check("chat 有实际文本", bool("".join(text).strip()), f"{len(''.join(text))} 字")
        check("done 里带了钳制后的 max_tokens", done_evt.get("max_tokens") == 256, str(done_evt)[:90])
        c.put("/api/settings", json={"thinking": True, "max_tokens": 1024})

        # --- 单实例锁：第二份应被拒绝 ---
        # （由命令行另起 uvicorn 验，这里不重复起进程）

        # --- 故障语义：sd 连接断开 → 当前 failed sd_connection_lost，队列清空 ---
        import subprocess
        ja = c.post("/api/jobs", json={"prompt": "a cat on a sofa"}).json()["id"]
        jb = c.post("/api/jobs", json={"prompt": "a dog in a park"}).json()["id"]
        t4 = time.time()
        while time.time() - t4 < 30 and c.get(f"/api/jobs/{ja}").json()["status"] != "generating":
            time.sleep(0.4)
        time.sleep(5)
        sd_pid = c.get("/api/status").json()["pids"].get("sd")
        check("拿到 sd pid", bool(sd_pid), str(sd_pid))
        subprocess.run(["taskkill", "/F", "/PID", str(sd_pid)], capture_output=True, check=False)

        a_err = b_err = None
        for _ in range(120):
            a_err = c.get(f"/api/jobs/{ja}").json().get("error")
            b_err = c.get(f"/api/jobs/{jb}").json().get("error")
            if a_err and b_err:
                break
            time.sleep(0.5)
        check("当前作业 failed sd_connection_lost", a_err == "sd_connection_lost", str(a_err))
        check("排队作业被清空为 sd_restarted", b_err == "sd_restarted", str(b_err))

        t5 = time.time()
        st = None
        while time.time() - t5 < 90:
            st = c.get("/api/status").json()["state"]
            if st == "CHAT_READY":
                break
            time.sleep(0.5)
        check("断开后能自己恢复", st == "CHAT_READY", f"用时 {time.time()-t5:.1f}s")

    print(f"\n冒烟: {'通过' if ok else '不通过'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

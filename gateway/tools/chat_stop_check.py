"""「停止」的副作用断言：前端断开后，llama 侧必须真的停止生成。

判据：
  1. 生成期间至少有一个 slot 报 is_processing=True —— 先证明探针看得见"忙"，
     否则后面的"全部 False"是空断言（这条本身就是"让断言能变红"的自检）
  2. 断开后连续两次全量读 /slots，所有 slot 都必须是 False
  3. 立刻发一条短请求，能正常返回

注意：本机 llama 是 4 个 slot（total_slots=4），所以只能看"全量 slot 是否都空闲"，
不能靠"新请求秒回"推断旧生成被中止——另一个空 slot 会接住新请求。
"""

import sys
import threading
import time

import httpx

GW = "http://127.0.0.1:8000"
LLAMA = "http://127.0.0.1:8099"


def slots() -> list[dict]:
    r = httpx.get(f"{LLAMA}/slots", timeout=10)
    r.raise_for_status()
    return r.json() or []


def busy_count() -> int:
    return sum(1 for s in slots() if s.get("is_processing"))


def main() -> int:
    ok = True
    st = httpx.get(f"{GW}/api/status", timeout=10).json()
    print("state:", st["state"], "| 初始忙 slot 数:", busy_count())
    if st["state"] != "CHAT_READY":
        print("不是 CHAT_READY，先等模型就绪")
        return 1

    stop = False
    seen_busy = {"max": 0}

    def watch():
        while not stop:
            seen_busy["max"] = max(seen_busy["max"], busy_count())
            time.sleep(0.4)

    t = threading.Thread(target=watch, daemon=True)
    t.start()

    payload = {"messages": [{"role": "user", "content": "请写一篇关于秋天的散文，约六百字。"}]}
    t0 = time.time()
    with httpx.Client(timeout=None) as c:
        with c.stream("POST", f"{GW}/api/chat", json=payload) as r:
            print("HTTP", r.status_code)
            got = 0
            for line in r.iter_lines():
                if line.startswith("data:"):
                    got += 1
                if time.time() - t0 > 2.5:
                    break
    print(f"读了 {got} 个事件后断开（{time.time()-t0:.1f}s）")

    time.sleep(1.0)
    a = busy_count()
    print("断开后 1s 忙 slot 数:", a)
    time.sleep(2.0)
    b = busy_count()
    print("断开后 3s 忙 slot 数:", b)
    stop = True
    t.join(timeout=2)

    print("生成期间观察到的最大忙 slot 数:", seen_busy["max"])
    if seen_busy["max"] == 0:
        ok = False
        print("✗ 探针在生成期间一次都没看到忙 slot：这条断言无效（不是通过）")
    if a or b:
        ok = False
        print(f"✗ 断开后仍有 slot 在生成（1s:{a} 3s:{b}）")

    t1 = time.time()
    r2 = httpx.post(f"{GW}/api/chat", json={"messages": [{"role": "user", "content": "只回答两个字：收到"}]},
                    timeout=120)
    print("接着发短请求 →", r2.status_code, f"{time.time()-t1:.1f}s",
          (r2.text or "")[:40].replace("\n", " "))
    if r2.status_code != 200:
        ok = False
        print("✗ 停止后服务不可用")

    print("验收:", "通过" if ok else "不通过")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

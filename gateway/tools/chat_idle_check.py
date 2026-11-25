"""/api/chat/idle 的副作用断言：忙的时候必须读到忙，不然"全空"就是空断言。

流程：后台线程每 0.3s 探一次 /api/chat/idle；主线程发一条长对话；中途断言
"至少出现过一次 busy>0"，断流后再断言"回落 0"。
"""

import json
import sys
import threading
import time

import httpx

GW = "http://127.0.0.1:8000"
samples: list[tuple[float, object]] = []
stop = threading.Event()


def poll() -> None:
    t0 = time.time()
    while not stop.is_set():
        try:
            r = httpx.get(GW + "/api/chat/idle", timeout=6).json()
            samples.append((round(time.time() - t0, 1), r.get("busy")))
        except Exception as e:
            samples.append((round(time.time() - t0, 1), f"err:{type(e).__name__}"))
        time.sleep(0.3)


def main() -> int:
    th = threading.Thread(target=poll, daemon=True)
    th.start()

    payload = {"messages": [{"role": "user", "content": "请写一篇 800 字的散文，主题是秋天的山。"}]}
    t0 = time.time()
    got = 0
    with httpx.stream("POST", GW + "/api/chat", json=payload, timeout=120) as r:
        print("HTTP", r.status_code)
        for chunk in r.iter_bytes():
            got += len(chunk)
            if time.time() - t0 > 6:      # 生成 6 秒后主动断开，模拟"停止"
                break
    print(f"读了 {got} 字节后断开，用时 {time.time()-t0:.1f}s")

    time.sleep(3.5)
    stop.set()
    th.join(timeout=3)

    seen = [s for s in samples if s[1] == 1 or (isinstance(s[1], int) and s[1] > 0)]
    tail = [s for s in samples if s[0] > samples[-1][0] - 2.0]
    print("采样:", samples[:3], "...", samples[-3:])
    print("最大忙数:", max([s[1] for s in samples if isinstance(s[1], int)] or [0]))

    ok = True
    if not seen:
        print("失败: 整个窗口没读到忙，'回落 0' 是空断言"); ok = False
    if not all(s[1] == 0 for s in tail if isinstance(s[1], int)):
        print(f"失败: 断流 2s 后仍读到忙 {tail}"); ok = False
    print("验收:", "通过" if ok else "不通过")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

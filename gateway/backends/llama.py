"""llama-server HTTP 客户端（第 4 步只做探活；chat 的 SSE 留到 gateway 步）。

为什么现在不写 chat：urllib 无法增量读 body，SSE 会退化成"读完整个响应"，
硬写就是自己造流式轮子。gateway 步装 httpx 一次解决。
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request


class LlamaError(RuntimeError):
    pass


class LlamaBackend:
    def __init__(self, port: int, timeout: float = 10.0) -> None:
        self.base = f"http://127.0.0.1:{port}"
        self.timeout = timeout

    def _get(self, path: str, timeout: float | None = None) -> tuple[int, bytes]:
        try:
            with urllib.request.urlopen(f"{self.base}{path}", timeout=timeout or self.timeout) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()
        except Exception:
            return 0, b""

    def health(self) -> bool:
        """llama-server 的 /health 在加载中返回 503，就绪返回 200。"""
        code, _ = self._get("/health", timeout=3)
        return code == 200

    def wait_ready(self, timeout: float = 20.0, interval: float = 0.4) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.health():
                return True
            time.sleep(interval)
        return False

    def info(self) -> dict | None:
        code, body = self._get("/props", timeout=5)
        if code != 200 or not body:
            return None
        try:
            return json.loads(body.decode("utf-8"))
        except Exception:
            return None

    def busy_slots(self, timeout: float = 5.0) -> int | None:
        """全量读 /slots，数有几路在生成。失败返回 None（调用方按"未知"处理）。

        注意本机是 4 个 slot：不能用"新请求秒回"推断旧生成已停，必须看全量忙数。
        """
        code, body = self._get("/slots", timeout=timeout)
        if code != 200 or not body:
            return None
        try:
            slots = json.loads(body.decode("utf-8")) or []
        except Exception:
            return None
        return sum(1 for s in slots if s.get("is_processing"))

    # ---------------- tokenize（实测可用，chat 的钳制靠它） ----------------

    def count_tokens(self, text: str, timeout: float = 5.0) -> int | None:
        """POST /tokenize。失败返回 None，由调用方回落到字符估算。"""
        payload = json.dumps({"content": text}).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base}/tokenize", data=payload,
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = json.loads(r.read().decode("utf-8"))
            return len(body.get("tokens") or [])
        except Exception:
            return None

    async def chat_once(
        self,
        messages: list[dict],
        max_tokens: int,
        temperature: float = 0.7,
        extra: dict | None = None,
        read_timeout: float = 120.0,
    ) -> dict:
        """非流式一次性调用，给 tool_call 用（需要完整结果才能判断是否被截断）。"""
        import httpx

        payload = {"messages": messages, "max_tokens": max_tokens,
                   "temperature": temperature, "stream": False}
        if extra:
            payload.update(extra)
        timeout = httpx.Timeout(connect=5.0, read=read_timeout, write=10.0, pool=5.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(f"{self.base}/v1/chat/completions", json=payload)
            if r.status_code != 200:
                raise LlamaError(f"chat {r.status_code}: {r.text[:300]}")
            return r.json()

    # ---------------- chat（SSE，httpx） ----------------

    async def chat_stream(
        self,
        messages: list[dict],
        max_tokens: int,
        temperature: float = 0.7,
        extra: dict | None = None,
        read_timeout: float = 120.0,
    ):
        """逐块产出 usage/choices 原始事件。调用方自己拼 content / reasoning_content。"""
        import httpx

        payload = {
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if extra:
            payload.update(extra)

        timeout = httpx.Timeout(connect=5.0, read=read_timeout, write=10.0, pool=5.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream("POST", f"{self.base}/v1/chat/completions", json=payload) as r:
                if r.status_code != 200:
                    body = (await r.aread()).decode("utf-8", "replace")[:300]
                    raise LlamaError(f"chat {r.status_code}: {body}")
                async for line in r.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        yield json.loads(data)
                    except json.JSONDecodeError:
                        continue

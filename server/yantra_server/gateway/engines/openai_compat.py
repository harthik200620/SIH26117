"""Shared OpenAI-compatible HTTP engine: streaming chat, embeddings, rerank.

vLLM and llama.cpp both speak this dialect on loopback; subclasses contribute the
structured-output request fields and launch metadata.
"""

from __future__ import annotations

import base64
import json
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx

from .base import (
    Capabilities,
    ChatEvent,
    ChatMessage,
    ChatResult,
    ContentDelta,
    Engine,
    EngineChatRequest,
    EngineError,
    EngineHealth,
    EngineTimeout,
    FinalEvent,
    ImagePart,
    ReasoningDelta,
    TextPart,
    ToolCallOut,
    Usage,
)


class OpenAICompatEngine(Engine):
    name = "openai-compat"

    def __init__(
        self,
        base_url: str,
        *,
        timeout_s: float = 300.0,
        api_key: str | None = None,
        capabilities: Capabilities | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._caps = capabilities or Capabilities(
            chat=True, tools=True, structured=True, vision=True
        )
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            trust_env=False,
            follow_redirects=False,
            headers={"Authorization": "Bearer " + api_key} if api_key else {},
            timeout=httpx.Timeout(connect=10.0, read=timeout_s, write=30.0, pool=timeout_s),
        )

    # Subclasses map the constraint to engine-specific request fields.
    def structured_fields(self, request: EngineChatRequest) -> dict[str, Any]:
        return {}

    def extra_chat_fields(self, request: EngineChatRequest) -> dict[str, Any]:
        return {}

    def _payload(self, request: EngineChatRequest) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": request.model,
            "messages": [self._message_json(m) for m in request.messages],
            "temperature": request.decoding.temperature,
            "top_p": request.decoding.top_p,
            "max_tokens": request.decoding.max_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if request.decoding.seed is not None:
            payload["seed"] = request.decoding.seed
        if request.decoding.stop:
            payload["stop"] = request.decoding.stop
        if request.tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters or {"type": "object"},
                    },
                }
                for t in request.tools
            ]
        payload.update(self.structured_fields(request))
        payload.update(self.extra_chat_fields(request))
        return payload

    def _message_json(self, message: ChatMessage) -> dict[str, Any]:
        if isinstance(message.content, str):
            body: Any = message.content
        else:
            parts: list[dict[str, Any]] = []
            for part in message.content:
                if isinstance(part, TextPart):
                    parts.append({"type": "text", "text": part.text})
                elif isinstance(part, ImagePart):
                    data = part.data_b64
                    if data is None and part.path:
                        data = base64.b64encode(Path(part.path).read_bytes()).decode()
                    parts.append(
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{part.media_type};base64,{data or ''}"},
                        }
                    )
            body = parts
        out: dict[str, Any] = {"role": message.role, "content": body}
        if message.name:
            out["name"] = message.name
        if message.tool_call_id:
            out["tool_call_id"] = message.tool_call_id
        return out

    async def chat_stream(self, request: EngineChatRequest) -> AsyncIterator[ChatEvent]:
        payload = self._payload(request)
        started = time.monotonic()
        ttft_ms: float | None = None
        content: list[str] = []
        reasoning: list[str] = []
        usage = Usage()
        finish_reason = "stop"
        tool_fragments: dict[int, dict[str, Any]] = {}
        try:
            async with self._client.stream("POST", "/v1/chat/completions", json=payload) as resp:
                if resp.status_code != 200:
                    body = (await resp.aread()).decode(errors="replace")[:2000]
                    raise EngineError(f"{self.name} HTTP {resp.status_code}: {body}")
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[len("data:") :].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    if chunk.get("usage"):
                        usage = _usage_from(chunk["usage"])
                    for choice in chunk.get("choices", []):
                        if choice.get("finish_reason"):
                            finish_reason = str(choice["finish_reason"])
                        delta = choice.get("delta") or {}
                        if text := delta.get("content"):
                            if ttft_ms is None:
                                ttft_ms = (time.monotonic() - started) * 1000
                            content.append(text)
                            yield ContentDelta(text=text)
                        if thinking := delta.get("reasoning_content"):
                            if ttft_ms is None:
                                ttft_ms = (time.monotonic() - started) * 1000
                            reasoning.append(thinking)
                            yield ReasoningDelta(text=thinking)
                        for tc in delta.get("tool_calls") or []:
                            slot = tool_fragments.setdefault(
                                int(tc.get("index", 0)), {"id": "", "name": "", "arguments": ""}
                            )
                            if tc.get("id"):
                                slot["id"] = tc["id"]
                            fn = tc.get("function") or {}
                            if fn.get("name"):
                                slot["name"] = fn["name"]
                            if fn.get("arguments"):
                                slot["arguments"] += fn["arguments"]
        except httpx.TimeoutException as exc:
            raise EngineTimeout(f"{self.name}: request timed out: {exc}") from exc
        except httpx.HTTPError as exc:
            raise EngineError(f"{self.name}: transport error: {exc}") from exc

        tool_calls: list[ToolCallOut] = []
        for index in sorted(tool_fragments):
            fragment = tool_fragments[index]
            try:
                arguments = json.loads(fragment["arguments"]) if fragment["arguments"] else {}
            except json.JSONDecodeError:
                arguments = {"_raw": fragment["arguments"]}
            tool_calls.append(
                ToolCallOut(id=str(fragment["id"]), name=str(fragment["name"]), arguments=arguments)
            )
        yield FinalEvent(
            result=ChatResult(
                content="".join(content),
                reasoning="".join(reasoning) or None,
                tool_calls=tool_calls,
                usage=usage,
                model=request.model,
                finish_reason="tool_call" if tool_calls else finish_reason,
                ttft_ms=ttft_ms,
                latency_ms=(time.monotonic() - started) * 1000,
            )
        )

    async def embed(
        self, model: str, texts: list[str], instruction: str | None = None
    ) -> list[list[float]]:
        inputs = [f"{instruction}\n{t}" if instruction else t for t in texts]
        try:
            resp = await self._client.post("/v1/embeddings", json={"model": model, "input": inputs})
        except httpx.HTTPError as exc:
            raise EngineError(f"{self.name}: embeddings transport error: {exc}") from exc
        if resp.status_code != 200:
            raise EngineError(f"{self.name} embeddings HTTP {resp.status_code}: {resp.text[:500]}")
        data = resp.json().get("data", [])
        ordered = sorted(data, key=lambda d: d.get("index", 0))
        return [list(map(float, d["embedding"])) for d in ordered]

    async def rerank(self, model: str, query: str, documents: list[str]) -> list[float]:
        payload = {"model": model, "query": query, "documents": documents}
        last_error = ""
        for path in ("/v1/rerank", "/rerank", "/score"):
            try:
                resp = await self._client.post(path, json=payload)
            except httpx.HTTPError as exc:
                raise EngineError(f"{self.name}: rerank transport error: {exc}") from exc
            if resp.status_code == 404:
                last_error = f"{path} not found"
                continue
            if resp.status_code != 200:
                raise EngineError(f"{self.name} rerank HTTP {resp.status_code}: {resp.text[:500]}")
            body = resp.json()
            results = body.get("results") or body.get("data") or []
            scores = [0.0] * len(documents)
            for item in results:
                index = int(item.get("index", 0))
                if 0 <= index < len(scores):
                    scores[index] = float(item.get("relevance_score", item.get("score", 0.0)))
            return scores
        raise EngineError(f"{self.name}: no rerank endpoint ({last_error})")

    async def health(self) -> EngineHealth:
        try:
            resp = await self._client.get("/v1/models", timeout=5.0)
        except httpx.HTTPError as exc:
            return EngineHealth(ok=False, detail=str(exc))
        if resp.status_code != 200:
            return EngineHealth(ok=False, detail=f"HTTP {resp.status_code}")
        models = [str(m.get("id", "")) for m in resp.json().get("data", [])]
        return EngineHealth(ok=True, models=models)

    def capabilities(self) -> Capabilities:
        return self._caps

    async def aclose(self) -> None:
        await self._client.aclose()


def _usage_from(raw: dict[str, Any]) -> Usage:
    details = raw.get("prompt_tokens_details") or {}
    completion_details = raw.get("completion_tokens_details") or {}
    return Usage(
        prompt_tokens=int(raw.get("prompt_tokens", 0)),
        completion_tokens=int(raw.get("completion_tokens", 0)),
        cached_prefix_tokens=int(details.get("cached_tokens", 0) or 0),
        reasoning_tokens=int(completion_details.get("reasoning_tokens", 0) or 0),
    )

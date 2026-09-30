"""llama.cpp `llama-server` engine: OpenAI dialect + GBNF/json_schema constraints."""

from __future__ import annotations

from typing import Any

import httpx

from yantra_server.gateway.structured import llamacpp_structured_fields

from .base import Capabilities, EngineChatRequest, EngineHealth
from .openai_compat import OpenAICompatEngine


class LlamaCppEngine(OpenAICompatEngine):
    name = "llamacpp"

    def __init__(
        self, base_url: str, *, timeout_s: float = 300.0, api_key: str | None = None
    ) -> None:
        super().__init__(
            base_url,
            timeout_s=timeout_s,
            api_key=api_key,
            capabilities=Capabilities(
                chat=True, embeddings=True, rerank=True, vision=False, tools=True, structured=True
            ),
        )

    def structured_fields(self, request: EngineChatRequest) -> dict[str, Any]:
        return llamacpp_structured_fields(request.constraint)

    def extra_chat_fields(self, request: EngineChatRequest) -> dict[str, Any]:
        if request.constraint is not None:
            return {"chat_template_kwargs": {"enable_thinking": False}}
        return {}

    async def health(self) -> EngineHealth:
        # llama-server exposes /health with loading states; fall back to /v1/models.
        try:
            resp = await self._client.get("/health", timeout=5.0)
            if resp.status_code == 200:
                return EngineHealth(ok=True, models=[])
            if resp.status_code == 503:
                return EngineHealth(ok=False, detail="model loading")
        except httpx.HTTPError:
            pass
        return await super().health()

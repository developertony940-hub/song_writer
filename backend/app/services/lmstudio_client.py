"""LM Studio OpenAI-compatible client with fail-closed model validation."""
from __future__ import annotations

import logging
from typing import Any

import httpx

from app.config import get_settings

log = logging.getLogger(__name__)


class LMStudioModelUnavailable(RuntimeError):
    """Raised before a request is sent when a requested model is not loaded."""


class LMStudioClient:
    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        cfg = get_settings().lmstudio
        self.base_url = cfg.base_url.rstrip("/")
        self.timeout = cfg.request_timeout_s
        self.default_model = cfg.default_model
        self.vision_model = cfg.vision_model
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout)

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def list_models(self) -> list[str]:
        r = await self._client.get("/models")
        r.raise_for_status()
        payload = r.json()
        return [str(item["id"]) for item in payload.get("data", []) if item.get("id")]

    async def validate_model(self, model: str | None = None) -> list[str]:
        """Verify a literal OpenAI model ID against LM Studio's live inventory."""
        target = model or self.default_model
        models = await self.list_models()
        if target not in models:
            available = ", ".join(models) or "(none)"
            raise LMStudioModelUnavailable(
                f"LM Studio model '{target}' is not available from {self.base_url}/models. "
                f"Available: {available}"
            )
        return models

    async def status(self) -> dict[str, Any]:
        try:
            models = await self.list_models()
            return {
                "online": True, "base_url": self.base_url, "loaded_models": models,
                "default_model": self.default_model,
                "default_model_available": self.default_model in models,
                "vision_model": self.vision_model,
                "vision_model_available": self.vision_model in models,
            }
        except Exception as exc:
            return {"online": False, "base_url": self.base_url, "error": str(exc),
                    "default_model": self.default_model, "vision_model": self.vision_model}

    async def chat(self, prompt: str, *, model: str | None = None,
                   system_prompt: str | None = None, temperature: float = 0.7,
                   max_tokens: int = 2048, images_b64: list[str] | None = None,
                   response_format: dict[str, Any] | None = None) -> dict[str, Any]:
        target = model or (self.vision_model if images_b64 else self.default_model)
        await self.validate_model(target)
        if images_b64:
            content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
            content.extend({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image}"}}
                           for image in images_b64)
            user: dict[str, Any] = {"role": "user", "content": content}
        else:
            user = {"role": "user", "content": prompt}
        messages: list[dict[str, Any]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append(user)
        payload: dict[str, Any] = {"model": target, "messages": messages,
                                   "temperature": temperature, "max_tokens": max_tokens}
        if response_format is not None:
            payload["response_format"] = response_format
        response = await self._client.post("/chat/completions", json=payload)
        response.raise_for_status()
        return response.json()

    async def chat_text(self, prompt: str, **kwargs: Any) -> str:
        data = await self.chat(prompt, **kwargs)
        return data["choices"][0]["message"]["content"]

    async def generate_structured(self, prompt: str, *, schema: dict[str, Any],
                                  model: str | None = None,
                                  system_prompt: str | None = None) -> dict[str, Any]:
        import json
        result = await self.chat(prompt, model=model, system_prompt=system_prompt,
                                 response_format={"type": "json_schema", "json_schema": {"name": "out", "schema": schema}})
        return json.loads(result["choices"][0]["message"]["content"])

    async def unload(self) -> dict[str, Any]:
        """Report the real limitation of LM Studio's OpenAI-compatible server.

        It has no documented endpoint that verifies a loaded model was evicted.
        Returning an explicit unverified result keeps ModelRouter status honest
        rather than claiming that GPU0 was freed.
        """
        log.info("LM Studio model unload requested, but no verified server unload API is available")
        return {"attempted": False, "verified": False,
                "reason": "LM Studio OpenAI-compatible API has no verified unload endpoint"}


_client: LMStudioClient | None = None


def get_lmstudio() -> LMStudioClient:
    global _client
    if _client is None:
        _client = LMStudioClient()
    return _client

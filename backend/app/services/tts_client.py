"""Small Kokoro/OpenAI-compatible TTS client."""
from __future__ import annotations
from typing import Any
import httpx
from app.config import get_settings


class TTSClient:
    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        cfg = get_settings().tts
        self.base_url, self.default_voice, self.timeout = cfg.base_url.rstrip("/"), cfg.default_voice, 120
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout)

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def status(self) -> dict[str, Any]:
        try:
            response = await self._client.get("/health")
            response.raise_for_status()
            return {"online": True, "base_url": self.base_url, "engine": get_settings().tts.engine}
        except Exception as exc:
            return {"online": False, "base_url": self.base_url, "error": str(exc)}

    async def synthesize(self, text: str, *, voice: str | None = None, speed: float = 1.0) -> bytes:
        response = await self._client.post("/v1/audio/speech", json={
            "model": get_settings().tts.engine, "input": text, "voice": voice or self.default_voice,
            "speed": speed, "response_format": "wav",
        })
        response.raise_for_status()
        return response.content


_client: TTSClient | None = None

def get_tts() -> TTSClient:
    global _client
    if _client is None:
        _client = TTSClient()
    return _client

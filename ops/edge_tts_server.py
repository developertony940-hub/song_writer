"""Edge-TTS server: OpenAI-compatible /v1/audio/speech endpoint.

Wraps Microsoft Edge's online TTS (edge-tts) behind the same API contract
the backend's TTSClient expects. Runs on http://127.0.0.1:9880.

Usage:
    python ops/edge_tts_server.py
"""
from __future__ import annotations

import asyncio
import io
import logging
from typing import Any

import edge_tts
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger("edge-tts-server")

app = FastAPI(title="Edge-TTS Server", version="1.0.0")

# Map friendly voice names to edge-tts voice IDs
VOICE_MAP: dict[str, str] = {
    "af_heart": "en-US-AvaMultilingualNeural",
    "af_heart_2": "en-US-AnaNeural",
    "am_adam": "en-US-AndrewMultilingualNeural",
    "am_michael": "en-US-BrianMultilingualNeural",
    "bf_emma": "en-GB-SoniaNeural",
    "bm_george": "en-GB-RyanNeural",
}

DEFAULT_VOICE = "en-US-AvaMultilingualNeural"


class SpeechRequest(BaseModel):
    model: str = "tts-1"
    input: str
    voice: str = "af_heart"
    response_format: str = "wav"
    speed: float = 1.0


@app.get("/health")
async def health():
    return {"status": "ok", "engine": "edge-tts"}


@app.post("/v1/audio/speech")
async def create_speech(req: SpeechRequest):
    voice_id = VOICE_MAP.get(req.voice, DEFAULT_VOICE)
    log.info("TTS: voice=%s (%s), text=%d chars, speed=%.1f", req.voice, voice_id, len(req.input), req.speed)
    try:
        rate_pct = int((req.speed - 1) * 100)
        rate_str = f"+{rate_pct}%" if rate_pct > 0 else (f"{rate_pct}%" if rate_pct < 0 else "+0%")
        communicate = edge_tts.Communicate(
            text=req.input,
            voice=voice_id,
            rate=rate_str,
        )
        mp3_buf = io.BytesIO()
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                mp3_buf.write(chunk["data"])
        mp3_bytes = mp3_buf.getvalue()
        if not mp3_bytes:
            raise HTTPException(500, "TTS produced no audio")
        return Response(
            content=mp3_bytes,
            media_type="audio/mpeg",
            headers={"Content-Disposition": f"attachment; filename=speech.mp3"},
        )
    except Exception as e:
        log.exception("TTS failed")
        raise HTTPException(500, f"TTS failed: {e}")


@app.post("/tts")
async def tts_simple(payload: dict[str, Any]):
    """Simple fallback endpoint."""
    req = SpeechRequest(
        input=payload.get("text", ""),
        voice=payload.get("voice", "af_heart"),
        speed=payload.get("speed", 1.0),
    )
    return await create_speech(req)


@app.get("/v1/voices")
async def list_voices():
    voices = await edge_tts.list_voices()
    return {"voices": [{"id": v["ShortName"], "name": v["FriendlyName"]} for v in voices]}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=9880, log_level="info")
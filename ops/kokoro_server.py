"""Kokoro TTS server: OpenAI-compatible /v1/audio/speech endpoint.

Local, offline TTS using the Kokoro-82M model (Apache 2.0). Mirrors the same
API contract the backend's TTSClient expects (same as edge_tts_server.py) so
the pipeline can switch engines without code changes. Runs on 127.0.0.1:9880.

Usage:
    python ops/kokoro_server.py
"""
from __future__ import annotations

import io
import logging
import os
from pathlib import Path
from typing import Any

import torch
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger("kokoro-server")

MODEL_DIR = Path(__file__).resolve().parents[1] / "models" / "kokoro"
MODEL_PATH = MODEL_DIR / "kokoro-v1_0.pth"
CONFIG_PATH = MODEL_DIR / "config.json"
VOICES_DIR = MODEL_DIR / "voices"

app = FastAPI(title="Kokoro TTS Server", version="1.0.0")

# Lazy-load the pipeline on first request (model is ~320MB)
_pipeline = None


def get_pipeline():
    global _pipeline
    if _pipeline is None:
        from kokoro import KModel, KPipeline
        device = "cuda" if torch.cuda.is_available() else "cpu"
        log.info("Loading Kokoro model on %s...", device)
        model = KModel(config=str(CONFIG_PATH), model=str(MODEL_PATH)).to(device).eval()
        _pipeline = KPipeline(lang_code="a", model=model, device=device)
        log.info("Kokoro model loaded.")
    return _pipeline


class SpeechRequest(BaseModel):
    model: str = "tts-1"
    input: str
    voice: str = "af_heart"
    response_format: str = "wav"
    speed: float = 1.0


@app.get("/health")
async def health():
    return {"status": "ok", "engine": "kokoro"}


@app.post("/v1/audio/speech")
async def create_speech(req: SpeechRequest):
    if not req.input:
        raise HTTPException(400, "Empty input text")
    log.info("TTS: voice=%s, text=%d chars, speed=%.1f", req.voice, len(req.input), req.speed)
    try:
        pipe = get_pipeline()
        # Load voice pack
        voice_path = VOICES_DIR / f"{req.voice}.pt"
        if not voice_path.exists():
            raise HTTPException(400, f"Unknown voice: {req.voice}")
        # Synthesize (pipe is callable: pipe(text, voice=..., speed=...))
        wav_buf = io.BytesIO()
        import wave
        with wave.open(wav_buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(24000)
            for result in pipe(req.input, voice=req.voice, speed=req.speed):
                audio = result.audio
                if audio.dim() > 1:
                    audio = audio.squeeze(0)
                pcm = (audio * 32767).clamp(-32768, 32767).to(torch.int16).cpu().numpy()
                wf.writeframes(pcm.tobytes())
        wav_bytes = wav_buf.getvalue()
        if not wav_bytes:
            raise HTTPException(500, "TTS produced no audio")
        return Response(
            content=wav_bytes,
            media_type="audio/wav",
            headers={"Content-Disposition": "attachment; filename=speech.wav"},
        )
    except HTTPException:
        raise
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
    voices = [f.stem for f in VOICES_DIR.glob("*.pt")] if VOICES_DIR.exists() else []
    return {"voices": [{"id": v, "name": v} for v in voices]}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=9880, log_level="info")

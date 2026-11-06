"""Voice-note transcription (optional).

Backends, picked by VOICE_BACKEND (default "auto"):
  local  -> faster-whisper on your PC (pip install "agy-telegram-bridge[voice]"),
            free and private; the model downloads once on first use.
  openai -> OpenAI's transcription API (OPENAI_API_KEY), no extra installs.
  auto   -> local if faster-whisper is installed, else openai if a key is set.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from pathlib import Path

log = logging.getLogger(__name__)

_model = None
_model_lock = asyncio.Lock()


class VoiceUnavailable(RuntimeError):
    pass


def _has_faster_whisper() -> bool:
    try:
        import faster_whisper  # noqa: F401
    except ImportError:
        return False
    return True


def backend_name(configured: str, api_key: str) -> str:
    configured = (configured or "auto").lower()
    if configured == "off":
        return ""
    if configured == "local":
        return "local" if _has_faster_whisper() else ""
    if configured == "openai":
        return "openai" if api_key else ""
    if _has_faster_whisper():
        return "local"
    return "openai" if api_key else ""


async def transcribe(path: Path, backend: str, api_key: str = "", model: str = "", language: str = "") -> str:
    if backend == "local":
        return await _local(path, model or "base", language)
    if backend == "openai":
        return await asyncio.to_thread(_openai, path, api_key, model or "whisper-1", language)
    raise VoiceUnavailable(
        "Voice notes need a transcriber: `pip install faster-whisper` (local, free) "
        "or set OPENAI_API_KEY in .env."
    )


async def _local(path: Path, model_name: str, language: str) -> str:
    global _model
    async with _model_lock:
        if _model is None:
            from faster_whisper import WhisperModel

            log.info("loading faster-whisper model %r (first use downloads it)", model_name)
            _model = await asyncio.to_thread(WhisperModel, model_name, device="auto", compute_type="int8")

    def run() -> str:
        segments, _info = _model.transcribe(str(path), language=language or None, vad_filter=True)
        return " ".join(s.text.strip() for s in segments).strip()

    return await asyncio.to_thread(run)


def _openai(path: Path, api_key: str, model: str, language: str) -> str:
    import urllib.request

    boundary = uuid.uuid4().hex
    parts: list[bytes] = []

    def field(name: str, value: str) -> None:
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())

    field("model", model)
    if language:
        field("language", language)
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{path.name}"\r\n'
        f"Content-Type: application/octet-stream\r\n\r\n".encode()
        + path.read_bytes()
        + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())
    req = urllib.request.Request(
        os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/") + "/audio/transcriptions",
        data=b"".join(parts),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:  # noqa: S310 - fixed https endpoint
        return (json.loads(resp.read().decode("utf-8")).get("text") or "").strip()

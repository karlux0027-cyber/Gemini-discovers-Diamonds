"""
KRLX Sovereign Ingestion Layer — Local Transcription + TTS Server
==================================================================
Voice IN:  Receives audio from Karl's Pixel 10 via Tailscale, transcribes
           locally on RTX 5090 using faster-whisper, saves to KRLX_Inbox.
Voice OUT: Receives text, speaks it back as audio using Piper TTS (local).

Security: Tailscale-only access. No cloud. Fully sovereign.

10-80-10 Rule:
  - Karl owns the first 10%: direction, constraints, architecture decision
  - Digital Twin owns the middle 80%: this implementation
  - Karl owns the last 10%: review this file, approve, then launch
"""

import os
import io
import uuid
import wave
import struct
import logging
import subprocess
from datetime import datetime
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI, UploadFile, File, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from faster_whisper import WhisperModel

# ─────────────────────────────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────────────────────────────

TAILSCALE_BIND_IP = os.getenv("KRLX_BIND_IP", "0.0.0.0")
PORT = int(os.getenv("KRLX_PORT", "8741"))

INBOX_DIR = Path(os.getenv(
    "KRLX_INBOX",
    os.path.expanduser("~/Desktop/KRLX_Inbox")
))

# Whisper (Speech-to-Text)
MODEL_SIZE = os.getenv("KRLX_MODEL", "large-v3")
DEVICE = "cuda"
COMPUTE_TYPE = "float16"

# Piper TTS (Text-to-Speech) — natural male voice, runs on CPU (fast enough)
# Available voices: https://rhasspy.github.io/piper-samples/
TTS_VOICE = os.getenv("KRLX_TTS_VOICE", "en_US-ryan-high")

# Limits
MAX_UPLOAD_BYTES = 100 * 1024 * 1024
MAX_TTS_CHARS = 10000

# Security
TAILSCALE_PREFIX = "100."

# ─────────────────────────────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("krlx-ingestion")

# ─────────────────────────────────────────────────────────────────────
# MODEL LOADING
# ─────────────────────────────────────────────────────────────────────

model: WhisperModel | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Load Whisper model into VRAM on startup."""
    global model
    logger.info(f"Loading faster-whisper: {MODEL_SIZE} on {DEVICE} ({COMPUTE_TYPE})")
    model = WhisperModel(MODEL_SIZE, device=DEVICE, compute_type=COMPUTE_TYPE)
    logger.info("Whisper model loaded.")
    INBOX_DIR.mkdir(parents=True, exist_ok=True)
    logger.info(f"Inbox: {INBOX_DIR}")
    logger.info(f"TTS Voice: {TTS_VOICE}")
    logger.info(f"Server ready on {TAILSCALE_BIND_IP}:{PORT}")
    yield
    logger.info("KRLX Ingestion Server shutting down.")


# ─────────────────────────────────────────────────────────────────────
# APP
# ─────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="KRLX Ingestion Server",
    description="Sovereign voice I/O — transcribe in, speak out. No cloud.",
    version="2.0.0",
    lifespan=lifespan,
)


# ─────────────────────────────────────────────────────────────────────
# SECURITY MIDDLEWARE
# ─────────────────────────────────────────────────────────────────────

@app.middleware("http")
async def tailscale_guard(request: Request, call_next):
    """Only allow Tailscale IPs (100.x.x.x) and localhost."""
    client_ip = request.client.host if request.client else "unknown"
    allowed = (
        client_ip.startswith(TAILSCALE_PREFIX)
        or client_ip in ("127.0.0.1", "::1", "localhost")
    )
    if not allowed:
        logger.warning(f"BLOCKED: {client_ip}")
        return JSONResponse(status_code=403, content={"error": "Tailscale only."})
    return await call_next(request)


# ─────────────────────────────────────────────────────────────────────
# ENDPOINTS
# ─────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    """Health check — model status, TTS voice, inbox path."""
    return {
        "status": "operational",
        "whisper_model": MODEL_SIZE,
        "tts_voice": TTS_VOICE,
        "device": DEVICE,
        "inbox": str(INBOX_DIR),
    }


@app.post("/transcribe")
async def transcribe(audio: UploadFile = File(...)):
    """
    VOICE IN: Receive audio, transcribe locally, save to KRLX_Inbox.
    Accepts: .mp3, .m4a, .wav, .ogg, .webm, .flac, .mp4
    Returns: JSON with transcript text + metadata.
    """
    if model is None:
        raise HTTPException(status_code=503, detail="Model not loaded.")

    allowed_extensions = {".mp3", ".m4a", ".wav", ".ogg", ".webm", ".flac", ".mp4"}
    ext = Path(audio.filename or "audio.wav").suffix.lower()
    if ext not in allowed_extensions:
        raise HTTPException(status_code=400, detail=f"Bad format: {ext}")

    content = await audio.read()
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="File too large. Max 100MB.")

    temp_path = INBOX_DIR / f"_temp_{uuid.uuid4().hex}{ext}"
    try:
        temp_path.write_bytes(content)

        logger.info(f"Transcribing: {audio.filename} ({len(content) / 1024:.1f} KB)")
        segments, info = model.transcribe(
            str(temp_path),
            beam_size=5,
            language="en",
            vad_filter=True,
            vad_parameters=dict(min_silence_duration_ms=500),
        )

        full_text = ""
        segment_list = []
        for segment in segments:
            full_text += segment.text + " "
            segment_list.append({
                "start": round(segment.start, 2),
                "end": round(segment.end, 2),
                "text": segment.text.strip(),
            })
        full_text = full_text.strip()

        # Save to inbox
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_name = Path(audio.filename or "audio").stem[:50]
        output_filename = f"{timestamp}_{safe_name}.txt"
        output_path = INBOX_DIR / output_filename

        output_path.write_text(
            f"# KRLX Transcription\n"
            f"# Source: {audio.filename}\n"
            f"# Date: {datetime.now().isoformat()}\n"
            f"# Duration: {info.duration:.1f}s\n"
            f"# Language: {info.language} (prob: {info.language_probability:.2f})\n"
            f"\n{full_text}\n",
            encoding="utf-8"
        )

        logger.info(f"Saved: {output_path} ({len(full_text)} chars)")

        return {
            "status": "success",
            "filename": output_filename,
            "text": full_text,
            "duration_seconds": round(info.duration, 1),
            "language": info.language,
            "language_probability": round(info.language_probability, 3),
            "segments": segment_list,
            "saved_to": str(output_path),
        }

    except Exception as e:
        logger.error(f"Transcription failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if temp_path.exists():
            temp_path.unlink()


@app.post("/speak")
async def speak(request: Request):
    """
    VOICE OUT: Send text, get spoken audio back as WAV.
    Uses Piper TTS — local neural voice, no cloud.

    Body: {"text": "Your message here"}
    Returns: audio/wav stream (play directly on phone/speaker)
    """
    try:
        body = await request.json()
        text = body.get("text", "").strip()

        if not text:
            raise HTTPException(status_code=400, detail="No text provided.")
        if len(text) > MAX_TTS_CHARS:
            raise HTTPException(status_code=400, detail=f"Max {MAX_TTS_CHARS} chars.")

        logger.info(f"TTS: {len(text)} chars")

        # Piper outputs raw PCM (16-bit, 22050Hz mono) to stdout
        process = subprocess.run(
            ["piper", "--model", TTS_VOICE, "--output-raw"],
            input=text.encode("utf-8"),
            capture_output=True,
            timeout=120,
        )

        if process.returncode != 0:
            error_msg = process.stderr.decode()[:200]
            logger.error(f"Piper failed: {error_msg}")
            raise HTTPException(status_code=500, detail=f"TTS failed: {error_msg}")

        raw_audio = process.stdout

        # Wrap raw PCM in a proper WAV header
        sample_rate = 22050
        channels = 1
        sample_width = 2  # 16-bit

        wav_buffer = io.BytesIO()
        with wave.open(wav_buffer, "wb") as wav_file:
            wav_file.setnchannels(channels)
            wav_file.setsampwidth(sample_width)
            wav_file.setframerate(sample_rate)
            wav_file.writeframes(raw_audio)

        wav_buffer.seek(0)

        return StreamingResponse(
            wav_buffer,
            media_type="audio/wav",
            headers={"Content-Disposition": "inline; filename=krlx_response.wav"}
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"TTS error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/converse")
async def converse(audio: UploadFile = File(...)):
    """
    FULL LOOP: Send audio in, get transcript + spoken response back.
    This is the foundation for voice-to-voice interaction.

    Step 1: Transcribe the audio (same as /transcribe)
    Step 2: Return the text (you route it to LLM, get response, send to /speak)

    For now this just transcribes and confirms. The LLM routing layer
    will be added next to complete the loop.
    """
    # Just transcribe for now — the routing layer connects this to LLM + TTS
    result = await transcribe(audio)
    return result


# ─────────────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "app:app",
        host=TAILSCALE_BIND_IP,
        port=PORT,
        log_level="info",
    )

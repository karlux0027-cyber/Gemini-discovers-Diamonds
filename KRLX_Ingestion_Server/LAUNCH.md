# KRLX Ingestion Server v2.0 — Voice In + Voice Out

## What This Does

- **`/transcribe`** — You speak into your phone → audio goes to your rig → comes back as text
- **`/speak`** — You send text → your rig speaks it back as audio (WAV)
- **`/converse`** — Full loop foundation (transcribe now, LLM routing added next)
- **`/health`** — Quick status check

## Prerequisites

1. **NVIDIA Driver 565+** (RTX 5090 / Blackwell)
2. **CUDA 12.6+** and **cuDNN 9.x**
3. **Python 3.11+**
4. **Tailscale** running and connected

Verify GPU:
```bash
nvidia-smi
```

## One-Time Setup

```bash
# Create project folder
mkdir -p ~/Desktop/KRLX_Ingestion_Server
cd ~/Desktop/KRLX_Ingestion_Server

# Create virtual environment
python -m venv .venv
source .venv/bin/activate

# Install everything
pip install -r requirements.txt

# Create inbox folder
mkdir -p ~/Desktop/KRLX_Inbox
```

## Launch

```bash
cd ~/Desktop/KRLX_Ingestion_Server
source .venv/bin/activate
python app.py
```

Server starts on port `8741`. First launch downloads the Whisper model (~3GB) and Piper voice model (~50MB). After that, startup takes ~5 seconds.

## Test Commands

```bash
# Health check
curl http://localhost:8741/health

# Transcribe audio
curl -X POST http://localhost:8741/transcribe \
  -F "audio=@/path/to/voice_memo.m4a"

# Text-to-speech (hear a response)
curl -X POST http://localhost:8741/speak \
  -H "Content-Type: application/json" \
  -d '{"text": "Kirk, your life insurance production is up 40 percent this month."}' \
  --output response.wav

# Play the response (Linux)
aplay response.wav
# or (Windows)
# start response.wav
```

## From Your Pixel 10

Use **HTTP Shortcuts** app (free on Play Store) or **Tasker** to:

1. Record audio
2. POST it to `http://<YOUR_TAILSCALE_IP>:8741/transcribe`
3. Get text back
4. (Optional) Send text to `/speak` and play the WAV

Your Tailscale IP: run `tailscale ip -4` on your rig.

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `KRLX_BIND_IP` | `0.0.0.0` | Bind address |
| `KRLX_PORT` | `8741` | Server port |
| `KRLX_INBOX` | `~/Desktop/KRLX_Inbox` | Transcript output folder |
| `KRLX_MODEL` | `large-v3` | Whisper model (large-v3 = best accuracy) |
| `KRLX_TTS_VOICE` | `en_US-ryan-high` | Piper voice (natural male) |

## Security

- **Tailscale WireGuard mesh** — only your devices can reach this server
- **Application middleware** — rejects non-100.x.x.x IPs
- **No cloud** — all processing on your RTX 5090
- **No API keys** — Tailscale identity = authentication

## Architecture (Where This Is Going)

```
Phone (voice) → /transcribe → text
                                ↓
                         [Router Layer] ← (NEXT BUILD)
                           /        \
                    Insurance       Content
                    workflow         workflow
                      ↓                ↓
                 CRM notes         Video script
                 Client outreach   YouTube post
                      ↓                ↓
                   /speak ←── LLM response ──→ /speak
                   (audio back to you)
```

The router layer is the next piece. It takes transcribed text, classifies intent, and sends it to the right workflow. That's what makes "talk into phone → video posts to YouTube" possible.

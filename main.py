import os
import sys
import io
import json
import base64
import wave
import asyncio
import logging
import contextlib
from typing import Optional

import audioop

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, Response
from fastapi.responses import PlainTextResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

# ✅ Sarvam official SDK
from sarvamai import AsyncSarvamAI

# Windows asyncio policy (safe to set)
if sys.platform.startswith("win"):
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

load_dotenv()

# ---------- Config ----------
PORT = int(os.getenv("PORT", "3000"))
NGROK_HOST = os.getenv("NGROK_HOST")  # required

SARVAM_API_KEY = os.getenv("SARVAM_API_KEY")  # required
SARVAM_LANGUAGE_CODE = os.getenv("SARVAM_LANGUAGE_CODE", "en-IN")
SARVAM_STT_MODEL = os.getenv("SARVAM_STT_MODEL", "saarika:v2.5")  # optional; default ok

# Twilio sends μ-law at 8k; we upsample to 16k for better STT
TWILIO_IN_RATE = 8000
TWILIO_SAMPLE_RATE = 16000  # what we send to Sarvam

# ~300 ms buffering at 16 kHz mono PCM16
BUFFER_MS = 300
BYTES_PER_SAMPLE = 2
CHANNELS = 1
TARGET_RATE = TWILIO_SAMPLE_RATE
TARGET_CHUNK_BYTES = int(TARGET_RATE * CHANNELS * BYTES_PER_SAMPLE * (BUFFER_MS / 1000.0))

# LOG_LEVEL can be INFO or DEBUG (set in .env if you like)
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("twilio-sarvam-sdk")

app = FastAPI(title="Twilio ↔ Sarvam.ai STT (FastAPI + SDK)")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------- Audio conversion (pure Python: no ffmpeg) ----------

async def mulaw_to_wav_bytes(mulaw_bytes: bytes) -> bytes:
    """
    Convert μ-law @8k mono to WAV (PCM16) @16k using pure Python.
    Steps:
      1) μ-law -> PCM16: audioop.ulaw2lin
      2) 8k -> 16k resample: audioop.ratecv
      3) Wrap PCM16 in a WAV header (wave module)
    """
    # 1) μ-law -> linear PCM16 @ 8k
    pcm16_8k = audioop.ulaw2lin(mulaw_bytes, 2)  # width=2 bytes/sample

    # 2) Resample 8k -> 16k (mono)
    if TWILIO_SAMPLE_RATE != TWILIO_IN_RATE:
        pcm16_16k, _ = audioop.ratecv(
            pcm16_8k,  # fragment
            2,         # width
            1,         # nchannels
            TWILIO_IN_RATE,
            TWILIO_SAMPLE_RATE,
            None,      # state
        )
    else:
        pcm16_16k = pcm16_8k

    # 3) Build a WAV in-memory
    with io.BytesIO() as buf:
        with wave.open(buf, 'wb') as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(TWILIO_SAMPLE_RATE)
            wf.writeframes(pcm16_16k)
        return buf.getvalue()


# ---------- Sarvam recv logger ----------

async def sarvam_recv_logger(ws):
    """
    Background task: log partial/final transcripts from Sarvam.
    Works for both typed SDK objects and plain dict/JSON.
    """
    try:
        while True:
            msg = await ws.recv()

            # Typed SDK model (e.g., SpeechToTextStreamingResponse)
            if hasattr(msg, "type"):
                mtype = getattr(msg, "type", None)
                data  = getattr(msg, "data", None)

                # data may itself be a typed object
                text = None
                if data is not None:
                    text = getattr(data, "text", None) or getattr(data, "transcript", None)
                    # fallback if dict-like
                    if text is None and isinstance(data, dict):
                        text = data.get("text") or data.get("transcript")

                if mtype == "partial":
                    logger.info("📝 Partial: %s", text)
                elif mtype == "final":
                    logger.info("✅ Final: %s", text)
                elif mtype == "data":
                    metrics = getattr(data, "metrics", None) if data is not None else None
                    logger.info("📊 Data: transcript=%r, metrics=%r", text, metrics)
                else:
                    logger.info("📦 Other (typed): %r", msg)
                continue

            # If the SDK returns JSON text or bytes
            if isinstance(msg, (bytes, str)):
                try:
                    msg = json.loads(msg)
                except Exception:
                    logger.info("📦 Sarvam message (raw): %s", msg)
                    continue

            # Dict or list of dicts
            if isinstance(msg, list):
                for item in msg:
                    if isinstance(item, dict):
                        mtype = item.get("type")
                        data  = item.get("data", {})
                        text  = (data or {}).get("text") or (data or {}).get("transcript")
                        if mtype == "partial":
                            logger.info("📝 Partial: %s", text)
                        elif mtype == "final":
                            logger.info("✅ Final: %s", text)
                        elif mtype == "data":
                            logger.info("📊 Data: transcript=%r, metrics=%r", text, (data or {}).get("metrics"))
                        else:
                            logger.info("📦 Other (dict): %s", item)
                continue

            if isinstance(msg, dict):
                mtype = msg.get("type")
                data  = msg.get("data", {})
                text  = (data or {}).get("text") or (data or {}).get("transcript")
                if mtype == "partial":
                    logger.info("📝 Partial: %s", text)
                elif mtype == "final":
                    logger.info("✅ Final: %s", text)
                elif mtype == "data":
                    logger.info("📊 Data: transcript=%r, metrics=%r", text, (data or {}).get("metrics"))
                else:
                    logger.info("📦 Other (dict): %s", msg)
            else:
                logger.info("📦 Sarvam message (unknown): %r", msg)

    except asyncio.CancelledError:
        pass
    except Exception:
        logger.exception("❌ Sarvam recv loop error:")


# ---------- Routes ----------

@app.get("/")
async def root():
    return {
        "ok": True,
        "message": "Twilio ↔ Sarvam.ai STT bridge running",
        "voice_webhook": f"https://{NGROK_HOST}/voice" if NGROK_HOST else None
    }

@app.get("/health")
async def health():
    return JSONResponse({
        "ok": bool(NGROK_HOST and SARVAM_API_KEY),
        "ngrok_host_set": bool(NGROK_HOST),
        "sarvam_key_set": bool(SARVAM_API_KEY),
        "model": SARVAM_STT_MODEL,
        "language": SARVAM_LANGUAGE_CODE
    })


@app.post("/voice", response_class=PlainTextResponse)
async def twilio_voice_webhook(request: Request) -> Response:
    """
    Returns TwiML that:
      - Speaks a quick line
      - Opens <Connect><Stream> to our /audio WS
      - No <Pause>: the stream stays active until the caller hangs up
    """
    if not NGROK_HOST:
        return PlainTextResponse("Missing NGROK_HOST", status_code=500)

    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Say>This is connected to Chirag's Laptop</Say>
  <Connect>
    <Stream url="wss://{NGROK_HOST}/audio" />
  </Connect>
</Response>
"""
    return PlainTextResponse(content=twiml, media_type="text/xml")


@app.websocket("/audio")
async def audio_ws(ws: WebSocket):
    """
    Twilio connects here and sends JSON messages:
      - {"event":"start", ...}
      - {"event":"media","media":{"payload":"<base64 μ-law 8k mono>"}}
      - {"event":"stop"}
    We convert μ-law -> WAV (16k) in pure Python and stream to Sarvam via their SDK.
    """
    await ws.accept()
    logger.info("📞 Twilio WS connected.")

    if not SARVAM_API_KEY:
        await ws.close(code=1011)
        logger.error("SARVAM_API_KEY missing.")
        return

    client = AsyncSarvamAI(api_subscription_key=SARVAM_API_KEY)

    logger.info("Connecting to Sarvam SDK (model=%s, lang=%s)...", SARVAM_STT_MODEL, SARVAM_LANGUAGE_CODE)
    sarvam_ctx = client.speech_to_text_streaming.connect(
        language_code=SARVAM_LANGUAGE_CODE,
        model=SARVAM_STT_MODEL,
        high_vad_sensitivity=False,   # less strict -> partials sooner
        # if supported by SDK: disable_vad=True,
    )

    recv_task: Optional[asyncio.Task] = None
    media_count = 0
    pcm_buffer = bytearray()  # accumulate ~300ms of PCM16@16k

    try:
        async with sarvam_ctx as sarvam_ws:
            logger.info("✅ Sarvam SDK session established.")
            # Background logger for partial/final/data messages
            recv_task = asyncio.create_task(sarvam_recv_logger(sarvam_ws))

            while True:
                raw = await ws.receive_text()
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    logger.warning("Skipping non-JSON frame from Twilio")
                    continue

                event = msg.get("event")

                if event == "start":
                    logger.info("▶️ Twilio stream started: %s", msg.get("start", {}))

                elif event == "media":
                    media = msg.get("media", {})
                    b64_ulaw = media.get("payload")
                    if not b64_ulaw:
                        continue

                    # 1) decode base64 μ-law
                    try:
                        ulaw_bytes = base64.b64decode(b64_ulaw)
                    except Exception as e:
                        logger.error("Failed base64 decode: %s", e)
                        continue

                    # 2) μ-law (8k) -> WAV (PCM16, 16k) in pure Python
                    try:
                        wav_bytes = await mulaw_to_wav_bytes(ulaw_bytes)
                    except Exception:
                        logger.exception("μ-law → WAV conversion failed:")
                        continue
                    if not wav_bytes or len(wav_bytes) < 44:
                        continue

                    media_count += 1
                    if media_count % 50 == 0:
                        logger.debug("Received %d media frames so far", media_count)

                    # Strip 44-byte WAV header; buffer raw PCM to build ~300ms chunks
                    pcm_payload = wav_bytes[44:]
                    pcm_buffer.extend(pcm_payload)

                    # Once we have ~300 ms, wrap and send one WAV
                    if len(pcm_buffer) >= TARGET_CHUNK_BYTES:
                        with io.BytesIO() as buf:
                            with wave.open(buf, 'wb') as wf:
                                wf.setnchannels(CHANNELS)
                                wf.setsampwidth(BYTES_PER_SAMPLE)
                                wf.setframerate(TARGET_RATE)
                                wf.writeframes(pcm_buffer)
                            wav_to_send = buf.getvalue()

                        b64_wav = base64.b64encode(wav_to_send).decode()
                        logger.debug("Sending WAV chunk to Sarvam (%d base64 chars)", len(b64_wav))
                        await sarvam_ws.transcribe(
                            audio=b64_wav,
                            encoding="audio/wav",
                            sample_rate=TARGET_RATE,  # 16000
                        )
                        pcm_buffer.clear()

                elif event == "stop":
                    # Flush any remainder
                    if pcm_buffer:
                        with io.BytesIO() as buf:
                            with wave.open(buf, 'wb') as wf:
                                wf.setnchannels(CHANNELS)
                                wf.setsampwidth(BYTES_PER_SAMPLE)
                                wf.setframerate(TARGET_RATE)
                                wf.writeframes(pcm_buffer)
                            wav_to_send = buf.getvalue()
                        b64_wav = base64.b64encode(wav_to_send).decode()
                        logger.debug("Flushing final WAV chunk (%d base64 chars)", len(b64_wav))
                        await sarvam_ws.transcribe(
                            audio=b64_wav,
                            encoding="audio/wav",
                            sample_rate=TARGET_RATE,
                        )
                        pcm_buffer.clear()

                    logger.info("⏹️ Twilio stream stopped.")
                    break

                else:
                    # (Optional) handle 'mark', 'dtmf', etc.
                    logger.debug("Unhandled Twilio event: %s", event)

    except WebSocketDisconnect:
        logger.info("📴 Twilio WS disconnected.")
    except Exception:
        logger.exception("💥 Error in /audio:")  # prints stack trace
    finally:
        if recv_task:
            recv_task.cancel()
            with contextlib.suppress(Exception):
                await recv_task
        with contextlib.suppress(Exception):
            await ws.close()


# ---------- Entrypoint ----------
if __name__ == "__main__":
    import uvicorn
    logger.info(f"Twilio webhook: https://{NGROK_HOST}/voice")
    uvicorn.run("main:app", host="0.0.0.0", port=PORT, reload=False)

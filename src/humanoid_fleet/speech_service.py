from __future__ import annotations

import io
import os
from functools import lru_cache
from pathlib import Path

import numpy as np
import sherpa_onnx
import soundfile as sf
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ASR_MODEL_DIR = PROJECT_ROOT / "models/speech/sherpa-onnx-paraformer-zh-small-2024-03-09"
DEFAULT_TTS_MODEL_DIR = PROJECT_ROOT / "models/speech/vits-melo-tts-zh_en"
TARGET_ASR_SAMPLE_RATE = 16000

app = FastAPI(title="Humanoid Fleet Chinese Speech Service")


def _path_from_env(name: str, default: Path) -> Path:
    return Path(os.getenv(name, str(default))).expanduser().resolve()


def _thread_count() -> int:
    return max(1, int(os.getenv("SPEECH_NUM_THREADS", "4")))


@lru_cache(maxsize=1)
def _recognizer() -> sherpa_onnx.OfflineRecognizer:
    model_dir = _path_from_env("SPEECH_ASR_MODEL_DIR", DEFAULT_ASR_MODEL_DIR)
    model = model_dir / "model.int8.onnx"
    tokens = model_dir / "tokens.txt"
    if not model.exists() or not tokens.exists():
        raise RuntimeError(f"ASR model is incomplete under {model_dir}")

    return sherpa_onnx.OfflineRecognizer.from_paraformer(
        paraformer=str(model),
        tokens=str(tokens),
        num_threads=_thread_count(),
        sample_rate=TARGET_ASR_SAMPLE_RATE,
        provider=os.getenv("SPEECH_ONNX_PROVIDER", "cpu"),
    )


@lru_cache(maxsize=1)
def _tts() -> sherpa_onnx.OfflineTts:
    model_dir = _path_from_env("SPEECH_TTS_MODEL_DIR", DEFAULT_TTS_MODEL_DIR)
    model_name = os.getenv("SPEECH_TTS_MODEL_NAME", "model.onnx")
    model = model_dir / model_name
    if not model.exists() and model_name != "model.onnx":
        model = model_dir / "model.onnx"
    lexicon = model_dir / "lexicon.txt"
    tokens = model_dir / "tokens.txt"
    if not model.exists() or not lexicon.exists() or not tokens.exists():
        raise RuntimeError(f"TTS model is incomplete under {model_dir}")

    vits = sherpa_onnx.OfflineTtsVitsModelConfig(
        model=str(model),
        lexicon=str(lexicon),
        tokens=str(tokens),
        length_scale=float(os.getenv("SPEECH_TTS_LENGTH_SCALE", "1.0")),
    )
    model_config = sherpa_onnx.OfflineTtsModelConfig(
        vits=vits,
        num_threads=_thread_count(),
        provider=os.getenv("SPEECH_ONNX_PROVIDER", "cpu"),
    )
    config = sherpa_onnx.OfflineTtsConfig(
        model=model_config,
        max_num_sentences=int(os.getenv("SPEECH_TTS_MAX_SENTENCES", "2")),
    )
    return sherpa_onnx.OfflineTts(config)


@lru_cache(maxsize=64)
def _synthesize_wav(text: str, speaker_id: int) -> bytes:
    audio = _tts().generate(text, sid=speaker_id, speed=float(os.getenv("SPEECH_TTS_SPEED", "1.12")))
    buffer = io.BytesIO()
    sf.write(buffer, audio.samples, audio.sample_rate, format="WAV")
    return buffer.getvalue()


@app.on_event("startup")
def _warm_speech_models() -> None:
    if os.getenv("SPEECH_WARMUP", "true").strip().lower() in {"0", "false", "no"}:
        return
    try:
        _synthesize_wav("你好", int(os.getenv("SPEECH_TTS_SPEAKER_ID", "0")))
    except Exception:
        # Keep startup resilient; health still reports model file readiness.
        pass


def _to_mono_float32(samples: np.ndarray) -> np.ndarray:
    if samples.ndim == 2:
        samples = samples.mean(axis=1)
    return samples.astype(np.float32, copy=False)


def _resample_linear(samples: np.ndarray, source_rate: int, target_rate: int) -> np.ndarray:
    if source_rate == target_rate:
        return samples
    if samples.size == 0:
        return samples

    duration = samples.size / float(source_rate)
    target_size = max(1, int(round(duration * target_rate)))
    source_positions = np.linspace(0.0, duration, num=samples.size, endpoint=False)
    target_positions = np.linspace(0.0, duration, num=target_size, endpoint=False)
    return np.interp(target_positions, source_positions, samples).astype(np.float32)


@app.get("/health")
def health() -> dict[str, object]:
    asr_dir = _path_from_env("SPEECH_ASR_MODEL_DIR", DEFAULT_ASR_MODEL_DIR)
    tts_dir = _path_from_env("SPEECH_TTS_MODEL_DIR", DEFAULT_TTS_MODEL_DIR)
    return {
        "status": "ok",
        "language": "zh",
        "asr_model_dir": str(asr_dir),
        "tts_model_dir": str(tts_dir),
        "asr_ready": (asr_dir / "model.int8.onnx").exists(),
        "tts_ready": (tts_dir / "model.onnx").exists(),
    }


@app.post("/asr")
async def asr(audio: UploadFile = File(...)) -> dict[str, object]:
    try:
        raw = await audio.read()
        samples, sample_rate = sf.read(io.BytesIO(raw), dtype="float32", always_2d=False)
        samples = _resample_linear(_to_mono_float32(samples), sample_rate, TARGET_ASR_SAMPLE_RATE)

        stream = _recognizer().create_stream()
        stream.accept_waveform(TARGET_ASR_SAMPLE_RATE, samples)
        _recognizer().decode_stream(stream)
        text = stream.result.text.strip()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {
        "text": text,
        "language": "zh",
        "sample_rate": TARGET_ASR_SAMPLE_RATE,
        "filename": audio.filename,
    }


@app.post("/tts")
async def tts(
    text: str = Form(...),
    speaker_id: int = Form(int(os.getenv("SPEECH_TTS_SPEAKER_ID", "0"))),
) -> Response:
    cleaned = text.strip()
    if not cleaned:
        raise HTTPException(status_code=400, detail="text is required")

    try:
        audio = await run_in_threadpool(_synthesize_wav, cleaned, speaker_id)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return Response(content=audio, media_type="audio/wav")

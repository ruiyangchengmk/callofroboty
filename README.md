# Humanoid Fleet Control Prototype

This repository contains a lightweight Python prototype for a hierarchical humanoid robot fleet control system.

## What is implemented

- Model-first task understanding interface with multimodal-ready input objects
- Execution resolution order: SOP workflow first, then direct skill, then agent
- Task memory with a reusable workflow template
- Hierarchical orchestration from intent to workflow to skills
- Single-robot versus multi-robot scheduling
- Skill runtime execution planning

## Understanding architecture

The understanding layer is now designed as:

- `UserInput`: unified text and attachment input
- `HybridTaskInterpreter`: model-first parsing with deterministic fallback
- `ModelClient`: pluggable interface for a real LLM or VLM backend
- `StructuredIntentNormalizer`: converts model JSON into typed intents

The local prototype still uses a mock model client so it remains runnable without external API calls, but the orchestration layer no longer depends on handwritten parsing rules directly.

## Execution policy

The runtime selection order is:

1. Approved SOP or workflow template
2. Direct skill invocation
3. Agentic decomposition for uncovered requests

This keeps familiar store procedures deterministic, transparent, and low-latency.

## Run the demo

```bash
PYTHONPATH=src python3 -m humanoid_fleet.demo
```

## Run the web preview

```bash
PYTHONPATH=src python3 -m humanoid_fleet.web
```

Then open [http://127.0.0.1:8000](http://127.0.0.1:8000).

To use a local Ollama model for task understanding:

```bash
HUMANOID_FLEET_LLM_BACKEND=ollama OLLAMA_MODEL=qwen3.5:0.8b PYTHONPATH=src python3 -m humanoid_fleet.web
```

Thinking output is disabled by default for Ollama requests. Set `OLLAMA_DISABLE_THINKING=false`
if you want to enable it again.

## Run the local Chinese speech service

The speech service uses CPU-friendly `sherpa-onnx` models so it can run on aarch64 edge devices.
It exposes Chinese ASR and TTS over HTTP:

```bash
python3 -m venv .venv-speech
source .venv-speech/bin/activate
python -m pip install -r requirements-speech.txt
PYTHONPATH=src uvicorn humanoid_fleet.speech_service:app --host 0.0.0.0 --port 8010
```

Default model paths:

- ASR: `models/speech/sherpa-onnx-paraformer-zh-small-2024-03-09`
- TTS: `models/speech/vits-melo-tts-zh_en`

Example calls:

```bash
curl -F "audio=@models/speech/sherpa-onnx-paraformer-zh-small-2024-03-09/test_wavs/0.wav" http://127.0.0.1:8010/asr
curl -X POST -F "text=你好，我是门店服务机器人。" http://127.0.0.1:8010/tts --output outputs/tts.wav
```

## Run the test

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_*.py'
```

## Current scope

This is a planning prototype only. It does not include real robot control, ROS integration, or online model calls.

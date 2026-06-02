# Agent Guide

## Project Role

This repository is a lightweight prototype for a humanoid robot fleet control system in store-service scenarios. Keep changes small, explainable, and aligned with the existing model-first task understanding, SOP-first execution policy, and deterministic scheduling prototype.

## Local Commands

- Run demo: `PYTHONPATH=src python3 -m humanoid_fleet.demo`
- Run web preview: `PYTHONPATH=src python3 -m humanoid_fleet.web`
- Run tests: `PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_*.py'`
- Run Chinese speech service:
  `source .venv-speech/bin/activate && PYTHONPATH=src uvicorn humanoid_fleet.speech_service:app --host 0.0.0.0 --port 8010`

## Speech Stack

- ASR: sherpa-onnx Paraformer Chinese small model, default path `models/speech/sherpa-onnx-paraformer-zh-small-2024-03-09`.
- TTS: sherpa-onnx MeloTTS Chinese/English VITS model, default path `models/speech/vits-melo-tts-zh_en`.
- Both services are CPU/ONNX first so they can run on aarch64 edge devices such as Jetson/Orin.
- Model files and virtual environments are intentionally ignored by git.

## Development Notes

- Do not commit downloaded models, generated audio, or local virtual environments.
- Prefer adding adapters around external services instead of coupling ASR/TTS directly into orchestration logic.
- Preserve the execution order: approved SOP/template, direct skill, then agentic decomposition.
- When editing existing files, check for local user changes first and avoid reverting unrelated work.

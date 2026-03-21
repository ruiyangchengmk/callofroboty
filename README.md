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

## Run the test

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_*.py'
```

## Current scope

This is a planning prototype only. It does not include real robot control, ROS integration, or online model calls.

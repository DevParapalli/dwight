# dwight

A small prototype for a migration-assistant challenge: a system that ingests messy employee exports, maps them to a target schema, cleans and validates the records, escalates only the genuinely ambiguous cases, and pushes the approved result to a mock target API.

## The challenge

The goal is not to hard-code a transformation for one CSV, but to show a practical agent boundary:

- the agent should handle routine migration work autonomously,
- it should ask for help only when ambiguity is real,
- a human should be able to review, correct, and approve the final push through a simple UI.

## What the prototype does

The agent is designed to satisfy the assignment acceptance criteria:

1. Multi-file ingestion
   - Loads multiple source files representing the same employee set.
   - Reconciles divergent exports without a single field-by-field instruction set.

2. Autonomous mapping and cleanup
   - Proposes source-to-target mappings.
   - Fixes obvious issues such as date normalization, whitespace, casing, and duplicate detection.
   - Applies rules without blocking on every single field.

3. Defensible escalation boundary
   - Stops only when a mapping is genuinely uncertain or a record fails validation in a way a human should decide.
   - Leaves routine cleanup and dedupe inside the agent’s autonomous loop.

4. Human-in-the-loop UI
   - Shows the agent’s progress.
   - Surfaces escalations with enough context for a non-technical reviewer to act quickly.
   - Allows approval, correction, or rejection.

5. Mock system integration
   - Pushes cleaned records to a stub target API.
   - Handles retry and rollback behavior.
   - Records the audit trail for what changed and why.

6. Delta-driven improvement
   - Allows the data quality loop to continue after the first pass.

## Repository layout

- `app/` — FastAPI app, migration flow, UI routes, worker logic, and agent orchestration
- `app/agent/` — mapping, normalization, validation, dedupe, escalation, and push logic
- `app/ui/` — browser-facing interface and templates
- `schemas/` — target schema definition
- `policy/` — escalation thresholds and rules
- `tools/` — sample data generator and target API mock
- `data/samples/` — generated source files for local demos
- `docs/` — the assignment brief, deep-dive notes, write-up, and demo guidance

## Quick start

### 1) Install dependencies

```bash
uv sync
```

### 2) Generate sample data

This creates several deliberately messy employee exports and a measurement manifest:

```bash
uv run tools/generate_sources.py --rows 1500 --out data/samples
```

### 3) Start the mock target system

```bash
uv run tools/mock_target_api.py --port 8900
```

### 4) Start the app

```bash
uv run uvicorn app.main:app --reload
```

Then open:

```text
http://127.0.0.1:8000
```

Upload the files from `data/samples/` and follow the flow through mapping, cleanup, escalation review, and push approval.

## Convenience commands

The project includes a few helper commands via `just`:

```bash
just install
just target
just dev
just demo
just measure
```

- `just demo` prepares a smaller fixture and gives the exact browser flow to follow.
- `just measure` scores the latest run against the generator’s ground-truth manifest.

## Docker / compose

To run the app and the mock target together in containers:

```bash
podman compose up --build
```

This is useful for demoing the project in a reproducible environment without needing local Python setup beyond the container runtime.

## Configuration

Copy the sample environment file:

```bash
cp .env.example .env
```

The main settings are for:

- Groq model access (`GROQ_ENV_KEY`, `GROQ_MODEL`)
- local fallback model support (`LOCAL_FALLBACK_MODEL`, `LOCAL_FALLBACK_URL`)
- database path (`DB_PATH`)
- target API URL (`TARGET_API_URL`)

If a key is not available, the app can fall back to the local model path or deterministic-only behavior as configured in the settings.

## Tech stack

- Python 3.14
- FastAPI for the web app and API layer
- Pydantic for schema and validation
- SQLite for audit and run state
- Jinja2 templates for the review UI
- Open-source LLM usage for mapping and value normalization with deterministic fallbacks
- `uv` for project and dependency management

## Demo expectations

A solid demo should show:

- upload of multiple employee source files,
- autonomous normalization and mapping,
- at least one escalation that requires human judgment,
- resolution via the UI,
- final push to the mock target API,
- an audit trail and a clear summary of what was approved.

## Summary

This project is a compact but realistic implementation of the assignment: a migration agent that does the safe majority of the work without hand-holding, and escalates only the cases that genuinely need human judgment before a production-grade import is allowed to proceed.

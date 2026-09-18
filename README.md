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

## Running it end to end

Two processes: the agent, and a stand-in for the target HR system. They are
separate on purpose — pushing to the target is real HTTP to something the agent
does not own, so retry, idempotency and rollback are real rather than simulated.

Commands below use [just](https://github.com/casey/just), which reads the
[justfile](justfile) in this repo. Install it with `uv tool install rust-just` (the package is named
`rust-just`; the command it installs is `just`),
or run the underlying command shown beneath each step.

### 1. Install

```bash
just install                                  # uv sync
```

Python 3.14 and [uv](https://docs.astral.sh/uv/) are the only requirements.
Everything else, including the databases, is created on first run.

### 2. Configure a model

```bash
cp .env.example .env
```

The agent uses a model for two decisions only: proposing a column mapping, and
normalising a value the deterministic cleaners could not resolve. It needs at
least one of:

- **Hosted** — set `GROQ_ENV_KEY`. `GROQ_MODEL` is the first model tried and
  `GROQ_FALLBACK_MODELS` the ones after it, in order.
- **Local** — set `LOCAL_FALLBACK_MODEL` and `LOCAL_FALLBACK_URL` to any
  OpenAI-compatible endpoint (llama.cpp's `llama-server`, Ollama, vLLM). Used
  when no hosted model can serve the call.

With neither, the agent still runs: mapping falls back to name similarity and
unresolvable values are escalated instead of normalised. Fewer decisions get
made automatically, and nothing is guessed.

> If you use a local reasoning model, keep `LOCAL_DISABLE_THINKING=true`. A
> small model asked to think will spend its whole output budget reasoning and
> return nothing — measured on qwen3-8b: 4,096 tokens of reasoning, empty answer.

### 3. Generate source data

```bash
just samples                                  # or: just samples 1500
```

<sub>`uv run tools/generate_sources.py --rows 5000 --out data/samples`</sub>

Writes three deliberately divergent exports — a legacy HRIS CSV, a payroll CSV
and a CRM spreadsheet — plus `manifest.json`, which records every anomaly it
injected. That manifest is the ground truth the measurement script scores
against.

### 4. Start the target system (terminal 1)

```bash
just target
```

<sub>`uv run tools/mock_target_api.py --port 8900 --enable-nuke`</sub>

A standalone PEP-723 script with its own database. It injects mixed failures on
purpose: transient 500s and 429s that retrying fixes, and deterministic 422s
that it never will — the latter are the only reason the rejection and rollback
paths are demonstrable.

### 5. Start the agent (terminal 2)

```bash
just dev
```

<sub>`uv run uvicorn app.main:app --reload`</sub>

Then open **http://127.0.0.1:8000**.

### 6. Run a migration

1. **Upload** all three files from `data/samples/` at once. The page returns
   immediately; the agent works in the background and reports itself live, with
   a progress bar and a named line for every model call, retry and decision.
2. **Watch it run.** It goes `uploaded → mapped → reconciled` without any clicks.
3. **Answer the questions.** It stops for the things it will not decide. Each
   card says what approving and rejecting will actually do; questions whose
   answer differs per record open a row-by-row page instead, where you can type
   an instruction in plain English, paste `employee id, value` lines, or edit
   rows one at a time.
4. **Push.** The second and last place it waits for a person, because it writes
   to a system the agent does not own.
5. **Correct what the target refused.** A deterministic 4xx cannot be retried
   into success, so the agent works out what to change and asks. Approve, then
   push again — only corrected records are re-sent.
6. **Check the audit trail** at `/runs/{id}/audit`, or export it as CSV.

### 7. Score it against ground truth

```bash
just measure
```

<sub>`uv run tools/measure_escalations.py --json data/samples/measurement.json`</sub>

Compares what was escalated against every anomaly the generator injected, and
prints recall and precision per anomaly type.

## Clearing data between runs

```bash
just nuke                                     # needs ENABLE_NUKE=true
```

<sub>`curl -X POST localhost:8000/nuke`</sub>

Empties the agent's tables and asks the target to empty its own, in one call. It
deliberately **keeps the model cache**, so the next run does not pay for the same
answers twice — the difference between a 20-second re-run and a slow one. The
same thing is in the UI at `/nuke`, which shows what will be deleted first.

For a genuinely cold start:

```bash
just reset
```

<sub>`rm -rf data/dwight.db data/dwight.db-wal data/dwight.db-shm data/runs data/mock_target.db`</sub>

This deletes the database files outright, model cache included, so the next run
re-pays for every LLM call. `data/samples/` is untouched either way.

## Containers

```bash
just up          # podman compose up --build
just down
```

Brings up both services on :8000 and :8900. `docker compose` works the same way.

## All recipes

| Recipe | What it does |
|---|---|
| `just install` | `uv sync` |
| `just dev` | the agent on :8000, with reload |
| `just target` | the mock target on :8900, `/nuke` enabled |
| `just samples [rows]` | regenerate `data/samples/`, default 5,000 rows |
| `just demo [rows]` | reset and generate a smaller fixture that still fires every escalation type |
| `just measure` | score the last run against the ground-truth manifest |
| `just nuke [port]` | clear both stores, keep the model cache |
| `just reset` | delete the database files, cache included |
| `just up` / `just down` | podman compose |
| `just docs` | typeset the write-up and deep dive to PDF |

`just docs` additionally needs [typst](https://typst.app/) and a checkout of the
Centauri design system beside this repo; pass a different location with
`just docs ../elsewhere`.

## Configuration

| Variable | Purpose |
|---|---|
| `GROQ_ENV_KEY` | Hosted model key. Without it, the local model is used. |
| `GROQ_MODEL` | First hosted model tried. |
| `GROQ_FALLBACK_MODELS` | Comma-separated models tried after it, in order. |
| `LOCAL_FALLBACK_MODEL` | Model name at the local endpoint. |
| `LOCAL_FALLBACK_URL` | OpenAI-compatible endpoint, default `http://localhost:8080/v1`. |
| `LOCAL_DISABLE_THINKING` | Keep `true` for small reasoning models. |
| `DB_PATH` | SQLite file, default `data/dwight.db`. |
| `TARGET_API_URL` | Where to push, default `http://127.0.0.1:8900/v1`. |
| `ENABLE_NUKE` | Registers `POST /nuke`. Testing only; the route does not exist without it. |

Thresholds are **not** environment variables. Every one lives in
[policy/escalation.yaml](policy/escalation.yaml), read by a single module, and is
snapshotted into each run so old runs stay explicable.

## Tech stack

- Python 3.14, managed with `uv`
- FastAPI and Jinja2, server-rendered; JavaScript is enhancement only and every
  action works as a plain form POST
- Pydantic, with the validation model built at runtime from the YAML schema
- SQLite in WAL mode for run state and the audit trail
- rapidfuzz for duplicate scoring, phonenumbers for E.164, openpyxl for xlsx
- Any OpenAI-compatible model, hosted or local

## Where to read next

- [schemas/employee.v1.yaml](schemas/employee.v1.yaml) — the target model. Nothing
  about the employee schema is hardcoded in Python; changing this file changes
  the pipeline.
- [policy/escalation.yaml](policy/escalation.yaml) — every threshold that decides
  whether the agent acts or asks.
- [app/agent/policy.py](app/agent/policy.py) — the only module that emits a reason
  code. If you want to know why the agent stopped, it is in here.
- [app/agent/runner.py](app/agent/runner.py) — the autonomy boundary: what runs
  unattended, and the two places it deliberately does not.

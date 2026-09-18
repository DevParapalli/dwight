# Dwight

## Escalate classes, not instances

Dwight is an agentic migration assistant for moving employee data from a
fragmented legacy HR/CRM/payroll stack into a target HR platform. It ingests
inconsistent exports, proposes a schema mapping, cleans and reconciles the
records, validates them, and pushes approved data to a separate mock target
API.

The important design choice is the escalation boundary. The agent handles
routine, reversible work itself. It asks a human only when a wrong guess
would be silent or difficult to undo: ambiguous mappings, ambiguous dates,
material disagreements between sources, unsafe repairs, validation
contradictions, and approval to write to the target.

## What it demonstrates

- **Multi-file ingestion:** HRIS, payroll, and CRM exports with different
  headers, formats, coverage, and data quality are reconciled into one target
  dataset.
- **Deterministic execution:** the model proposes mappings or repairs; code
  applies only gated proposals. Whitespace, casing, dates, currencies,
  phone numbers, enums, validation, and safe deduplication are handled by
  deterministic code.
- **Grouped escalations:** repeated instances become one reusable question.
  Decisions are cached by stable signatures, so the same ambiguity is not
  re-asked on every import.
- **Human supervision:** a server-rendered UI shows live progress, the
  escalation queue, affected records, the push gate, and the audit trail.
- **Real integration behavior:** the target is a separate HTTP process with
  per-record status, retries for transient `500`/`429` failures, handling for
  deterministic `422` refusals, and rollback support.
- **Measured behavior:** the checked-in sample measurement covers 13,299
  source rows and compresses 1,350 escalation rows into 16 distinct
  questions. Measured escalation classes have recall from 0.9398 to 1.0 and
  code precision of 1.0.

The system fails toward asking. A missing value is safer than an invented
one, and leaving a possible duplicate is safer than merging two employees
incorrectly.

## Architecture

```text
source exports
    -> profile and propose mappings
    -> deterministic cleaning and schema validation
    -> cross-source reconciliation and deduplication
    -> grouped human escalations
    -> approval-gated, idempotent push
    -> audit log and per-record result
```

The policy in [`policy/escalation.yaml`](policy/escalation.yaml) is the single
source of truth for why the agent stops. It is snapshotted into each run so
old runs remain explainable even when policy thresholds change.

## Run locally

Requirements: Python 3.14 and [uv](https://docs.astral.sh/uv/). Install
[`just`](https://github.com/casey/just) for the shortcuts below, or run the
commands printed beneath each recipe directly.

### 1. Install

```bash
just install
```

This runs `uv sync`. SQLite databases and run data are created on demand.

### 2. Configure a model

```bash
cp .env.example .env
```

The model is used for column-mapping proposals and unresolved value
normalization. Configure either:

- `GROQ_ENV_KEY` for the hosted model chain. `GROQ_MODEL` is tried first,
  followed by the models in `GROQ_FALLBACK_MODELS`;
- `LOCAL_FALLBACK_MODEL` and `LOCAL_FALLBACK_URL` for an
  OpenAI-compatible local endpoint such as `llama-server`, Ollama, or vLLM.
  This is used when no hosted model can serve the call.

With neither configured, mapping uses name similarity and unresolved values
are escalated. The system does not invent values. For small local reasoning
models, keep `LOCAL_DISABLE_THINKING=true`.

In the demonstrated run, model serving was local `llama.cpp` with
`Qwen3.5-9B`, so confidential employee data stayed inside the trust boundary.

### 3. Generate source data

```bash
just samples
```

This writes deliberately divergent HRIS, payroll, and CRM exports to
`data/samples/`, together with `manifest.json`. The manifest is ground truth
for the measurement command. The generated files include missing fields,
mixed date formats, duplicate candidates, source conflicts, malformed enums,
and target-rejection cases.

For a quicker end-to-end fixture:

```bash
just demo
```

### 4. Start the target and agent

Run the target separately on purpose. It represents a vendor system that the
agent does not own, so HTTP retries, idempotency, per-record outcomes, and
rollback are observable rather than mocked inside the same process.

In terminal 1:

```bash
just target
```

In terminal 2:

```bash
just dev
```

Open <http://127.0.0.1:8000>.

### 5. Run a migration

1. Upload all generated source files from `data/samples/`. The page returns
  immediately while the agent works in the background.
2. Watch the live run move through profiling, mapping, reconciliation,
  cleaning, and validation. The activity view reports model calls, retries,
  decisions, and progress.
3. Resolve grouped questions in the queue. Each card explains the policy,
  affected records, and the effect of approving or rejecting the proposal.
  Row-specific questions provide the affected record and accept a correction
  or instruction.
4. Approve the push to the target. This is the second deliberate human gate:
  the agent is about to write to a system it does not own.
5. Resolve any deterministic target refusals and push corrected records again.
  Transient failures retry automatically; deterministic `422` responses do
  not.
6. Inspect the run summary and audit log at `/runs/{id}/audit`, or export the
  audit log as CSV.

### 6. Measure escalation quality

```bash
just measure
```

This compares the run with the injected-anomaly manifest and reports recall
and precision by anomaly type. The JSON report is written to
`data/samples/measurement.json`.

For model comparisons, use `just eval`. It scores the model chain on the
mapping and normalization calls the agent actually makes.

## Resetting local data

Keep the model decision cache and clear the agent and target stores:

```bash
just nuke
```

For a cold start that also deletes cached model decisions:

```bash
just reset
```

The reset command removes local SQLite files and generated run directories;
`data/samples/` is left intact.

## Containers

```bash
just up
just down
```

This starts the agent on port `8000` and the separate mock target on port
`8900` with Podman Compose. Docker Compose works with the same
`compose.yaml`.

## Recipes

| Recipe | What it does |
|---|---|
| `just install` | Install or update the locked environment with `uv sync` |
| `just dev` | Run the FastAPI agent on port `8000` with reload |
| `just target` | Run the mock target on port `8900` with `/nuke` enabled |
| `just samples [rows]` | Generate source exports and a ground-truth manifest; defaults to 5,000 rows |
| `just demo [rows]` | Reset and generate a smaller fixture that still exercises every escalation type |
| `just measure` | Score the latest run against the ground-truth manifest |
| `just eval [args]` | Evaluate configured models on the agent's model calls |
| `just nuke [port]` | Clear both stores while keeping the model decision cache |
| `just reset` | Delete local databases, run data, and the model cache |
| `just up` / `just down` | Start or stop both container services |
| `just docs` | Typeset the write-up and deep dive to PDF |

`just docs` additionally needs [Typst](https://typst.app/) and a checkout of
the Centauri design system beside this repository. Pass another location with
`just docs ../elsewhere`.

## Repository layout

| Path | Purpose |
|---|---|
| `app/agent/` | Mapping, cleaning, reconciliation, dedupe, validation, escalation, and push orchestration |
| `app/ingest/` | CSV/XLSX readers and source profiling |
| `app/ui/` | FastAPI routes, Jinja templates, CSS, and browser enhancements |
| `app/llm/` | OpenAI-compatible model client and prompts |
| `schemas/` | Target employee schema |
| `policy/` | Escalation rules and thresholds |
| `tools/` | Source generator, measurement harness, model evaluation, and mock target API |
| `data/samples/` | Generated fixtures, anomaly manifest, and measurement artifacts |

## Configuration

| Variable | Purpose |
|---|---|
| `GROQ_ENV_KEY` | Hosted model key |
| `GROQ_MODEL` | First hosted model to try |
| `GROQ_FALLBACK_MODELS` | Comma-separated hosted fallback models |
| `LOCAL_FALLBACK_MODEL` | Model name at the local endpoint |
| `LOCAL_FALLBACK_URL` | OpenAI-compatible endpoint; defaults to `http://localhost:8080/v1` |
| `LOCAL_DISABLE_THINKING` | Disable reasoning output for small local models |
| `DB_PATH` | Agent SQLite file; defaults to `data/dwight.db` |
| `TARGET_API_URL` | Push destination; defaults to `http://127.0.0.1:8900/v1` |
| `ENABLE_NUKE` | Enables the testing-only `POST /nuke` route |

Thresholds are configured in [`policy/escalation.yaml`](policy/escalation.yaml),
not in environment variables.

## Tech stack

- Python 3.14, `uv`, FastAPI, Uvicorn, and Jinja2
- Pydantic models built from the YAML target schema
- SQLite in WAL mode for run state, decisions, and audit history
- LiteLLM/Groq or any OpenAI-compatible local model endpoint
- `rapidfuzz`, `phonenumbers`, `openpyxl`, and `python-dateutil`
- Plain HTML, CSS, and JavaScript served by FastAPI

## Next steps

- Calibrate override thresholds against labelled ground truth instead of
  hand-picked values.
- Extend the evaluation harness to score threshold choices.
- Add incremental sync against a real target such as Workday, ADP, EY
  Payroll, or ServiceNow.
- Add cross-run and cross-agent memory with explicit retention controls.

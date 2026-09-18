from pathlib import Path

from app.agent.orchestrator import clean_and_validate, ingest_and_map
from app.agent.reconcile import reconcile_run
from app.audit import record_change
from app.db import connect
from app.progress import emit
from app.schema.loader import Schema

# Where the agent stops on its own.
#
# Everything up to 'reconciled' is reversible and stays inside this system, so
# the runner does it unattended. Two things are deliberately not automatic:
#
#   1. A flagged column mapping. Mapping is decided once and every row inherits
#      it, so guessing wrong here corrupts the whole import silently. The runner
#      pauses before row processing if any mapping was flagged, and resumes by
#      itself the moment the last one is answered.
#   2. The push. It writes to a system of record this agent does not own, and
#      it is the only step a human cannot simply undo by re-running. A person
#      says go; rollback exists because even then it can be wrong.
#
# Everything else -- typos, ambiguous dates, duplicate pairs, cross-source
# conflicts -- raises an escalation without stopping the batch. Blocked records
# carry blocked_on and are simply left out of the push.
GATE_STAGE = "mapped"
AUTONOMOUS_FINAL_STAGE = "reconciled"


def mapping_gate_is_open(run_id: str) -> bool:
    """True when nothing about the column mapping still needs a human."""
    with connect() as conn:
        pending = conn.execute(
            "SELECT COUNT(*) AS n FROM escalations "
            "WHERE run_id = ? AND status = 'open' AND reason_code IN ('MAP_AMBIGUOUS', 'MAP_UNMAPPED')",
            (run_id,),
        ).fetchone()["n"]
    return pending == 0


def _stage_of(run_id: str) -> str | None:
    with connect() as conn:
        row = conn.execute("SELECT stage FROM runs WHERE id = ?", (run_id,)).fetchone()
    return row["stage"] if row else None


def _note(run_id: str, note: str, stage: str, kind: str = "waiting") -> None:
    with connect() as conn:
        record_change(
            conn, run_id=run_id, actor="agent", stage=stage, entity_type="run",
            entity_id=run_id, after=stage, note=note,
        )
    emit(run_id, kind, note, stage=stage)


def advance_run(run_id: str, schema: Schema, policy: dict, source_paths: list[Path] | None = None) -> str:
    """Drives a run as far as it can go without a person, then stops and says
    why. Safe to call again: it resumes from whatever stage the run is in, which
    is what makes a human resolution simply something the runner picks up."""
    stage = _stage_of(run_id)

    if stage == "uploaded" and source_paths:
        emit(run_id, "stage", f"Reading {len(source_paths)} source file(s)", stage="uploaded",
             files=[p.name for p in source_paths])
        ingest_and_map(run_id, source_paths, schema, policy)
        stage = _stage_of(run_id)

    if stage == GATE_STAGE and not mapping_gate_is_open(run_id):
        _note(run_id, "paused: column mappings need review before row processing", GATE_STAGE)
        return "waiting_on_mapping_review"

    if stage == GATE_STAGE:
        emit(run_id, "stage", "Cleaning and validating every row", stage="mapped")
        clean_and_validate(run_id, schema, policy)
        stage = _stage_of(run_id)

    if stage == "validated":
        emit(run_id, "stage", "Matching the same person across files", stage="validated")
        reconcile_run(run_id, schema, policy)
        stage = _stage_of(run_id)

    if stage == AUTONOMOUS_FINAL_STAGE:
        _note(run_id, "ready to push: waiting for a person to approve writing to the target",
              AUTONOMOUS_FINAL_STAGE)
        return "waiting_on_push_approval"

    return stage or "unknown"


def run_status(run_id: str) -> dict:
    """What the agent is doing or waiting for, in words a consultant can read."""
    stage = _stage_of(run_id) or "unknown"
    if stage == GATE_STAGE and not mapping_gate_is_open(run_id):
        return {"stage": stage, "waiting": True,
                "message": "Paused: some columns could not be mapped confidently. "
                           "Resolve those and the run continues on its own."}
    if stage == AUTONOMOUS_FINAL_STAGE:
        return {"stage": stage, "waiting": True,
                "message": "Reconciled and ready. Pushing writes to the target system, "
                           "so it needs your go-ahead."}
    if stage in ("done", "rolled_back"):
        return {"stage": stage, "waiting": False, "message": f"Run {stage}."}
    return {"stage": stage, "waiting": False, "message": f"Working ({stage})."}

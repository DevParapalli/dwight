import asyncio
import csv
import io
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from fastapi import APIRouter, Form, Request, Response, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sse_starlette.sse import EventSourceResponse
from starlette.concurrency import run_in_threadpool

from app import progress
from app.agent.instruct import interpret_bulk_instruction
from app.agent.policy import RULE_DESCRIPTIONS, load_policy
from app.agent.push import push_run, rollback_run
from app.agent.resolve import resolve_escalation
from app.agent.runner import advance_run, mapping_gate_is_open, run_status
from app.audit import record_change, utcnow
from app.db import connect, new_id, truncate_all
from app.progress import emit
from app.schema.loader import load_schema
from app.schema.pydantic_builder import build_model
from app.schema.rules import RuleContext, evaluate_rules
from app.settings import settings

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

# Timestamps are stored UTC and read by people in India.
_DISPLAY_TZ = ZoneInfo("Asia/Kolkata")


def _local_time(value: str | None) -> str:
    """UTC ISO-8601 as stored, rendered in the reader's timezone."""
    if not value:
        return ""
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return value
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(_DISPLAY_TZ).strftime("%d %b %Y, %H:%M")


templates.env.filters["local_time"] = _local_time


def _asset_version() -> str:
    """Newest mtime across the static assets, as a cache-busting suffix.

    The activity feed's styling used to live in a <style> block inside the
    template, so it could never be out of step with the markup. Moving it to a
    stylesheet made that possible: a browser holding yesterday's dwight.css
    renders today's markup unstyled, which looks like broken code rather than a
    stale cache. Keying the URL to the file's mtime means a changed stylesheet
    is a different URL.
    """
    static = Path(__file__).parent / "static"
    try:
        return str(int(max(f.stat().st_mtime for f in static.rglob("*") if f.is_file())))
    except ValueError:
        return "0"


templates.env.globals["asset_version"] = _asset_version()

# Background tasks are kept referenced so they aren't garbage collected mid-run.
_background_tasks: set[asyncio.Task] = set()


def _start_background(func, *args) -> None:
    task = asyncio.create_task(run_in_threadpool(func, *args))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


@router.get("/")
async def upload_form(request: Request):
    return templates.TemplateResponse(request, "upload.html", {})


@router.post("/")
async def start_run(request: Request, files: list[UploadFile]):
    schema = load_schema(settings.schema_path)
    policy = load_policy()
    policy_snapshot = settings.policy_path.read_text()

    run_id = new_id()
    run_dir = Path("data/runs") / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    saved_paths = []
    for upload in files:
        dest = run_dir / upload.filename
        dest.write_bytes(await upload.read())
        saved_paths.append(dest)

    with connect() as conn:
        conn.execute(
            "INSERT INTO runs (id, schema_version, policy_snapshot, stage, created_at, updated_at) "
            "VALUES (?, ?, ?, 'uploaded', ?, ?)",
            (run_id, schema.version, policy_snapshot, utcnow(), utcnow()),
        )

    # One background task per run. The upload returns immediately and the agent
    # carries the run forward on its own, stopping only where a person is
    # genuinely needed (see app/agent/runner.py).
    _start_background(advance_run, run_id, schema, policy, saved_paths)

    return RedirectResponse(url=f"/runs/{run_id}", status_code=303)


# Registered only when enabled, so when it is off the path genuinely does not
# exist rather than existing and refusing. Each system nukes its own store: this
# clears dwight's tables and the mock target clears its own, because neither
# owns the other's database.
if settings.enable_nuke:

    @router.get("/nuke")
    async def nuke_confirm(request: Request):
        """Deliberate second step before emptying everything.

        A JavaScript confirm() was the only guard, which is no guard at all on
        the no-JS path this app promises to support: a plain POST wiped both
        databases with nothing in the way. Now the destructive action needs a
        page of its own that a person has to arrive at on purpose.
        """
        with connect() as conn:
            counts = {
                "runs": conn.execute("SELECT COUNT(*) n FROM runs").fetchone()["n"],
                "records": conn.execute("SELECT COUNT(*) n FROM records").fetchone()["n"],
                "questions": conn.execute("SELECT COUNT(*) n FROM escalations").fetchone()["n"],
                "audit events": conn.execute("SELECT COUNT(*) n FROM audit_events").fetchone()["n"],
            }
        return templates.TemplateResponse(request, "nuke.html", {"counts": counts})

    @router.post("/nuke")
    async def nuke(request: Request):
        """Empties every table. Testing only."""
        deleted = await run_in_threadpool(truncate_all)

        target_result = "not contacted"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(settings.target_api_url.rsplit("/v1", 1)[0] + "/nuke")
            target_result = (f"{response.json().get('deleted', {})}" if response.status_code == 200
                             else f"refused with {response.status_code}")
        except httpx.HTTPError as exc:
            # Reported, not swallowed: a half-cleared pair of systems is worse
            # than a failed clear, because the next run starts from a state
            # nobody chose.
            target_result = f"unreachable: {exc.__class__.__name__}"

        if request.headers.get("accept", "").startswith("application/json"):
            return JSONResponse({"dwight": deleted, "target": target_result})
        return RedirectResponse(url="/runs", status_code=303)


@router.get("/runs")
async def list_runs(request: Request):
    """Every run this instance has done, newest first."""
    with connect() as conn:
        # UUIDv7 sorts by creation time, but order by created_at anyway: that is
        # the column a reader would expect to be authoritative here.
        rows = conn.execute(
            """SELECT r.id, r.stage, r.created_at, r.updated_at,
                      (SELECT COUNT(*) FROM records WHERE run_id = r.id) AS record_count,
                      (SELECT COUNT(*) FROM (
                          SELECT 1 FROM escalations
                          WHERE run_id = r.id AND status = 'open'
                          GROUP BY reason_code, signature)) AS open_questions,
                      (SELECT COUNT(DISTINCT record_id) FROM push_attempts
                       WHERE run_id = r.id AND outcome = 'success') AS pushed,
                      (SELECT GROUP_CONCAT(filename, ', ') FROM source_files
                       WHERE run_id = r.id) AS sources
               FROM runs r
               ORDER BY r.created_at DESC""",
        ).fetchall()

    return templates.TemplateResponse(
        request, "runs.html",
        {"runs": [dict(r) for r in rows], "nuke_enabled": settings.enable_nuke},
    )


@router.get("/runs/{run_id}/mappings")
async def view_mappings(request: Request, run_id: str):
    schema = load_schema(settings.schema_path)

    with connect() as conn:
        rows = conn.execute(
            """SELECT m.id, m.target_field, m.confidence, m.rationale, m.alternatives, m.status,
                      sc.name AS column_name, sc.sample_values, sf.filename
               FROM mappings m
               JOIN source_columns sc ON sc.id = m.source_column_id
               JOIN source_files sf ON sf.id = sc.source_file_id
               WHERE m.run_id = ?
               ORDER BY sf.filename, sc.name""",
            (run_id,),
        ).fetchall()

    mappings = [
        {
            "id": r["id"],
            "filename": r["filename"],
            "column_name": r["column_name"],
            "target_field": r["target_field"],
            "confidence": r["confidence"],
            "rationale": r["rationale"],
            "sample_values": json.loads(r["sample_values"]) if r["sample_values"] else [],
            "status": r["status"],
        }
        for r in rows
    ]
    return templates.TemplateResponse(
        request, "mappings.html",
        {"run_id": run_id, "mappings": mappings, "target_fields": list(schema.fields),
         "open_escalations": open_question_count(run_id)},
    )


@router.post("/runs/{run_id}/mappings/accept-all")
async def accept_all_mappings(run_id: str):
    with connect() as conn:
        conn.execute("UPDATE mappings SET status = 'accepted' WHERE run_id = ?", (run_id,))
    return RedirectResponse(url=f"/runs/{run_id}/mappings", status_code=303)


@router.post("/runs/{run_id}/mappings/{mapping_id}/override")
async def override_mapping(run_id: str, mapping_id: str, target_field: str = Form("")):
    new_target = target_field or None
    with connect() as conn:
        before = conn.execute(
            "SELECT target_field FROM mappings WHERE id = ? AND run_id = ?", (mapping_id, run_id)
        ).fetchone()
        conn.execute(
            "UPDATE mappings SET target_field = ?, status = 'accepted' WHERE id = ? AND run_id = ?",
            (new_target, mapping_id, run_id),
        )
        record_change(
            conn, run_id=run_id, actor="human", stage="mapped", entity_type="mapping",
            entity_id=mapping_id, field="target_field",
            before=before["target_field"] if before else None, after=new_target,
            note="human override during mapping review",
        )
    return RedirectResponse(url=f"/runs/{run_id}/mappings", status_code=303)


@router.post("/runs/{run_id}/continue")
async def continue_run(run_id: str):
    """Manual nudge. Normally unnecessary -- resolving the last blocking
    escalation resumes the run by itself -- but it makes a stalled run
    recoverable without restarting anything."""
    schema = load_schema(settings.schema_path)
    policy = load_policy()
    _start_background(advance_run, run_id, schema, policy, None)
    return RedirectResponse(url=f"/runs/{run_id}", status_code=303)


CARDS_PER_REASON_CODE = 25

# How many affected records a card shows behind its accordion. A record-scoped
# question ("the termination date is before the hire date") is unanswerable
# without seeing the row it is about, so the row travels with the question.
ROWS_PER_CARD = 5

# How many records the bulk-fill page renders at once. Enough that a realistic
# batch fits on one page, small enough that a pathological one cannot produce an
# unusable page. What is left stays in the queue and appears on the next visit.
FILL_PAGE_SIZE = 500

# Questions where every affected record needs its own value, so the queue card
# offers a row-by-row page instead of pretending one answer will do.
FILLABLE_REASON_CODES = {"PUSH_REJECTED", "LOGIC_CONTRADICTION"}


# Plain-English wrapper around each reason code, for the person answering the
# question rather than the engineer who named it. "DUPE_AMBIGUOUS" tells a
# consultant nothing; "two records might be the same person" tells them what
# they are being asked and what happens either way. Presentation only -- the
# codes themselves are still emitted solely by app/agent/policy.py.
REASON_CODE_HELP = {
    "MAP_AMBIGUOUS": {
        "title": "A column could belong in more than one place",
        "means": "A column in the uploaded file matches more than one field in the "
                 "target system, and the agent will not guess which one.",
        "approve": "the column is filed under the field shown, for every row in the file",
        "reject": "the column is left out of the import entirely",
    },
    "MAP_UNMAPPED": {
        "title": "A column has no obvious home",
        "means": "A column in the uploaded file does not clearly match any field in "
                 "the target system.",
        "approve": "the column is filed under the field you pick",
        "reject": "the column is left out of the import entirely",
    },
    "VALUE_LOW_CONFIDENCE": {
        "title": "A value could not be tidied up confidently",
        "means": "A value did not match any of the accepted options and the agent is "
                 "not confident enough to change it on its own.",
        "approve": "the value is replaced with the one shown, everywhere it appears",
        "reject": "the value is left exactly as it arrived",
    },
    "DATE_FORMAT_AMBIGUOUS": {
        "title": "A date could be read two different ways",
        "means": "A date like 03/04/2024 is a different day depending on whether the "
                 "day or the month comes first, and both readings are valid.",
        "approve": "every date in that column is read the way shown",
        "reject": "the dates are left unread and the affected records wait",
    },
    "VALIDATE_TWICE": {
        "title": "A record is still wrong after one attempt to fix it",
        "means": "The agent tried to correct this record once, and it still does not "
                 "pass the rules. It will not keep trying.",
        "approve": "the correction shown is applied to the record",
        "reject": "the record is held back and not sent to the target system",
    },
    "LOGIC_CONTRADICTION": {
        "title": "A record contradicts itself",
        "means": "Two fields in the same record cannot both be true — for example a "
                 "leaving date that falls before the joining date. There is no safe "
                 "way to guess which one is wrong.",
        "approve": "the correction shown is applied",
        "reject": "the record is held back and not sent to the target system",
    },
    "DUPE_AMBIGUOUS": {
        "title": "Two records might be the same person",
        "means": "Two records look similar enough that they could be one employee "
                 "entered twice — but similar enough is not the same as certain, and "
                 "combining two real employees cannot be undone.",
        "approve": "the two records are combined into one person",
        "reject": "the two are kept as separate people",
    },
    "CONFLICT_ACROSS_SOURCES": {
        "title": "Your systems disagree with each other",
        "means": "The same field has different values in different files, so one of "
                 "them has to be treated as correct.",
        "approve": "the system you choose wins for that field, now and in future imports",
        "reject": "the agent falls back to the default order of precedence",
    },
    "PUSH_REJECTED": {
        "title": "The target system refused these records",
        "means": "The target system applied a rule the agent could not have known "
                 "about and rejected these employees. Retrying will not help — "
                 "something has to change first.",
        "approve": "the correction is saved to the record and sent on the next push",
        "reject": "these employees are set aside and not sent to the target system",
    },
}

# Every status the pipeline can put a record in, in reading order. The overview
# renders a tile for all of them from the first paint, including the ones still
# at zero -- otherwise tiles appear one at a time as each status first occurs and
# the page visibly reflows while the run works.
RECORD_STATUS_ORDER = ["clean", "merged_survivor", "merged", "blocked", "incomplete", "pending"]
AUDIT_PAGE_SIZE = 200

_AUDIT_COLUMNS = [
    "ts", "actor", "stage", "reason_code", "entity_type", "entity_id", "field",
    "before", "after", "confidence", "rule_id", "model", "latency_ms", "note",
]


def _audit_rows(run_id: str, actor: str = "", stage: str = "", reason_code: str = "",
                entity_id: str = "", limit: int | None = AUDIT_PAGE_SIZE) -> list[dict]:
    clauses = ["run_id = ?"]
    params: list = [run_id]
    for column, value in (("actor", actor), ("stage", stage),
                          ("reason_code", reason_code), ("entity_id", entity_id)):
        if value:
            clauses.append(f"{column} = ?")
            params.append(value)

    sql = (f"SELECT {', '.join(_AUDIT_COLUMNS)} FROM audit_events "
           f"WHERE {' AND '.join(clauses)} ORDER BY ts")
    if limit:
        sql += f" LIMIT {int(limit)}"
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


ACTIVE_STAGES = {"uploaded", "profiled", "mapped", "cleaned", "validated", "reconciled", "pushing"}


def open_question_count(run_id: str) -> int:
    """Distinct questions still open, which is what the queue shows and what the
    nav badge must therefore say. Counting escalation rows instead reports 1,210
    where the consultant sees four."""
    with connect() as conn:
        return conn.execute(
            "SELECT COUNT(*) AS n FROM (SELECT 1 FROM escalations "
            "WHERE run_id = ? AND status = 'open' GROUP BY reason_code, signature)",
            (run_id,),
        ).fetchone()["n"]


def _run_snapshot(run_id: str) -> dict:
    with connect() as conn:
        run = conn.execute("SELECT stage FROM runs WHERE id = ?", (run_id,)).fetchone()
        records = conn.execute(
            "SELECT status, COUNT(*) AS n FROM records WHERE run_id = ? GROUP BY status", (run_id,)
        ).fetchall()
        pushed = conn.execute(
            "SELECT COUNT(DISTINCT record_id) AS n FROM push_attempts "
            "WHERE run_id = ? AND outcome = 'success'", (run_id,)
        ).fetchone()["n"]
    return {
        "stage": run["stage"] if run else "unknown",
        "records": {r["status"]: r["n"] for r in records},
        "open_escalations": open_question_count(run_id),
        "pushed": pushed,
    }


@router.get("/runs/{run_id}/events")
async def run_events(request: Request, run_id: str):
    """Replayable progress stream.

    Every frame carries its `seq` as the SSE event id, so a browser that drops
    the connection reconnects with Last-Event-ID and resumes exactly where it
    stopped. A browser opening the page for the first time sends no such header
    and therefore replays the whole run from frame one, which is what makes the
    live view correct for a run that started before the page was opened.

    Enhancement only: the page renders the same state server-side without it.
    """
    last_header = request.headers.get("last-event-id") or request.query_params.get("after") or "0"
    cursor = int(last_header) if last_header.isdigit() else 0

    async def stream():
        nonlocal cursor
        woken = progress.subscribe(run_id)
        try:
            while True:
                if await request.is_disconnected():
                    break

                frames = progress.read_frames(run_id, cursor)
                for frame in frames:
                    cursor = frame["seq"]
                    yield {
                        "id": str(frame["seq"]),
                        "event": "frame",
                        "data": json.dumps({**frame, "snapshot": None}),
                    }

                snapshot = _run_snapshot(run_id)
                yield {"event": "snapshot", "data": json.dumps(snapshot)}

                if frames and frames[-1]["kind"] in progress.TERMINAL_KINDS:
                    break
                if snapshot["stage"] not in ACTIVE_STAGES and not frames:
                    break

                woken.clear()
                try:
                    # Woken the instant a stage emits; the timeout is only a
                    # heartbeat so a dropped notify can't strand the client.
                    await asyncio.wait_for(woken.wait(), timeout=10)
                except TimeoutError:
                    pass
        finally:
            progress.unsubscribe(run_id, woken)

    return EventSourceResponse(stream())


@router.get("/runs/{run_id}/audit")
async def view_audit(request: Request, run_id: str, actor: str = "", stage: str = "",
                     reason_code: str = "", entity_id: str = ""):
    with connect() as conn:
        total = conn.execute(
            "SELECT COUNT(*) AS n FROM audit_events WHERE run_id = ?", (run_id,)
        ).fetchone()["n"]
        actors = [r["actor"] for r in conn.execute(
            "SELECT DISTINCT actor FROM audit_events WHERE run_id = ? ORDER BY actor", (run_id,))]
        stages = [r["stage"] for r in conn.execute(
            "SELECT DISTINCT stage FROM audit_events WHERE run_id = ? AND stage IS NOT NULL "
            "ORDER BY stage", (run_id,))]

    rows = _audit_rows(run_id, actor, stage, reason_code, entity_id)
    return templates.TemplateResponse(
        request, "audit.html",
        {
            "run_id": run_id, "rows": rows, "total": total, "columns": _AUDIT_COLUMNS,
            "open_escalations": open_question_count(run_id),
            "actors": actors, "stages": stages,
            "filters": {"actor": actor, "stage": stage,
                        "reason_code": reason_code, "entity_id": entity_id},
            "page_size": AUDIT_PAGE_SIZE,
        },
    )


@router.get("/runs/{run_id}/audit.json")
async def export_audit_json(run_id: str, actor: str = "", stage: str = "",
                            reason_code: str = "", entity_id: str = ""):
    return JSONResponse(_audit_rows(run_id, actor, stage, reason_code, entity_id, limit=None))


@router.get("/runs/{run_id}/audit.csv")
async def export_audit_csv(run_id: str, actor: str = "", stage: str = "",
                           reason_code: str = "", entity_id: str = ""):
    rows = _audit_rows(run_id, actor, stage, reason_code, entity_id, limit=None)
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=_AUDIT_COLUMNS)
    writer.writeheader()
    writer.writerows(rows)
    return Response(
        content=buffer.getvalue(), media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="audit-{run_id}.csv"'},
    )


@router.get("/runs/{run_id}/records")
async def view_records(request: Request, run_id: str, q: str = ""):
    with connect() as conn:
        if q:
            rows = conn.execute(
                "SELECT r.id, r.natural_key, r.status, r.blocked_on, r.data, sf.filename "
                "FROM records r JOIN source_files sf ON sf.id = r.source_file_id "
                "WHERE r.run_id = ? AND (r.natural_key LIKE ? OR r.data LIKE ?) "
                "ORDER BY r.natural_key LIMIT 100",
                (run_id, f"%{q}%", f"%{q}%"),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT r.id, r.natural_key, r.status, r.blocked_on, r.data, sf.filename "
                "FROM records r JOIN source_files sf ON sf.id = r.source_file_id "
                "WHERE r.run_id = ? ORDER BY r.natural_key LIMIT 100",
                (run_id,),
            ).fetchall()

    records = []
    for r in rows:
        data = json.loads(r["data"])
        name = " ".join(filter(None, [data.get("first_name"), data.get("last_name")]))
        # A source that carries no name and no employee id (CRM, whose name
        # column needs a split) would otherwise render as a row of blanks. Fall
        # back to whatever actually identifies the record.
        identifier = r["natural_key"] or data.get("work_email") or data.get("phone")
        records.append({
            "id": r["id"],
            "natural_key": r["natural_key"],
            "identifier": identifier or "no identifier in this source",
            "identified_by": ("employee id" if r["natural_key"]
                              else "email" if data.get("work_email")
                              else "phone" if data.get("phone") else "nothing"),
            "status": r["status"],
            "blocked_on": r["blocked_on"],
            "name": name or "—",
            "source": r["filename"],
            "department": data.get("department") or "—",
        })
    return templates.TemplateResponse(
        request, "records.html",
        {"run_id": run_id, "records": records, "q": q,
         "open_escalations": open_question_count(run_id)},
    )


@router.get("/runs/{run_id}/records/{record_id}")
async def view_record(request: Request, run_id: str, record_id: str):
    with connect() as conn:
        record = conn.execute("SELECT * FROM records WHERE id = ? AND run_id = ?",
                              (record_id, run_id)).fetchone()
        merges = conn.execute(
            "SELECT absorbed_record_id, score, field_survivorship FROM merges "
            "WHERE survivor_record_id = ?", (record_id,),
        ).fetchall()

    timeline = _audit_rows(run_id, entity_id=record_id, limit=None)
    return templates.TemplateResponse(
        request, "record_detail.html",
        {
            "run_id": run_id, "record_id": record_id,
            "open_escalations": open_question_count(run_id),
            "record": dict(record) if record else None,
            "data": json.loads(record["data"]) if record else {},
            "survivorship": json.loads(merges[0]["field_survivorship"]) if merges else {},
            "absorbed": len(merges),
            "timeline": timeline,
        },
    )


@router.post("/runs/{run_id}/push")
async def start_push(run_id: str):
    schema = load_schema(settings.schema_path)
    policy = load_policy()

    # Pushing thousands of records takes minutes. Awaiting it here held the POST
    # open for the whole run, so the browser sat on "waiting for localhost" with
    # no way to tell a slow push from a hung one. It runs in the background and
    # reports itself through the same progress stream as every other stage.
    emit(run_id, "stage", "Starting the push to the target system", stage="pushing")
    _start_background(push_run, run_id, schema, policy)

    return RedirectResponse(url=f"/runs/{run_id}/push", status_code=303)


@router.post("/runs/{run_id}/rollback")
async def start_rollback(run_id: str):
    # Same reasoning as the push: a rollback deletes thousands of records over
    # HTTP and must not hold the browser open while it does.
    emit(run_id, "stage", "Starting the rollback", stage="pushing")
    _start_background(rollback_run, run_id)
    return RedirectResponse(url=f"/runs/{run_id}/push", status_code=303)


@router.get("/runs/{run_id}/push")
async def view_push(request: Request, run_id: str):
    policy = load_policy()
    with connect() as conn:
        run = conn.execute("SELECT stage FROM runs WHERE id = ?", (run_id,)).fetchone()
        by_outcome = conn.execute(
            "SELECT outcome, COUNT(*) AS n FROM push_attempts WHERE run_id = ? GROUP BY outcome",
            (run_id,),
        ).fetchall()
        by_status = conn.execute(
            "SELECT status_code, COUNT(*) AS n FROM push_attempts WHERE run_id = ? "
            "GROUP BY status_code ORDER BY n DESC",
            (run_id,),
        ).fetchall()
        retried = conn.execute(
            "SELECT COUNT(*) AS n FROM push_attempts WHERE run_id = ? AND attempt_number > 1",
            (run_id,),
        ).fetchone()["n"]
        rejected = conn.execute(
            "SELECT COUNT(*) AS n FROM escalations WHERE run_id = ? AND reason_code = 'PUSH_REJECTED'",
            (run_id,),
        ).fetchone()["n"]
        succeeded = conn.execute(
            "SELECT COUNT(DISTINCT record_id) AS n FROM push_attempts "
            "WHERE run_id = ? AND outcome = 'success'",
            (run_id,),
        ).fetchone()["n"]

    total = succeeded + rejected
    failure_rate = (rejected / total) if total else 0.0
    return templates.TemplateResponse(
        request, "push.html",
        {
            "run_id": run_id,
            "stage": run["stage"] if run else "unknown",
            "by_outcome": [dict(r) for r in by_outcome],
            "by_status": [dict(r) for r in by_status],
            "retried": retried,
            "succeeded": succeeded,
            "rejected": rejected,
            "failure_rate": round(failure_rate * 100, 1),
            "offer_rollback": failure_rate > policy["push"]["offer_rollback_above_failure_rate"],
            "rollback_threshold": int(policy["push"]["offer_rollback_above_failure_rate"] * 100),
            "open_escalations": open_question_count(run_id),
            # The push page streams the same frames as the overview, so it needs
            # the same two inputs the shared activity component reads.
            "is_active": (run["stage"] if run else "unknown") == "pushing",
            "frames": progress.read_frames(run_id, 0, limit=300),
        },
    )


def _mapped_column(run_id: str, entity_id: str) -> dict | None:
    """The column a mapping question is about, with what is actually in it.

    Confidence numbers say how unsure the agent is, not what the column holds --
    and nobody can decide where 'mgr_code' belongs without seeing a few of its
    values. The mapping page has always shown them; the card asking the question
    did not.
    """
    if not entity_id:
        return None
    with connect() as conn:
        row = conn.execute(
            """SELECT sc.name, sc.sample_values, sc.inferred_type, sc.null_rate,
                      sc.distinct_count, sf.filename
               FROM mappings m
               JOIN source_columns sc ON sc.id = m.source_column_id
               JOIN source_files sf ON sf.id = sc.source_file_id
               WHERE m.id = ? AND m.run_id = ?""",
            (entity_id, run_id),
        ).fetchone()
    if row is None:
        return None
    return {
        "name": row["name"],
        "filename": row["filename"],
        "inferred_type": row["inferred_type"],
        "null_rate": row["null_rate"],
        "distinct_count": row["distinct_count"],
        "samples": [str(v) for v in (json.loads(row["sample_values"]) if row["sample_values"] else [])][:8],
    }


def _compared_pair(run_id: str, entity_id: str) -> dict | None:
    """The two records behind one DUPE_AMBIGUOUS question, side by side.

    A duplicate question is unanswerable as prose -- "are these the same person"
    only means something once you can see both of them. Fields are shown in one
    list with each record's value beside the other's, and the ones that differ
    are marked, because the differences are the entire decision.
    """
    if not entity_id or ":" not in entity_id:
        return None
    left_id, _, right_id = entity_id.partition(":")

    with connect() as conn:
        rows = conn.execute(
            """SELECT r.id, r.natural_key, r.data, sf.filename
               FROM records r JOIN source_files sf ON sf.id = r.source_file_id
               WHERE r.run_id = ? AND r.id IN (?, ?)""",
            (run_id, left_id, right_id),
        ).fetchall()

    by_id = {r["id"]: r for r in rows}
    left, right = by_id.get(left_id), by_id.get(right_id)
    if not left or not right:
        return None

    left_data, right_data = json.loads(left["data"]), json.loads(right["data"])
    field_names = [f for f in dict.fromkeys([*left_data, *right_data])
                   if left_data.get(f) not in (None, "") or right_data.get(f) not in (None, "")]

    fields = []
    for name in field_names:
        a, b = left_data.get(name), right_data.get(name)
        fields.append({
            "name": name,
            "left": "" if a in (None, "") else a,
            "right": "" if b in (None, "") else b,
            "differs": str(a or "") != str(b or ""),
        })

    return {
        "left": {"id": left["id"], "label": left["natural_key"] or "no employee id",
                 "filename": left["filename"]},
        "right": {"id": right["id"], "label": right["natural_key"] or "no employee id",
                  "filename": right["filename"]},
        "fields": fields,
        "differing": sum(1 for f in fields if f["differs"]),
    }


def _affected_records(run_id: str, reason_code: str, signature: str) -> list[dict]:
    """The rows one question is actually about, for the card's accordion."""
    with connect() as conn:
        rows = conn.execute(
            """SELECT r.id, r.natural_key, r.status, r.blocked_on, r.data, sf.filename
               FROM escalations e
               JOIN records r ON r.id = e.entity_id
               JOIN source_files sf ON sf.id = r.source_file_id
               WHERE e.run_id = ? AND e.status = 'open'
                 AND e.reason_code = ? AND e.signature = ?
               LIMIT ?""",
            (run_id, reason_code, signature, ROWS_PER_CARD),
        ).fetchall()

    examples = []
    for row in rows:
        data = json.loads(row["data"])
        examples.append({
            "id": row["id"],
            "filename": row["filename"],
            "natural_key": row["natural_key"],
            "fields": [(k, v) for k, v in data.items() if v not in (None, "")],
        })
    return examples


def _per_row_rules(schema):
    """The rules that can be judged from one record. R6 needs the full id set,
    which only exists after reconciliation -- the same exclusion validate.py
    makes, for the same reason."""
    return [r for r in schema.cross_field_rules if r.id != "R6"]


def _fill_targets(run_id: str, escalation_id: str) -> tuple[list[dict], str | None, str, int]:
    """Every record behind one question, with the field they are all missing."""
    with connect() as conn:
        anchor = conn.execute(
            "SELECT reason_code, signature, question, context FROM escalations "
            "WHERE id = ? AND run_id = ?", (escalation_id, run_id),
        ).fetchone()
        if anchor is None:
            return [], None, "", 0

        context = json.loads(anchor["context"]) if anchor["context"] else {}
        field = context.get("repair_field")
        if not field:
            return [], None, "", 0

        rows = conn.execute(
            """SELECT e.id AS escalation_id, r.id AS record_id, r.natural_key, r.data,
                      e.suggested_value,
                      json_extract(e.context, '$.employee_id') AS employee_id
               FROM escalations e JOIN records r ON r.id = e.entity_id
               WHERE e.run_id = ? AND e.reason_code = ? AND e.signature = ? AND e.status = 'open'
               ORDER BY r.natural_key
               LIMIT ?""",
            (run_id, anchor["reason_code"], anchor["signature"], FILL_PAGE_SIZE),
        ).fetchall()

        remaining = conn.execute(
            """SELECT COUNT(*) AS n FROM escalations
               WHERE run_id = ? AND reason_code = ? AND signature = ? AND status = 'open'""",
            (run_id, anchor["reason_code"], anchor["signature"]),
        ).fetchone()["n"]

    out = []
    for row in rows:
        data = json.loads(row["data"])
        out.append({
            "escalation_id": row["escalation_id"],
            "record_id": row["record_id"],
            "employee_id": row["employee_id"] or row["natural_key"] or "",
            "name": " ".join(x for x in (data.get("first_name"), data.get("last_name")) if x),
            "current": data.get(field) or "",
            "proposed": row["suggested_value"] or "",
        })
    return out, field, anchor["question"], remaining


def _apply_filled_values(run_id: str, field: str, rows: list[dict],
                         supplied: dict[str, str], schema,
                         note: str = "") -> tuple[int, list[dict]]:
    """Validates and applies one value per record. Returns (applied, rejected).

    Each value is checked by building the whole record with it and running the
    same strict model the push uses, so anything accepted here is something the
    target will accept -- rather than discovering it was wrong on the next push.
    """
    strict_model = build_model(schema, require_all=True)
    by_escalation = {r["escalation_id"]: r for r in rows}
    applied, rejected = 0, []

    for escalation_id, value in supplied.items():
        row = by_escalation.get(escalation_id)
        if row is None or not value:
            continue

        with connect() as conn:
            record = conn.execute("SELECT data, status FROM records WHERE id = ?",
                                  (row["record_id"],)).fetchone()
            if record is None:
                continue
            data = json.loads(record["data"])
            try:
                cleaned = strict_model(**{**data, field: value})
            except ValidationError as exc:
                rejected.append({
                    "employee_id": row["employee_id"],
                    "value": value,
                    "why": exc.errors()[0].get("msg", "not a valid value for this field"),
                })
                continue

            # Field validation is not the whole bar. A record blocked by a
            # cross-field rule is only fixed when the rule passes, and correcting
            # one field can leave another rule broken -- so the same rules the
            # pipeline runs are run again here. Unblocking on the strict model
            # alone would hand the push a record it still has to refuse.
            # From the validated model, not the stored JSON: the rules compare
            # dates, and everything in the record's JSON is a string. Passing
            # those straight in raised a TypeError instead of failing the rule.
            broken = evaluate_rules(cleaned.model_dump(), _per_row_rules(schema), RuleContext())
            if broken:
                rejected.append({
                    "employee_id": row["employee_id"],
                    "value": value,
                    "why": "still breaks " + ", ".join(
                        RULE_DESCRIPTIONS.get(rule_id, rule_id) for rule_id in broken),
                })
                continue

            before = data.get(field)
            data[field] = str(getattr(cleaned, field))
            # A record that was held back is now genuinely clean: it passed the
            # field model and every per-row rule above. One that was already
            # pushable keeps the status it had -- a merge survivor stays one.
            restored = ("clean" if record["status"] in ("excluded", "blocked", "incomplete")
                        else record["status"])
            conn.execute(
                "UPDATE records SET data = ?, status = ?, blocked_on = NULL, updated_at = ? WHERE id = ?",
                (json.dumps(data, default=str), restored, utcnow(), row["record_id"]),
            )
            record_change(
                conn, run_id=run_id, actor="human", stage="pushed",
                reason_code="PUSH_REJECTED", entity_type="record", entity_id=row["record_id"],
                field=field, before=str(before or ""), after=data[field],
                note=note or "corrected by a person; the agent had no safe automatic fix",
            )
            conn.execute(
                "UPDATE escalations SET status = 'resolved', resolved_at = ? WHERE id = ?",
                (utcnow(), escalation_id),
            )
            applied += 1

    return applied, rejected


@router.get("/runs/{run_id}/escalations/{escalation_id}/fill")
async def fill_form(request: Request, run_id: str, escalation_id: str):
    """One value per record, for a question no single answer can settle.

    Most escalations compress: one answer closes every case. A missing value does
    not, because each record needs a *different* value that only a person has.
    Asking for them one card at a time would be hundreds of clicks, and a single
    text box cannot express hundreds of different answers -- so this is the one
    place the UI stops being a queue and becomes a spreadsheet.
    """
    schema = load_schema(settings.schema_path)
    rows, field, question, remaining = _fill_targets(run_id, escalation_id)
    if field is None:
        return RedirectResponse(url=f"/runs/{run_id}/queue", status_code=303)

    return templates.TemplateResponse(
        request, "fill.html",
        {
            "run_id": run_id, "escalation_id": escalation_id,
            "field": field, "field_spec": schema.fields.get(field),
            "question": question, "rows": rows, "remaining": remaining,
            "open_escalations": open_question_count(run_id),
        },
    )


@router.post("/runs/{run_id}/escalations/{escalation_id}/fill")
async def fill_submit(request: Request, run_id: str, escalation_id: str):
    schema = load_schema(settings.schema_path)
    rows, field, question, remaining = _fill_targets(run_id, escalation_id)
    if field is None:
        return RedirectResponse(url=f"/runs/{run_id}/queue", status_code=303)

    form = await request.form()
    action = form.get("action") or "save"

    # Reading an instruction back before acting on it is the whole point: the
    # consultant sees what was understood and how many records it touches, and
    # nothing is written until they say go.
    instruction = (form.get("instruction") or "").strip()

    # The instruction fills the form in; it does not write anything. Every row
    # comes back populated and editable, so what is about to be saved is on
    # screen as rows rather than described in a sentence -- and one row can still
    # be corrected before saving, which an "apply to all" button cannot offer.
    if action == "interpret":
        plan, why_not = interpret_bulk_instruction(instruction, field, remaining, schema)
        return templates.TemplateResponse(
            request, "fill.html",
            {
                "run_id": run_id, "escalation_id": escalation_id,
                "field": field, "field_spec": schema.fields.get(field),
                "question": question, "rows": rows, "remaining": remaining,
                "plan": plan, "plan_refused": why_not, "instruction": instruction,
                "prefill": plan["value"] if plan else "",
                "open_escalations": open_question_count(run_id),
            },
        )

    supplied = {r["escalation_id"]: (form.get(f"value-{r['escalation_id']}") or "").strip()
                for r in rows}

    # A pasted "employee_id,value" block is the realistic path: the consultant
    # has this in a spreadsheet, not in their head. It fills any row it names
    # that the per-row inputs left blank.
    by_employee = {r["employee_id"]: r["escalation_id"] for r in rows if r["employee_id"]}
    for line in (form.get("pasted") or "").splitlines():
        parts = [p.strip() for p in re.split(r"[,\t;]", line, maxsplit=1)]
        if len(parts) != 2 or not parts[1]:
            continue
        escalation_for_employee = by_employee.get(parts[0])
        if escalation_for_employee and not supplied.get(escalation_for_employee):
            supplied[escalation_for_employee] = parts[1]

    note = (f"set in bulk on the consultant instruction: {instruction}"
            if instruction else "")
    applied, rejected = _apply_filled_values(run_id, field, rows, supplied, schema, note=note)

    if rejected:
        rows_after, _f, question_after, remaining_after = _fill_targets(run_id, escalation_id)
        return templates.TemplateResponse(
            request, "fill.html",
            {
                "run_id": run_id, "escalation_id": escalation_id,
                "field": field, "field_spec": schema.fields.get(field),
                "question": question_after, "rows": rows_after, "remaining": remaining_after,
                "applied": applied, "rejected": rejected,
                "open_escalations": open_question_count(run_id),
            },
        )
    return RedirectResponse(url=f"/runs/{run_id}/queue", status_code=303)


@router.get("/runs/{run_id}/queue")
async def view_queue(request: Request, run_id: str):
    schema = load_schema(settings.schema_path)

    with connect() as conn:
        rows = conn.execute(
            """SELECT reason_code, signature, scope,
                      MIN(id) AS id, MIN(entity_id) AS entity_id,
                      MIN(question) AS question, MIN(evidence) AS evidence,
                      MIN(suggested_action) AS suggested_action,
                      MIN(suggested_value) AS suggested_value, MIN(options) AS options,
                      -- Extracted rather than taking MIN over the whole JSON:
                      -- context differs per row (each carries its own payload),
                      -- so MIN(context) picked an arbitrary row and lost the
                      -- field whenever that row happened not to have one.
                      MAX(json_extract(context, '$.repair_field')) AS repair_field,
                      COUNT(*) AS similar_count, SUM(affected_count) AS rows_affected
               FROM escalations
               WHERE run_id = ? AND status = 'open'
               GROUP BY reason_code, signature
               ORDER BY reason_code, rows_affected DESC""",
            (run_id,),
        ).fetchall()
        resolved_count = conn.execute(
            "SELECT COUNT(*) AS n FROM escalations WHERE run_id = ? AND status = 'resolved'", (run_id,)
        ).fetchone()["n"]

    groups: dict[str, dict] = {}
    for r in rows:
        group = groups.setdefault(r["reason_code"], {"cards": [], "total": 0, "rows_affected": 0})
        group["total"] += 1
        group["rows_affected"] += r["rows_affected"] or 0
        if len(group["cards"]) < CARDS_PER_REASON_CODE:
            card = dict(r)
            card["options"] = json.loads(card["options"]) if card["options"] else []
            # A duplicate question needs both records; everything else needs the
            # one it is about.
            card["pair"] = (_compared_pair(run_id, r["entity_id"])
                            if r["reason_code"] == "DUPE_AMBIGUOUS" else None)
            # Any refusal whose field is known can be worked through row by row.
            # That is the only honest view when one refusal covers many different
            # offending values -- 102 misspelled job titles share a question but
            # not a correction, and a single text box cannot say that.
            # Codes whose answer differs per record. The rest are left alone on
            # purpose: one target field for a column, one truth ordering for a
            # disagreement, one reading for a date column -- those genuinely do
            # have a single answer, and a row-by-row page would imply otherwise.
            card["column"] = (_mapped_column(run_id, r["entity_id"])
                              if r["reason_code"] in ("MAP_AMBIGUOUS", "MAP_UNMAPPED") else None)
            card["fill_field"] = (r["repair_field"]
                                  if r["reason_code"] in FILLABLE_REASON_CODES else None)
            card["examples"] = ([] if card["pair"]
                                else _affected_records(run_id, r["reason_code"], r["signature"]))
            group["cards"].append(card)

    return templates.TemplateResponse(
        request, "queue.html",
        {
            "run_id": run_id,
            "groups": groups,
            "resolved_count": resolved_count,
            "open_escalations": open_question_count(run_id),
            "target_fields": list(schema.fields),
            "cards_per_code": CARDS_PER_REASON_CODE,
            "help": REASON_CODE_HELP,
        },
    )


@router.post("/runs/{run_id}/escalations/{escalation_id}/resolve")
async def resolve_escalation_route(
    run_id: str, escalation_id: str,
    action: str = Form(...), value: str = Form(""), apply_to_all: str = Form(""),
):
    with connect() as conn:
        resolve_escalation(
            conn, run_id, escalation_id, action=action, value=value or None,
            apply_to_all=bool(apply_to_all),
        )

    # Answering the last blocking mapping question resumes the run on its own --
    # the consultant doesn't have to know there was a stage to restart.
    if mapping_gate_is_open(run_id):
        schema = load_schema(settings.schema_path)
        policy = load_policy()
        _start_background(advance_run, run_id, schema, policy, None)

    return RedirectResponse(url=f"/runs/{run_id}/queue", status_code=303)


def _status_tiles(rows) -> list[dict]:
    """One tile per known status in a fixed order, so the mosaic keeps its shape
    while the numbers fill in. Any status not on the list still gets a tile."""
    counts = {r["status"]: r["n"] for r in rows}
    tiles = [{"status": status, "n": counts.pop(status, 0)} for status in RECORD_STATUS_ORDER]
    tiles.extend({"status": status, "n": n} for status, n in counts.items())
    # 'pending' is an implementation detail of a row mid-flight; show it only if
    # rows are actually sitting in it.
    return [t for t in tiles if t["status"] != "pending" or t["n"]]


@router.get("/runs/{run_id}")
async def run_summary(request: Request, run_id: str):
    with connect() as conn:
        run = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        record_counts = conn.execute(
            "SELECT status, COUNT(*) AS n FROM records WHERE run_id = ? GROUP BY status", (run_id,)
        ).fetchall()
        # Distinct questions, matching the queue and the nav badge. Counting
        # escalation rows here instead said "80 LOGIC_CONTRADICTION" while the
        # queue showed three cards, which is the same number told two ways.
        escalations = conn.execute(
            """SELECT reason_code, COUNT(*) AS n, SUM(affected) AS affected FROM (
                   SELECT reason_code, signature, SUM(affected_count) AS affected
                   FROM escalations
                   WHERE run_id = ? AND status = 'open'
                   GROUP BY reason_code, signature)
               GROUP BY reason_code""",
            (run_id,),
        ).fetchall()
        merge_count = conn.execute(
            "SELECT COUNT(*) AS n FROM merges WHERE run_id = ?", (run_id,)
        ).fetchone()["n"]

    return templates.TemplateResponse(
        request, "run_summary.html",
        {
            "run_id": run_id,
            "stage": run["stage"] if run else "unknown",
            "record_counts": _status_tiles(record_counts),
            "escalations": [dict(r) for r in escalations],
            "merge_count": merge_count,
            "is_active": (run["stage"] if run else "unknown") in ACTIVE_STAGES,
            "status": run_status(run_id),
            "open_escalations": open_question_count(run_id),
            # Rendered server-side so the page is correct before any stream
            # connects, and so it still works with JavaScript off.
            "frames": progress.read_frames(run_id, 0, limit=300),
        },
    )

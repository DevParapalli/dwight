import asyncio
import csv
import io
import json
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Form, Request, Response, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
import httpx
from sse_starlette.sse import EventSourceResponse
from starlette.concurrency import run_in_threadpool

from app.agent.policy import load_policy
from app.agent.push import push_run, rollback_run
from app.agent.resolve import resolve_escalation
from app.agent.runner import advance_run, mapping_gate_is_open, run_status
from app import progress
from app.audit import record_change, utcnow
from app.db import connect, new_id, truncate_all
from app.schema.loader import load_schema
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
        {"run_id": run_id, "mappings": mappings, "target_fields": list(schema.fields)},
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


def _run_snapshot(run_id: str) -> dict:
    with connect() as conn:
        run = conn.execute("SELECT stage FROM runs WHERE id = ?", (run_id,)).fetchone()
        records = conn.execute(
            "SELECT status, COUNT(*) AS n FROM records WHERE run_id = ? GROUP BY status", (run_id,)
        ).fetchall()
        open_escalations = conn.execute(
            "SELECT COUNT(*) AS n FROM escalations WHERE run_id = ? AND status = 'open'", (run_id,)
        ).fetchone()["n"]
        pushed = conn.execute(
            "SELECT COUNT(DISTINCT record_id) AS n FROM push_attempts "
            "WHERE run_id = ? AND outcome = 'success'", (run_id,)
        ).fetchone()["n"]
    return {
        "stage": run["stage"] if run else "unknown",
        "records": {r["status"]: r["n"] for r in records},
        "open_escalations": open_escalations,
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
        request, "records.html", {"run_id": run_id, "records": records, "q": q},
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
    await run_in_threadpool(push_run, run_id, schema, policy)
    return RedirectResponse(url=f"/runs/{run_id}/push", status_code=303)


@router.post("/runs/{run_id}/rollback")
async def start_rollback(run_id: str):
    await run_in_threadpool(rollback_run, run_id)
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
        },
    )


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
            card["examples"] = _affected_records(run_id, r["reason_code"], r["signature"])
            group["cards"].append(card)

    return templates.TemplateResponse(
        request, "queue.html",
        {
            "run_id": run_id,
            "groups": groups,
            "resolved_count": resolved_count,
            "open_escalations": sum(g["total"] for g in groups.values()),
            "target_fields": list(schema.fields),
            "cards_per_code": CARDS_PER_REASON_CODE,
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
        # Distinct questions, not escalation rows: the nav badge should say how
        # many things need answering, which is what the queue actually shows.
        open_questions = conn.execute(
            "SELECT COUNT(*) AS n FROM (SELECT 1 FROM escalations WHERE run_id = ? "
            "AND status = 'open' GROUP BY reason_code, signature)", (run_id,)
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
            "open_escalations": open_questions,
            # Rendered server-side so the page is correct before any stream
            # connects, and so it still works with JavaScript off.
            "frames": progress.read_frames(run_id, 0, limit=300),
        },
    )

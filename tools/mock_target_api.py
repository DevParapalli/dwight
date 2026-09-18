#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.14"
# dependencies = [
#   "fastapi>=0.141.1",
#   "jinja2>=3.1.6",
#   "uvicorn>=0.53.0",
# ]
# ///
"""A stand-in for the Darwinbox target system: a separate process the migration
agent pushes to over real HTTP, with its own storage.

It serves its own read-only HTML at / so what it holds can be inspected without
going through the agent, which is the only way to see the stored record rather
than the agent's account of it.

It is deliberately not mounted inside the agent. Being a genuinely external
service is the point -- the agent has to survive transient failures, honour
idempotency keys on retry, and handle a rejection for a rule it could not have
known in advance, exactly as it would against a real vendor API.

Failure injection is mixed on purpose. A stub that only ever returns 500 makes
retry logic trivially always-succeed; the deterministic 422 is the only way
PUSH_REJECTED and rollback are demonstrable at all.

Usage:
    uv run tools/mock_target_api.py
    uv run tools/mock_target_api.py --port 8900 --rate-500 0.10 --rate-429 0.03
"""

import argparse
import json
import random
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import uvicorn
from fastapi import FastAPI, Header, Request, Response
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

DDL = """
CREATE TABLE IF NOT EXISTS target_employees (
    employee_id TEXT PRIMARY KEY,
    run_id TEXT,
    data TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

# Rules the agent cannot know up front. Each one is something a real vendor
# system enforces and no source file announces, which is the whole point of
# PUSH_REJECTED: the agent finds out by being told no.
#
# Every message names what the target WILL accept. A refusal that says only
# "invalid" leaves the consultant, and the agent, with nothing to act on.
ALLOWED_COST_CENTERS = {"CC-100", "CC-200", "CC-300", "CC-400", "CC-500"}

# This API onboards permanent and contract staff. Interns go through a separate
# campus system, so the source systems' own "intern" is refused here.
ALLOWED_EMPLOYMENT_TYPES = {"full_time", "part_time", "contract"}

# The target keeps its own job-title catalogue and will not invent a new one on
# an employee's say-so, so a misspelled title is refused rather than absorbed.
ALLOWED_DESIGNATIONS = {
    "Software Engineer", "Senior Software Engineer", "Engineering Manager",
    "Data Analyst", "Business Analyst", "Sales Executive", "Account Manager",
    "HR Specialist", "Finance Analyst", "Operations Manager",
    "Customer Support Associate", "Legal Counsel", "Marketing Specialist", "Director",
}

state = {
    "db": Path("data/mock_target.db"),
    "rate_500": 0.10,
    "rate_429": 0.03,
    "rng": random.Random(7),
    "idempotency": {},
    "enable_nuke": False,
}

app = FastAPI(title="mock darwinbox target")

# The browsable pages borrow Proxima from the agent's static directory. A
# stylesheet is the only thing the two systems share -- resolved from this
# file rather than the working directory, so it holds under `just target`,
# under compose, and from a test.
STATIC_DIR = Path(__file__).resolve().parent.parent / "app" / "ui" / "static"
if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
else:
    print(f"warning: {STATIC_DIR} is missing, the browsable pages will render unstyled")

templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "target_templates"))

PAGE_SIZE = 50

# This system stores UTC and is read by people in India, same as the agent.
_DISPLAY_TZ = ZoneInfo("Asia/Kolkata")


def _local_time(value: str | None) -> str:
    if not value:
        return ""
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return value
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(_DISPLAY_TZ).strftime("%d %b %Y, %H:%M")


def _connect() -> sqlite3.Connection:
    state["db"].parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(state["db"])
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _now() -> str:
    return datetime.now(UTC).isoformat()


@app.post("/v1/employees")
@app.put("/v1/employees")
async def upsert_employee(
    request: Request, response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    payload = await request.json()

    # Replaying a key returns the first outcome instead of applying twice --
    # this is what makes the agent's retries safe.
    if idempotency_key and idempotency_key in state["idempotency"]:
        status, body = state["idempotency"][idempotency_key]
        response.status_code = status
        return body

    employee_id = payload.get("employee_id")
    if not employee_id:
        response.status_code = 400
        return {"error": "employee_id is required"}

    roll = state["rng"].random()
    if roll < state["rate_500"]:
        response.status_code = 500
        return {"error": "internal error, retry"}
    if roll < state["rate_500"] + state["rate_429"]:
        response.status_code = 429
        response.headers["Retry-After"] = "1"
        return {"error": "rate limited"}

    cost_center = payload.get("cost_center")
    if cost_center and cost_center not in ALLOWED_COST_CENTERS:
        response.status_code = 422
        return {"error": (f"cost_center {cost_center!r} is not an accepted cost centre; "
                          f"accepted values are {', '.join(sorted(ALLOWED_COST_CENTERS))}")}

    employment_type = payload.get("employment_type")
    if employment_type and employment_type not in ALLOWED_EMPLOYMENT_TYPES:
        response.status_code = 422
        return {"error": (f"employment_type {employment_type!r} is not onboarded through this "
                          f"API; accepted values are {', '.join(sorted(ALLOWED_EMPLOYMENT_TYPES))}")}

    designation = payload.get("designation")
    if designation and designation not in ALLOWED_DESIGNATIONS:
        response.status_code = 422
        return {"error": (f"designation {designation!r} is not in the job-title catalogue; "
                          f"accepted values are {', '.join(sorted(ALLOWED_DESIGNATIONS))}")}

    # Deliberately a rule nothing can satisfy automatically: a date of birth
    # cannot be derived from any other field, so this is the case where the
    # right answer is to stop and ask a person.
    if not payload.get("date_of_birth"):
        response.status_code = 422
        return {"error": "date_of_birth is required for identity verification and is missing"}

    now = _now()
    with _connect() as conn:
        conn.execute(
            """INSERT INTO target_employees (employee_id, run_id, data, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT (employee_id) DO UPDATE SET
                   run_id = excluded.run_id, data = excluded.data, updated_at = excluded.updated_at""",
            (employee_id, payload.get("run_id"), json.dumps(payload, default=str), now, now),
        )

    result = (200, {"employee_id": employee_id, "status": "upserted"})
    if idempotency_key:
        state["idempotency"][idempotency_key] = result
    response.status_code = result[0]
    return result[1]


@app.delete("/v1/employees/{employee_id}")
async def delete_employee(employee_id: str, response: Response):
    """Rollback path -- removing a pushed record is observable in this store."""
    with _connect() as conn:
        removed = conn.execute(
            "DELETE FROM target_employees WHERE employee_id = ?", (employee_id,)
        ).rowcount
    if not removed:
        response.status_code = 404
        return {"error": "not found"}
    return {"employee_id": employee_id, "status": "deleted"}


@app.get("/v1/employees")
async def list_employees(run_id: str | None = None, limit: int = 50):
    with _connect() as conn:
        if run_id:
            rows = conn.execute(
                "SELECT employee_id, run_id, updated_at FROM target_employees WHERE run_id = ? LIMIT ?",
                (run_id, limit),
            ).fetchall()
            total = conn.execute(
                "SELECT COUNT(*) AS n FROM target_employees WHERE run_id = ?", (run_id,)
            ).fetchone()["n"]
        else:
            rows = conn.execute(
                "SELECT employee_id, run_id, updated_at FROM target_employees LIMIT ?", (limit,)
            ).fetchall()
            total = conn.execute("SELECT COUNT(*) AS n FROM target_employees").fetchone()["n"]
    return {"total": total, "employees": [dict(r) for r in rows]}


@app.get("/v1/employees/{employee_id}")
async def get_employee(employee_id: str, response: Response):
    """The stored record itself, not a summary of it."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM target_employees WHERE employee_id = ?", (employee_id,)
        ).fetchone()
    if row is None:
        response.status_code = 404
        return {"error": "not found"}
    return {
        "employee_id": row["employee_id"], "run_id": row["run_id"],
        "created_at": row["created_at"], "updated_at": row["updated_at"],
        "data": json.loads(row["data"]),
    }


@app.get("/", response_class=HTMLResponse)
async def browse_employees(request: Request, q: str = "", offset: int = 0):
    """Everything this system holds, regardless of which run put it here."""
    offset = max(offset, 0)
    like = f"%{q}%"
    with _connect() as conn:
        total = conn.execute("SELECT COUNT(*) AS n FROM target_employees").fetchone()["n"]
        if q:
            # data is the whole payload as JSON, so one LIKE covers every field
            # a person would search by -- name, email, department, cost centre.
            matching = conn.execute(
                "SELECT COUNT(*) AS n FROM target_employees "
                "WHERE employee_id LIKE ? OR data LIKE ?", (like, like),
            ).fetchone()["n"]
            rows = conn.execute(
                "SELECT * FROM target_employees WHERE employee_id LIKE ? OR data LIKE ? "
                "ORDER BY updated_at DESC, employee_id LIMIT ? OFFSET ?",
                (like, like, PAGE_SIZE, offset),
            ).fetchall()
        else:
            matching = total
            rows = conn.execute(
                "SELECT * FROM target_employees ORDER BY updated_at DESC, employee_id "
                "LIMIT ? OFFSET ?", (PAGE_SIZE, offset),
            ).fetchall()

    employees = []
    for row in rows:
        data = json.loads(row["data"])
        name = " ".join(filter(None, [data.get("first_name"), data.get("last_name")]))
        employees.append({
            "employee_id": row["employee_id"],
            "name": name or "\u2014",
            "work_email": data.get("work_email") or "\u2014",
            "department": data.get("department") or "\u2014",
            "run_id": row["run_id"] or "\u2014",
            "updated_at": _local_time(row["updated_at"]),
        })

    return templates.TemplateResponse(request, "index.html", {
        "db_path": state["db"], "q": q, "total": total, "matching": matching,
        "employees": employees, "offset": offset,
        "prev_offset": max(offset - PAGE_SIZE, 0), "next_offset": offset + PAGE_SIZE,
        "has_next": offset + len(employees) < matching,
    })


@app.get("/employees/{employee_id}", response_class=HTMLResponse)
async def browse_employee(request: Request, employee_id: str):
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM target_employees WHERE employee_id = ?", (employee_id,)
        ).fetchone()

    if row is None:
        return templates.TemplateResponse(
            request, "not_found.html",
            {"db_path": state["db"], "employee_id": employee_id}, status_code=404,
        )

    data = json.loads(row["data"])
    # Insertion order, which is the order the agent sent the fields in, rather
    # than alphabetical: it keeps employee_id and the name at the top where a
    # reader looks for them.
    fields = [{"name": k, "value": "" if v is None else str(v)} for k, v in data.items()]
    return templates.TemplateResponse(request, "employee.html", {
        "db_path": state["db"], "employee_id": row["employee_id"],
        "name": " ".join(filter(None, [data.get("first_name"), data.get("last_name")])),
        "run_id": row["run_id"], "fields": fields,
        "created_at": _local_time(row["created_at"]),
        "updated_at": _local_time(row["updated_at"]),
        "raw": json.dumps(data, indent=2, ensure_ascii=False),
    })


@app.post("/nuke")
async def nuke():
    """Empties this system's own store. Testing only, off unless --enable-nuke."""
    if not state["enable_nuke"]:
        return Response(status_code=404)
    with _connect() as conn:
        deleted = conn.execute("DELETE FROM target_employees").rowcount
    # Idempotency keys live in memory and are keyed to rows that no longer
    # exist; leaving them would make a replayed push a silent no-op against an
    # empty store.
    state["idempotency"].clear()
    return {"deleted": {"target_employees": deleted}}


@app.get("/health")
async def health():
    return {"status": "ok", "rate_500": state["rate_500"], "rate_429": state["rate_429"]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8900)
    parser.add_argument("--host", default="127.0.0.1",
                        help="use 0.0.0.0 when running in a container")
    parser.add_argument("--db", type=Path, default=Path("data/mock_target.db"))
    parser.add_argument("--rate-500", type=float, default=0.10)
    parser.add_argument("--rate-429", type=float, default=0.03)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--reset", action="store_true", help="drop any previously pushed employees")
    parser.add_argument("--enable-nuke", action="store_true",
                        help="expose POST /nuke, which empties this store. testing only")
    args = parser.parse_args()

    state["db"] = args.db
    state["rate_500"] = args.rate_500
    state["rate_429"] = args.rate_429
    state["rng"] = random.Random(args.seed)
    state["enable_nuke"] = args.enable_nuke

    with _connect() as conn:
        conn.executescript(DDL)
        if args.reset:
            conn.execute("DELETE FROM target_employees")

    print(f"mock target on :{args.port}, store={args.db}, "
          f"500s={args.rate_500:.0%}, 429s={args.rate_429:.0%}"
          + (", /nuke ENABLED" if args.enable_nuke else ""))
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()

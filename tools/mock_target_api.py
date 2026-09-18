#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.14"
# dependencies = [
#   "fastapi>=0.141.1",
#   "uvicorn>=0.53.0",
# ]
# ///
"""A stand-in for the Darwinbox target system: a separate process the migration
agent pushes to over real HTTP, with its own storage.

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

import uvicorn
from fastapi import FastAPI, Header, Request, Response

DDL = """
CREATE TABLE IF NOT EXISTS target_employees (
    employee_id TEXT PRIMARY KEY,
    run_id TEXT,
    data TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

# A rule the agent cannot know up front: these source systems legitimately emit
# CC-600, and this target simply does not accept it.
ALLOWED_COST_CENTERS = {"CC-100", "CC-200", "CC-300", "CC-400", "CC-500"}

state = {
    "db": Path("data/mock_target.db"),
    "rate_500": 0.10,
    "rate_429": 0.03,
    "rng": random.Random(7),
    "idempotency": {},
    "enable_nuke": False,
}

app = FastAPI(title="mock darwinbox target")


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
        return {"error": f"cost_center {cost_center!r} is not an accepted cost centre"}

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

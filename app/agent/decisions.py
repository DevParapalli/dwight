import json

from app.audit import utcnow
from app.db import new_id


def lookup_decision(conn, reason_code: str, signature: str) -> dict | None:
    """Prior human resolutions are keyed by (reason_code, signature) and are
    global, not scoped to one run -- that's what makes the same question never
    get asked twice across re-imports."""
    row = conn.execute(
        "SELECT resolution, resolved_by, created_at FROM decisions "
        "WHERE reason_code = ? AND signature = ?",
        (reason_code, signature),
    ).fetchone()
    if row is None:
        return None
    return {
        "resolution": json.loads(row["resolution"]),
        "resolved_by": row["resolved_by"],
        "created_at": row["created_at"],
    }


def record_decision(conn, reason_code: str, signature: str, resolution: dict, resolved_by: str) -> None:
    conn.execute(
        """INSERT INTO decisions (id, reason_code, signature, resolution, resolved_by, created_at)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT (reason_code, signature) DO UPDATE SET
               resolution = excluded.resolution,
               resolved_by = excluded.resolved_by,
               created_at = excluded.created_at""",
        (new_id(), reason_code, signature, json.dumps(resolution), resolved_by, utcnow()),
    )

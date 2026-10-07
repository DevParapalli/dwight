import json

from app.db import connect, init_db
from app.settings import settings
from app.ui.routes import record_progress

T = "2026-10-07T00:00:00+00:00"


def _seed(conn) -> None:
    conn.execute("INSERT INTO runs VALUES ('run', 1, '', 'pushing', ?, ?)", (T, T))
    conn.execute("INSERT INTO source_files VALUES ('f', 'run', 'legacy_hris_employees.csv', 'csv', 8, ?)", (T,))
    records = [
        ("pushed", "clean", None),
        ("pushed2", "merged_survivor", None),
        ("dupe", "merged", None),
        ("rule", "blocked", "LOGIC_CONTRADICTION"),        # answered: held back
        ("value", "blocked", "VALUE_LOW_CONFIDENCE"),      # class question still open
        ("pair_a", "clean", None),                         # one side of an open pair
        ("refused", "clean", None),                        # open refusal
        ("ready", "clean", None),
    ]
    for rid, status, blocked_on in records:
        conn.execute("INSERT INTO records (id, run_id, source_file_id, data, status, blocked_on, created_at, "
                     "updated_at) VALUES (?, 'run', 'f', '{}', ?, ?, ?, ?)", (rid, status, blocked_on, T, T))
    for rid in ("pushed", "pushed2"):
        conn.execute("INSERT INTO push_attempts VALUES (?, 'run', ?, 1, 200, NULL, 'success', ?)", (f"p-{rid}", rid, T))
    conn.execute("INSERT INTO push_attempts VALUES ('p-refused', 'run', 'refused', 1, 422, NULL, 'failure', ?)", (T,))
    for eid, code, entity, status in [
        ("e1", "LOGIC_CONTRADICTION", "rule", "resolved"),
        ("e2", "VALUE_LOW_CONFIDENCE", "some-value", "open"),
        ("e3", "DUPE_AMBIGUOUS", "pair_a:pushed", "open"),
        ("e4", "PUSH_REJECTED", "refused", "open"),
    ]:
        conn.execute("INSERT INTO escalations (id, run_id, reason_code, scope, signature, entity_id, question, "
                     "status, created_at) VALUES (?, 'run', ?, 'record', ?, ?, '?', ?, ?)",
                     (eid, code, eid, entity, status, T))


def test_every_record_lands_in_exactly_one_bucket(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", tmp_path / "t.db")
    init_db()
    with connect() as conn:
        _seed(conn)

    progress = record_progress("run")
    counts = {b["key"]: b["n"] for b in progress["buckets"]}

    # A pushed record stays in the target even when it is one side of an open pair.
    assert counts == {"target": 2, "merged": 1, "held": 1, "waiting": 3, "ready": 1}
    assert sum(counts.values()) == progress["total"] == 8
    # Final outcomes only: target, merged and held back.
    assert progress["accounted_pct"] == 50.0


def test_unchanged_record_skipped_by_a_later_push_is_in_the_target(tmp_path, monkeypatch):
    from app.agent.push import _payload_hash

    monkeypatch.setattr(settings, "db_path", tmp_path / "t.db")
    init_db()
    same, changed = {"employee_id": "E1", "first_name": "Asha"}, {"employee_id": "E2", "first_name": "Ravi"}
    with connect() as conn:
        conn.execute("INSERT INTO runs VALUES ('run2', 1, '', 'reconciled', ?, ?)", (T, T))
        conn.execute("INSERT INTO source_files VALUES ('f', 'run2', 'legacy_hris_employees.csv', 'csv', 2, ?)", (T,))
        for rid, data in (("r1", same), ("r2", changed)):
            conn.execute("INSERT INTO records (id, run_id, source_file_id, natural_key, data, status, created_at, "
                         "updated_at) VALUES (?, 'run2', 'f', ?, ?, 'clean', ?, ?)",
                         (rid, data["employee_id"], json.dumps(data), T, T))
        # An earlier run pushed E1 with these values and E2 with older ones.
        conn.execute("INSERT INTO pushed_state VALUES ('E1', ?, 'run1', ?)", (_payload_hash(same), T))
        conn.execute("INSERT INTO pushed_state VALUES ('E2', 'stale', 'run1', ?)", (T,))

    def counts():
        return {b["key"]: b["n"] for b in record_progress("run2")["buckets"]}

    # Before this run pushes, both are still to be sent.
    assert counts()["ready"] == 2
    with connect() as conn:
        conn.execute("UPDATE runs SET stage = 'done' WHERE id = 'run2'")
    assert (counts()["target"], counts()["ready"]) == (1, 1)

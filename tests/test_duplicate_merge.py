from app.agent.resolve import resolve_escalation
from app.db import connect, init_db
from app.settings import settings
from app.ui.routes import _answer, record_progress

T = "2026-10-07T00:00:00+00:00"


def _seed(conn) -> None:
    conn.execute("INSERT INTO runs VALUES ('run', 1, '', 'reconciled', ?, ?)", (T, T))
    conn.execute("INSERT INTO source_files VALUES ('f', 'run', 'legacy_hris_employees.csv', 'csv', 3, ?)", (T,))
    # The reformatted copy (b) is read first, as it can be in a real export; the
    # rest of the file (c) writes ids the way a does.
    for rid, key, created in [("b", "EMP71", "2026-10-07T00:00:01+00:00"),
                              ("a", "EMP-000071", "2026-10-07T00:00:02+00:00"),
                              ("c", "EMP-000072", "2026-10-07T00:00:03+00:00")]:
        conn.execute("INSERT INTO records (id, run_id, source_file_id, natural_key, data, status, created_at, "
                     "updated_at) VALUES (?, 'run', 'f', ?, '{}', 'clean', ?, ?)", (rid, key, created, created))
    conn.execute("INSERT INTO escalations (id, run_id, reason_code, scope, signature, entity_id, question, "
                 "suggested_value, status, created_at) VALUES ('e', 'run', 'DUPE_AMBIGUOUS', 'pair', 's', 'a:b', "
                 "'?', 'keep_separate', 'open', ?)", (T,))


def _setup(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", tmp_path / "t.db")
    init_db()
    with connect() as conn:
        _seed(conn)


def _statuses() -> dict:
    with connect() as conn:
        return {r["id"]: r["status"] for r in conn.execute("SELECT id, status FROM records")}


def test_merge_keeps_the_record_numbered_like_its_export(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    with connect() as conn:
        resolve_escalation(conn, "run", "e", "edit", value="merge")
        merges = conn.execute("SELECT survivor_record_id, absorbed_record_id FROM merges").fetchall()

    assert _statuses() == {"a": "merged_survivor", "b": "merged", "c": "clean"}
    assert [tuple(m) for m in merges] == [("a", "b")]
    counts = {b["key"]: b["n"] for b in record_progress("run")["buckets"]}
    assert counts["merged"] == 1 and counts["ready"] == 2


def test_approving_the_proposal_keeps_them_separate(tmp_path, monkeypatch):
    # The proposal is keep_separate, so a bare approve must not merge.
    _setup(tmp_path, monkeypatch)
    with connect() as conn:
        resolve_escalation(conn, "run", "e", "approve")

    assert _statuses() == {"a": "clean", "b": "clean", "c": "clean"}


def test_merge_joins_the_records_that_now_stand_for_the_pair(tmp_path, monkeypatch):
    # b was absorbed into s while reconciling, before anyone answered the pair.
    _setup(tmp_path, monkeypatch)
    with connect() as conn:
        conn.execute("INSERT INTO records (id, run_id, source_file_id, natural_key, data, status, created_at, "
                     "updated_at) VALUES ('s', 'run', 'f', 'EMP-000099', '{}', 'merged_survivor', ?, ?)", (T, T))
        conn.execute("UPDATE records SET status = 'merged' WHERE id = 'b'")
        conn.execute("INSERT INTO merges (id, run_id, survivor_record_id, absorbed_record_id, score, "
                     "field_survivorship, created_at) VALUES ('m', 'run', 's', 'b', 1.0, '{}', ?)", (T,))
        resolve_escalation(conn, "run", "e", "edit", value="merge")
        absorbed = {r["absorbed_record_id"] for r in conn.execute("SELECT absorbed_record_id FROM merges")}

    # Both ids are written the usual way, so the one read first (s) survives.
    assert absorbed == {"b", "a"}
    assert _statuses() == {"a": "merged", "b": "merged", "c": "clean", "s": "merged_survivor"}


def test_one_approve_reads_the_control(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    with connect() as conn:
        # The control still on the proposal is approving the proposal.
        assert _answer(conn, "e", "approve", "keep_separate") == ("approve", None)
        # Changed, it is the person's own answer.
        assert _answer(conn, "e", "approve", "merge") == ("edit", "merge")
        # Reject ignores whatever the control held.
        assert _answer(conn, "e", "reject", "merge") == ("reject", None)
        action, value = _answer(conn, "e", "approve", "merge")
        resolve_escalation(conn, "run", "e", action, value=value)

    assert _statuses() == {"a": "merged_survivor", "b": "merged", "c": "clean"}

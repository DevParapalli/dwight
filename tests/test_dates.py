from datetime import date, datetime

from app.agent.decisions import record_decision
from app.agent.orchestrator import _ask_date_questions
from app.agent.policy import load_policy, signature
from app.agent.validate import _clean_date_field, date_question, reset_learned_rules
from app.db import connect, init_db
from app.ingest.readers import load_source_table
from app.schema.loader import load_schema
from app.settings import settings

T = "2026-10-07T00:00:00+00:00"
FILE = "legacy_hris_employees.csv"


def _env(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", tmp_path / "t.db")
    init_db()
    reset_learned_rules()
    return load_schema(settings.schema_path), load_policy()


def test_answered_day_order_reads_the_ambiguous_dates(tmp_path, monkeypatch):
    schema, policy = _env(tmp_path, monkeypatch)
    field = schema.fields["hire_date"]
    assert date_question(field, "03/04/2024", FILE, policy).reason_code == "DATE_FORMAT_AMBIGUOUS"

    with connect() as conn:
        record_decision(conn, "DATE_FORMAT_AMBIGUOUS", signature("DATE_FORMAT_AMBIGUOUS", f"{FILE}:hire_date"),
                        {"action": "approve", "value": "month_first"}, "test")
    reset_learned_rules()

    parsed, issues = _clean_date_field(field, "03/04/2024", FILE, policy)
    assert (parsed, issues) == (date(2024, 3, 4), [])
    # The rule belongs to that export's column, not to every file.
    assert date_question(field, "03/04/2024", "payroll_dump.csv", policy) is not None


def test_unknown_shape_is_asked_with_a_proposed_format(tmp_path, monkeypatch):
    schema, policy = _env(tmp_path, monkeypatch)
    decision = date_question(schema.fields["hire_date"], "2024.03.18", FILE, policy)
    assert decision.reason_code == "DATE_FORMAT_UNKNOWN"
    assert decision.suggested_value == "%Y.%m.%d"

    with connect() as conn:
        record_decision(conn, "DATE_FORMAT_UNKNOWN", signature("DATE_FORMAT_UNKNOWN", decision.scope_key),
                        {"action": "approve", "value": "%Y.%m.%d"}, "test")
    reset_learned_rules()
    assert _clean_date_field(schema.fields["hire_date"], "2024.03.18", FILE, policy) == (date(2024, 3, 18), [])


def test_spreadsheet_datetime_cell_reads_as_its_date(tmp_path, monkeypatch):
    schema, policy = _env(tmp_path, monkeypatch)
    # openpyxl hands date-formatted cells over as naive datetimes.
    cell = datetime(2024, 3, 18)  # noqa: DTZ001
    parsed, issues = _clean_date_field(schema.fields["hire_date"], cell, FILE, policy)
    assert (parsed, issues) == (date(2024, 3, 18), [])


def test_gate_asks_one_question_per_column(tmp_path, monkeypatch):
    schema, policy = _env(tmp_path, monkeypatch)
    path = tmp_path / FILE
    path.write_text("emp_code,doj\nE1,03/04/2024\nE2,05/06/2023\nE3,18-03-2024\n")
    with connect() as conn:
        conn.execute("INSERT INTO runs VALUES ('run', 1, '', 'mapped', ?, ?)", (T, T))
        conn.execute("INSERT INTO source_files VALUES ('f', 'run', ?, 'csv', NULL, ?)", (FILE, T))

    _ask_date_questions("run", load_source_table(path), "f", {"doj": "hire_date"}, schema, policy)

    with connect() as conn:
        rows = conn.execute("SELECT reason_code, entity_id, affected_count FROM escalations").fetchall()
    assert [tuple(r) for r in rows] == [("DATE_FORMAT_AMBIGUOUS", "f", 2)]


def test_gate_scans_only_settled_date_mappings(tmp_path, monkeypatch):
    from app.agent import orchestrator
    from app.agent.mapper import MappingProposal

    schema, policy = _env(tmp_path, monkeypatch)
    path = tmp_path / FILE
    path.write_text("emp_code,doj\nEMP000001,03/04/2024\nEMP000002,05/06/2023\n")
    with connect() as conn:
        conn.execute("INSERT INTO runs VALUES ('run', 1, '', 'uploaded', ?, ?)", (T, T))

    # The id column scores weakly against hire_date; doj is mapped with confidence.
    guesses = {"emp_code": ("hire_date", 0.47), "doj": ("hire_date", 0.95)}
    monkeypatch.setattr(orchestrator, "propose_mapping", lambda profile, _schema: MappingProposal(
        target_field=guesses[profile.name][0], confidence=guesses[profile.name][1], rationale="test",
        alternatives=[], source="llm", model="test", latency_ms=0, cache_hit=False))
    orchestrator.ingest_and_map("run", [path], schema, policy)

    with connect() as conn:
        dates = conn.execute("SELECT reason_code, affected_count FROM escalations "
                             "WHERE reason_code LIKE 'DATE_FORMAT%'").fetchall()
    # One question about doj's day/month order; nothing asked of the id column's "dates".
    assert [tuple(r) for r in dates] == [("DATE_FORMAT_AMBIGUOUS", 2)]

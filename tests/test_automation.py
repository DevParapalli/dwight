import pytest

from app.agent.orchestrator import learned_mapping
from app.db import connect, init_db
from app.llm import client
from app.settings import settings

T = "2026-10-07T00:00:00+00:00"


def test_automation_mode_makes_no_model_call(monkeypatch):
    monkeypatch.setattr(settings, "dwight_mode", "automation")
    with pytest.raises(client.LLMNotConfigured, match="automation"):
        client.complete_json("system", "user")


def _seed_mapping(conn, run_id: str, filename: str, column: str, field: str | None, status: str, at: str):
    conn.execute("INSERT OR IGNORE INTO runs VALUES (?, 1, '', 'mapped', ?, ?)", (run_id, at, at))
    conn.execute("INSERT INTO source_files VALUES (?, ?, ?, 'csv', 1, ?)", (f"f-{run_id}", run_id, filename, at))
    conn.execute("INSERT INTO source_columns (id, source_file_id, name) VALUES (?, ?, ?)",
                 (f"c-{run_id}", f"f-{run_id}", column))
    conn.execute("INSERT INTO mappings (id, run_id, source_column_id, target_field, status, created_at) "
                 "VALUES (?, ?, ?, ?, ?, ?)", (f"m-{run_id}", run_id, f"c-{run_id}", field, status, at))


def test_accepted_mapping_is_reused_for_the_same_export(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", tmp_path / "t.db")
    init_db()
    with connect() as conn:
        _seed_mapping(conn, "run1", "legacy_hris_employees.csv", "emp_no", "employee_id", "accepted", T)
        _seed_mapping(conn, "run2", "payroll_dump.csv", "doj", "date_of_joining", "proposed", T)

    rule = learned_mapping("legacy_hris_employees.csv", "emp_no")
    assert rule is not None
    assert (rule.target_field, rule.source, rule.confidence) == ("employee_id", "rule", 1.0)
    # Same header in another export, or a mapping nobody accepted, is not a rule.
    assert learned_mapping("payroll_dump.csv", "emp_no") is None
    assert learned_mapping("payroll_dump.csv", "doj") is None


def test_automation_mode_reuses_a_value_any_model_learned(tmp_path, monkeypatch):
    from app.agent.normalize_llm import _store_cached, normalize_values

    monkeypatch.setattr(settings, "db_path", tmp_path / "t.db")
    init_db()
    monkeypatch.setattr("app.agent.normalize_llm.active_model", lambda: "hosted-model")
    _store_cached("employment_status", "A", {"value": "active", "confidence": 0.9, "rationale": "flag"})

    # Another model is active now. In agent mode it asks again, under its own name.
    monkeypatch.setattr("app.agent.normalize_llm.active_model", lambda: "local-model")
    monkeypatch.setattr("app.agent.normalize_llm.complete_json_reported",
                        lambda *a, **k: (_ for _ in ()).throw(client.LLMNotConfigured("off")))
    assert normalize_values("employment_status", ["active"], ["A"])["A"]["value"] is None

    # With the model off, the answer any model gave is the rule.
    monkeypatch.setattr(settings, "dwight_mode", "automation")
    results = normalize_values("employment_status", ["active", "on_leave", "terminated"], ["A", "Z"])
    assert results["A"]["value"] == "active"
    assert results["Z"]["value"] is None


def test_model_off_is_not_reported_as_a_failed_call(monkeypatch):
    monkeypatch.setattr(settings, "dwight_mode", "automation")
    reports = []
    with pytest.raises(client.LLMNotConfigured):
        client.complete_json_reported("system", "user", report=lambda kind, message: reports.append(kind))
    assert reports == []
    assert client.model_enabled() is False

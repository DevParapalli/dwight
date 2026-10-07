from app.ingest.readers import iter_raw_rows


def test_csv_with_bom_reads_a_clean_first_header(tmp_path):
    path = tmp_path / "hris.csv"
    path.write_bytes("﻿employee_id,first_name\nE1,Asha\n".encode())
    rows = list(iter_raw_rows(path))
    assert rows[0] == ["employee_id", "first_name"]


def test_semicolon_delimited_csv_splits_on_semicolons(tmp_path):
    path = tmp_path / "payroll.csv"
    path.write_text("employee_id;annual_ctc\nE1;1.234.567,50\n")
    rows = list(iter_raw_rows(path))
    assert rows == [["employee_id", "annual_ctc"], ["E1", "1.234.567,50"]]

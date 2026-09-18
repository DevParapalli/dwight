#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.14"
# ///
"""Score a finished run against the generator's ground-truth manifest.

The point is to state escalation quality as measured numbers rather than
claims. Every injected anomaly has a known expected outcome, and they are not
all the same kind of expectation:

  * some must raise a specific reason code (and are scored with real precision
    and recall against the employee ids the manifest recorded),
  * some must be fixed silently and must NOT raise anything (scored as "handled
    without asking"),
  * some are known gaps -- reported as gaps rather than quietly omitted.

Usage:
    uv run tools/measure_escalations.py
    uv run tools/measure_escalations.py --run <run_id> --json report.json
"""

import argparse
import json
import sqlite3
from pathlib import Path

# What the system is supposed to do with each injected anomaly.
#   escalates: the reason code that should fire, scored by precision/recall
#   silent:    must be corrected without asking anyone
#   gap:       known not to be handled; reported honestly instead of hidden
EXPECTATIONS = {
    "terminated_without_termination_date": ("escalates", "LOGIC_CONTRADICTION"),
    "termination_before_hire": ("escalates", "LOGIC_CONTRADICTION"),
    "cross_source_department_conflict": ("escalates", "CONFLICT_ACROSS_SOURCES"),
    "ambiguous_slash_date": ("escalates_column", "DATE_FORMAT_AMBIGUOUS"),
    "self_referencing_manager": ("escalates", "LOGIC_CONTRADICTION"),
    "name_case_whitespace_damage": ("silent", "names normalised, nothing asked"),
    "mixed_currency": ("silent", "amount and currency parsed, nothing asked"),
    "excel_serial_date": ("silent", "serial parsed to a date"),
    "epoch_timestamp_date": ("silent", "epoch parsed to a date"),
    "empty_date": ("silent", "left null, optional field"),
    "exact_duplicate": ("silent", "merged automatically"),
    "near_duplicate": ("mixed", "merged above 0.90, asked inside the 0.72-0.90 band"),
    "missing_required_field": ("mixed", "blocks the record or is filled from another source"),
    "dangling_manager": ("gap", "R6 needs the full id set; deferred to reconciliation"),
    # Department is free text in the schema, so enum coercion cannot touch a typo
    # in it. It still gets caught, one layer further on: a typo'd value in one
    # source disagrees with the clean value in the others, which is a
    # cross-source conflict on a material field.
    "category_typo": ("escalates", "CONFLICT_ACROSS_SOURCES"),
    # Designation is free text too, but it is not a material field, so a
    # disagreement on it is resolved by source precedence without asking.
    "designation_typo": ("gap", "designation is free text and not material; resolved by precedence"),
}


def _connect(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    return conn


def _latest_run(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT id FROM runs ORDER BY created_at DESC LIMIT 1").fetchone()
    if row is None:
        raise SystemExit("no runs in the database yet")
    return row["id"]


def _escalated_employees(conn: sqlite3.Connection, run_id: str, reason_code: str) -> set[str]:
    """Employee ids behind escalations of one reason code.

    Two sources, unioned. A record-scoped escalation points straight at the
    record it is about. A class-scoped one carries a single example, because its
    whole purpose is that one question covers many rows -- so counting only its
    entity_id would credit one employee out of hundreds and report a recall of
    nearly zero for a code that caught everything. The per-occurrence audit rows
    written alongside those escalations are what make the covered employees
    recoverable.
    """
    rows = conn.execute(
        """SELECT DISTINCT r.natural_key
           FROM escalations e JOIN records r ON r.id = e.entity_id
           WHERE e.run_id = ? AND e.reason_code = ? AND r.natural_key IS NOT NULL""",
        (run_id, reason_code),
    ).fetchall()
    employees = {r["natural_key"] for r in rows}

    audited = conn.execute(
        """SELECT DISTINCT r.natural_key
           FROM audit_events a JOIN records r ON r.id = a.entity_id
           WHERE a.run_id = ? AND a.reason_code = ? AND a.entity_type = 'record'
             AND r.natural_key IS NOT NULL""",
        (run_id, reason_code),
    ).fetchall()
    return employees | {r["natural_key"] for r in audited}


def _score(expected: set[str], detected: set[str]) -> dict:
    hit = expected & detected
    precision = len(hit) / len(detected) if detected else 0.0
    recall = len(hit) / len(expected) if expected else 0.0
    return {
        "ground_truth": len(expected),
        "detected": len(detected),
        "matched": len(hit),
        "precision": round(precision, 4),
        "recall": round(recall, 4),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("data/dwight.db"))
    parser.add_argument("--manifest", type=Path, default=Path("data/samples/manifest.json"))
    parser.add_argument("--run", default=None)
    parser.add_argument("--json", type=Path, default=None, help="also write the report here")
    args = parser.parse_args()

    manifest = json.loads(args.manifest.read_text())
    conn = _connect(args.db)
    run_id = args.run or _latest_run(conn)

    source_rows = sum(f["row_count"] for f in manifest["files"].values())
    open_escalations = conn.execute(
        "SELECT COUNT(*) AS n FROM escalations WHERE run_id = ?", (run_id,)
    ).fetchone()["n"]
    distinct_questions = conn.execute(
        "SELECT COUNT(*) AS n FROM (SELECT 1 FROM escalations WHERE run_id = ? "
        "GROUP BY reason_code, signature)", (run_id,)
    ).fetchone()["n"]

    # Cache the escalated-employee sets once per reason code.
    codes = {code for kind, code in EXPECTATIONS.values() if kind.startswith("escalates")}
    escalated = {code: _escalated_employees(conn, run_id, code) for code in codes}

    # Several anomalies share one reason code (three of them are all
    # LOGIC_CONTRADICTION). Precision has to be scored against the union of
    # everything that legitimately produces that code, or each anomaly counts
    # the others' true positives as its own false positives.
    ground_truth_by_code: dict[str, set[str]] = {}
    for anomaly, entry in manifest["anomalies"].items():
        kind, code = EXPECTATIONS.get(anomaly, ("", ""))
        if kind == "escalates":
            ground_truth_by_code.setdefault(code, set()).update(
                i["employee_id"] for i in entry["instances"]
            )

    report, rows = {}, []
    for anomaly, entry in sorted(manifest["anomalies"].items()):
        kind, detail = EXPECTATIONS.get(anomaly, ("unclassified", ""))
        injected = entry["count"]
        sampled = {i["employee_id"] for i in entry["instances"]}

        if kind == "escalates":
            detected = escalated.get(detail, set())
            # Recall is this anomaly's own; precision belongs to the code, since
            # the code's escalations answer for every anomaly that produces it.
            scored = _score(sampled, detected & sampled if sampled else set())
            shared = _score(ground_truth_by_code.get(detail, set()), detected)
            result = {
                "expected": f"escalate {detail}",
                "ground_truth": len(sampled),
                "matched": len(detected & sampled),
                "recall": scored["recall"],
                "code_precision": shared["precision"],
                "code_ground_truth": shared["ground_truth"],
                "code_detected": shared["detected"],
            }
        elif kind == "escalates_column":
            affected = conn.execute(
                "SELECT COALESCE(SUM(affected_count), 0) AS n FROM escalations "
                "WHERE run_id = ? AND reason_code = ?", (run_id, detail),
            ).fetchone()["n"]
            result = {"expected": f"escalate {detail} (column-scoped)",
                      "ground_truth": injected, "rows_flagged": affected,
                      "recall": round(min(affected, injected) / injected, 4) if injected else 0.0}
        else:
            result = {"expected": detail, "ground_truth": injected, "class": kind}

        report[anomaly] = result
        rows.append((anomaly, kind, injected, result))

    # Split the headline. DUPE_AMBIGUOUS is definitionally one question per
    # record pair (PLAN.md's own table says so), so it cannot compress the way a
    # column- or value-scoped code does. Folding it into one average hides how
    # the codes that *are* class-scoped actually behave.
    pairwise = conn.execute(
        "SELECT COUNT(*) AS n FROM (SELECT 1 FROM escalations WHERE run_id = ? "
        "AND reason_code = 'DUPE_AMBIGUOUS' GROUP BY signature)", (run_id,)
    ).fetchone()["n"]
    classwise = distinct_questions - pairwise

    print(f"run {run_id}")
    print(f"{source_rows} source rows -> {open_escalations} escalation row(s), "
          f"{distinct_questions} distinct question(s)")
    print(f"  {classwise} are class-scoped questions "
          f"({classwise / source_rows * 1000:.2f} per 1000 source rows)")
    print(f"  {pairwise} are identity-pair questions, one per candidate duplicate pair "
          f"-- inherently not compressible")

    print("\nhow much each reason code compresses:")
    print(f"  {'reason code':26} {'rows':>7} {'questions':>10} {'affected':>9}")
    for row in conn.execute(
        """SELECT reason_code, COUNT(*) AS rows_, COUNT(DISTINCT signature) AS questions,
                  COALESCE(SUM(affected_count), 0) AS affected
           FROM escalations WHERE run_id = ? GROUP BY reason_code ORDER BY rows_ DESC""",
        (run_id,),
    ):
        print(f"  {row['reason_code']:26} {row['rows_']:>7} {row['questions']:>10} {row['affected']:>9}")

    print(f"\n{'anomaly':38} {'expectation':16} {'injected':>8} {'result'}")
    for anomaly, kind, injected, result in rows:
        if kind == "escalates":
            detail = (f"R={result['recall']:.2f} ({result['matched']}/{result['ground_truth']}), "
                      f"{result['expected'].split()[-1]} precision="
                      f"{result['code_precision']:.2f}")
        elif kind == "escalates_column":
            detail = f"R={result['recall']:.2f} ({result['rows_flagged']} rows flagged)"
        else:
            detail = result["expected"]
        print(f"{anomaly:38} {kind:16} {injected:>8} {detail}")

    if args.json:
        args.json.write_text(json.dumps(
            {"run_id": run_id, "source_rows": source_rows,
             "escalation_rows": open_escalations, "distinct_questions": distinct_questions,
             "questions_per_1000_rows": round(distinct_questions / source_rows * 1000, 4),
             "anomalies": report}, indent=2))
        print(f"\nwrote {args.json}")


if __name__ == "__main__":
    main()

#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.14"
# dependencies = [
#   "faker>=37",
#   "openpyxl>=3.1",
# ]
# ///
"""Generate three deliberately divergent employee-data exports plus a ground-truth
manifest for the darwinbox-migration-agent demo.

One canonical (clean) employee record is generated per row, seeded for
reproducibility. Each of the three source files then gets its own
structural shape (abbreviated headers, split full name, excel-serial dates,
etc.) plus a set of rare, independently-triggered anomalies. Every anomaly
type fires on a flat `rng.random() < rate` check against the full row
population -- never nested inside another anomaly's condition -- so the
rate actually measured in the manifest cannot silently collapse the way it
did in the earlier ServiceNow generator (a 4% rate gated behind an unrelated
8-way categorical came out near 0.67% in practice).

Employer used in the sample data: Meridian Logistics (meridianlogistics.example).

Usage:
    uv run tools/generate_sources.py --rows 5000
    uv run tools/generate_sources.py --rows 1000000 --out data/samples
"""

import argparse
import csv
import json
import random
from collections import deque
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import openpyxl
from faker import Faker

DEFAULT_SEED = 42
EXCEL_EPOCH = date(1899, 12, 30)
DOMAIN = "meridianlogistics.example"
INSTANCE_SAMPLE_CAP = 500

DEPARTMENTS = [
    "Engineering", "Sales", "Marketing", "Finance", "Human Resources",
    "Operations", "Customer Support", "Legal",
]
DESIGNATIONS = [
    "Software Engineer", "Senior Software Engineer", "Engineering Manager",
    "Data Analyst", "Business Analyst", "Sales Executive", "Account Manager",
    "HR Specialist", "Finance Analyst", "Operations Manager",
    "Customer Support Associate", "Legal Counsel", "Marketing Specialist", "Director",
]
COST_CENTERS = [f"CC-{n}" for n in (100, 200, 300, 400, 500, 600)]

# Rate is a fraction of the stated base population, never of another anomaly's
# eligible subset. "base" documents what that population is so the manifest is
# self-explanatory instead of silently redefining the denominator.
ANOMALY_SPECS = {
    "ambiguous_slash_date":            {"rate": 0.02,  "base": "hris_rows", "field": "doj"},
    "excel_serial_date":               {"rate": 0.015, "base": "hris_rows", "field": "dob"},
    "epoch_timestamp_date":            {"rate": 0.01,  "base": "hris_rows", "field": "term_date"},
    "empty_date":                      {"rate": 0.02,  "base": "hris_rows", "field": "dob"},
    "category_typo":                   {"rate": 0.03,  "base": "hris_rows", "field": "dept_nm"},
    "designation_typo":                {"rate": 0.03,  "base": "hris_rows", "field": "desig"},
    "missing_required_field":          {"rate": 0.02,  "base": "hris_rows", "field": "varies"},
    "self_referencing_manager":        {"rate": 0.005, "base": "hris_rows", "field": "mgr_code"},
    "dangling_manager":                {"rate": 0.01,  "base": "hris_rows", "field": "mgr_code"},
    "terminated_without_termination_date": {"rate": 0.01, "base": "hris_rows", "field": "status_flag+term_date"},
    "termination_before_hire":         {"rate": 0.005, "base": "hris_rows", "field": "term_date"},
    "near_duplicate":                  {"rate": 0.02,  "base": "hris_rows", "field": "row"},
    "exact_duplicate":                 {"rate": 0.01,  "base": "hris_rows", "field": "row"},
    "name_case_whitespace_damage":     {"rate": 0.05,  "base": "hris_rows", "field": "fname/lname"},
    "mixed_currency":                  {"rate": 0.30,  "base": "payroll_rows", "field": "annual_salary"},
    "cross_source_department_conflict": {"rate": 0.05, "base": "payroll_rows", "field": "department"},
}

HRIS_MISSING_CANDIDATES = ["fname", "lname", "doj", "dept_nm", "desig"]
MOBILE_TEMPLATES = ["+91 {d}", "0{d}", "{d}", "+91-{a}-{b}-{c}"]


def indian_grouping(n: int) -> str:
    s = str(n)
    if len(s) <= 3:
        return s
    last3, rest = s[-3:], s[:-3]
    parts = []
    while len(rest) > 2:
        parts.insert(0, rest[-2:])
        rest = rest[:-2]
    if rest:
        parts.insert(0, rest)
    return ",".join(parts) + "," + last3


def typo(word: str, rng: random.Random) -> str:
    if len(word) < 3:
        return word[::-1]
    kind = rng.choice(["drop", "swap", "dup"])
    i = rng.randrange(1, len(word) - 1)
    if kind == "drop":
        return word[:i] + word[i + 1:]
    if kind == "swap":
        return word[:i] + word[i + 1] + word[i] + word[i + 2:]
    return word[:i] + word[i] + word[i:]


def to_excel_serial(d: date) -> int:
    return (d - EXCEL_EPOCH).days


def format_mobile(digits: str, rng: random.Random) -> str:
    template = rng.choice(MOBILE_TEMPLATES)
    return template.format(d=digits, a=digits[:3], b=digits[3:6], c=digits[6:])


def damage_name(value: str, rng: random.Random) -> str:
    kind = rng.choice(["upper", "lower", "pad", "double_space"])
    if kind == "upper":
        return value.upper()
    if kind == "lower":
        return value.lower()
    if kind == "pad":
        return f"  {value}  "
    parts = value.split(" ")
    return "  ".join(parts)


class AnomalyLog:
    """Counts every anomaly instance and keeps a bounded sample for the manifest,
    so a 1,000,000-row run doesn't have to hold a full instance list in memory."""

    def __init__(self):
        self.counts: dict[str, int] = {k: 0 for k in ANOMALY_SPECS}
        self.eligible: dict[str, int | None] = {k: None for k in ANOMALY_SPECS}
        self.samples: dict[str, list[dict]] = {k: [] for k in ANOMALY_SPECS}

    def record(self, kind: str, employee_id: str, true_value) -> None:
        self.counts[kind] += 1
        if len(self.samples[kind]) < INSTANCE_SAMPLE_CAP:
            self.samples[kind].append({"employee_id": employee_id, "true_value": str(true_value)})

    def note_eligible(self, kind: str) -> None:
        """Some anomalies can only apply to a sub-population (e.g. a termination-date
        corruption needs a termination date to exist). Call this once per row that
        meets the precondition, regardless of whether the coin flip then fires, so
        the manifest can show target-rate-vs-eligible-population honestly instead
        of a measured rate that looks silently suppressed."""
        self.eligible[kind] = (self.eligible[kind] or 0) + 1

    def to_manifest(self, bases: dict[str, int]) -> dict:
        out = {}
        for kind, spec in ANOMALY_SPECS.items():
            base_n = bases[spec["base"]]
            count = self.counts[kind]
            eligible_n = self.eligible[kind] if self.eligible[kind] is not None else base_n
            out[kind] = {
                "field": spec["field"],
                "base_population": spec["base"],
                "base_count": base_n,
                "target_rate": spec["rate"],
                "count": count,
                "measured_rate": round(count / base_n, 6) if base_n else 0.0,
                "eligible_count": eligible_n,
                "rate_within_eligible": round(count / eligible_n, 6) if eligible_n else 0.0,
                "instances_sampled": len(self.samples[kind]),
                "instances_truncated": self.counts[kind] > len(self.samples[kind]),
                "instances": self.samples[kind],
            }
        return out


def build_canonical(i: int, rng: random.Random, fake: Faker, all_ids: list[str]) -> dict:
    employee_id = f"EMP{i:06d}"
    first = fake.first_name()
    last = fake.last_name()
    hire_date = fake.date_between(start_date="-8y", end_date="today")
    dob = hire_date - timedelta(days=rng.randint(20, 45) * 365)

    status = rng.choices(["active", "on_leave", "terminated"], weights=[0.82, 0.10, 0.08])[0]
    term_date = None
    if status == "terminated":
        term_date = hire_date + timedelta(days=rng.randint(30, 2000))
        term_date = min(term_date, datetime.now(UTC).date())

    manager_id = None if i < 5 or not all_ids else rng.choice(all_ids)

    return {
        "employee_id": employee_id,
        "first_name": first,
        "last_name": last,
        "work_email": f"{first}.{last}{i}@{DOMAIN}".lower(),
        "phone_digits": f"9{rng.randint(100000000, 999999999)}",
        "date_of_birth": dob,
        "hire_date": hire_date,
        "employment_status": status,
        "termination_date": term_date,
        "employment_type": rng.choices(
            ["full_time", "part_time", "contract", "intern"], weights=[0.75, 0.1, 0.1, 0.05]
        )[0],
        "department": rng.choice(DEPARTMENTS),
        "designation": rng.choice(DESIGNATIONS),
        "manager_employee_id": manager_id,
        "cost_center": rng.choice(COST_CENTERS),
        "annual_ctc": rng.randint(350_000, 3_500_000),
    }


STATUS_FLAG = {"active": "A", "on_leave": "L", "terminated": "T"}


def make_hris_row(rec: dict) -> dict:
    return {
        "emp_code": rec["employee_id"],
        "fname": rec["first_name"],
        "lname": rec["last_name"],
        "dob": rec["date_of_birth"].strftime("%d-%m-%Y"),
        "doj": rec["hire_date"].strftime("%d-%m-%Y"),
        "dept_nm": rec["department"],
        "desig": rec["designation"],
        "mgr_code": rec["manager_employee_id"] or "",
        "ctc_annual": str(rec["annual_ctc"]),
        "status_flag": STATUS_FLAG[rec["employment_status"]],
        "term_date": rec["termination_date"].strftime("%d-%m-%Y") if rec["termination_date"] else "",
    }


def corrupt_hris_row(row: dict, rec: dict, rng: random.Random, log: AnomalyLog, recent_hris: deque) -> list[dict]:
    """Applies the HRIS-scoped rated anomalies to one row. Each check is an
    independent Bernoulli draw against the whole HRIS population -- never
    conditioned on another anomaly having fired. Returns the row(s) to write
    (normally one; two if a duplicate was injected)."""
    touched: set[str] = set()

    def spec(name):
        return ANOMALY_SPECS[name]["rate"]

    if rng.random() < spec("empty_date") and "dob" not in touched:
        log.record("empty_date", row["emp_code"], row["dob"])
        row["dob"] = ""
        touched.add("dob")
    elif rng.random() < spec("excel_serial_date") and "dob" not in touched:
        log.record("excel_serial_date", row["emp_code"], row["dob"])
        row["dob"] = str(to_excel_serial(rec["date_of_birth"]))
        touched.add("dob")

    day, month, year = rec["hire_date"].day, rec["hire_date"].month, rec["hire_date"].year
    if day <= 12:
        log.note_eligible("ambiguous_slash_date")
        if rng.random() < spec("ambiguous_slash_date") and "doj" not in touched:
            log.record("ambiguous_slash_date", row["emp_code"], row["doj"])
            row["doj"] = f"{day:02d}/{month:02d}/{year}"
            touched.add("doj")

    if rec["termination_date"]:
        log.note_eligible("epoch_timestamp_date")
        if rng.random() < spec("epoch_timestamp_date") and "term_date" not in touched:
            log.record("epoch_timestamp_date", row["emp_code"], row["term_date"])
            # Anchored to UTC on purpose. A naive datetime here is interpreted
            # in the generating machine's local zone, so the same seed produced
            # different epoch values in different timezones and the "same" sample
            # data was not actually the same.
            epoch = int(datetime(rec["termination_date"].year, rec["termination_date"].month,
                                 rec["termination_date"].day, tzinfo=UTC).timestamp())
            row["term_date"] = str(epoch)
            touched.add("term_date")

    if rng.random() < spec("category_typo"):
        log.record("category_typo", row["emp_code"], row["dept_nm"])
        row["dept_nm"] = typo(row["dept_nm"], rng)

    if rng.random() < spec("designation_typo"):
        log.record("designation_typo", row["emp_code"], row["desig"])
        row["desig"] = typo(row["desig"], rng)

    if rng.random() < spec("missing_required_field"):
        field_name = rng.choice(HRIS_MISSING_CANDIDATES)
        log.record("missing_required_field", row["emp_code"], f"{field_name}={row[field_name]}")
        row[field_name] = ""

    if rec["manager_employee_id"]:
        log.note_eligible("self_referencing_manager")
    fire_self_ref = bool(rec["manager_employee_id"]) and rng.random() < spec("self_referencing_manager")
    fire_dangling = rng.random() < spec("dangling_manager")
    if fire_self_ref:
        log.record("self_referencing_manager", row["emp_code"], row["mgr_code"])
        row["mgr_code"] = row["emp_code"]
    elif fire_dangling:
        log.record("dangling_manager", row["emp_code"], row["mgr_code"])
        row["mgr_code"] = f"EMP{rng.randint(900000, 999999)}"

    if rec["employment_status"] != "terminated":
        log.note_eligible("terminated_without_termination_date")
        if rng.random() < spec("terminated_without_termination_date"):
            log.record("terminated_without_termination_date", row["emp_code"], row["status_flag"])
            row["status_flag"] = "T"
            row["term_date"] = ""

    if rng.random() < spec("termination_before_hire"):
        log.record("termination_before_hire", row["emp_code"], row["doj"])
        row["status_flag"] = "T"
        before_hire = rec["hire_date"] - timedelta(days=rng.randint(10, 300))
        row["term_date"] = before_hire.strftime("%d-%m-%Y")

    if rng.random() < spec("name_case_whitespace_damage"):
        field_name = rng.choice(["fname", "lname"])
        log.record("name_case_whitespace_damage", row["emp_code"], row[field_name])
        row[field_name] = damage_name(row[field_name], rng)

    rows = [row]

    if recent_hris and rng.random() < spec("exact_duplicate"):
        source = rng.choice(recent_hris)
        log.record("exact_duplicate", source["emp_code"], "duplicate row appended")
        rows.append(dict(source))

    if recent_hris and rng.random() < spec("near_duplicate"):
        source = dict(rng.choice(recent_hris))
        new_id = f"{source['emp_code']}-D{rng.randint(1, 99)}"
        varying_field = rng.choice(["lname", "dept_nm", "ctc_annual"])
        log.record("near_duplicate", source["emp_code"], f"{varying_field}={source[varying_field]}")
        source["emp_code"] = new_id
        if varying_field == "ctc_annual":
            source[varying_field] = str(int(source[varying_field]) + rng.randint(-5000, 5000))
        else:
            source[varying_field] = typo(source[varying_field], rng)
        rows.append(source)

    return rows


def format_currency(amount: int, rng: random.Random, log: AnomalyLog, employee_id: str) -> str:
    if rng.random() < ANOMALY_SPECS["mixed_currency"]["rate"]:
        log.record("mixed_currency", employee_id, f"INR {amount}")
        usd = round(amount / 83)
        return f"USD {usd:,}"
    return f"₹ {indian_grouping(amount)}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=5000)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--out", type=Path, default=Path("data/samples"))
    parser.add_argument("--payroll-coverage", type=float, default=0.92)
    parser.add_argument("--crm-coverage", type=float, default=0.70)
    args = parser.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    rng = random.Random(args.seed)
    fake = Faker()
    fake.seed_instance(args.seed)
    log = AnomalyLog()

    all_ids: list[str] = []
    recent_hris: deque = deque(maxlen=200)

    hris_path = args.out / "legacy_hris_employees.csv"
    payroll_path = args.out / "payroll_dump.csv"
    crm_path = args.out / "crm_contacts_export.xlsx"

    hris_fields = ["emp_code", "fname", "lname", "dob", "doj", "dept_nm", "desig",
                   "mgr_code", "ctc_annual", "status_flag", "term_date"]
    payroll_fields = ["id", "email", "annual_salary", "cost_centre", "type", "department"]
    type_map = {"full_time": "FT", "part_time": "PT", "contract": "CON", "intern": "INT"}

    hris_row_count = 0
    payroll_row_count = 0
    crm_row_count = 0

    with hris_path.open("w", newline="") as hf, payroll_path.open("w", newline="") as pf:
        hris_writer = csv.DictWriter(hf, fieldnames=hris_fields)
        hris_writer.writeheader()
        payroll_writer = csv.DictWriter(pf, fieldnames=payroll_fields)
        payroll_writer.writeheader()

        wb = openpyxl.Workbook(write_only=True)
        ws = wb.create_sheet("Contacts")
        ws.append(["Meridian Logistics -- CRM Contact Export (Confidential)"])
        ws.append([f"Generated {datetime.now(UTC):%Y-%m-%d %H:%M} UTC"])
        ws.append(["Full Name", "Email", "Mobile", "Date of Joining", "Department", "Designation"])

        for i in range(args.rows):
            rec = build_canonical(i, rng, fake, all_ids)
            all_ids.append(rec["employee_id"])

            hris_row = make_hris_row(rec)
            for out_row in corrupt_hris_row(hris_row, rec, rng, log, recent_hris):
                hris_writer.writerow(out_row)
                hris_row_count += 1
            recent_hris.append(hris_row)

            if rng.random() < args.payroll_coverage:
                department = rec["department"]
                if rng.random() < ANOMALY_SPECS["cross_source_department_conflict"]["rate"]:
                    log.record("cross_source_department_conflict", rec["employee_id"], department)
                    alt = rng.choice([d for d in DEPARTMENTS if d != department])
                    department = alt
                payroll_writer.writerow({
                    "id": rec["employee_id"],
                    "email": rec["work_email"],
                    "annual_salary": format_currency(rec["annual_ctc"], rng, log, rec["employee_id"]),
                    "cost_centre": rec["cost_center"],
                    "type": type_map[rec["employment_type"]],
                    "department": department,
                })
                payroll_row_count += 1

            if rng.random() < args.crm_coverage:
                ws.append([
                    f"{rec['first_name']} {rec['last_name']}",
                    rec["work_email"],
                    format_mobile(rec["phone_digits"], rng),
                    to_excel_serial(rec["hire_date"]),
                    rec["department"],
                    rec["designation"],
                ])
                crm_row_count += 1

        wb.save(crm_path)

    bases = {"hris_rows": hris_row_count, "payroll_rows": payroll_row_count}
    manifest = {
        "seed": args.seed,
        "rows_requested": args.rows,
        "generated_at": datetime.now(UTC).isoformat(),
        "employer": {"name": "Meridian Logistics", "domain": DOMAIN},
        "files": {
            "legacy_hris_employees.csv": {"row_count": hris_row_count},
            "crm_contacts_export.xlsx": {"row_count": crm_row_count, "junk_header_rows": 2},
            "payroll_dump.csv": {"row_count": payroll_row_count},
        },
        "anomalies": log.to_manifest(bases),
    }
    (args.out / "manifest.json").write_text(json.dumps(manifest, indent=2))

    print(f"wrote {hris_row_count} HRIS rows, {payroll_row_count} payroll rows, "
          f"{crm_row_count} CRM rows to {args.out}")


if __name__ == "__main__":
    main()

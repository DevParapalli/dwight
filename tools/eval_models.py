#!/usr/bin/env -S uv run python
"""Score each candidate model on the four calls this system actually makes.

Model choice was being made on vibes. This measures it, on the real prompts --
`app.llm.prompts` is imported rather than re-stated, so a prompt change is
reflected here without anyone remembering to copy it across.

Ground truth comes from the generator's manifest and the vocabularies it encodes,
not from cases invented for the eval:

  duplicate judge   near-duplicate rows carry `<emp_code>-D<n>` and vary exactly
                    one field, so the pair is known-same. Negatives are drawn
                    from employees who share a surname -- a negative that is
                    obviously negative measures nothing, and the real call only
                    fires on the 0.72-0.90 similarity band where it is hard.
  column mapping    every source header maps to one known target field.
                    "Full Name" is deliberately included and scored as correct
                    for either name half: the schema has no such field and the
                    honest answers disagree.
  normalization     the per-source enum vocabularies (A/L/T, FT/PT/CON/INT) are
                    the generator's own, so the expected output is exact.
  push repair       the target's rejection messages, which are the one input
                    with no manifest entry. Scored on whether the right field is
                    identified and whether the value is valid for it -- not on
                    matching one blessed answer, because more than one correction
                    is defensible.

Each model is pinned. This deliberately does NOT go through
`app.llm.client.complete_json`: that falls down the chain on a 429, which is
right in production and ruinous in an eval -- model A's rate limit would be
silently answered by model B and scored as A. A failure here is recorded as a
failure.

The cache is never read. A cached answer hides run-to-run variance, and variance
is the thing worth measuring: at temperature 0.2 a model that answers correctly
nine times in ten looks perfect from cache and still breaks a run. `--repeat`
measures it directly.

Usage:
    uv run python tools/eval_models.py --tasks dupe --samples 20
    uv run python tools/eval_models.py --models openai/gpt-oss-20b --repeat 3
    uv run python tools/eval_models.py --models local:qwen3-8b --json report.json
"""

import argparse
import json
import random
import sqlite3
import statistics
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from pathlib import Path

# The project is not an installed package (`package = false`), and running a file
# in tools/ puts tools/ on sys.path rather than the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent.dupe_llm import _comparable
from app.ingest.profile import ColumnProfile
from app.llm.prompts import (
    build_duplicate_judgement_prompt,
    build_mapping_prompt,
    build_push_repair_prompt,
    build_value_normalization_prompt,
)
from app.schema.loader import load_schema
from app.settings import settings

# Source header -> target field, as tools/generate_sources.py writes them.
# Kept beside the generator's own shape rather than inferred from a finished run:
# a run's mappings are the very thing under test.
MAPPING_TRUTH = {
    "legacy_hris_employees.csv": {
        "emp_code": "employee_id", "fname": "first_name", "lname": "last_name",
        "dob": "date_of_birth", "doj": "hire_date", "dept_nm": "department",
        "desig": "designation", "mgr_code": "manager_employee_id",
        "ctc_annual": "annual_ctc", "status_flag": "employment_status",
        "term_date": "termination_date",
    },
    "payroll_dump.csv": {
        "id": "employee_id", "email": "work_email", "annual_salary": "annual_ctc",
        "cost_centre": "cost_center", "type": "employment_type", "department": "department",
    },
    "crm_contacts_export.xlsx": {
        "Email": "work_email", "Mobile": "phone", "Date of Joining": "hire_date",
        "Department": "department", "Designation": "designation",
    },
}
# One header has no single right answer -- the schema splits the name in two.
# Scored as correct for either half rather than being quietly dropped.
MAPPING_EITHER = {"Full Name": {"first_name", "last_name"}}

# STATUS_FLAG and type_map in tools/generate_sources.py.
NORMALIZATION_TRUTH = [
    ("employment_status", ["active", "on_leave", "terminated"],
     {"A": "active", "L": "on_leave", "T": "terminated"}),
    ("employment_type", ["full_time", "part_time", "contract", "intern"],
     {"FT": "full_time", "PT": "part_time", "CON": "contract", "INT": "intern"}),
]

# What a real target refuses, and which field the answer has to be about. The
# value is not pinned: "make the termination date not precede the hire date" has
# many defensible answers and exactly one defensible field.
REPAIR_CASES = [
    ("termination_date must not be earlier than hire_date", "termination_date",
     {"employee_id": "EMP000015", "first_name": "Asha", "last_name": "Nair",
      "work_email": "asha.nair15@meridianlogistics.example", "hire_date": "2020-12-08",
      "employment_status": "terminated", "termination_date": "2020-02-27",
      "employment_type": "full_time", "department": "Sales", "designation": "Executive"}),
    ("employment_status must be one of: active, on_leave, terminated", "employment_status",
     {"employee_id": "EMP000221", "first_name": "Rohit", "last_name": "Menon",
      "work_email": "rohit.menon221@meridianlogistics.example", "hire_date": "2019-04-02",
      "employment_status": "Resigned", "employment_type": "full_time",
      "department": "Engineering", "designation": "Engineer"}),
    ("manager_employee_id must not equal employee_id", "manager_employee_id",
     {"employee_id": "EMP000330", "first_name": "Kavya", "last_name": "Rao",
      "work_email": "kavya.rao330@meridianlogistics.example", "hire_date": "2022-01-17",
      "employment_status": "active", "manager_employee_id": "EMP000330",
      "employment_type": "contract", "department": "Support", "designation": "Analyst"}),
    ("work_email is not a valid email address", "work_email",
     {"employee_id": "EMP000401", "first_name": "Imran", "last_name": "Sheikh",
      "work_email": "imran.sheikh401(at)meridianlogistics.example", "hire_date": "2018-06-30",
      "employment_status": "active", "employment_type": "part_time",
      "department": "Marketing", "designation": "Specialist"}),
    ("annual_ctc must be a positive number", "annual_ctc",
     {"employee_id": "EMP000512", "first_name": "Neha", "last_name": "Gupta",
      "work_email": "neha.gupta512@meridianlogistics.example", "hire_date": "2021-09-13",
      "employment_status": "active", "annual_ctc": "-45000",
      "employment_type": "full_time", "department": "Finance", "designation": "Manager"}),
]


class CallFailed(Exception):
    """The model did not return usable JSON. Recorded, never retried away: a
    model that fails one call in twenty is a finding, not a blip to smooth over."""


def _call_groq(model: str, system_prompt: str, user_prompt: str, max_tokens: int) -> tuple[dict, int, int]:
    """Returns (parsed, latency_ms, completion_tokens). No retry, no fallback."""
    from groq import Groq

    client = Groq(api_key=settings.groq_env_key, timeout=120.0)
    started = time.monotonic()
    try:
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": system_prompt},
                      {"role": "user", "content": user_prompt}],
            temperature=0.2, max_tokens=max_tokens,
            response_format={"type": "json_object"},
        )
    except Exception as exc:
        raise CallFailed(f"{type(exc).__name__}: {str(exc)[:160]}") from exc
    latency_ms = int((time.monotonic() - started) * 1000)
    content = response.choices[0].message.content or ""
    try:
        return json.loads(content), latency_ms, response.usage.completion_tokens
    except json.JSONDecodeError as exc:
        raise CallFailed(f"not JSON: {content[:120]!r}") from exc


def _call_local(model: str, system_prompt: str, user_prompt: str, max_tokens: int) -> tuple[dict, int, int]:
    """llama.cpp's OpenAI-compatible endpoint, so a local candidate is scored on
    exactly the same prompts and metrics as a hosted one."""
    body = {
        "model": model,
        "messages": [{"role": "system", "content": system_prompt},
                     {"role": "user", "content": user_prompt}],
        "temperature": 0.2, "max_tokens": max(max_tokens, settings.local_max_output_tokens),
        "response_format": {"type": "json_object"},
    }
    if settings.local_disable_thinking:
        body["chat_template_kwargs"] = {"enable_thinking": False}
    req = urllib.request.Request(
        f"{settings.local_fallback_url}/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=180.0) as resp:
            payload = json.loads(resp.read())
    except (urllib.error.URLError, TimeoutError) as exc:
        raise CallFailed(f"{settings.local_fallback_url} unreachable: {exc}") from exc
    latency_ms = int((time.monotonic() - started) * 1000)
    choice = payload["choices"][0]
    content = choice["message"].get("content") or ""
    if not content.strip():
        raise CallFailed(f"empty answer (finish_reason={choice.get('finish_reason')!r}); "
                         "it spent the budget thinking")
    try:
        return json.loads(content), latency_ms, payload.get("usage", {}).get("completion_tokens", 0)
    except json.JSONDecodeError as exc:
        raise CallFailed(f"not JSON: {content[:120]!r}") from exc


# Pacing, not retrying. A hosted account's tokens-per-minute cap is a property of
# the account, not of the model under test, so letting it 429 measures the wrong
# thing -- but so would retrying, which is why the call itself still does not.
PACE_SECONDS = 0.0
_last_call_ended_at = 0.0


def call(model: str, system_prompt: str, user_prompt: str, max_tokens: int) -> tuple[dict, int, int]:
    global _last_call_ended_at
    if model.startswith("local:"):
        return _call_local(model.split(":", 1)[1], system_prompt, user_prompt, max_tokens)
    wait = PACE_SECONDS - (time.monotonic() - _last_call_ended_at)
    if wait > 0:
        time.sleep(wait)
    try:
        return _call_groq(model, system_prompt, user_prompt, max_tokens)
    finally:
        _last_call_ended_at = time.monotonic()


def _records(conn: sqlite3.Connection, run_id: str) -> dict[str, dict]:
    """Cleaned records by natural key. The judge sees records after cleaning, so
    scoring it on raw source rows would measure a different input."""
    out = {}
    for row in conn.execute(
        "SELECT natural_key, data FROM records WHERE run_id = ? AND natural_key IS NOT NULL "
        "AND TRIM(natural_key) != ''", (run_id,)
    ):
        out.setdefault(row["natural_key"], json.loads(row["data"]))
    return out


def dupe_cases(conn: sqlite3.Connection, run_id: str, manifest: dict, n: int,
               rng: random.Random) -> list[tuple]:
    """Half known-same (a row and its injected near-duplicate), half known-different
    (two employees sharing a surname, so the negative is not a giveaway)."""
    records = _records(conn, run_id)
    cases = []

    bases = [i["employee_id"] for i in manifest["anomalies"]["near_duplicate"]["instances"]]
    rng.shuffle(bases)
    for base in bases:
        twin = next((k for k in records if k.startswith(f"{base}-D")), None)
        if twin and base in records:
            cases.append((f"{base}~{twin}", records[base], records[twin], "same"))
        if len(cases) >= n // 2:
            break

    by_surname = defaultdict(list)
    for key, data in records.items():
        if "-D" not in key and data.get("last_name"):
            by_surname[data["last_name"].strip().lower()].append(key)
    pools = [v for v in by_surname.values() if len(v) >= 2]
    rng.shuffle(pools)
    for pool in pools:
        a, b = rng.sample(pool, 2)
        cases.append((f"{a}|{b}", records[a], records[b], "different"))
        if len(cases) >= n:
            break
    return cases[:n]


def score_dupe(model: str, cases: list[tuple], repeat: int) -> dict:
    hits = misses = unsure = 0
    latencies, tokens, failures = [], [], []
    for label, left, right, expected in cases:
        for _ in range(repeat):
            sp, up = build_duplicate_judgement_prompt(_comparable(left), _comparable(right))
            try:
                result, ms, tk = call(model, sp, up, settings.llm_max_output_tokens)
            except CallFailed as exc:
                failures.append(f"{label}: {exc}")
                continue
            latencies.append(ms)
            tokens.append(tk)
            verdict = str(result.get("verdict", "")).lower()
            if verdict == "unsure":
                unsure += 1
            elif verdict == expected:
                hits += 1
            else:
                misses += 1
                failures.append(f"{label}: said {verdict!r}, expected {expected!r}")
    return _summary(hits, misses, unsure, latencies, tokens, failures)


def score_mapping(model: str, conn: sqlite3.Connection, run_id: str, schema, repeat: int) -> dict:
    hits = misses = 0
    latencies, tokens, failures = [], [], []
    columns = conn.execute(
        """SELECT sf.filename, sc.name, sc.inferred_type, sc.null_rate, sc.distinct_count,
                  sc.sample_values
           FROM source_columns sc JOIN source_files sf ON sf.id = sc.source_file_id
           WHERE sf.run_id = ?""", (run_id,)
    ).fetchall()
    for col in columns:
        truth = MAPPING_TRUTH.get(col["filename"], {}).get(col["name"])
        allowed = MAPPING_EITHER.get(col["name"])
        if truth is None and allowed is None:
            continue
        profile = ColumnProfile(
            name=col["name"], inferred_type=col["inferred_type"], null_rate=col["null_rate"],
            distinct_count=col["distinct_count"], distinct_capped=False,
            sample_values=json.loads(col["sample_values"]) if col["sample_values"] else [],
        )
        for _ in range(repeat):
            sp, up = build_mapping_prompt(profile, schema)
            try:
                result, ms, tk = call(model, sp, up, settings.llm_max_output_tokens)
            except CallFailed as exc:
                failures.append(f"{col['filename']}:{col['name']}: {exc}")
                continue
            latencies.append(ms)
            tokens.append(tk)
            candidates = result.get("candidates") or []
            got = candidates[0].get("field") if candidates else None
            if got == truth or (allowed and got in allowed):
                hits += 1
            else:
                misses += 1
                failures.append(f"{col['filename']}:{col['name']}: said {got!r}, "
                                f"expected {truth or sorted(allowed)!r}")
    return _summary(hits, misses, 0, latencies, tokens, failures)


def score_normalization(model: str, repeat: int) -> dict:
    hits = misses = 0
    latencies, tokens, failures = [], [], []
    for field_name, allowed_values, truth in NORMALIZATION_TRUTH:
        batch = list(truth)
        for _ in range(repeat):
            sp, up = build_value_normalization_prompt(field_name, allowed_values, batch)
            try:
                result, ms, tk = call(model, sp, up, settings.llm_max_output_tokens)
            except CallFailed as exc:
                failures.append(f"{field_name}: {exc}")
                continue
            latencies.append(ms)
            tokens.append(tk)
            # Read exactly the way app/agent/normalize_llm.py reads it: a
            # `results` list whose entries carry the `raw` they answer for. A
            # model that returns a different shape fails here for the same
            # reason it would fail in the pipeline.
            by_raw = {r.get("raw"): r for r in (result.get("results") or [])}
            for raw, expected in truth.items():
                got = (by_raw.get(raw) or {}).get("value")
                if got == expected:
                    hits += 1
                else:
                    misses += 1
                    failures.append(f"{field_name} {raw!r}: said {got!r}, expected {expected!r}")
    return _summary(hits, misses, 0, latencies, tokens, failures)


def score_repair(model: str, schema, repeat: int) -> dict:
    """Scored on the field, not the value: several corrections are defensible and
    only one field is.

    `accepted` is the more interesting number and comes from repair._validate --
    the project's own policy gate -- rather than a second opinion written here.
    A proposal that names the right field and still gets refused is the case
    worth seeing, and only the real gate can tell you that.
    """
    from app.agent.repair import _validate

    accepted = 0
    hits = misses = 0
    latencies, tokens, failures = [], [], []
    for target_error, expected_field, record in REPAIR_CASES:
        for _ in range(repeat):
            sp, up = build_push_repair_prompt(target_error, record, schema)
            try:
                result, ms, tk = call(model, sp, up, settings.llm_max_output_tokens)
            except CallFailed as exc:
                failures.append(f"{expected_field}: {exc}")
                continue
            latencies.append(ms)
            tokens.append(tk)
            got = result.get("field")
            if got == expected_field:
                hits += 1
            else:
                misses += 1
                failures.append(f"{target_error[:40]}: said field {got!r}, "
                                f"expected {expected_field!r}")
            proposal, why_not = _validate(result, target_error, record, schema)
            if proposal:
                accepted += 1
            else:
                failures.append(f"{target_error[:40]}: policy refused it -- {why_not}")
    summary = _summary(hits, misses, 0, latencies, tokens, failures)
    summary["accepted_by_policy"] = accepted
    return summary


def _summary(hits: int, misses: int, unsure: int, latencies: list[int],
             tokens: list[int], failures: list[str]) -> dict:
    answered = hits + misses + unsure
    return {
        "correct": hits, "wrong": misses, "unsure": unsure,
        "accuracy": round(hits / answered, 3) if answered else None,
        "call_failures": len(failures) - misses if len(failures) > misses else 0,
        "median_latency_ms": int(statistics.median(latencies)) if latencies else None,
        "max_latency_ms": max(latencies) if latencies else None,
        "median_tokens": int(statistics.median(tokens)) if tokens else None,
        # The tail is the number that matters for a ceiling: the call that broke
        # the run was the long one, not the typical one.
        "max_tokens_used": max(tokens) if tokens else None,
        "notes": failures[:12],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", type=Path, default=settings.db_path)
    parser.add_argument("--manifest", type=Path, default=Path("data/samples/manifest.json"))
    parser.add_argument("--run", help="run id; default is the most recent")
    parser.add_argument("--models", help="comma-separated; default is the configured chain. "
                                         "Prefix a local one with 'local:'")
    parser.add_argument("--tasks", default="dupe,mapping,normalize,repair")
    parser.add_argument("--samples", type=int, default=20, help="duplicate-judge pairs")
    parser.add_argument("--repeat", type=int, default=1,
                        help="calls per case; >1 measures run-to-run variance")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--sleep", type=float, default=0.0, metavar="SECONDS",
                        help="minimum gap between hosted calls, to stay under an "
                             "account's tokens-per-minute cap. Local calls are unpaced.")
    parser.add_argument("--json", type=Path, help="write the full report here")
    args = parser.parse_args()

    if not args.db.exists():
        print(f"no database at {args.db} -- run an import first", file=sys.stderr)
        return 2
    if not args.manifest.exists():
        print(f"no manifest at {args.manifest} -- run `just samples` first", file=sys.stderr)
        return 2

    if args.models:
        models = [m.strip() for m in args.models.split(",") if m.strip()]
    else:
        from app.llm.client import groq_chain
        models = groq_chain()
    if not models:
        print("no models to evaluate: set GROQ_ENV_KEY or pass --models local:<name>",
              file=sys.stderr)
        return 2

    global PACE_SECONDS
    PACE_SECONDS = args.sleep

    tasks = [t.strip() for t in args.tasks.split(",") if t.strip()]
    manifest = json.loads(args.manifest.read_text())
    schema = load_schema(settings.schema_path)
    rng = random.Random(args.seed)

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    run_id = args.run or conn.execute(
        "SELECT id FROM runs ORDER BY created_at DESC LIMIT 1").fetchone()["id"]

    pairs = dupe_cases(conn, run_id, manifest, args.samples, rng) if "dupe" in tasks else []
    if "dupe" in tasks and not pairs:
        print("no duplicate pairs found in this run -- is it reconciled?", file=sys.stderr)

    report = {"run_id": run_id, "repeat": args.repeat, "seed": args.seed, "models": {}}
    for model in models:
        print(f"\n=== {model} ===", flush=True)
        results = {}
        if "dupe" in tasks and pairs:
            results["dupe"] = score_dupe(model, pairs, args.repeat)
        if "mapping" in tasks:
            results["mapping"] = score_mapping(model, conn, run_id, schema, args.repeat)
        if "normalize" in tasks:
            results["normalize"] = score_normalization(model, args.repeat)
        if "repair" in tasks:
            results["repair"] = score_repair(model, schema, args.repeat)
        report["models"][model] = results

        for task, r in results.items():
            accuracy = "n/a" if r["accuracy"] is None else f"{r['accuracy']:.0%}"
            print(f"  {task:<10} accuracy={accuracy:>5}  "
                  f"correct={r['correct']:<4} wrong={r['wrong']:<4} unsure={r['unsure']:<4} "
                  f"failed={r['call_failures']:<3} "
                  f"median={r['median_latency_ms'] or '-'}ms  "
                  f"tokens med/max={r['median_tokens'] or '-'}/{r['max_tokens_used'] or '-'}")
            for note in r["notes"][:3]:
                print(f"      {note}")

    conn.close()
    if args.json:
        args.json.write_text(json.dumps(report, indent=2))
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

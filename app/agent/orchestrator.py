import json
from pathlib import Path

from app.agent.clean import clean_string, coerce_enum
from app.agent.escalate import create_or_merge_escalation
from app.agent.mapper import propose_mapping
from app.agent.normalize_llm import normalize_values
from app.agent.policy import decide_mapping
from app.agent.validate import validate_and_clean_record
from app.audit import record_change, utcnow
from app.db import connect, new_id
from app.ingest.profile import profile_table
from app.ingest.readers import load_source_table
from app.progress import Ticker, emit
from app.schema.loader import Schema


def ingest_and_map(run_id: str, source_paths: list[Path], schema: Schema, policy: dict) -> None:
    """Column-level work only: read each file, profile its columns, propose a
    mapping for each, and decide (per policy) whether it's confident enough to
    auto-accept or needs to be flagged. Runs synchronously -- with a few dozen
    columns per run this is fast regardless of row count, unlike the row-level
    cleaning/validation stages, which is where async backgrounding earns its keep."""
    for path in source_paths:
        table = load_source_table(path)
        emit(run_id, "progress", f"Profiling {table.filename}", stage="profiled",
             what="profile", file=table.filename)
        profiles = profile_table(table)
        emit(run_id, "progress",
             f"{table.filename}: {len(profiles)} columns, header on row {table.header_row_index + 1}",
             stage="profiled", file=table.filename, columns=len(profiles))

        with connect() as conn:
            file_id = new_id()
            conn.execute(
                "INSERT INTO source_files (id, run_id, filename, source_type, row_count, created_at) "
                "VALUES (?, ?, ?, ?, NULL, ?)",
                (file_id, run_id, table.filename, table.source_type, utcnow()),
            )

        for index, profile in enumerate(profiles, start=1):
            proposal = propose_mapping(profile, schema)
            emit(run_id, "llm",
                 (f"{profile.name!r} -> "
                  + (f"{proposal.target_field} at {proposal.confidence:.0%} confidence"
                     if proposal.target_field else "no target field")
                  + (" (from cache)" if proposal.cache_hit
                     else f" ({proposal.latency_ms / 1000:.1f}s)" if proposal.source == "llm"
                     else " (name similarity, no model configured)")),
                 stage="mapped", what="column mapping", model=proposal.model,
                 column=profile.name, field=proposal.target_field,
                 confidence=proposal.confidence, cached=proposal.cache_hit,
                 rationale=proposal.rationale)
            decision = decide_mapping(
                profile.name, table.filename, proposal.target_field, proposal.confidence,
                proposal.alternatives, profile.null_rate, policy,
            )
            status = "proposed" if decision.reason_code else "accepted"

            with connect() as conn:
                column_id = new_id()
                conn.execute(
                    """INSERT INTO source_columns
                       (id, source_file_id, name, inferred_type, null_rate, distinct_count, sample_values)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (column_id, file_id, profile.name, profile.inferred_type, profile.null_rate,
                     profile.distinct_count, json.dumps(profile.sample_values)),
                )
                mapping_id = new_id()
                conn.execute(
                    """INSERT INTO mappings
                       (id, run_id, source_column_id, target_field, confidence, rationale,
                        alternatives, status, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (mapping_id, run_id, column_id, proposal.target_field, proposal.confidence,
                     proposal.rationale, json.dumps(proposal.alternatives), status, utcnow()),
                )
                record_change(
                    conn, run_id=run_id, actor="llm" if proposal.source == "llm" else "agent",
                    stage="mapped", reason_code=decision.reason_code,
                    entity_type="mapping", entity_id=mapping_id,
                    field=profile.name, after=proposal.target_field, confidence=proposal.confidence,
                    model=proposal.model, latency_ms=proposal.latency_ms, note=proposal.rationale,
                )
                if decision.reason_code:
                    create_or_merge_escalation(conn, run_id, decision, entity_id=mapping_id)

            emit(
                run_id,
                "escalation" if decision.reason_code else "progress",
                (f"{profile.name} -> {proposal.target_field or 'nothing'}"
                 + (f" ({decision.reason_code}, needs you)" if decision.reason_code
                    else f" ({proposal.confidence:.0%} confident)")),
                stage="mapped", what="map", file=table.filename, column=profile.name,
                target=proposal.target_field, confidence=proposal.confidence,
                done=index, total=len(profiles), reason_code=decision.reason_code,
                cached=proposal.cache_hit, model=proposal.model,
            )

    with connect() as conn:
        conn.execute("UPDATE runs SET stage = 'mapped', updated_at = ? WHERE id = ?", (utcnow(), run_id))
    emit(run_id, "stage", "Mapping done", stage="mapped")


def clean_and_validate(run_id: str, schema: Schema, policy: dict) -> None:
    """Row-level work: for every mapped column with a target field (regardless of
    whether a human has explicitly clicked accept -- see app/ui/routes.py), clean
    and validate each source row independently. Each source row becomes one
    `records` row; reconciling same-employee rows across files is M5's job."""
    with connect() as conn:
        run = conn.execute("SELECT stage FROM runs WHERE id = ?", (run_id,)).fetchone()
    if run and run["stage"] not in ("uploaded", "profiled", "mapped"):
        # Already past this stage -- re-running would duplicate every record.
        # Reprocessing a *changed* source is M5's delta-reimport, not this guard.
        return

    with connect() as conn:
        mapping_rows = conn.execute(
            """SELECT sc.name AS column_name, sf.filename, sf.id AS source_file_id, m.target_field
               FROM mappings m
               JOIN source_columns sc ON sc.id = m.source_column_id
               JOIN source_files sf ON sf.id = sc.source_file_id
               WHERE m.run_id = ? AND m.target_field IS NOT NULL""",
            (run_id,),
        ).fetchall()

    files: dict[str, dict] = {}
    for r in mapping_rows:
        info = files.setdefault(r["source_file_id"], {"filename": r["filename"], "column_map": {}})
        info["column_map"][r["column_name"]] = r["target_field"]

    max_edit_distance = policy["values"]["enum_auto_max_edit_distance"]

    for source_file_id, info in files.items():
        filename = info["filename"]
        column_map = info["column_map"]
        path = Path("data/runs") / run_id / filename
        table = load_source_table(path)

        # Pass 1: collect distinct enum raw values deterministic coercion can't
        # resolve, across the whole file, before any LLM call -- batching needs
        # the full unresolved set up front, not per-row.
        enum_fields = {tf for tf in column_map.values() if schema.fields[tf].type == "enum"}
        unresolved: dict[str, set[str]] = {f: set() for f in enum_fields}
        for row in table.rows():
            for source_col, target_field in column_map.items():
                if target_field not in enum_fields:
                    continue
                s = clean_string(row.get(source_col))
                if s is None:
                    continue
                coerced, _, _ = coerce_enum(s, schema.fields[target_field].values, max_edit_distance)
                if coerced is None:
                    unresolved[target_field].add(s)

        for field_name, values in unresolved.items():
            if values:
                emit(run_id, "progress",
                     f"{field_name}: asking the model about {len(values)} unrecognised value(s)",
                     stage="cleaned", what="normalise", file=filename,
                     field=field_name, distinct=len(values))
        normalization_maps = {
            field_name: normalize_values(field_name, schema.fields[field_name].values,
                                         sorted(values), run_id=run_id)
            for field_name, values in unresolved.items() if values
        }

        # Pass 2: clean, validate, and persist each row using the now-resolved
        # normalization map. One connection for the whole file's rows, not one
        # per row -- per-row connections make SQLite's WAL commit overhead the
        # dominant cost at scale (this was the ~29x-slower path found in M5's
        # reconcile_run before the same fix was applied there). Pass 1 already
        # finished any LLM calls, so nothing slow happens while this is open.
        ticker = Ticker(run_id, "cleaned", f"Cleaning {filename}")
        with connect() as conn:
            for row in table.rows():
                ticker.tick(conn=conn)
                raw_record = {target: row.get(source_col) for source_col, target in column_map.items()}
                result = validate_and_clean_record(raw_record, schema, filename, policy, normalization_maps)

                record_id = new_id()
                natural_key = result.cleaned.get(schema.natural_key)
                status = "blocked" if result.issues else "clean"
                blocked_on = result.issues[0].decision.reason_code if result.issues else None

                conn.execute(
                    """INSERT INTO records
                       (id, run_id, source_file_id, natural_key, data, status, blocked_on, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (record_id, run_id, source_file_id, natural_key, json.dumps(result.cleaned, default=str),
                     status, blocked_on, utcnow(), utcnow()),
                )
                for issue in result.issues:
                    create_or_merge_escalation(conn, run_id, issue.decision, entity_id=record_id)
                record_change(
                    conn, run_id=run_id, actor="agent", stage="validated", entity_type="record",
                    entity_id=record_id, after=status,
                    note=f"{len(result.issues)} issue(s)" if result.issues else "clean",
                )

        ticker.emit_now()

    with connect() as conn:
        conn.execute("UPDATE runs SET stage = 'validated', updated_at = ? WHERE id = ?", (utcnow(), run_id))
    emit(run_id, "stage", "Every row cleaned and validated", stage="validated")

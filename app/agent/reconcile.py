import json
from collections import defaultdict

from app.agent.dedupe import blocking_key, similarity_score
from app.agent.escalate import create_or_merge_escalation
from app.agent.policy import (
    Issue,
    decide_cross_source_conflict,
    decide_dupe_ambiguous,
    dupe_scope_key,
    signature,
)
from app.audit import record_change, utcnow
from app.db import connect, new_id
from app.progress import emit
from app.schema.loader import Schema


class _UnionFind:
    def __init__(self, ids):
        self.parent = {i: i for i in ids}

    def find(self, x):
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb


def _source_rank(filename: str, policy: dict) -> int:
    """Lower rank wins on a conflict. Names in policy.cross_source.precedence
    are short forms (legacy_hris, payroll, crm); matched by filename prefix."""
    for i, name in enumerate(policy["cross_source"]["precedence"]):
        if filename.lower().startswith(name.lower()):
            return i
    return 999


def _identity(record: dict) -> str:
    """Something about a record that survives a re-import, for keying a human's
    duplicate ruling. Record ids do not: they are regenerated every run."""
    data = record["data"]
    return record["natural_key"] or str(data.get("work_email") or "").strip().lower() or record["id"]


def _jsonable(v):
    return v if isinstance(v, (str, int, float, bool)) or v is None else str(v)


def _trusted_sources(policy: dict) -> dict[str, str]:
    """Field -> the source a human said to trust when these sources disagree.
    Read once per run; this is what makes answering the conflict question
    actually change the next import rather than only closing a card."""
    trusted: dict[str, str] = {}
    with connect() as conn:
        rows = conn.execute(
            "SELECT signature, resolution FROM decisions WHERE reason_code = 'CONFLICT_ACROSS_SOURCES'"
        ).fetchall()
    for row in rows:
        resolution = json.loads(row["resolution"])
        if resolution.get("action") == "reject" or not resolution.get("value"):
            continue
        trusted[row["signature"]] = resolution["value"]
    return trusted


def _survivorship(cluster: list[dict], schema: Schema, policy: dict,
                  trusted: dict[str, str] | None = None) -> tuple[dict, dict, list[Issue]]:
    """cluster: list of {"id", "filename", "data"}. Returns (merged_data,
    field_survivorship, issues). A field with one distinct non-null value (or
    only null values) never escalates. A field with multiple distinct non-null
    values escalates if material (CONFLICT_ACROSS_SOURCES) and otherwise is
    resolved silently by source precedence -- policy.cross_source explicitly
    scopes escalation to material fields only."""
    survivorship: dict[str, dict] = {}
    merged: dict = {}
    issues: list[Issue] = []

    currency_by_source: dict[str, object] = {}
    for member in cluster:
        v = member["data"].get("ctc_currency")
        if v not in (None, "") and member["filename"] not in currency_by_source:
            currency_by_source[member["filename"]] = v

    for field_name, f in schema.fields.items():
        values_by_source: dict[str, object] = {}
        for member in cluster:
            v = member["data"].get(field_name)
            if v not in (None, "") and member["filename"] not in values_by_source:
                values_by_source[member["filename"]] = v

        if not values_by_source:
            continue

        distinct = set(values_by_source.values())
        best_source = min(values_by_source, key=lambda fn: _source_rank(fn, policy))
        merged[field_name] = values_by_source[best_source]
        entry = {"value": _jsonable(values_by_source[best_source]), "source": best_source}

        if len(distinct) > 1:
            entry["conflict"] = True
            # A standing answer for this exact disagreement shape wins, and the
            # question is not asked again.
            sig = signature("CONFLICT_ACROSS_SOURCES",
                            f"{field_name}:{'|'.join(sorted(values_by_source))}")
            winner = (trusted or {}).get(sig)
            if winner and winner in values_by_source:
                merged[field_name] = values_by_source[winner]
                entry["value"] = _jsonable(values_by_source[winner])
                entry["source"] = winner
                entry["resolved_by_policy"] = True
                survivorship[field_name] = entry
                continue
            # annual_ctc across sources reporting different currencies isn't a
            # genuine disagreement -- the raw amounts aren't on the same scale
            # (e.g. "779895" INR vs "9396" USD look like a huge conflict but may
            # be the same compensation shown two ways). Currency itself isn't a
            # material field, so a currency mismatch alone is silently resolved
            # by precedence like any non-material field, not escalated here.
            is_incomparable_currency = (
                field_name == "annual_ctc"
                and len({currency_by_source.get(src) for src in values_by_source if currency_by_source.get(src)}) > 1
            )
            if f.material and not is_incomparable_currency:
                issues.append(Issue(decide_cross_source_conflict(field_name, values_by_source, policy), field_name))

        survivorship[field_name] = entry

    return merged, survivorship, issues


def reconcile_run(run_id: str, schema: Schema, policy: dict) -> None:
    """Clusters records by natural_key (exact -- this alone correlates HRIS with
    payroll, since they deliberately share employee_id, and catches HRIS's own
    exact-duplicate rows) plus fuzzy blocking+scoring for everything else (CRM
    rows, which carry no natural key by design, and near-duplicate rows, which
    are deliberately given a new key). Auto-merges clusters above
    dedupe.auto_merge_above; the 0.72-0.90 band escalates as DUPE_AMBIGUOUS and
    stays unmerged; below that, records are left as distinct people."""
    with connect() as conn:
        run = conn.execute("SELECT stage FROM runs WHERE id = ?", (run_id,)).fetchone()
        if run and run["stage"] not in ("uploaded", "profiled", "mapped", "validated"):
            return  # already reconciled -- avoid re-merging and duplicating merge rows

        rows = conn.execute(
            """SELECT r.id, r.status, sf.filename, r.natural_key, r.data
               FROM records r JOIN source_files sf ON sf.id = r.source_file_id
               WHERE r.run_id = ?""",
            (run_id,),
        ).fetchall()

    records = {
        r["id"]: {"id": r["id"], "filename": r["filename"], "natural_key": r["natural_key"],
                   "data": json.loads(r["data"])}
        for r in rows
    }

    # Pairs a human already ruled on, keyed by stable identity so they still
    # match after a re-import assigns every record a fresh id.
    with connect() as conn:
        prior_dupe_calls = {
            row["signature"]: json.loads(row["resolution"])
            for row in conn.execute(
                "SELECT signature, resolution FROM decisions WHERE reason_code = 'DUPE_AMBIGUOUS'"
            )
        }

    trusted = _trusted_sources(policy)

    uf = _UnionFind(records.keys())

    by_key: dict[str, list[str]] = defaultdict(list)
    for rec_id, rec in records.items():
        if rec["natural_key"]:
            by_key[rec["natural_key"]].append(rec_id)
    for ids in by_key.values():
        for other in ids[1:]:
            uf.union(ids[0], other)

    emit(run_id, "progress", f"Grouping {len(records):,} records by natural key",
         stage="reconciled", what="cluster")

    blocks: dict[str, list[str]] = defaultdict(list)
    for rec_id, rec in records.items():
        blocks[blocking_key(rec["data"])].append(rec_id)
    emit(run_id, "progress",
         f"Comparing candidates inside {len(blocks):,} blocks", stage="reconciled",
         what="score", blocks=len(blocks))

    dupe_pairs_evaluated: set[tuple[str, str]] = set()
    ambiguous_pairs: list[tuple] = []
    for bucket in blocks.values():
        for i in range(len(bucket)):
            for j in range(i + 1, len(bucket)):
                a_id, b_id = bucket[i], bucket[j]
                if uf.find(a_id) == uf.find(b_id):
                    continue  # already the same employee via natural_key
                pair_key = tuple(sorted((a_id, b_id)))
                if pair_key in dupe_pairs_evaluated:
                    continue
                dupe_pairs_evaluated.add(pair_key)

                score = similarity_score(records[a_id]["data"], records[b_id]["data"])
                if score >= policy["dedupe"]["auto_merge_above"]:
                    uf.union(a_id, b_id)
                    continue

                scope_key = dupe_scope_key(_identity(records[a_id]), _identity(records[b_id]))
                prior = prior_dupe_calls.get(signature("DUPE_AMBIGUOUS", scope_key))
                if prior is not None:
                    # Already answered in an earlier run -- honour it instead of asking again.
                    if prior.get("action") == "approve":
                        uf.union(a_id, b_id)
                    continue

                decision = decide_dupe_ambiguous(
                    _identity(records[a_id]), _identity(records[b_id]), score, policy
                )
                if decision.reason_code:
                    ambiguous_pairs.append((decision, f"{a_id}:{b_id}"))

    # Written once, after scoring every pair, rather than opening a connection
    # per pair inside the O(blocks * block_size^2) loop above.
    if ambiguous_pairs:
        with connect() as conn:
            for decision, pair_id in ambiguous_pairs:
                create_or_merge_escalation(conn, run_id, decision, entity_id=pair_id)

    if ambiguous_pairs:
        emit(run_id, "escalation",
             f"{len(ambiguous_pairs):,} pair(s) too close to call, asking rather than merging",
             stage="reconciled", what="dupe", pairs=len(ambiguous_pairs))

    clusters: dict[str, list[str]] = defaultdict(list)
    for rec_id in records:
        clusters[uf.find(rec_id)].append(rec_id)

    # One connection for every cluster's writes, not one per cluster -- with
    # thousands of clusters, per-cluster connections make SQLite's WAL commit
    # overhead the dominant cost (this was ~59s of a ~60s run before batching).
    with connect() as conn:
        for member_ids in clusters.values():
            if len(member_ids) < 2:
                continue
            cluster = [records[i] for i in member_ids]
            cluster.sort(key=lambda r: _source_rank(r["filename"], policy))
            survivor = cluster[0]
            absorbed = cluster[1:]

            merged_data, field_survivorship, issues = _survivorship(cluster, schema, policy, trusted)
            score = (1.0 if any(r["natural_key"] for r in cluster)
                     else similarity_score(cluster[0]["data"], cluster[1]["data"]))

            conn.execute(
                "UPDATE records SET data = ?, status = 'merged_survivor', updated_at = ? WHERE id = ?",
                (json.dumps(merged_data, default=str), utcnow(), survivor["id"]),
            )
            for member in absorbed:
                conn.execute(
                    "UPDATE records SET status = 'merged', updated_at = ? WHERE id = ?",
                    (utcnow(), member["id"]),
                )
                conn.execute(
                    """INSERT INTO merges (id, run_id, survivor_record_id, absorbed_record_id, score,
                       field_survivorship, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (new_id(), run_id, survivor["id"], member["id"], score,
                     json.dumps(field_survivorship), utcnow()),
                )
            record_change(
                conn, run_id=run_id, actor="agent", stage="reconciled", entity_type="record",
                entity_id=survivor["id"], after="merged_survivor",
                note=f"merged {len(absorbed)} record(s), score={score:.2f}",
            )
            for issue in issues:
                create_or_merge_escalation(conn, run_id, issue.decision, entity_id=survivor["id"])

        conn.execute("UPDATE runs SET stage = 'reconciled', updated_at = ? WHERE id = ?", (utcnow(), run_id))

    merged = sum(1 for ids in clusters.values() if len(ids) > 1)
    emit(run_id, "stage", f"Merged into {merged:,} people", stage="reconciled", people=merged)

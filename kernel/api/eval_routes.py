"""Evaluation API — batch evaluation execution, results, comparison, stats.

Endpoints under /api/_eval/ orchestrate evaluation workflows beyond
raw entity CRUD (which the auto-generated routes handle for Rubric,
TestSet, EvaluationRun, EvaluationResult).
"""

import logging
from datetime import datetime, timedelta, timezone

from bson import ObjectId
from fastapi import APIRouter, Depends, Query

from kernel.auth.middleware import get_current_actor
from kernel.context import current_org_id
from kernel.db import ENTITY_REGISTRY, get_database

logger = logging.getLogger(__name__)

eval_router = APIRouter(prefix="/api/_eval", tags=["eval"])


def _safe(v):
    """Recursively convert ObjectId/datetime to JSON-safe types."""
    if isinstance(v, ObjectId):
        return str(v)
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, list):
        return [_safe(i) for i in v]
    if isinstance(v, dict):
        return {k: _safe(val) for k, val in v.items()}
    return v


def _parse_since(since_str: str) -> datetime:
    """Parse a 'since' value — supports durations (24h, 7d, 30m) and ISO dates."""
    if since_str.endswith("h"):
        return datetime.now(timezone.utc) - timedelta(hours=int(since_str[:-1]))
    if since_str.endswith("d"):
        return datetime.now(timezone.utc) - timedelta(days=int(since_str[:-1]))
    if since_str.endswith("m"):
        return datetime.now(timezone.utc) - timedelta(minutes=int(since_str[:-1]))
    return datetime.fromisoformat(since_str)


@eval_router.post("/run")
async def trigger_eval_run(
    body: dict,
    actor=Depends(get_current_actor),
):
    """Trigger a batch evaluation run.

    1. Resolve associate by name
    2. Find rubric(s) — specified IDs or all active for this associate
    3. Create EvaluationRun entity
    4. Query matching Traces
    5. Create queue messages for evaluator role (one per Trace)
    6. Return run_id + total
    """
    org_id = current_org_id.get()
    db = get_database()

    associate_name = body.get("associate_name")
    if not associate_name:
        return {"error": "associate_name is required"}

    actors_coll = db["actors"]
    associate_doc = await actors_coll.find_one(
        {"org_id": org_id, "name": associate_name, "type": "associate"}
    )
    if not associate_doc:
        return {"error": f"Associate '{associate_name}' not found"}
    associate_id = associate_doc["_id"]

    rubric_ids = body.get("rubric_ids", [])
    if body.get("all_rubrics"):
        rubrics_coll = db["rubrics"]
        cursor = rubrics_coll.find(
            {"org_id": org_id, "associate_id": associate_id, "status": "active"}
        )
        rubric_ids = [str(r["_id"]) async for r in cursor]
    if not rubric_ids:
        return {"error": "No rubrics specified or found for this associate"}

    trace_query: dict = {"org_id": org_id, "associate_id": associate_id}
    if body.get("since"):
        trace_query["created_at"] = {"$gte": _parse_since(body["since"])}
        if body.get("until"):
            trace_query["created_at"]["$lte"] = datetime.fromisoformat(body["until"])
    if body.get("entity_type"):
        trace_query["entity_type"] = body["entity_type"]
    if body.get("correlation_id"):
        trace_query["correlation_id"] = body["correlation_id"]

    traces_coll = db["traces"]
    sort = [("created_at", -1)]
    limit = body.get("sample", 100)

    if body.get("cascade"):
        pipeline = [
            {"$match": trace_query},
            {"$sort": {"created_at": -1}},
            {"$group": {"_id": "$correlation_id", "traces": {"$push": "$$ROOT"}, "count": {"$sum": 1}}},
            {"$limit": limit},
        ]
        cascade_groups = await traces_coll.aggregate(pipeline).to_list(length=limit)
        trace_ids = []
        for group in cascade_groups:
            for t in group.get("traces", []):
                trace_ids.append(t["_id"])
        total = len(trace_ids)
    else:
        cursor = traces_coll.find(trace_query).sort(sort).limit(limit)
        trace_docs = await cursor.to_list(length=limit)
        trace_ids = [t["_id"] for t in trace_docs]
        total = len(trace_ids)

    if total == 0:
        return {"error": "No matching traces found", "query": _safe(trace_query)}

    trigger_mode = "cascade" if body.get("cascade") else "batch"
    if body.get("test_set_id"):
        trigger_mode = "prospective"

    eval_runs_coll = db["evaluation_runs"]
    run_doc = {
        "org_id": org_id,
        "associate_id": associate_id,
        "associate_name": associate_name,
        "rubric_ids": [ObjectId(r) if not isinstance(r, ObjectId) else r for r in rubric_ids],
        "trigger_mode": trigger_mode,
        "sample_size": total,
        "total": total,
        "passed": 0,
        "failed": 0,
        "status": "pending",
        "created_at": datetime.now(timezone.utc),
        "started_at": datetime.now(timezone.utc),
    }
    insert_result = await eval_runs_coll.insert_one(run_doc)
    run_id = insert_result.inserted_id

    evaluator_role = await db["roles"].find_one({"org_id": org_id, "name": "evaluator"})
    if not evaluator_role:
        return {"error": "Evaluator role not found. Create it first.", "run_id": str(run_id)}

    messages_coll = db["message_queues"]
    messages_to_insert = []
    for trace_id in trace_ids:
        messages_to_insert.append({
            "org_id": org_id,
            "entity_type": "Trace",
            "entity_id": trace_id,
            "target_role": "evaluator",
            "event_type": "batch_eval",
            "status": "pending",
            "correlation_id": str(run_id),
            "causation_id": str(run_id),
            "attempt_count": 0,
            "max_attempts": 3,
            "created_at": datetime.now(timezone.utc),
            "depth": 0,
        })

    if messages_to_insert:
        await messages_coll.insert_many(messages_to_insert)

    await eval_runs_coll.update_one(
        {"_id": run_id},
        {"$set": {"status": "running"}},
    )

    logger.info("Evaluation run %s started: %d traces, rubrics=%s", run_id, total, rubric_ids)

    return {
        "run_id": str(run_id),
        "status": "running",
        "total": total,
        "trigger_mode": trigger_mode,
        "rubric_ids": [str(r) for r in rubric_ids],
        "associate": associate_name,
    }


@eval_router.get("/runs")
async def list_eval_runs(
    associate_name: str = Query(None),
    status: str = Query(None),
    since: str = Query(None),
    limit: int = Query(20, le=100),
    actor=Depends(get_current_actor),
):
    """List evaluation runs with optional filters."""
    org_id = current_org_id.get()
    db = get_database()

    query: dict = {"org_id": org_id}
    if associate_name:
        query["associate_name"] = associate_name
    if status:
        query["status"] = status
    if since:
        query["created_at"] = {"$gte": _parse_since(since)}

    runs = (
        await db["evaluation_runs"]
        .find(query)
        .sort("created_at", -1)
        .limit(limit)
        .to_list(length=limit)
    )
    return _safe(runs)


@eval_router.get("/runs/{run_id}")
async def get_eval_run(
    run_id: str,
    actor=Depends(get_current_actor),
):
    """Get evaluation run summary."""
    org_id = current_org_id.get()
    db = get_database()

    run = await db["evaluation_runs"].find_one(
        {"_id": ObjectId(run_id), "org_id": org_id}
    )
    if not run:
        return {"error": "Run not found"}
    return _safe(run)


@eval_router.get("/runs/{run_id}/results")
async def get_eval_results(
    run_id: str,
    failed_only: str = Query(None),
    rule_id: str = Query(None),
    actor=Depends(get_current_actor),
):
    """Get per-item results for an evaluation run."""
    org_id = current_org_id.get()
    db = get_database()

    query: dict = {"org_id": org_id, "run_id": ObjectId(run_id)}
    if failed_only == "true":
        query["passed"] = False
    if rule_id:
        query["rubric_scores.rule_id"] = rule_id

    results = (
        await db["evaluation_results"]
        .find(query)
        .sort("created_at", -1)
        .to_list(length=500)
    )
    return _safe(results)


@eval_router.get("/compare/{run_id_1}/{run_id_2}")
async def compare_runs(
    run_id_1: str,
    run_id_2: str,
    actor=Depends(get_current_actor),
):
    """Compare two evaluation runs — per-rule score diff."""
    org_id = current_org_id.get()
    db = get_database()

    run1 = await db["evaluation_runs"].find_one({"_id": ObjectId(run_id_1), "org_id": org_id})
    run2 = await db["evaluation_runs"].find_one({"_id": ObjectId(run_id_2), "org_id": org_id})
    if not run1 or not run2:
        return {"error": "One or both runs not found"}

    scores1 = run1.get("scores_by_rule", {})
    scores2 = run2.get("scores_by_rule", {})
    all_rules = set(list(scores1.keys()) + list(scores2.keys()))

    comparison = []
    for rule in sorted(all_rules):
        s1 = scores1.get(rule, {}).get("pass_rate")
        s2 = scores2.get(rule, {}).get("pass_rate")
        comparison.append({
            "rule": rule,
            "run_1": s1,
            "run_2": s2,
            "delta": round(s2 - s1, 4) if s1 is not None and s2 is not None else None,
        })

    return {
        "run_1": {"id": run_id_1, "total": run1.get("total"), "pass_rate": run1.get("pass_rate")},
        "run_2": {"id": run_id_2, "total": run2.get("total"), "pass_rate": run2.get("pass_rate")},
        "comparison": comparison,
    }


@eval_router.get("/stats")
async def eval_stats(
    associate_name: str = Query(None),
    since: str = Query(None),
    group_by: str = Query(None),
    actor=Depends(get_current_actor),
):
    """Aggregate evaluation stats across runs."""
    org_id = current_org_id.get()
    db = get_database()

    match: dict = {"org_id": org_id, "status": "completed"}
    if associate_name:
        match["associate_name"] = associate_name
    if since:
        match["created_at"] = {"$gte": _parse_since(since)}

    if group_by == "rule":
        pipeline = [
            {"$match": match},
            {"$project": {"scores_by_rule": {"$objectToArray": "$scores_by_rule"}}},
            {"$unwind": "$scores_by_rule"},
            {"$group": {
                "_id": "$scores_by_rule.k",
                "avg_pass_rate": {"$avg": "$scores_by_rule.v.pass_rate"},
                "runs": {"$sum": 1},
            }},
            {"$sort": {"avg_pass_rate": 1}},
        ]
        results = await db["evaluation_runs"].aggregate(pipeline).to_list(length=100)
        return _safe(results)

    if group_by == "failure_attribution":
        pipeline = [
            {"$match": match},
            {"$project": {"failure_attribution": {"$objectToArray": "$failure_attribution"}}},
            {"$unwind": "$failure_attribution"},
            {"$group": {
                "_id": "$failure_attribution.k",
                "total_count": {"$sum": "$failure_attribution.v"},
                "runs": {"$sum": 1},
            }},
            {"$sort": {"total_count": -1}},
        ]
        results = await db["evaluation_runs"].aggregate(pipeline).to_list(length=100)
        return _safe(results)

    pipeline = [
        {"$match": match},
        {"$group": {
            "_id": "$associate_name",
            "runs": {"$sum": 1},
            "avg_pass_rate": {"$avg": "$pass_rate"},
            "total_evaluated": {"$sum": "$total"},
            "total_passed": {"$sum": "$passed"},
            "total_failed": {"$sum": "$failed"},
        }},
        {"$sort": {"avg_pass_rate": 1}},
    ]
    results = await db["evaluation_runs"].aggregate(pipeline).to_list(length=100)
    return _safe(results)

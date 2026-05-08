"""Evaluation API — batch evaluation execution, results, comparison, stats.

Endpoints under /api/_eval/ orchestrate evaluation workflows beyond
raw entity CRUD (which the auto-generated routes handle for Rubric,
TestSet, EvaluationRun, EvaluationResult).
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from bson import ObjectId
from fastapi import APIRouter, Depends, Query

from kernel.auth.middleware import get_current_actor
from kernel.context import current_org_id
from kernel.db import ENTITY_REGISTRY, get_database
from kernel.message.schema import Message

logger = logging.getLogger(__name__)

eval_router = APIRouter(prefix="/api/_eval", tags=["eval"])


def _fire_dispatch(created_messages):
    """Fire-and-forget optimistic dispatch after save_tracked commits."""
    if created_messages:
        from kernel.message.dispatch import optimistic_dispatch

        asyncio.create_task(optimistic_dispatch(created_messages))


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

    # Capture rubric versions for lineage tracking
    rubric_oids = [ObjectId(r) if not isinstance(r, ObjectId) else r for r in rubric_ids]
    rubric_versions = []
    rubrics_coll = db["rubrics"]
    for rid in rubric_oids:
        rdoc = await rubrics_coll.find_one({"_id": rid, "org_id": org_id})
        rubric_versions.append(rdoc.get("version", 1) if rdoc else 1)

    trigger_mode = "cascade" if body.get("cascade") else "batch"
    if body.get("test_set_id"):
        trigger_mode = "prospective"
    if body.get("experiment"):
        trigger_mode = "retroactive"

    # Build filter criteria record for reproducibility
    filter_criteria = {}
    if body.get("since"):
        filter_criteria["since"] = body["since"]
    if body.get("until"):
        filter_criteria["until"] = body["until"]
    if body.get("entity_type"):
        filter_criteria["entity_type"] = body["entity_type"]
    if body.get("correlation_id"):
        filter_criteria["correlation_id"] = body["correlation_id"]
    if body.get("sample"):
        filter_criteria["sample"] = body["sample"]
    if body.get("data"):
        filter_criteria["data"] = body["data"]

    # Retroactive mode: load traces from an existing LangSmith experiment
    if trigger_mode == "retroactive":
        experiment_ref = body["experiment"]
        try:
            from langsmith import Client as LSClient
            ls_client = LSClient()
            runs = list(ls_client.list_runs(
                project_name=experiment_ref,
                is_root=True,
                limit=body.get("sample", 100),
            ))
        except Exception as e:
            return {"error": f"Failed to load experiment '{experiment_ref}': {e}"}
        if not runs:
            return {"error": f"No runs found in experiment '{experiment_ref}'"}
        ls_run_ids = [str(r.id) for r in runs]
        traces_coll = db["traces"]
        trace_docs = await traces_coll.find(
            {"org_id": org_id, "langsmith_run_id": {"$in": ls_run_ids}}
        ).to_list(length=len(ls_run_ids))
        trace_ids = [t["_id"] for t in trace_docs]
        total = len(trace_ids)
        if total == 0:
            return {"error": f"No OS Traces found matching experiment runs (checked {len(ls_run_ids)} LangSmith runs)"}
        filter_criteria["experiment"] = experiment_ref

    # Prospective mode: read TestSet items, not existing traces
    elif trigger_mode == "prospective":
        test_set_id = body.get("test_set_id")
        test_set_coll = db["test_sets"]
        test_set_doc = await test_set_coll.find_one(
            {"_id": ObjectId(test_set_id), "org_id": org_id}
        )
        if not test_set_doc:
            return {"error": f"TestSet '{test_set_id}' not found"}
        test_items = test_set_doc.get("items", [])
        eval_limit = body.get("limit")
        if eval_limit and eval_limit < len(test_items):
            test_items = test_items[:eval_limit]
        total = len(test_items)
        if total == 0:
            return {"error": "TestSet has no items"}
        trace_ids = None
    else:
        # Batch/cascade: query existing traces
        trace_query: dict = {"org_id": org_id, "associate_id": associate_id}
        if body.get("since"):
            trace_query["created_at"] = {"$gte": _parse_since(body["since"])}
            if body.get("until"):
                trace_query["created_at"]["$lte"] = datetime.fromisoformat(body["until"])
        if body.get("entity_type"):
            trace_query["entity_type"] = body["entity_type"]
        if body.get("correlation_id"):
            trace_query["correlation_id"] = body["correlation_id"]
        if body.get("data"):
            for k, v in body["data"].items():
                trace_query[k] = v

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

    # Create EvaluationRun via save_tracked (audit trail + watches)
    EvalRunCls = ENTITY_REGISTRY.get("EvaluationRun")
    if not EvalRunCls:
        return {"error": "EvaluationRun entity not registered"}

    eval_run = EvalRunCls(
        org_id=org_id,
        associate_id=associate_id,
        associate_name=associate_name,
        rubric_ids=rubric_oids,
        rubric_versions=rubric_versions,
        test_set_id=ObjectId(body["test_set_id"]) if body.get("test_set_id") else None,
        test_set_version=test_set_doc.get("version", 1) if trigger_mode == "prospective" else None,
        trigger_mode=trigger_mode,
        sample_size=total,
        filter_criteria=filter_criteria,
        total=total,
        passed=0,
        failed=0,
        status="pending",
        started_at=datetime.now(timezone.utc),
    )
    created_messages = await eval_run.save_tracked(method="create")
    _fire_dispatch(created_messages)
    run_id = eval_run.id

    # Create LangSmith Dataset + Experiment for LangSmith-native tracking
    langsmith_dataset_id = None
    langsmith_experiment_id = None
    try:
        from langsmith import Client as LSClient
        ls_client = LSClient()
        dataset_name = f"eval-{associate_name}-{run_id}"
        ls_dataset = ls_client.create_dataset(
            dataset_name=dataset_name,
            description=f"Evaluation run {run_id} ({trigger_mode}) for {associate_name}",
            metadata={"run_id": str(run_id), "trigger_mode": trigger_mode,
                       "associate_name": associate_name},
        )
        langsmith_dataset_id = str(ls_dataset.id)

        if trigger_mode == "prospective" and test_items:
            for item in test_items:
                ls_client.create_example(
                    inputs=item.get("inputs", {}),
                    outputs=item.get("expected", {}),
                    dataset_id=ls_dataset.id,
                    metadata={"item_id": item.get("item_id", ""),
                              "name": item.get("name", "")},
                )
        elif trace_ids:
            traces_coll_ls = db["traces"]
            for tid in trace_ids:
                tdoc = await traces_coll_ls.find_one({"_id": tid})
                if tdoc:
                    ls_client.create_example(
                        inputs=tdoc.get("inputs", {}),
                        outputs=tdoc.get("outputs", {}),
                        dataset_id=ls_dataset.id,
                        metadata={"trace_id": str(tid),
                                  "langsmith_run_id": tdoc.get("langsmith_run_id", "")},
                    )

        langsmith_experiment_id = dataset_name
        eval_run.langsmith_experiment_id = langsmith_experiment_id
        await eval_run.save_tracked()
        logger.info("LangSmith dataset %s created with %d examples", langsmith_dataset_id, total)
    except ImportError:
        logger.info("langsmith not installed — skipping LangSmith experiment creation")
    except Exception as e:
        logger.warning("LangSmith experiment creation failed (non-blocking): %s", e)

    evaluator_role = await db["roles"].find_one({"org_id": org_id, "name": "evaluator"})
    if not evaluator_role:
        return {"error": "Evaluator role not found. Create it first.", "run_id": str(run_id)}

    # Create queue messages via Message schema (proper formatting)
    run_id_str = str(run_id)
    if trigger_mode == "prospective":
        for item in test_items:
            msg = Message(
                org_id=org_id,
                entity_type="Trace",
                entity_id=ObjectId(),
                event_type="prospective_eval",
                target_role="evaluator",
                correlation_id=run_id_str,
                causation_id=run_id_str,
                depth=0,
                context={"test_item": item, "run_id": run_id_str,
                         "rubric_ids": [str(r) for r in rubric_oids]},
            )
            await msg.insert()
    else:
        for trace_id in trace_ids:
            msg = Message(
                org_id=org_id,
                entity_type="Trace",
                entity_id=trace_id,
                event_type="batch_eval",
                target_role="evaluator",
                correlation_id=run_id_str,
                causation_id=run_id_str,
                depth=0,
                context={"run_id": run_id_str,
                         "rubric_ids": [str(r) for r in rubric_oids]},
            )
            await msg.insert()

    # Transition to running
    eval_run.transition_to("running")
    await eval_run.save_tracked(method="transition")

    logger.info("Evaluation run %s started: %d items, rubrics=%s, mode=%s",
                run_id, total, rubric_ids, trigger_mode)

    result = {
        "run_id": str(run_id),
        "status": "running",
        "total": total,
        "trigger_mode": trigger_mode,
        "rubric_ids": [str(r) for r in rubric_oids],
        "rubric_versions": rubric_versions,
        "associate": associate_name,
    }
    if langsmith_dataset_id:
        result["langsmith_dataset_id"] = langsmith_dataset_id
    if langsmith_experiment_id:
        result["langsmith_experiment_id"] = langsmith_experiment_id
    return result


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
    EvalRunCls = ENTITY_REGISTRY.get("EvaluationRun")
    if not EvalRunCls:
        return {"error": "EvaluationRun entity not registered"}

    query: dict = {"org_id": org_id}
    if associate_name:
        query["associate_name"] = associate_name
    if status:
        query["status"] = status
    if since:
        query["created_at"] = {"$gte": _parse_since(since)}

    runs = (
        await EvalRunCls.get_motor_collection()
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
    EvalRunCls = ENTITY_REGISTRY.get("EvaluationRun")
    if not EvalRunCls:
        return {"error": "EvaluationRun entity not registered"}

    run = await EvalRunCls.get_motor_collection().find_one(
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
    """Per-item results for an evaluation run."""
    org_id = current_org_id.get()
    EvalResultCls = ENTITY_REGISTRY.get("EvaluationResult")
    if not EvalResultCls:
        return {"error": "EvaluationResult entity not registered"}

    query: dict = {"org_id": org_id, "run_id": ObjectId(run_id)}
    if failed_only == "true":
        query["passed"] = False
    if rule_id:
        query["rubric_scores.rule_id"] = rule_id

    results = (
        await EvalResultCls.get_motor_collection()
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
    EvalRunCls = ENTITY_REGISTRY.get("EvaluationRun")
    if not EvalRunCls:
        return {"error": "EvaluationRun entity not registered"}
    coll = EvalRunCls.get_motor_collection()

    run1 = await coll.find_one({"_id": ObjectId(run_id_1), "org_id": org_id})
    run2 = await coll.find_one({"_id": ObjectId(run_id_2), "org_id": org_id})
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
    EvalRunCls = ENTITY_REGISTRY.get("EvaluationRun")
    if not EvalRunCls:
        return {"error": "EvaluationRun entity not registered"}
    coll = EvalRunCls.get_motor_collection()

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
        results = await coll.aggregate(pipeline).to_list(length=100)
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
        results = await coll.aggregate(pipeline).to_list(length=100)
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
    results = await coll.aggregate(pipeline).to_list(length=100)
    return _safe(results)

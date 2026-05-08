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

    # Prospective mode: run the associate on TestSet inputs, then evaluate
    # the resulting traces. Two-phase: pipeline associate runs → creates
    # Trace → evaluator watch fires → evaluates against rubric + criteria.
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

    evaluator_role = await db["roles"].find_one({"org_id": org_id, "name": "evaluator"})
    if not evaluator_role:
        return {"error": "Evaluator role not found. Create it first.", "run_id": str(run_id)}

    # Create queue messages via Message schema (proper formatting)
    run_id_str = str(run_id)
    if trigger_mode == "prospective":
        # Prospective: create test entities, then send to the pipeline
        # associate's role. The associate processes them → creates Traces →
        # evaluator watch fires → evaluates against rubric + TestSet criteria.
        associate_role = await db["roles"].find_one({"_id": {"$in": [ObjectId(r) for r in associate_doc.get("role_ids", [])]}})
        if not associate_role:
            return {"error": f"No role found for associate '{associate_name}'", "run_id": str(run_id)}
        target_role = associate_role.get("name", "")

        for item in test_items:
            inputs = item.get("inputs", {})
            entity_type = inputs.get("entity_type", "Email")
            entity_data = inputs.get("entity_data", {})

            entity_cls = ENTITY_REGISTRY.get(entity_type)
            if not entity_cls:
                logger.warning("Prospective: entity type %s not registered, skipping", entity_type)
                continue

            test_entity = entity_cls(org_id=org_id, **entity_data)
            entity_msgs = await test_entity.save_tracked(method="create")
            _fire_dispatch(entity_msgs)

            msg = Message(
                org_id=org_id,
                entity_type=entity_type,
                entity_id=test_entity.id,
                event_type="prospective_eval",
                target_role=target_role,
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

    return {
        "run_id": str(run_id),
        "status": "running",
        "total": total,
        "trigger_mode": trigger_mode,
        "rubric_ids": [str(r) for r in rubric_oids],
        "rubric_versions": rubric_versions,
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


@eval_router.post("/runs/{run_id}/create-experiment")
async def create_langsmith_experiment(
    run_id: str,
    actor=Depends(get_current_actor),
):
    """Create a LangSmith Experiment from a completed EvaluationRun.

    Called after the evaluator has finished processing all traces for a run.
    Reads OS EvaluationResults and creates a proper LangSmith Experiment
    via client.evaluate() — the LangSmith-native evaluation mechanism.
    """
    org_id = current_org_id.get()
    db = get_database()

    EvalRunCls = ENTITY_REGISTRY.get("EvaluationRun")
    EvalResultCls = ENTITY_REGISTRY.get("EvaluationResult")
    if not EvalRunCls or not EvalResultCls:
        return {"error": "Evaluation entities not registered"}

    run = await EvalRunCls.get_motor_collection().find_one(
        {"_id": ObjectId(run_id), "org_id": org_id}
    )
    if not run:
        return {"error": "Run not found"}

    results = await EvalResultCls.get_motor_collection().find(
        {"org_id": org_id, "run_id": ObjectId(run_id)}
    ).to_list(length=1000)

    if not results:
        return {"error": "No results found for this run"}

    traces_coll = db["traces"]

    try:
        from langsmith import Client as LSClient

        ls_client = LSClient()

        dataset_name = f"eval-{run.get('associate_name', 'unknown')}-{run_id}"
        try:
            ls_dataset = ls_client.create_dataset(
                dataset_name=dataset_name,
                description=f"Evaluation run {run_id} for {run.get('associate_name')}",
            )
        except Exception:
            ls_dataset = ls_client.read_dataset(dataset_name=dataset_name)

        trace_cache = {}
        for result in results:
            trace_id = result.get("trace_id")
            if trace_id and trace_id not in trace_cache:
                tdoc = await traces_coll.find_one({"_id": trace_id})
                if tdoc:
                    trace_cache[trace_id] = tdoc

            trace = trace_cache.get(trace_id, {})
            ls_client.create_example(
                inputs=trace.get("inputs", {}),
                outputs=trace.get("outputs", {}),
                dataset_id=ls_dataset.id,
                metadata={
                    "trace_id": str(trace_id),
                    "entity_type": result.get("entity_type", ""),
                    "entity_id": str(result.get("entity_id", "")),
                    "langsmith_run_id": trace.get("langsmith_run_id", ""),
                },
            )

        def _target(inputs: dict) -> dict:
            return inputs

        def _make_evaluator(rule_id, rule_results):
            def evaluator(run, example):
                for r in rule_results:
                    trace_id_str = str(example.metadata.get("trace_id", ""))
                    if str(r.get("trace_id", "")) == trace_id_str:
                        for score in r.get("rubric_scores", []):
                            if score.get("rule_id") == rule_id:
                                return {
                                    "key": rule_id,
                                    "score": score.get("score", 0.0),
                                    "comment": score.get("reasoning", ""),
                                }
                return {"key": rule_id, "score": 0.0, "comment": "No result found"}
            return evaluator

        rule_ids = set()
        for r in results:
            for score in r.get("rubric_scores", []):
                rule_ids.add(score.get("rule_id"))

        evaluators = [_make_evaluator(rid, results) for rid in rule_ids]

        experiment_results = ls_client.evaluate(
            _target,
            data=dataset_name,
            evaluators=evaluators,
            experiment_prefix=f"eval-{run.get('associate_name', 'unknown')}",
            description=f"EvaluationRun {run_id} — {run.get('trigger_mode', 'batch')} mode",
            max_concurrency=0,
        )

        experiment_name = getattr(experiment_results, 'experiment_name', dataset_name)

        eval_run_entity = await EvalRunCls.get_scoped(run_id)
        if eval_run_entity:
            eval_run_entity.langsmith_experiment_id = experiment_name
            await eval_run_entity.save_tracked()

        logger.info("LangSmith experiment '%s' created for run %s", experiment_name, run_id)

        return {
            "status": "created",
            "experiment_name": experiment_name,
            "dataset_name": dataset_name,
            "results_count": len(results),
        }

    except ImportError:
        return {"error": "langsmith not installed"}
    except Exception as e:
        logger.exception("Failed to create LangSmith experiment for run %s", run_id)
        return {"error": f"Failed to create experiment: {e}"}


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


@eval_router.get("/versions/{entity_type}/{entity_id}")
async def entity_versions(
    entity_type: str,
    entity_id: str,
    actor=Depends(get_current_actor),
):
    """List version history for any entity from the changes collection."""
    org_id = current_org_id.get()
    db = get_database()

    changes = (
        await db["changes"]
        .find(
            {"org_id": org_id, "entity_type": entity_type, "entity_id": ObjectId(entity_id)},
        )
        .sort("timestamp", -1)
        .to_list(length=100)
    )

    versions = []
    for change in changes:
        version_entry = {
            "version": None,
            "change_type": change.get("change_type"),
            "timestamp": change.get("timestamp"),
            "actor_id": str(change.get("actor_id", "")),
            "effective_actor_id": str(change.get("effective_actor_id", "")),
        }
        for field_change in change.get("changes", []):
            if field_change.get("field") == "version":
                version_entry["version"] = field_change.get("new_value")
            if field_change.get("field") == "rules":
                version_entry["rules_changed"] = True
            if field_change.get("field") == "status":
                version_entry["status_from"] = field_change.get("old_value")
                version_entry["status_to"] = field_change.get("new_value")
        if change.get("change_type") == "create":
            version_entry["version"] = 1
        versions.append(version_entry)

    return _safe(versions)


@eval_router.get("/versions/{entity_type}/{entity_id}/at/{version}")
async def entity_at_version(
    entity_type: str,
    entity_id: str,
    version: int,
    actor=Depends(get_current_actor),
):
    """Reconstruct any entity at a specific version from the changes collection."""
    org_id = current_org_id.get()
    db = get_database()
    entity_cls = ENTITY_REGISTRY.get(entity_type)
    if not entity_cls:
        return {"error": f"{entity_type} entity not registered"}

    current = await entity_cls.get_motor_collection().find_one(
        {"_id": ObjectId(entity_id), "org_id": org_id}
    )
    if not current:
        return {"error": f"{entity_type} not found"}

    current_version = current.get("version", 1)
    if version == current_version:
        return _safe(current)
    if version > current_version:
        return {"error": f"Version {version} does not exist (current: {current_version})"}

    changes = (
        await db["changes"]
        .find(
            {"org_id": org_id, "entity_type": entity_type, "entity_id": ObjectId(entity_id)},
        )
        .sort("timestamp", -1)
        .to_list(length=100)
    )

    doc = dict(current)
    for change in changes:
        doc_version = doc.get("version", 1)
        if doc_version <= version:
            break
        for field_change in change.get("changes", []):
            field = field_change.get("field")
            if field and field in doc:
                doc[field] = field_change.get("old_value")

    return _safe(doc)

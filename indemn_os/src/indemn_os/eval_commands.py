"""Evaluation management commands — trigger runs, query results, compare, stats.

Higher-level commands beyond raw entity CRUD. Domain entities (Rubric,
TestSet, EvaluationRun, EvaluationResult) auto-generate their own CRUD
commands. These commands orchestrate evaluation workflows.
"""

import json

import typer

from indemn_os.client import CLIClient, render

eval_app = typer.Typer(name="eval", help="Evaluation framework — run, results, compare, stats")


@eval_app.command("run")
def run_evaluation(
    associate: str = typer.Option(..., "--associate", help="Associate name"),
    rubric: list[str] = typer.Option(None, "--rubric", help="Rubric ID (repeatable)"),
    all_rubrics: bool = typer.Option(False, "--all-rubrics", help="Use all active rubrics for this associate"),
    sample: int = typer.Option(None, "--sample", help="Number of recent traces to evaluate"),
    since: str = typer.Option(None, "--since", help="Time window start (duration like 24h or date)"),
    until: str = typer.Option(None, "--until", help="Time window end (date)"),
    cascade: bool = typer.Option(False, "--cascade", help="Cascade-level evaluation"),
    correlation_id: str = typer.Option(None, "--correlation-id", help="Evaluate a specific cascade"),
    test_set: str = typer.Option(None, "--test-set", help="TestSet ID for prospective evaluation"),
    entity_type: str = typer.Option(None, "--entity-type", help="Filter by processed entity type"),
    experiment: str = typer.Option(None, "--experiment", help="LangSmith experiment ID for retroactive evaluation"),
    limit: int = typer.Option(None, "--limit", help="Max items to evaluate (for prospective mode)"),
    data: str = typer.Option(None, "--data", help="JSON metadata filter on traces"),
):
    """Trigger an evaluation run.

    Creates an EvaluationRun entity, selects items to evaluate (from traces,
    test sets, or existing experiments), and creates queue messages for the
    evaluator to process each one.
    """
    client = CLIClient()
    body: dict = {"associate_name": associate}
    if rubric:
        body["rubric_ids"] = rubric
    if all_rubrics:
        body["all_rubrics"] = True
    if sample:
        body["sample"] = sample
    if since:
        body["since"] = since
    if until:
        body["until"] = until
    if cascade:
        body["cascade"] = True
    if correlation_id:
        body["correlation_id"] = correlation_id
    if test_set:
        body["test_set_id"] = test_set
    if entity_type:
        body["entity_type"] = entity_type
    if experiment:
        body["experiment"] = experiment
    if limit:
        body["limit"] = limit
    if data:
        body["data"] = json.loads(data)

    result = client.post("/api/_eval/run", json=body)
    render(result)


@eval_app.command("list")
def list_runs(
    associate: str = typer.Option(None, "--associate", help="Filter by associate name"),
    status: str = typer.Option(None, "--status", help="pending, running, completed, failed"),
    since: str = typer.Option(None, "--since", help="Time window"),
    limit: int = typer.Option(20, "--limit"),
):
    """List evaluation runs."""
    client = CLIClient()
    params: dict = {"limit": limit}
    if associate:
        params["associate_name"] = associate
    if status:
        params["status"] = status
    if since:
        params["since"] = since
    result = client.get("/api/_eval/runs", params=params)
    render(result)


@eval_app.command("get")
def get_run(run_id: str):
    """Get evaluation run summary with aggregate scores."""
    client = CLIClient()
    result = client.get(f"/api/_eval/runs/{run_id}")
    render(result)


@eval_app.command("results")
def get_results(
    run_id: str,
    failed_only: bool = typer.Option(False, "--failed-only"),
    rule: str = typer.Option(None, "--rule", help="Filter by specific rule ID"),
):
    """Get per-item results for an evaluation run."""
    client = CLIClient()
    params: dict = {}
    if failed_only:
        params["failed_only"] = "true"
    if rule:
        params["rule_id"] = rule
    result = client.get(f"/api/_eval/runs/{run_id}/results", params=params)
    render(result)


@eval_app.command("complete")
def complete_run(run_id: str):
    """Aggregate results and complete a batch evaluation run."""
    client = CLIClient()
    result = client.post(f"/api/_eval/runs/{run_id}/complete", json={})
    render(result)


@eval_app.command("create-experiment")
def create_experiment(run_id: str):
    """Create a LangSmith Experiment from a completed evaluation run."""
    client = CLIClient()
    result = client.post(f"/api/_eval/runs/{run_id}/create-experiment", json={})
    render(result)


@eval_app.command("compare")
def compare_runs(run_id_1: str, run_id_2: str):
    """Compare two evaluation runs — side-by-side rule scores."""
    client = CLIClient()
    result = client.get(f"/api/_eval/compare/{run_id_1}/{run_id_2}")
    render(result)


@eval_app.command("stats")
def eval_stats(
    associate: str = typer.Option(None, "--associate", help="Filter by associate"),
    since: str = typer.Option(None, "--since", help="Time window"),
    group_by: str = typer.Option(None, "--group-by", help="rule or failure_attribution"),
):
    """Aggregate evaluation stats across runs."""
    client = CLIClient()
    params: dict = {}
    if associate:
        params["associate_name"] = associate
    if since:
        params["since"] = since
    if group_by:
        params["group_by"] = group_by
    result = client.get("/api/_eval/stats", params=params)
    render(result)


@eval_app.command("rubric-versions")
def rubric_versions(rubric_id: str):
    """List version history for a rubric."""
    client = CLIClient()
    result = client.get(f"/api/_eval/versions/Rubric/{rubric_id}")
    render(result)


@eval_app.command("rubric-at-version")
def rubric_at_version(rubric_id: str, version: int = typer.Option(..., "--version")):
    """Get a rubric at a specific version."""
    client = CLIClient()
    result = client.get(f"/api/_eval/versions/Rubric/{rubric_id}/at/{version}")
    render(result)

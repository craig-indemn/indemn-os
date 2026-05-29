"""Pin `indemn trace list --summary` translates to server-side exclude.

Pre-fix, `trace list` dumped the full row including `messages` (entire
conversation), `child_runs` (full LangGraph tree), and `inputs`/`outputs`.
A 20-trace list was unusable for scanning — single rows ran 100KB+ each.

`--summary` adds `exclude=messages,child_runs,inputs,outputs` to the
request. The auto-gen list route at `kernel/api/registration.py:339-411`
already supports `exclude=` (param at line 354, response filter at line
409-410). No server-side change needed for this CLI flag to work.

These tests pin the CLI behavior. The server-side exclude param is
already covered by the eval_routes test suite indirectly.
"""



def test_summary_flag_adds_exclude_param(monkeypatch):
    """When --summary is passed, exclude param lists the bulky fields."""
    from indemn_os import trace_commands

    captured = {}

    class FakeClient:
        def get(self, path, params=None):
            captured["path"] = path
            captured["params"] = params or {}
            return []

    monkeypatch.setattr(trace_commands, "CLIClient", lambda: FakeClient())
    monkeypatch.setattr(trace_commands, "render", lambda result, fmt=None: None)

    trace_commands.list_traces(
        associate=None,
        entity_type=None,
        status=None,
        execution_status=None,
        correlation_id=None,
        limit=20,
        summary=True,
    )

    exclude = captured["params"].get("exclude", "")
    assert "messages" in exclude
    assert "child_runs" in exclude
    assert "inputs" in exclude
    assert "outputs" in exclude


def test_no_summary_omits_exclude_param(monkeypatch):
    """Default behavior unchanged: without --summary, no exclude param."""
    from indemn_os import trace_commands

    captured = {}

    class FakeClient:
        def get(self, path, params=None):
            captured["params"] = params or {}
            return []

    monkeypatch.setattr(trace_commands, "CLIClient", lambda: FakeClient())
    monkeypatch.setattr(trace_commands, "render", lambda result, fmt=None: None)

    trace_commands.list_traces(
        associate=None,
        entity_type=None,
        status=None,
        execution_status=None,
        correlation_id=None,
        limit=20,
        summary=False,
    )

    assert "exclude" not in captured["params"]


def test_summary_combines_with_filters(monkeypatch):
    """--summary doesn't conflict with --associate or --filter — both ship."""
    from indemn_os import trace_commands

    captured = {}

    class FakeClient:
        def get(self, path, params=None):
            captured["params"] = params or {}
            return []

    monkeypatch.setattr(trace_commands, "CLIClient", lambda: FakeClient())
    monkeypatch.setattr(trace_commands, "render", lambda result, fmt=None: None)

    trace_commands.list_traces(
        associate="Touchpoint Synthesizer",
        entity_type=None,
        status=None,
        execution_status="success",
        correlation_id=None,
        limit=5,
        summary=True,
    )

    assert "exclude" in captured["params"]
    # Filter should also be present (via JSON-encoded filter field)
    import json
    parsed_filter = json.loads(captured["params"]["filter"])
    assert parsed_filter["associate_name"] == "Touchpoint Synthesizer"
    assert parsed_filter["execution_status"] == "success"

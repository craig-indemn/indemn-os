"""Tests for list endpoint --data filter propagation to the API's ?filter= param.

The regression: CLI's `list` command had no --data flag at all, so `indemn email list
--data '{"company":"69eb..."}'` silently ignored the filter (Typer error) and
`?data=...` as a raw URL query param reached the API but wasn't read by the
endpoint (which uses `filter` not `data`). The fix adds --data to the list command
and passes it as the `filter` query parameter.

Also: parse_filter should 400 on unknown fields (it always did — but was unreachable
without the CLI/URL wiring). Pin that behavior here.
"""

import inspect

import pytest


class TestListDataParam:
    """Pin the --data flag existence and propagation."""

    def test_list_cmd_has_data_parameter(self):
        """The list_cmd must accept --data and map to the filter query param."""
        from indemn_os import main as main_mod

        src = inspect.getsource(main_mod._register_entity_commands)
        assert '"--data"' in src
        assert 'params["filter"] = data' in src

    def test_data_param_maps_to_filter_query_param(self):
        """When --data is provided, it becomes the `filter` query parameter."""
        from indemn_os import main as main_mod

        src = inspect.getsource(main_mod._register_entity_commands)
        assert 'params["filter"] = data' in src


class TestParseFilterValidation:
    """Pin parse_filter's unknown-field → 400 behavior."""

    def test_unknown_field_raises_400(self):
        """A totally made-up field should raise HTTPException 400."""
        from unittest.mock import MagicMock

        from fastapi import HTTPException

        from kernel.api._filter_safelist import parse_filter

        # Mock entity_cls with known fields
        entity_cls = MagicMock()
        entity_cls.model_fields = {
            "name": MagicMock(annotation=str, alias=None),
            "status": MagicMock(annotation=str, alias=None),
        }
        # Access alias attr
        for info in entity_cls.model_fields.values():
            info.alias = None

        with pytest.raises(HTTPException) as exc_info:
            parse_filter(entity_cls, "TestEntity", '{"totally_made_up_field": "foo"}')
        assert exc_info.value.status_code == 400
        assert "Unknown field" in str(exc_info.value.detail)

    def test_known_field_passes(self):
        """A valid field should not raise."""
        from unittest.mock import MagicMock

        from kernel.api._filter_safelist import parse_filter

        entity_cls = MagicMock()
        info = MagicMock(annotation=str)
        info.alias = None
        entity_cls.model_fields = {"source_entity_type": info}

        result = parse_filter(entity_cls, "Touchpoint", '{"source_entity_type": "Email"}')
        assert result == {"source_entity_type": "Email"}

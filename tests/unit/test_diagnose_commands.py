"""Tests for the `indemn diagnose` command group — shape + endpoint contract pins.

Tests verify:
- CLI command shapes (sub-commands exist, call correct endpoints)
- API endpoint registration (routes exist with correct prefixes)
- Response contract shapes
"""

import inspect


class TestDiagnoseCommandShapes:
    """Pin CLI command existence and structure."""

    def test_diagnose_app_exists(self):
        from indemn_os.diagnose_commands import diagnose_app

        assert diagnose_app is not None
        assert diagnose_app.info.name == "diagnose"

    def test_diagnose_actor_command_exists(self):
        from indemn_os.diagnose_commands import diagnose_actor

        assert callable(diagnose_actor)
        sig = inspect.signature(diagnose_actor)
        assert "actor_id" in sig.parameters
        assert "limit" in sig.parameters

    def test_diagnose_message_command_exists(self):
        from indemn_os.diagnose_commands import diagnose_message

        assert callable(diagnose_message)
        sig = inspect.signature(diagnose_message)
        assert "message_id" in sig.parameters

    def test_diagnose_cron_command_exists(self):
        from indemn_os.diagnose_commands import diagnose_cron

        assert callable(diagnose_cron)
        sig = inspect.signature(diagnose_cron)
        assert "actor_name" in sig.parameters
        assert "limit" in sig.parameters

    def test_diagnose_actor_hits_correct_endpoint(self):
        src = inspect.getsource(
            __import__("indemn_os.diagnose_commands", fromlist=["diagnose_actor"]).diagnose_actor
        )
        assert "/api/_diagnose/actor/" in src

    def test_diagnose_message_hits_correct_endpoint(self):
        src = inspect.getsource(
            __import__(
                "indemn_os.diagnose_commands", fromlist=["diagnose_message"]
            ).diagnose_message
        )
        assert "/api/_diagnose/message/" in src

    def test_diagnose_cron_hits_correct_endpoint(self):
        src = inspect.getsource(
            __import__("indemn_os.diagnose_commands", fromlist=["diagnose_cron"]).diagnose_cron
        )
        assert "/api/_diagnose/cron" in src


class TestDiagnoseRouteShapes:
    """Pin API route registration and handler signatures."""

    def test_diagnose_router_prefix(self):
        from kernel.api.diagnose_routes import diagnose_router

        assert diagnose_router.prefix == "/api/_diagnose"

    def test_actor_endpoint_registered(self):
        from kernel.api.diagnose_routes import diagnose_router

        routes = [r.path for r in diagnose_router.routes]
        assert any("actor" in r for r in routes)

    def test_message_endpoint_registered(self):
        from kernel.api.diagnose_routes import diagnose_router

        routes = [r.path for r in diagnose_router.routes]
        assert any("message" in r for r in routes)

    def test_cron_endpoint_registered(self):
        from kernel.api.diagnose_routes import diagnose_router

        routes = [r.path for r in diagnose_router.routes]
        assert any("cron" in r for r in routes)

    def test_diagnose_router_included_in_app(self):
        src = inspect.getsource(__import__("kernel.api.app", fromlist=["create_app"]))
        assert "diagnose_router" in src

"""First-use diagnostics use synthetic credentials and read-only lab requests."""
from contextlib import ExitStack
import importlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from caprover_operator.client import CapRoverClient
from caprover_operator.lab import FakeCapRover


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))
plugin = importlib.import_module(ROOT.name)
SECRET = "synthetic-private-diagnostic-token"


class Context:
    def __init__(self, config=None):
        self.config = config or {}
        self.tools = {}

    def get_config(self, key, default=None):
        return self.config.get(key, default)

    def register_tool(self, **values):
        self.tools[values["name"]] = values

    def register_hook(self, *args):
        pass

    def register_middleware(self, *args):
        pass


class DoctorTests(unittest.TestCase):
    def setUp(self):
        self.ctx = Context({"url": "https://captain.example.invalid", "app_name": "demo-app"})
        plugin.register(self.ctx)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.runtime = self.stack.enter_context(patch.object(plugin, "_make_runtime", side_effect=AssertionError("doctor must not open a ledger")))
        self.token = self.stack.enter_context(patch.object(plugin, "_token_provider", return_value=SECRET))

    def call(self, args=None):
        return json.loads(self.ctx.tools["caprover_doctor"]["handler"]({} if args is None else args))

    def lab_client(self, lab):
        self.ctx.config.update(url=lab.base_url, app_name=lab.app, health_url=lab.health_url)
        self.token.return_value = lab.token
        def construct(settings, token_provider):
            return CapRoverClient(settings.url, token_provider, b"lab-only-diagnostic-key-32-bytes!!",
                                  allowed_apps=[settings.app], health_urls={settings.app: settings.health_url},
                                  timeout=0.5, allow_insecure_local=True)
        self.stack.enter_context(patch.object(plugin, "_make_diagnostic_client", side_effect=construct))

    def test_missing_configuration_is_actionable_without_network_or_ledger(self):
        self.ctx.config = {}
        self.token.side_effect = plugin._Refused("token_missing")
        with patch.object(plugin, "_make_diagnostic_client") as client:
            result = self.call()
        self.assertEqual(result["status"], "needs_configuration")
        self.assertEqual(result["checks"]["configuration"]["missing_fields"], ["url", "app_name"])
        self.assertEqual(result["checks"]["credential"]["status"], "missing")
        self.assertEqual(result["guidance"]["next_step"]["tool"], "caprover_doctor")
        self.assertFalse(result["guidance"]["automatic"])
        client.assert_not_called()
        self.runtime.assert_not_called()

    def test_missing_credential_never_calls_the_api(self):
        self.token.side_effect = plugin._Refused("token_missing")
        with patch.object(plugin, "_make_diagnostic_client") as client:
            result = self.call()
        self.assertEqual(result["status"], "needs_credentials")
        self.assertEqual(result["checks"]["connection"]["status"], "not_checked")
        client.return_value.snapshot.assert_not_called()
        client.return_value.health.assert_not_called()

    def test_diagnostic_unavailable_does_not_echo_exception_or_secret(self):
        with patch.object(plugin, "_make_diagnostic_client", side_effect=RuntimeError(SECRET)):
            result = self.call()
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["checks"]["credential"]["status"], "present_unverified")
        self.assertEqual(result["guidance"]["code"], "diagnostic_unavailable")
        self.assertNotIn(SECRET, json.dumps(result))
        self.assertFalse(result["checks"]["approval"]["deployment_authorized"])

    def test_invalid_owner_url_does_not_echo_embedded_credential(self):
        self.ctx.config["url"] = "https://captain.example.invalid/?token=" + SECRET
        result = self.call()
        self.assertEqual(result["status"], "needs_configuration")
        self.assertEqual(result["checks"]["configuration"]["status"], "invalid")
        self.assertNotIn(SECRET, json.dumps(result))

    def test_model_cannot_supply_target_token_or_approval(self):
        for args in ({"url": "https://other.invalid"}, {"token": SECRET}, {"approved": True}):
            result = self.call(args)
            self.assertEqual(result["error"]["code"], "invalid_input")
            self.assertNotIn(SECRET, json.dumps(result))
        self.token.assert_not_called()

    def test_valid_gets_allow_planning_only_and_do_not_store_response_data(self):
        with FakeCapRover() as lab:
            self.lab_client(lab)
            result = self.call()
            self.assertEqual(result["status"], "ready_for_planning")
            self.assertEqual(result["checks"]["credential"]["status"], "accepted_by_api")
            self.assertEqual(result["checks"]["runtime"]["status"], "unavailable")
            self.assertEqual(result["checks"]["health"]["status_code"], 200)
            self.assertEqual(result["checks"]["approval"]["status"], "not_tested")
            self.assertFalse(result["checks"]["approval"]["deployment_authorized"])
            self.assertEqual(result["readiness_scope"], "planning_only")
            self.assertEqual(result["guidance"]["next_step"]["required_input"], ["image"])
            self.assertEqual(lab.trigger_count, 0)
            self.assertTrue(all(request["method"] == "GET" for request in lab.requests))
            for private in (lab.token, "synthetic-env-value", "SYNTHETIC_SECRET", lab.health_body.decode()):
                self.assertNotIn(private, json.dumps(result))
            self.runtime.assert_not_called()

    def test_token_presence_does_not_become_validity_after_rejected_get(self):
        with FakeCapRover() as lab:
            self.lab_client(lab)
            self.token.return_value = SECRET
            result = self.call()
            self.assertEqual(result["status"], "unavailable")
            self.assertEqual(result["checks"]["credential"]["status"], "rejected")
            self.assertEqual(result["checks"]["app"]["status"], "not_confirmed")
            self.assertEqual(lab.trigger_count, 0)
            self.assertNotIn(SECRET, json.dumps(result))

    def test_permission_denial_does_not_claim_the_token_is_invalid(self):
        with FakeCapRover() as lab:
            self.lab_client(lab)
            lab.http_statuses["/api/v2/user/apps/appDefinitions/"] = 403
            result = self.call()
            self.assertEqual(result["status"], "unavailable")
            self.assertEqual(result["checks"]["credential"]["status"], "present_unverified")
            self.assertEqual(result["checks"]["connection"]["code"], "authorization_denied")
            self.assertIn("does not prove", result["guidance"]["message"])
            self.assertEqual(lab.trigger_count, 0)

    def test_unavailable_secret_scope_is_not_reported_as_a_missing_secret(self):
        self.token.side_effect = RuntimeError(SECRET)
        result = self.call()
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["checks"]["credential"]["status"], "unavailable")
        self.assertNotIn(SECRET, json.dumps(result))

    def test_missing_app_distinguishes_successful_authenticated_get(self):
        with FakeCapRover() as lab:
            self.lab_client(lab)
            lab.definition["appName"] = "other-app"
            result = self.call()
            self.assertEqual(result["status"], "unavailable")
            self.assertEqual(result["checks"]["app"]["status"], "missing")
            self.assertEqual(result["checks"]["credential"]["status"], "accepted_by_api")
            self.assertIn("app_name", result["guidance"]["message"])
            self.assertEqual(lab.trigger_count, 0)

    def test_building_or_failed_health_never_reports_ready_for_planning(self):
        with FakeCapRover() as lab:
            self.lab_client(lab)
            lab.build_status["isAppBuilding"] = True
            self.assertEqual(self.call()["status"], "needs_attention")
            lab.build_status["isAppBuilding"] = False
            lab.health_status = 503
            result = self.call()
            self.assertEqual(result["status"], "needs_attention")
            self.assertEqual(result["checks"]["health"]["status_code"], 503)
            self.assertEqual(lab.trigger_count, 0)

    def test_runtime_capability_is_not_a_deployment_or_approval_proof(self):
        with FakeCapRover() as lab:
            self.lab_client(lab)
            lab.runtime_available = True
            result = self.call()
            self.assertEqual(result["status"], "ready_for_planning")
            self.assertEqual(result["checks"]["runtime"]["current_task_count"], 1)
            self.assertNotIn("runtime_rollout_proven", json.dumps(result))
            self.assertFalse(result["checks"]["approval"]["deployment_authorized"])
            self.assertEqual(lab.trigger_count, 0)


@unittest.skipUnless(importlib.util.find_spec("hermes_cli"), "requires an installed Hermes runtime")
class NativeFirstUseTests(unittest.TestCase):
    def test_installed_without_settings_or_token_exposes_read_only_doctor(self):
        source = Path(importlib.util.find_spec("hermes_cli").origin).parents[1]
        with tempfile.TemporaryDirectory(prefix="caprover-first-use-") as temporary:
            home = Path(temporary).resolve()
            shutil.copytree(ROOT, home / "plugins" / "caprover-operator",
                            ignore=shutil.ignore_patterns(".git", "__pycache__", "tests", "docs", "demo", ".venv"))
            (home / "config.yaml").write_text("plugins:\n  enabled: [caprover-operator]\n  isolation: in_process\n")
            environment = {**os.environ, "HERMES_HOME": str(home), "HERMES_ENABLE_PROJECT_PLUGINS": "0", "PYTHONDONTWRITEBYTECODE": "1"}
            environment.pop("CAPROVER_OPERATOR_TOKEN", None)
            program = r'''
import json, os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from hermes_cli.plugins import get_plugin_manager
get_plugin_manager().discover_and_load()
from tools.registry import registry
assert registry.get_entry("caprover_doctor") is not None
from model_tools import handle_function_call
result = json.loads(handle_function_call("caprover_doctor", {}, task_id="first-use", session_id="first-use",
                                       turn_id="first-use", tool_call_id="first-use"))
assert not (Path(os.environ["HERMES_HOME"]) / "plugin-data" / "caprover-operator").exists()
print("FIRST_USE_RESULT=" + json.dumps(result))
'''
            completed = subprocess.run([sys.executable, "-c", program, str(source)], env=environment,
                                       capture_output=True, text=True, timeout=30, check=False)
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            line = next(line for line in completed.stdout.splitlines() if line.startswith("FIRST_USE_RESULT="))
            result = json.loads(line.partition("=")[2])
            self.assertEqual(result["status"], "needs_configuration")
            self.assertEqual(result["checks"]["credential"]["status"], "missing")
            self.assertFalse(result["checks"]["approval"]["deployment_authorized"])


if __name__ == "__main__":
    unittest.main()

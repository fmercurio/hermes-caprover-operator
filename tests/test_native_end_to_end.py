"""Native Hermes -> real adapter/engine/HTTP -> disposable protocol fixture.

The server is FakeCapRover, NOT a CapRover installation or Docker deployment.
Each tool invocation starts a fresh Hermes process against the same private
ledger. The only substitute in the copied plugin is its client constructor:
it enables the client's explicit HTTP-loopback lab option for this fixture.
Neither the Operator nor _make_runtime nor Hermes core is monkeypatched.
Human approval is simulated by the host's approval callback, never tool args.
"""

import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

from fake_caprover import FakeCapRover, NEW_IMAGE


ROOT = Path(__file__).resolve().parents[1]
HERMES = importlib.util.find_spec("hermes_cli")

# A subprocess keeps Hermes global registries and approval context isolated
# from the test runner and proves persistence across complete runtime restarts.
_DISPATCH = r'''
import importlib, json, os, stat, sys
from pathlib import Path
source, tool, arguments, mode = sys.argv[1:]
sys.path.insert(0, source)
from hermes_cli.plugins import get_plugin_manager
manager = get_plugin_manager()
manager.discover_and_load()
from tools.registry import registry
entry = registry.get_entry("caprover_apply")
assert entry is not None, "native plugin registration failed"
adapter = entry.handler.__self__
module = sys.modules[adapter.__class__.__module__]
client_module = importlib.import_module(module.__name__ + ".caprover_operator.client")
real_client = client_module.CapRoverClient
class LoopbackFixtureClient(real_client):
    def __init__(self, base_url, *args, **kwargs):
        from urllib.parse import urlsplit
        assert urlsplit(base_url).hostname == "127.0.0.1"
        kwargs["allow_insecure_local"] = True
        super().__init__(base_url, *args, **kwargs)
if mode != "storage":
    client_module.CapRoverClient = LoopbackFixtureClient
if mode == "storage":
    from plugins.plugin_storage import plugin_data_dir
    previous_mask = os.umask(0o022)
    try:
        storage = plugin_data_dir("caprover-operator")
    finally:
        os.umask(previous_mask)
    assert stat.S_IMODE(storage.stat().st_mode) == 0o755
from tools.terminal_tool import set_approval_callback
from tools.approval_context import set_current_session_key
from tools.approval import enable_session_yolo
from model_tools import handle_function_call
set_current_session_key("synthetic-native-e2e-session")
prompts = []
def simulated_human_response(*args, **kwargs):
    # This is an explicit host test fixture, not a production approval shortcut.
    prompts.append(True)
    return "once"
set_approval_callback(simulated_human_response)
if mode == "yolo":
    enable_session_yolo("synthetic-native-e2e-session")
options = dict(task_id="synthetic-task", session_id="synthetic-session",
               turn_id="synthetic-turn", tool_call_id="synthetic-call")
result = handle_function_call(tool, json.loads(arguments), **options)
if mode == "storage":
    assert stat.S_IMODE(storage.stat().st_mode) == 0o700
print("NATIVE_E2E_RESULT=" + json.dumps({"result": json.loads(result), "prompts": len(prompts)}))
'''


@unittest.skipUnless(HERMES, "requires an installed Hermes runtime")
class NativeEndToEndTests(unittest.TestCase):
    def test_public_default_storage_and_real_runtime_need_no_network_for_status(self):
        """Public mkdir(0755) -> actual runtime -> owner-only real ledger."""
        source = Path(HERMES.origin).parents[1]
        with tempfile.TemporaryDirectory(prefix="caprover-native-storage-") as temporary:
            home = Path(temporary).resolve()
            shutil.copytree(
                ROOT, home / "plugins" / "caprover-operator",
                ignore=shutil.ignore_patterns(".git", "__pycache__", "tests", "docs", ".venv"),
            )
            (home / "config.yaml").write_text(json.dumps({
                "plugins": {"enabled": ["caprover-operator"], "isolation": "in_process",
                            "entries": {"caprover-operator": {"settings": {
                                "url": "https://captain.example.invalid", "app_name": "demo-app"}}}},
                "approvals": {"mode": "manual"},
            }))
            environment = {**os.environ, "HERMES_HOME": str(home), "HERMES_INTERACTIVE": "1",
                           "PYTHONDONTWRITEBYTECODE": "1"}
            # No token is needed: constructing a runtime and reading the local
            # ledger must not resolve a credential or contact the server.
            environment.pop("CAPROVER_OPERATOR_TOKEN", None)
            completed = subprocess.run(
                [sys.executable, "-c", _DISPATCH, str(source), "caprover_status", "{}", "storage"],
                env=environment, text=True, capture_output=True, timeout=30, check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            line = next(line for line in completed.stdout.splitlines() if line.startswith("NATIVE_E2E_RESULT="))
            result = json.loads(line.partition("=")[2])
            self.assertEqual(result["result"]["operations"], [])
            self.assertEqual(result["prompts"], 0)

    def test_native_approval_drift_lost_ack_restart_reconcile_and_yolo(self):
        """Real public hooks, secret scope, storage, ledger and wire protocol."""
        source = Path(HERMES.origin).parents[1]
        with tempfile.TemporaryDirectory(prefix="caprover-native-e2e-") as temporary, FakeCapRover() as server:
            home = Path(temporary).resolve()
            shutil.copytree(
                ROOT, home / "plugins" / "caprover-operator",
                ignore=shutil.ignore_patterns(".git", "__pycache__", "tests", "docs", ".venv"),
            )
            config = {"plugins": {"enabled": ["caprover-operator"], "isolation": "in_process",
                                  "entries": {"caprover-operator": {"settings": {
                                      "url": server.base_url, "app_name": server.app,
                                      "health_url": server.health_url}}}},
                      "approvals": {"mode": "manual"}}
            (home / "config.yaml").write_text(json.dumps(config))

            # Reproduce the public plugin_data_dir default (umask 022): the
            # production adapter must tighten this before opening the engine.
            data_dir = home / "plugin-data" / "caprover-operator"
            data_dir.mkdir(parents=True, mode=0o755)
            data_dir.chmod(0o755)
            self.assertEqual(stat.S_IMODE(data_dir.stat().st_mode), 0o755)
            environment = {**os.environ, "HERMES_HOME": str(home), "HERMES_INTERACTIVE": "1",
                           "CAPROVER_OPERATOR_TOKEN": server.token, "PYTHONDONTWRITEBYTECODE": "1"}
            environment.pop("HERMES_YOLO_MODE", None)

            def call(tool, arguments, mode="manual"):
                completed = subprocess.run(
                    [sys.executable, "-c", _DISPATCH, str(source), tool, json.dumps(arguments), mode],
                    env=environment, text=True, capture_output=True, timeout=30, check=False,
                )
                self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
                lines = [line for line in completed.stdout.splitlines() if line.startswith("NATIVE_E2E_RESULT=")]
                self.assertEqual(len(lines), 1, completed.stdout + completed.stderr)
                return json.loads(lines[0].partition("=")[2])

            planned = call("caprover_plan", {"image": NEW_IMAGE})["result"]
            self.assertEqual(planned["status"], "planned")
            self.assertEqual(stat.S_IMODE(data_dir.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE((data_dir / "fingerprint.key").stat().st_mode), 0o600)

            server.drift(instanceCount=2)
            drifted = call("caprover_apply", {"plan_id": planned["plan_id"]})
            self.assertEqual(drifted["prompts"], 1)
            self.assertEqual(drifted["result"]["status"], "consumed_drift")
            self.assertFalse(drifted["result"]["checks"]["post_attempted"])
            self.assertEqual(server.trigger_count, 0)

            fresh = call("caprover_plan", {"image": NEW_IMAGE})["result"]
            bypass_plan = call("caprover_plan", {"image": NEW_IMAGE})["result"]
            server.lose_next_ack = True
            uncertain = call("caprover_apply", {"plan_id": fresh["plan_id"]})
            self.assertEqual(uncertain["prompts"], 1)
            self.assertEqual(uncertain["result"]["status"], "reconcile_required")
            self.assertEqual(uncertain["result"]["trigger_outcome"], "uncertain")
            self.assertEqual(server.trigger_count, 1)

            replay = call("caprover_apply", {"plan_id": fresh["plan_id"]})
            self.assertIn("error", replay["result"])
            self.assertEqual(replay["prompts"], 0)
            self.assertEqual(server.trigger_count, 1)

            # A different still-planned intent reaches Hermes' YOLO branch.
            # There is no post-approval receipt, so our handler cannot write.
            bypassed = call("caprover_apply", {"plan_id": bypass_plan["plan_id"]}, mode="yolo")
            self.assertEqual(bypassed["prompts"], 0)
            self.assertEqual(bypassed["result"]["error"]["code"], "native_approval_required")
            self.assertEqual(server.trigger_count, 1)

            # A fresh complete Hermes process reopens the durable ledger and
            # observes the fixture's applied image plus newer generation.
            reconciled = call("caprover_reconcile", {"plan_id": fresh["plan_id"]})
            self.assertEqual(reconciled["prompts"], 0)
            receipt = reconciled["result"]
            self.assertEqual(receipt["status"], "metadata_http_observed")
            self.assertFalse(receipt["checks"]["runtime_rollout_proven"])
            self.assertTrue(receipt["checks"]["newer_version"])
            self.assertTrue(receipt["checks"]["exact_image"])
            self.assertTrue(receipt["checks"]["http_health"]["ok"])
            self.assertEqual(receipt["checks"]["replica_evidence"], "unavailable")
            self.assertEqual(server.trigger_count, 1)
            posts = [request for request in server.requests if request["method"] == "POST"]
            self.assertEqual(len(posts), 1)
            encoded = json.dumps([planned, drifted, uncertain, replay, bypassed, reconciled])
            for private in (server.token, "synthetic-env-value", server.health_body.decode()):
                self.assertNotIn(private, encoded)

            # Independently exercise the optional current-task capability. The
            # fixture changes task identity only when explicitly synchronized;
            # updating its version metadata alone never claims convergence.
            server.runtime_available = True
            server.sync_runtime_to_definition()
            runtime_image = "ghcr.io/example/demo@sha256:" + "3" * 64
            runtime_plan = call("caprover_plan", {"image": runtime_image})["result"]
            requested = call("caprover_apply", {"plan_id": runtime_plan["plan_id"]})
            self.assertEqual(requested["result"]["status"], "reconcile_required")
            self.assertEqual(server.trigger_count, 2)
            server.sync_runtime_to_definition()
            runtime_receipt = call("caprover_reconcile", {"plan_id": runtime_plan["plan_id"]})["result"]
            self.assertEqual(runtime_receipt["status"], "verified")
            self.assertTrue(runtime_receipt["checks"]["runtime_rollout_proven"])
            self.assertEqual(runtime_receipt["checks"]["running_replicas"], 2)
            self.assertEqual(server.trigger_count, 2)


if __name__ == "__main__":
    unittest.main()

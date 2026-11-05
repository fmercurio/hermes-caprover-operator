"""Synthetic tests for approval provenance and native adapter boundaries.

No Hermes home, network service or credential store is touched by these tests.
"""

from contextlib import contextmanager
from copy import deepcopy
import importlib
import json
from pathlib import Path
import stat
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))
plugin = importlib.import_module(ROOT.name)

IMAGE = "registry.example.test/demo@sha256:" + "a" * 64


class Context:
    def __init__(self):
        self.tools = {}
        self.hooks = {}
        self.middleware = {}
        self.reads = 0
        self.config = {"url": "https://captain.example.test", "app_name": "demo", "health_url": "", "plan_ttl_seconds": 300}

    def get_config(self, key, default=None):
        self.reads += 1
        return self.config.get(key, default)

    def register_tool(self, **kwargs):
        self.tools[kwargs["name"]] = kwargs

    def register_hook(self, name, callback):
        self.hooks[name] = callback

    def register_middleware(self, name, callback):
        self.middleware[name] = callback


class Operator:
    def __init__(self):
        self.saved = {
            "plan_id": "plan-1234", "plan_hash": "1" * 64,
            "target": "https://captain.example.test", "app": "demo", "image": IMAGE,
            "baseline": {"version": 4, "replicas": 1}, "status": "planned",
        }
        self.writes = 0
        self.apply_calls = 0
        self.plan_calls = []
        self.observations = 0
        self.authorization_plan_change = False
        self.lock = threading.Lock()

    def get_plan(self, plan_id):
        if plan_id != self.saved["plan_id"]:
            raise ValueError("unknown plan")
        return deepcopy(self.saved)

    def plan(self, app, image, ttl_seconds=300):
        self.plan_calls.append((app, image, ttl_seconds))
        return deepcopy(self.saved)

    def apply(self, plan_id, authorize):
        with self.lock:
            self.apply_calls += 1
            actual = deepcopy(self.saved)
            if self.authorization_plan_change:
                actual["plan_hash"] = "3" * 64
            if not authorize(actual):
                return {"status": "authorization_denied"}
            self.writes += 1
            return {"status": "reconcile_required", "plan_id": plan_id}

    def reconcile(self, plan_id):
        self.observations += 1
        return {"status": "deployment_observed", "plan_id": plan_id}

    def status(self, plan_id=None, limit=20):
        return deepcopy(self.saved) if plan_id else {"plans": [deepcopy(self.saved)]}


class HermesAdapterTests(unittest.TestCase):
    def setUp(self):
        self.ctx = Context()
        self.operator = Operator()
        self.scope = "/synthetic/profile/plugin-data/caprover-operator"
        self.failure = None
        self.ids = {"session_id": "session-1", "turn_id": "turn-1", "tool_call_id": "call-1"}
        self.args = {"plan_id": self.operator.saved["plan_id"]}
        self.runtime_patch = patch.object(plugin, "_make_runtime", self.runtime)
        self.runtime_patch.start()
        self.addCleanup(self.runtime_patch.stop)
        plugin.register(self.ctx)

    @contextmanager
    def runtime(self, settings):
        if self.failure:
            raise self.failure
        target = settings.url
        binding = plugin._Binding(self.scope, target, plugin._configuration_hash(settings, target))
        yield plugin._Runtime(self.operator, settings, binding)

    def handler(self, name, args=None, **kwargs):
        return self.ctx.tools[name]["handler"](self.args if args is None else args, **kwargs)

    def before(self, args=None, ids=None):
        return self.ctx.hooks["pre_tool_call"](
            tool_name="caprover_apply", args=self.args if args is None else args,
            **(self.ids if ids is None else ids),
        )

    def approve(self, *, choice="once", decided_by=None, ids=None, rule=None):
        directive = self.before()
        self.assertEqual(directive["action"], "approve")
        pattern = rule or "plugin_rule:" + directive["rule_key"]
        self.ctx.hooks["post_approval_response"](
            pattern_key=pattern, pattern_keys=[pattern], choice=choice,
            decided_by=decided_by, **(self.ids if ids is None else ids),
        )
        return directive

    def execute(self, *, ids=None, args=None, next_call=None):
        context = self.ids if ids is None else ids
        callback = next_call or (lambda values: self.handler("caprover_apply", values, session_id=context.get("session_id", "")))
        return self.ctx.middleware["tool_execution"](
            tool_name="caprover_apply", args=self.args if args is None else args,
            next_call=callback, **context,
        )

    def assert_refused(self, result):
        self.assertIn("error", json.loads(result))
        self.assertEqual(self.operator.writes, 0)

    def test_registration_does_not_read_settings_storage_or_credentials(self):
        ctx = Context()
        with patch.object(plugin, "_make_runtime", side_effect=AssertionError("registration I/O")):
            plugin.register(ctx)
        self.assertEqual(ctx.reads, 0)
        self.assertEqual(set(ctx.tools), {"caprover_doctor", "caprover_plan", "caprover_apply", "caprover_reconcile", "caprover_status"})
        self.assertEqual(set(ctx.hooks), {"pre_tool_call", "post_approval_response"})
        self.assertEqual(set(ctx.middleware), {"tool_execution"})

    def test_model_cannot_supply_approval_or_choose_server_and_app(self):
        for extra in ({"approved": True}, {"url": "https://other.example.test"}, {"app": "other"}):
            with self.subTest(extra=extra):
                forged = {**self.args, **extra}
                self.assertEqual(self.before(forged)["action"], "block")
                self.assert_refused(self.handler("caprover_apply", forged, session_id="session-1"))
        schema = self.ctx.tools["caprover_apply"]["schema"]["parameters"]
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(set(schema["properties"]), {"plan_id"})

    def test_handler_without_native_hooks_cannot_apply(self):
        self.assert_refused(self.handler("caprover_apply", session_id="session-1"))

    def test_pre_hook_does_not_count_as_human_approval(self):
        self.assertEqual(self.before()["action"], "approve")
        self.assert_refused(self.execute())
        self.assertEqual(self.operator.apply_calls, 0)

    def test_human_choice_without_matching_pre_hook_has_no_authority(self):
        self.ctx.hooks["post_approval_response"](
            pattern_key="plugin_rule:invented", pattern_keys=["plugin_rule:invented"],
            choice="once", **self.ids,
        )
        self.assert_refused(self.execute())

    def test_approval_without_execution_middleware_cannot_apply(self):
        self.approve()
        self.assert_refused(self.handler("caprover_apply", session_id="session-1"))

    def test_actual_human_choice_permits_one_matching_call(self):
        self.approve()
        self.assertEqual(json.loads(self.execute())["status"], "reconcile_required")
        self.assertEqual(self.operator.writes, 1)
        self.assertIn("error", json.loads(self.execute()))
        self.assertEqual(self.operator.writes, 1)

    def test_denial_timeout_smart_and_auxiliary_choices_do_not_authorize(self):
        cases = [("deny", None), ("timeout", None), ("cancelled", None), ("smart_ALLOW", "aux_llm"), ("once", "aux_llm")]
        for index, (choice, decided_by) in enumerate(cases):
            with self.subTest(choice=choice, decided_by=decided_by):
                self.ids["tool_call_id"] = f"call-{index}"
                self.approve(choice=choice, decided_by=decided_by)
                self.assert_refused(self.execute())
        self.assertEqual(self.operator.apply_calls, 0)

    def test_other_approval_pattern_cannot_authorize_this_plan(self):
        self.approve(rule="plugin_rule:different-plan")
        self.assert_refused(self.execute())

    def test_ticket_cannot_cross_calls_turns_or_sessions(self):
        for field in self.ids:
            with self.subTest(field=field):
                self.ids["tool_call_id"] = "owner-call-" + field
                self.approve()
                wrong = {**self.ids, field: "other"}
                self.assert_refused(self.execute(ids=wrong))

    def test_config_change_after_approval_prevents_apply(self):
        self.approve()
        self.ctx.config["health_url"] = "https://app.example.test/health"
        self.assert_refused(self.execute())
        self.assertEqual(self.operator.apply_calls, 0)

    def test_plan_hash_change_after_approval_prevents_apply(self):
        self.approve()
        self.operator.saved["plan_hash"] = "2" * 64
        self.assert_refused(self.execute())
        self.assertEqual(self.operator.apply_calls, 0)

    def test_profile_storage_change_prevents_apply(self):
        self.approve()
        self.scope = "/synthetic/other-profile/plugin-data/caprover-operator"
        self.assert_refused(self.execute())

    def test_core_authorization_rechecks_the_actual_plan(self):
        self.approve()
        self.operator.authorization_plan_change = True
        self.assertEqual(json.loads(self.execute())["status"], "authorization_denied")
        self.assertEqual(self.operator.writes, 0)

    def test_missing_event_identifiers_fail_closed(self):
        self.assertEqual(self.before(ids={})["action"], "block")
        self.assert_refused(self.execute(ids={}))

    def test_permit_is_cleared_even_if_downstream_raises(self):
        self.approve()

        def broken(values):
            raise RuntimeError("synthetic downstream failure")

        with self.assertRaises(RuntimeError):
            self.execute(next_call=broken)
        self.assert_refused(self.handler("caprover_apply", session_id="session-1"))

    def test_unrelated_tools_continue_once(self):
        seen = []
        result = self.ctx.middleware["tool_execution"](
            tool_name="read_file", args={"path": "synthetic"},
            next_call=lambda values: seen.append(values) or "unchanged",
        )
        self.assertEqual(result, "unchanged")
        self.assertEqual(seen, [{"path": "synthetic"}])

    def test_planning_uses_only_owner_configured_target(self):
        self.handler("caprover_plan", {"image": IMAGE})
        self.assertEqual(self.operator.plan_calls, [("demo", IMAGE, 300)])
        self.assert_refused(self.handler("caprover_plan", {"image": IMAGE, "app": "other"}))

    def test_secret_shaped_unexpected_exception_is_not_returned(self):
        self.failure = RuntimeError("private-token-must-not-appear")
        result = self.handler("caprover_status", {})
        self.assert_refused(result)
        self.assertNotIn("private-token-must-not-appear", result)

    def test_concurrent_consumers_cannot_reuse_one_approval(self):
        self.approve()
        barrier = threading.Barrier(2)
        results = []

        def worker():
            barrier.wait()
            results.append(self.execute())

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive())
        self.assertEqual(self.operator.writes, 1)
        self.assertEqual(sum("error" in json.loads(result) for result in results), 1)

    def test_ticket_expires_without_refreshing_plan_authority(self):
        self.approve()
        with patch.object(plugin.time, "monotonic", return_value=1e30):
            self.assert_refused(self.execute())


class PrivateStorageTests(unittest.TestCase):
    def test_hermes_default_directory_is_tightened_before_engine_use(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary).resolve() / "plugin-data"
            directory.mkdir(mode=0o755)
            directory.chmod(0o755)
            self.assertEqual(plugin._prepare_private_directory(directory), directory)
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)

    def test_symlink_or_writable_directory_is_not_chmodded(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            target = root / "target"
            target.mkdir(mode=0o755)
            target.chmod(0o755)
            link = root / "link"
            link.symlink_to(target, target_is_directory=True)
            with self.assertRaises(plugin._Refused):
                plugin._prepare_private_directory(link)
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o755)
            target.chmod(0o777)
            with self.assertRaises(plugin._Refused):
                plugin._prepare_private_directory(target)
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o777)


@unittest.skipUnless(importlib.util.find_spec("hermes_cli"), "requires an installed Hermes runtime")
class NativeHermesApprovalTests(unittest.TestCase):
    def test_real_gate_events_and_dispatch_refuse_bypasses(self):
        """Exercise Hermes itself; replace only our network/engine boundary."""
        import subprocess

        source = Path(importlib.util.find_spec("hermes_cli").origin).parents[1]
        program = r'''
from contextlib import contextmanager
from pathlib import Path
import json, os, shutil, sys, tempfile
repo, source = map(Path, sys.argv[1:])
with tempfile.TemporaryDirectory(prefix="caprover-native-approval-") as directory:
    home = Path(directory)
    os.environ.update(HERMES_HOME=str(home), CAPROVER_OPERATOR_TOKEN="synthetic-validation-token", HERMES_INTERACTIVE="1")
    os.environ.pop("HERMES_YOLO_MODE", None)
    shutil.copytree(repo, home / "plugins" / "caprover-operator", ignore=shutil.ignore_patterns(".git", "__pycache__", "tests", "docs"))
    (home / "config.yaml").write_text("plugins:\n  enabled: [caprover-operator]\n  isolation: in_process\n  entries:\n    caprover-operator:\n      settings:\n        url: https://captain.example.test\n        app_name: demo\napprovals:\n  mode: manual\n")
    sys.path.insert(0, str(source))
    sys.path.insert(0, str(repo / "tests"))
    from test_hermes_adapter import Operator
    from hermes_cli.plugins import get_plugin_manager
    manager = get_plugin_manager()
    manager.discover_and_load()
    from tools.registry import registry
    adapter = registry.get_entry("caprover_apply").handler.__self__
    module = sys.modules[adapter.__class__.__module__]
    fake = Operator()
    @contextmanager
    def runtime(settings):
        target = "https://captain.example.test"
        binding = module._Binding("synthetic-profile", target, module._configuration_hash(settings, target))
        yield module._Runtime(fake, settings, binding)
    module._make_runtime = runtime
    from tools.terminal_tool import set_approval_callback
    from tools.approval_context import set_current_session_key
    from tools.approval import enable_session_yolo, disable_session_yolo
    from model_tools import handle_function_call
    set_current_session_key("native-test-session")
    prompts = []
    def response(*args, **kwargs):
        prompts.append(True)
        return "once"
    set_approval_callback(response)
    ids = dict(task_id="task", session_id="session", turn_id="turn", tool_call_id="approved")
    def call(plan_id, call_id, **options):
        fake.saved["plan_id"] = plan_id
        return handle_function_call("caprover_apply", {"plan_id": plan_id}, **{**ids, "tool_call_id": call_id}, **options)
    approved = call("native-plan-1", "approved")
    assert fake.writes == 1 and len(prompts) == 1, approved
    enable_session_yolo("native-test-session")
    bypassed = call("native-plan-2", "bypassed")
    assert fake.writes == 1 and len(prompts) == 1, bypassed
    disable_session_yolo("native-test-session")
    set_approval_callback(lambda *args, **kwargs: "deny")
    denied = call("native-plan-3", "denied")
    assert fake.writes == 1, denied
    skipped = call("native-plan-4", "skipped", skip_pre_tool_call_hook=True, skip_tool_execution_middleware=True)
    assert fake.writes == 1, skipped
    print(json.dumps({"approved": json.loads(approved), "bypassed": json.loads(bypassed), "denied": json.loads(denied), "skipped": json.loads(skipped), "writes": fake.writes}))
'''
        completed = subprocess.run(
            [sys.executable, "-c", program, str(ROOT), str(source)],
            text=True, capture_output=True, timeout=30, check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        result = json.loads(completed.stdout.strip().splitlines()[-1])
        self.assertEqual(result["writes"], 1)
        self.assertIn("error", result["bypassed"])
        self.assertIn("error", result["denied"])
        self.assertIn("error", result["skipped"])


if __name__ == "__main__":
    unittest.main()

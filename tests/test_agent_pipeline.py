"""Regression for the real AIAgent middleware-before-approval call order.

The model client/metadata are synthetic boundaries; the AIAgent constructor,
tool pipeline, plugin hooks/middleware, engine and HTTP methods are real.
Human responses are explicitly simulated and the API is a loopback fixture.
"""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
HERMES = importlib.util.find_spec("hermes_cli")


_AGENT_PIPELINE = r'''
import importlib, json, os, shutil, sys, tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
repo, source = map(Path, sys.argv[1:3])
mode = sys.argv[3]
sys.path.insert(0, str(repo))
from caprover_operator.lab import FakeCapRover, NEW_IMAGE
with tempfile.TemporaryDirectory(prefix="caprover-agent-pipeline-") as temporary, FakeCapRover() as lab:
    home = Path(temporary).resolve()
    os.environ.update(HERMES_HOME=str(home), HERMES_INTERACTIVE="1", HERMES_ENABLE_PROJECT_PLUGINS="0",
                      HERMES_DISABLE_LAZY_INSTALLS="1",
                      CAPROVER_OPERATOR_TOKEN=lab.token)
    os.environ.pop("HERMES_YOLO_MODE", None)
    shutil.copytree(repo, home / "plugins" / "caprover-operator",
                    ignore=shutil.ignore_patterns(".git", "__pycache__", "tests", "docs", "demo", ".venv"))
    (home / "config.yaml").write_text(json.dumps({
        "plugins": {"enabled": ["caprover-operator"], "isolation": "in_process",
                    "entries": {"caprover-operator": {"settings": {
                        "url": lab.base_url, "app_name": lab.app, "health_url": lab.health_url}}}},
        "approvals": {"mode": "manual"},
    }))
    sys.path.insert(0, str(source))
    from hermes_cli.plugins import get_plugin_manager
    get_plugin_manager().discover_and_load()
    from tools.registry import registry
    adapter = registry.get_entry("caprover_apply").handler.__self__
    module = sys.modules[adapter.__class__.__module__]
    client_module = importlib.import_module(module.__name__ + ".caprover_operator.client")
    real_client = client_module.CapRoverClient
    class LoopbackClient(real_client):
        def __init__(self, url, *args, **kwargs):
            assert url == lab.base_url
            kwargs["allow_insecure_local"] = True
            super().__init__(url, *args, **kwargs)
    client_module.CapRoverClient = LoopbackClient

    from tools.terminal_tool import set_approval_callback
    from tools.approval_context import set_current_session_key
    set_current_session_key("synthetic-agent-pipeline")
    prompts, observed = [], []
    def simulated_host_response(command, description, **kwargs):
        choice = "deny" if mode == "deny" else "smart_ALLOW" if mode == "smart" else "once"
        prompts.append({"choice": choice, "decision_kind": "SIMULATED LAB HOST RESPONSE"})
        if mode == "config_drift":
            adapter.ctx.set_config("plan_ttl_seconds", 301)
        return choice
    set_approval_callback(simulated_host_response)
    def observe_pre(tool_name, args, **ids):
        if tool_name == "caprover_apply":
            observed.append({"event": "pre_tool_call", "ids": {key: ids.get(key) for key in ("session_id", "turn_id", "tool_call_id")}})
    def observe_post(**ids):
        observed.append({"event": "post_approval_response", "choice": ids.get("choice"),
                         "ids": {key: ids.get(key) for key in ("session_id", "turn_id", "tool_call_id")}})
    adapter.ctx.register_hook("pre_tool_call", observe_pre)
    adapter.ctx.register_hook("post_approval_response", observe_post)

    from run_agent import AIAgent
    # Only the uncalled external model boundary is mocked. No tool policy,
    # approval hook, middleware or dispatch function is replaced.
    with patch("agent.process_bootstrap.OpenAI"), patch("agent.model_metadata.fetch_model_metadata", return_value={}):
        agent = AIAgent(model="test/model", api_key="synthetic-model-key", base_url="http://127.0.0.1:1/v1",
                        enabled_toolsets=["caprover_operator"], quiet_mode=True, platform="cli",
                        skip_context_files=True, skip_memory=True, skip_background_review=True,
                        session_id="synthetic-agent-pipeline", max_iterations=1)
    agent._current_turn_id = "synthetic-agent-turn"
    agent._current_api_request_id = "synthetic-agent-api-request"
    messages = [{"role": "user", "content": "Synthetic fixture tool pipeline; no real service or human approval."}]
    def call(name, arguments, call_id):
        call = SimpleNamespace(id=call_id, type="function", function=SimpleNamespace(name=name, arguments=json.dumps(arguments)))
        messages.append({"role": "assistant", "content": "", "tool_calls": [
            {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}]})
        agent._execute_tool_calls(SimpleNamespace(tool_calls=[call]), messages, "synthetic-agent-task")
        result = next(message["content"] for message in reversed(messages) if message.get("role") == "tool" and message.get("tool_call_id") == call_id)
        return json.loads(result)
    try:
        plan = call("caprover_plan", {"image": NEW_IMAGE}, "agent-plan")
        assert plan["status"] == "planned", plan
        if mode == "yolo":
            from tools.approval import enable_session_yolo
            enable_session_yolo("synthetic-agent-pipeline")
        lab.lose_next_ack = True
        result = call("caprover_apply", {"plan_id": plan["plan_id"]}, "agent-apply")
        evidence = {"result": result, "host_prompts": len(prompts), "post_attempts": lab.trigger_count,
                    "events": observed, "same_adapter_for_handler": registry.get_entry("caprover_apply").handler.__self__ is adapter}
        print("AGENT_PIPELINE_RESULT=" + json.dumps(evidence))
        assert adapter._call_context.get() is None
        assert not adapter._active_calls and not adapter._tickets and not adapter._pending
        assert observed[0]["event"] == "pre_tool_call", observed
        if mode == "once":
            assert len(prompts) == 1 and lab.trigger_count == 1, evidence
            assert result["status"] == "reconcile_required", evidence
        else:
            assert "error" in result and lab.trigger_count == 0, evidence
            assert len(prompts) == (0 if mode == "yolo" else 1), evidence
            if mode == "config_drift":
                assert result["error"]["code"] == "configuration_changed", evidence
        if mode != "yolo":
            assert observed[1]["event"] == "post_approval_response", observed
            assert observed[0]["ids"] == observed[1]["ids"], observed
    finally:
        agent.close()
'''


@unittest.skipUnless(HERMES, "requires an installed Hermes runtime")
class NativeAgentPipelineTests(unittest.TestCase):
    def run_mode(self, mode):
        source = Path(HERMES.origin).parents[1]
        completed = subprocess.run([sys.executable, "-c", _AGENT_PIPELINE, str(ROOT), str(source), mode],
                                   text=True, capture_output=True, timeout=60, check=False)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        line = next(line for line in completed.stdout.splitlines() if line.startswith("AGENT_PIPELINE_RESULT="))
        result = json.loads(line.partition("=")[2])
        return result

    def test_real_agent_pipeline_prompts_before_consuming_a_per_call_ticket(self):
        result = self.run_mode("once")
        self.assertEqual(result["host_prompts"], 1)
        self.assertEqual(result["post_attempts"], 1)
        self.assertEqual(result["result"]["status"], "reconcile_required")

    def test_real_agent_denial_never_dispatches_and_clears_approval_state(self):
        self.assertEqual(self.run_mode("deny")["post_attempts"], 0)

    def test_real_agent_yolo_does_not_create_a_ticket(self):
        result = self.run_mode("yolo")
        self.assertEqual(result["post_attempts"], 0)
        self.assertEqual(result["host_prompts"], 0)

    def test_real_agent_smart_response_is_not_a_human_grant(self):
        self.assertEqual(self.run_mode("smart")["post_attempts"], 0)

    def test_real_agent_owner_config_drift_after_approval_refuses_write(self):
        self.assertEqual(self.run_mode("config_drift")["post_attempts"], 0)


if __name__ == "__main__":
    unittest.main()

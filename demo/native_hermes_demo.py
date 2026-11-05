#!/usr/bin/env python3
"""Record real Hermes dispatch against a SYNTHETIC loopback API.

No CapRover server or container is started. Human approval is SIMULATED by a
host callback in an isolated lab. There are deliberately no server, app,
credential, image or live-approval command-line options.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from caprover_operator.lab import FakeCapRover, NEW_IMAGE


_RESULT = "NATIVE_CAPROVER_LAB_RESULT="
_NATIVE_DISPATCH = r'''
import importlib, json, os, sys
source, tool, arguments, call_id = sys.argv[1:]
assert tool in {"caprover_plan", "caprover_apply", "caprover_reconcile"}
sys.path.insert(0, source)
from hermes_cli.plugins import get_plugin_manager
manager = get_plugin_manager()
manager.discover_and_load()
from tools.registry import registry
entry = registry.get_entry("caprover_apply")
assert entry is not None, "native registration failed"
adapter = entry.handler.__self__
module = sys.modules[adapter.__class__.__module__]
client_module = importlib.import_module(module.__name__ + ".caprover_operator.client")
real_client = client_module.CapRoverClient
class LoopbackLabClient(real_client):
    def __init__(self, base_url, *args, **kwargs):
        from urllib.parse import urlsplit
        assert base_url == os.environ["CAPROVER_NATIVE_LAB_ORIGIN"]
        assert urlsplit(base_url).hostname == "127.0.0.1"
        kwargs["allow_insecure_local"] = True
        super().__init__(base_url, *args, **kwargs)
# Lab-only constructor option; all real transport/engine methods are retained.
client_module.CapRoverClient = LoopbackLabClient
from tools.terminal_tool import set_approval_callback
from tools.approval_context import set_current_session_key
from model_tools import handle_function_call
set_current_session_key("synthetic-native-demo-session")
approvals = []
def simulated_lab_human(command, description, **kwargs):
    # Explicit simulated host response, bound to this generated lab origin.
    assert tool == "caprover_apply"
    assert description.startswith("Apply plan ")
    assert os.environ["CAPROVER_NATIVE_LAB_ORIGIN"] in description
    approvals.append({"surface": "native_cli", "command": command,
                      "message": description, "choice": "once",
                      "decision_kind": "SIMULATED LAB HUMAN APPROVAL"})
    return "once"
set_approval_callback(simulated_lab_human)
result = handle_function_call(
    tool, json.loads(arguments), task_id="synthetic-native-demo-task",
    session_id="synthetic-native-demo-session", turn_id="synthetic-native-demo-turn",
    tool_call_id=call_id,
)
print("NATIVE_CAPROVER_LAB_RESULT=" + json.dumps({
    "public_result": json.loads(result), "host_approvals": approvals,
    "native_dispatch": True, "fresh_hermes_process": True,
}))
'''


def run_native_lab() -> dict:
    """Return a fresh JSON-serializable evidence trace; never contact live URLs."""
    hermes = importlib.util.find_spec("hermes_cli")
    if hermes is None or not hermes.origin:
        raise RuntimeError("hermes_not_importable_in_current_interpreter")
    source = Path(hermes.origin).parents[1]
    events: list[dict] = []

    def event(name: str, message: str, **evidence) -> None:
        events.append({"index": len(events) + 1, "event": name, "message": message, **evidence})

    event("scope", "SYNTHETIC API: no real CapRover or images. Real Hermes dispatch; SIMULATED host human approvals.")
    with tempfile.TemporaryDirectory(prefix="caprover-native-demo-") as temporary, FakeCapRover() as lab:
        home = Path(temporary).resolve()
        lab.runtime_available = True
        shutil.copytree(
            ROOT, home / "plugins" / "caprover-operator",
            ignore=shutil.ignore_patterns(".git", "__pycache__", "tests", "docs", "demo", ".venv", "*.egg-info"),
        )
        (home / "config.yaml").write_text(json.dumps({
            "plugins": {"enabled": ["caprover-operator"], "isolation": "in_process",
                        "entries": {"caprover-operator": {"settings": {
                            "url": lab.base_url, "app_name": lab.app,
                            "health_url": lab.health_url, "plan_ttl_seconds": 300}}}},
            "approvals": {"mode": "manual"},
        }))
        # Inherit no Hermes profile, token or approval settings from the caller.
        environment = {key: value for key, value in os.environ.items()
                       if key in {"PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "SYSTEMROOT"}}
        environment.update({"HERMES_HOME": str(home), "HERMES_ENABLE_PROJECT_PLUGINS": "0",
                            "HERMES_INTERACTIVE": "1", "PYTHONDONTWRITEBYTECODE": "1",
                            "CAPROVER_OPERATOR_TOKEN": lab.token,
                            "CAPROVER_NATIVE_LAB_ORIGIN": lab.base_url})

        def call(tool: str, arguments: dict, name: str, message: str) -> dict:
            completed = subprocess.run(
                [sys.executable, "-c", _NATIVE_DISPATCH, str(source), tool,
                 json.dumps(arguments), f"synthetic-native-demo-call-{len(events) + 1}"],
                env=environment, text=True, capture_output=True, timeout=30, check=False,
            )
            if completed.returncode:
                raise RuntimeError("native_dispatch_failed")
            lines = [line for line in completed.stdout.splitlines() if line.startswith(_RESULT)]
            if len(lines) != 1:
                raise RuntimeError("native_result_missing")
            evidence = json.loads(lines[0][len(_RESULT):])
            event(name, message, tool_name=tool, tool_arguments=arguments,
                  post_count=lab.trigger_count, **evidence)
            return evidence

        first = call("caprover_plan", {"image": NEW_IMAGE}, "plan", "Saved a plan through native Hermes.")["public_result"]
        assert first["status"] == "planned"
        lab.drift(instanceCount=2)
        event("fixture_drift", "LAB FIXTURE changed requested replicas after planning.", post_count=lab.trigger_count)
        refused = call("caprover_apply", {"plan_id": first["plan_id"]}, "drift_refused",
                       "Simulated native approval occurred; fresh preflight refused drift with zero deployment POSTs.")
        assert refused["public_result"]["status"] == "consumed_drift"
        assert len(refused["host_approvals"]) == 1 and lab.trigger_count == 0

        lab.sync_runtime_to_definition()
        event("fixture_baseline", "LAB FIXTURE synchronized existing old-image tasks to the new replica count.", post_count=lab.trigger_count)
        plan = call("caprover_plan", {"image": NEW_IMAGE}, "fresh_plan", "Saved a new plan against the changed baseline.")["public_result"]
        assert plan["status"] == "planned"
        lab.lose_next_ack = True
        event("fixture_fault", "LAB FIXTURE will record the image change, retain old tasks and drop the next deployment reply.", post_count=lab.trigger_count)
        uncertain = call("caprover_apply", {"plan_id": plan["plan_id"]}, "lost_ack",
                         "Native apply persisted uncertainty after the fixture dropped its acknowledgement.")
        assert uncertain["public_result"]["status"] == "reconcile_required"
        assert uncertain["public_result"]["trigger_outcome"] == "uncertain"
        assert len(uncertain["host_approvals"]) == 1 and lab.trigger_count == 1

        repeated = call("caprover_apply", {"plan_id": plan["plan_id"]}, "restart_replay_refused",
                        "A new Hermes process reopened the ledger; repeating apply was blocked without another POST.")
        assert "error" in repeated["public_result"]
        assert not repeated["host_approvals"] and lab.trigger_count == 1

        observed = call("caprover_reconcile", {"plan_id": plan["plan_id"]}, "old_tasks_observed",
                        "New metadata and HTTP 200 were observed, but current tasks still run the old image.")["public_result"]
        assert observed["status"] == "metadata_http_observed"
        assert observed["checks"]["runtime_rollout_proven"] is False and lab.trigger_count == 1

        lab.sync_runtime_to_definition()
        event("fixture_task_sync", "LAB FIXTURE explicitly changed current tasks to the requested image digest.", post_count=lab.trigger_count)
        verified = call("caprover_reconcile", {"plan_id": plan["plan_id"]}, "runtime_verified",
                        "A fresh native observation now proves exact-image tasks plus HTTP health in the synthetic fixture.")["public_result"]
        assert verified["status"] == "verified"
        assert verified["checks"]["runtime_rollout_proven"] is True and lab.trigger_count == 1
        assert len([request for request in lab.requests if request["method"] == "POST"]) == 1

        trace = {"demo_kind": "native_hermes_synthetic_api_fixture",
                 "generated_at": datetime.now(timezone.utc).isoformat(),
                 "scope": {"real_hermes_dispatch": True, "real_engine_and_http_client": True,
                           "real_caprover": False, "real_images_or_containers": False,
                           "human_approval": "SIMULATED LAB HOST CALLBACK",
                           "network": "generated 127.0.0.1 loopback fixture only",
                           "plugin_transport_override": "explicit HTTP loopback lab option in copied plugin only"},
                 "python_version": platform.python_version(), "events": events,
                 "post_attempts": lab.trigger_count, "result": verified}
        encoded = json.dumps(trace)
        for private in (lab.token, "synthetic-env-value", lab.health_body.decode()):
            if private in encoded:
                raise RuntimeError("fixture_private_value_in_trace")
        return trace


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="Create this new JSON trace file; never overwrite")
    arguments = parser.parse_args(argv)
    if arguments.output and (arguments.output.exists() or arguments.output.is_symlink()):
        parser.error("output already exists; choose a new path")
    try:
        result = run_native_lab()
        encoded = json.dumps(result, indent=2, sort_keys=True) + "\n"
        if arguments.output:
            with arguments.output.open("x", encoding="utf-8") as output:
                output.write(encoded)
        sys.stdout.write(encoded)
        return 0
    except (OSError, RuntimeError, AssertionError, ValueError):
        print(json.dumps({"error": "native_synthetic_demo_failed",
                          "hint": "Use an interpreter with Hermes installed and allow local loopback networking."}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

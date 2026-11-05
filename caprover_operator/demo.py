"""Executable evidence from a synthetic API, never a real-server claim."""
from __future__ import annotations

from pathlib import Path
import tempfile
from typing import Callable

from .engine import Operator, load_fingerprint_key
from .lab import FakeCapRover, NEW_IMAGE


def run_demo(emit: Callable[[str], None] | None = print) -> dict:
    events: list[dict] = []

    def event(name: str, message: str, **evidence) -> None:
        events.append({"event": name, "message": message, **evidence})
        if emit:
            emit(f"[{name}] {message}")

    event("scope", "SYNTHETIC CAPROVER API FIXTURE. No real server, images or production changes.")
    with tempfile.TemporaryDirectory(prefix="caprover-operator-demo-") as temporary, FakeCapRover() as lab:
        data_dir = Path(temporary).resolve() / "private-ledger"
        key = load_fingerprint_key(data_dir)

        def open_operator() -> Operator:
            return Operator(data_dir, lab.client(fingerprint_key=key))

        # A trusted LAB driver, not a native approval substitute. This command
        # cannot select an external server, credentials or a real image.
        def lab_approval(plan: dict) -> bool:
            return plan["target"] == lab.base_url and plan["app"] == lab.app and plan["image"] == NEW_IMAGE

        operator = open_operator()
        first = operator.plan(lab.app, NEW_IMAGE)
        event("plan", "Saved a plan against the current app state.", plan=first)
        lab.drift(instanceCount=2)
        refused = operator.apply(first["plan_id"], lab_approval)
        assert refused["status"] == "consumed_drift" and lab.trigger_count == 0
        event("drift", "App changed after planning. Old plan refused; POST count = 0.", receipt=refused, post_count=lab.trigger_count)

        plan = operator.plan(lab.app, NEW_IMAGE)
        lab.lose_next_ack = True
        uncertain = operator.apply(plan["plan_id"], lab_approval)
        assert uncertain["status"] == "reconcile_required" and lab.trigger_count == 1
        event("lost_ack", "Fixture applied the change and dropped its reply. Outcome persisted as uncertain.", receipt=uncertain, post_count=lab.trigger_count)
        operator.close()
        operator = open_operator()
        repeated = operator.apply(plan["plan_id"], lab_approval)
        assert repeated["status"] == "reconcile_required" and lab.trigger_count == 1
        event("restart", "Reopened the ledger. Repeating apply sent no additional POST.", receipt=repeated, post_count=lab.trigger_count)

        observed = operator.reconcile(plan["plan_id"])
        assert observed["status"] == "metadata_http_observed" and lab.trigger_count == 1
        assert observed["checks"]["runtime_rollout_proven"] is False
        event("observe", "New metadata and HTTP 200 observed. Running-task rollout is still unproven.", receipt=observed, post_count=lab.trigger_count)

        lab.runtime_available = True
        before_convergence = operator.reconcile(plan["plan_id"])
        assert before_convergence["checks"]["runtime_rollout_proven"] is False
        event("old_tasks", "Old running tasks prevent a verified rollout despite HTTP 200.", receipt=before_convergence)
        lab.sync_runtime_to_definition()
        completed = operator.reconcile(plan["plan_id"])
        assert completed["status"] == "verified" and lab.trigger_count == 1
        assert completed["checks"]["runtime_rollout_proven"] is True
        event("verify", "Exact new-image tasks and HTTP health now observed. Total POST attempts = 1.", receipt=completed, post_count=lab.trigger_count)
        operator.close()

    return {"demo_kind": "synthetic_protocol_fixture", "real_caprover": False, "native_hermes_approval": False,
            "events": events, "post_attempts": 1, "result": completed}

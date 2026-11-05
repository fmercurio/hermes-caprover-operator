"""Suggestions explain outcomes without performing any follow-up action."""
from copy import deepcopy
from contextlib import contextmanager
import importlib
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from caprover_operator.engine import Operator
from caprover_operator.guidance import error_guidance, explain, safe_error_code


PLAN_ID = "a" * 32
IMAGE = "registry.example/demo@sha256:" + "b" * 64


class GuidanceTests(unittest.TestCase):
    def test_uncertainty_suggests_same_plan_reconciliation_without_changing_receipt(self):
        receipt = {"status": "reconcile_required", "plan_id": PLAN_ID, "plan_hash": "c" * 64,
                   "trigger_outcome": "uncertain", "checks": {"post_attempted": True}}
        original = deepcopy(receipt)
        result = explain(receipt)
        self.assertEqual(receipt, original)
        self.assertEqual(result["status"], "reconcile_required")
        self.assertEqual(result["plan_hash"], original["plan_hash"])
        self.assertEqual(result["guidance"]["next_step"], {
            "tool": "caprover_reconcile", "arguments": {"plan_id": PLAN_ID},
            "requires_native_human_approval": False,
        })
        self.assertFalse(result["guidance"]["automatic"])
        self.assertIn("Do not repeat apply", result["guidance"]["message"])

    def test_drift_and_expiry_suggest_new_intent_never_reusing_apply(self):
        for status in ("consumed_drift", "expired", "consumed_expired"):
            with self.subTest(status=status):
                result = explain({"status": status, "plan_id": PLAN_ID, "image": IMAGE})
                self.assertEqual(result["guidance"]["next_step"]["tool"], "caprover_plan")
                self.assertEqual(result["guidance"]["next_step"]["arguments"], {"image": IMAGE})
                self.assertFalse(result["guidance"]["automatic"])

    def test_metadata_http_does_not_become_a_verified_result(self):
        for status in ("deployment_recorded", "metadata_http_observed"):
            result = explain({"status": status, "plan_id": PLAN_ID,
                              "checks": {"runtime_rollout_proven": False}})
            self.assertEqual(result["status"], status)
            self.assertFalse(result["checks"]["runtime_rollout_proven"])
            self.assertEqual(result["guidance"]["next_step"]["tool"], "caprover_reconcile")
            self.assertIn("not serving-image rollout proof", result["guidance"]["message"])

    def test_planned_requires_native_approval_and_verified_is_dated_history(self):
        planned = explain({"status": "planned", "plan_id": PLAN_ID})
        self.assertEqual(planned["guidance"]["next_step"]["tool"], "caprover_apply")
        self.assertTrue(planned["guidance"]["next_step"]["requires_native_human_approval"])
        self.assertFalse(planned["guidance"]["automatic"])
        verified = explain({"status": "verified", "plan_id": PLAN_ID})
        self.assertEqual(verified["guidance"]["next_step"]["tool"], "caprover_status")
        self.assertIn("dated", verified["guidance"]["message"])

    def test_claim_conflict_lists_existing_operations_instead_of_new_plan(self):
        result = error_guidance("operation_in_progress", PLAN_ID)
        self.assertEqual(result["next_step"]["tool"], "caprover_status")
        self.assertEqual(result["next_step"]["arguments"], {})
        self.assertIn("do not create another plan", result["message"])

    def test_unknown_exception_codes_are_not_exposed(self):
        private = "privatecredentialthatlookslikeanerrorcode"
        self.assertEqual(safe_error_code(private), "operation_failed")
        result = error_guidance(private, PLAN_ID)
        self.assertNotIn(private, json.dumps(result))
        self.assertEqual(result["next_step"]["tool"], "caprover_status")
        self.assertIn("Do not repeat apply", result["message"])

    def test_operation_list_gets_guidance_without_mutating_history(self):
        history = {"operations": [{"status": "sending", "plan_id": PLAN_ID}]}
        result = explain(history)
        self.assertNotIn("guidance", history["operations"][0])
        self.assertEqual(result["operations"][0]["guidance"]["next_step"]["tool"], "caprover_reconcile")

    def test_empty_history_starts_with_doctor_instead_of_status_loop(self):
        result = explain({"operations": []})
        self.assertEqual(result["guidance"]["next_step"]["tool"], "caprover_doctor")

    def test_expired_planned_receipt_does_not_suggest_apply_or_change_its_status(self):
        receipt = {"status": "planned", "plan_id": PLAN_ID, "image": IMAGE, "app": "current", "expires_at": 9}
        result = explain(receipt, configured_app="current", now=10)
        self.assertEqual(result["status"], "planned")
        self.assertEqual(result["guidance"]["code"], "plan_validity_ended")
        self.assertEqual(result["guidance"]["next_step"]["tool"], "caprover_plan")

    def test_historical_other_app_or_target_never_suggests_mutation_or_old_image(self):
        for status in ("planned", "expired", "consumed_drift", "reconcile_required", "metadata_http_observed"):
            for receipt in ({"app": "previous-app", "target": "https://current.invalid"},
                            {"app": "current-app", "target": "https://previous.invalid"}):
                with self.subTest(status=status, receipt=receipt):
                    result = explain({**receipt, "status": status, "image": IMAGE, "plan_id": PLAN_ID},
                                     configured_app="current-app", configured_target="https://current.invalid")
                    self.assertEqual(result["guidance"]["code"], "owner_scope_changed")
                    self.assertEqual(result["guidance"]["next_step"]["tool"], "caprover_doctor")
                    self.assertEqual(result["guidance"]["next_step"]["arguments"], {})

    def test_missing_plan_lists_available_operations_instead_of_repeating_bad_id(self):
        result = error_guidance("plan_not_found", PLAN_ID)
        self.assertEqual(result["next_step"]["tool"], "caprover_status")
        self.assertEqual(result["next_step"]["arguments"], {})

    def test_access_denial_does_not_assert_credential_is_invalid(self):
        result = error_guidance("authorization_denied")
        self.assertIn("does not establish", result["message"])
        self.assertEqual(result["code"], "review_server_access")

    def test_real_ledger_from_previous_origin_does_not_repeat_the_rejected_id(self):
        root = Path(__file__).resolve().parents[1]
        sys.path.insert(0, str(root.parent))
        plugin = importlib.import_module(root.name)
        old = SimpleNamespace(target="https://previous.invalid", snapshot=lambda app: {
            "app": app, "version": 1, "image": "registry.example/demo@sha256:" + "a" * 64,
            "replicas": 1, "is_building": False, "config_fingerprint": "c" * 64,
        })
        current = SimpleNamespace(target="https://current.invalid")
        config = {"url": current.target, "app_name": "demo-app"}
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary).resolve() / "private"
            operator = Operator(directory, old)
            plan = operator.plan("demo-app", IMAGE)
            operator.close()
            operator = Operator(directory, current)
            try:
                @contextmanager
                def runtime(settings):
                    binding = plugin._Binding(str(directory), current.target, plugin._configuration_hash(settings, current.target))
                    yield plugin._Runtime(operator, settings, binding)
                adapter = plugin._Adapter(SimpleNamespace(get_config=lambda key, default=None: config.get(key, default)))
                with patch.object(plugin, "_make_runtime", runtime):
                    result = json.loads(adapter.status({"plan_id": plan["plan_id"]}))
                self.assertEqual(result["error"]["code"], "invalid_plan")
                self.assertEqual(result["guidance"]["next_step"]["tool"], "caprover_doctor")
                self.assertEqual(result["guidance"]["next_step"]["arguments"], {})
                self.assertIn("Preserve the ledger", result["guidance"]["message"])
            finally:
                operator.close()


if __name__ == "__main__":
    unittest.main()

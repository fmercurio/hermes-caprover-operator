"""Manual next steps for native tool results; no execution or ledger changes."""
from __future__ import annotations

import re
import time
from typing import Any


_ID = re.compile(r"[0-9a-f]{32}\Z")
SAFE_ERROR_CODES = frozenset({
    "invalid_input", "configuration_required", "token_missing", "native_approval_required",
    "approval_context_missing", "approval_pending", "configuration_changed", "plan_not_applicable",
    "operation_failed", "secure_storage_unsupported", "unsafe_data_directory", "unsafe_state_file",
    "invalid_fingerprint_key", "state_write_failed", "ledger_unavailable", "ledger_replaced",
    "unsupported_ledger", "operator_closed", "invalid_clock", "invalid_plan_id", "plan_not_found",
    "invalid_plan", "target_changed", "operation_in_progress", "invalid_ttl", "snapshot_unavailable",
    "app_building", "no_op", "authorization_failed", "claim_lost", "reconciliation_not_required",
    "invalid_image", "immutable_image_required", "invalid_app", "invalid_target", "invalid_snapshot",
    "configuration_invalid", "app_not_allowed", "app_not_found", "image_invalid",
    "authentication_unavailable", "authentication_rejected", "authorization_denied",
    "redirect_rejected", "response_invalid", "response_too_large", "endpoint_unavailable",
    "request_timeout", "request_failed",
})


def safe_error_code(value: Any) -> str:
    return value if isinstance(value, str) and value in SAFE_ERROR_CODES else "operation_failed"


def suggestion(code: str, message: str, tool: str, arguments: dict | None = None, *, required_input=None) -> dict:
    next_step = {"tool": tool, "arguments": arguments or {},
                 "requires_native_human_approval": tool == "caprover_apply"}
    if required_input:
        next_step["required_input"] = list(required_input)
    return {"code": code, "message": message, "next_step": next_step, "automatic": False}


def _plan_arguments(plan_id: Any) -> dict:
    return {"plan_id": plan_id} if isinstance(plan_id, str) and _ID.fullmatch(plan_id) else {}


def error_guidance(code: str, plan_id: Any = None) -> dict:
    code = safe_error_code(code)
    args = _plan_arguments(plan_id)
    if code in {"native_approval_required", "approval_context_missing", "approval_pending", "authorization_failed"}:
        return suggestion("request_native_human_approval",
                          "Use an interactive Hermes session with in_process isolation and fresh manual human approval. This diagnostic cannot grant approval.",
                          "caprover_status", args)
    if code in {"configuration_required", "configuration_invalid", "invalid_app", "invalid_target", "invalid_ttl", "target_changed", "configuration_changed"}:
        return suggestion("check_owner_configuration",
                          "Check the owner-configured HTTPS URL, app and optional health URL, then run the read-only doctor. Do not pass a target or token in tool arguments.",
                          "caprover_doctor")
    if code == "authorization_denied":
        return suggestion("review_server_access",
                          "The server denied access. Review CapRover permissions and any proxy/access policy for the configured origin; this response alone does not establish that the credential is invalid.",
                          "caprover_doctor")
    if code in {"token_missing", "authentication_unavailable", "authentication_rejected"}:
        return suggestion("check_hermes_credential",
                          "Set or correct CAPROVER_OPERATOR_TOKEN through Hermes credentials for this profile and server, then run the doctor. Never paste the token into a tool call.",
                          "caprover_doctor")
    if code == "operation_in_progress":
        return suggestion("inspect_active_operation",
                          "An active or uncertain operation blocks this app. Inspect the saved operations and reconcile that operation; do not create another plan to bypass the claim.",
                          "caprover_status")
    if code in {"plan_not_found", "invalid_plan_id"}:
        return suggestion("find_saved_operation",
                          "No matching saved plan was found. List this target's saved operations and choose an existing plan ID; do not invent a replacement for an uncertain operation.",
                          "caprover_status")
    if code == "invalid_plan":
        return suggestion("review_owner_scope_and_ledger",
                          "This saved intent could not be validated for the current origin or ledger integrity. Preserve the ledger, review the owner profile/origin and run the read-only doctor. Do not apply, rewrite or retarget this intent.",
                          "caprover_doctor")
    if code == "no_op":
        return suggestion("inspect_current_image",
                          "The requested image already matches the app's recorded image, so no new plan was created. Inspect current state if that is unexpected.",
                          "caprover_doctor")
    if code in {"plan_not_applicable", "reconciliation_not_required"}:
        return suggestion("inspect_saved_operation",
                          "Inspect the saved plan and its status. If it is uncertain, reconcile it instead of applying again. A completed or consumed plan cannot authorize a new deployment.",
                          "caprover_status", args)
    if code in {"immutable_image_required", "invalid_image", "image_invalid"}:
        return suggestion("supply_immutable_image",
                          "Choose the intended image using an immutable @sha256 digest, then request a new plan. A mutable tag cannot authorize a deployment.",
                          "caprover_plan", required_input=["image"])
    if code in {"app_not_found", "app_not_allowed", "app_building", "snapshot_unavailable", "invalid_snapshot",
                "redirect_rejected", "response_invalid", "response_too_large", "endpoint_unavailable", "request_timeout", "request_failed"}:
        return suggestion("diagnose_read_only",
                          "Run the read-only doctor to check the configured app, API access and optional evidence. If a deployment attempt already exists, inspect and reconcile it before any new write.",
                          "caprover_doctor")
    if code == "invalid_input":
        return suggestion("use_declared_arguments",
                          "Use only the declared tool arguments. Server, app, credentials and approval are not model-controlled inputs.",
                          "caprover_doctor")
    return suggestion("preserve_and_inspect_ledger",
                      "Preserve the ledger and inspect this operation after resolving local access problems. Do not repeat apply or delete state: a request may already have occurred.",
                      "caprover_status", args)


def result_guidance(result: dict, context_plan_id: Any = None, *, configured_app=None, configured_target=None, now=None) -> dict:
    status = result.get("status")
    args = _plan_arguments(result.get("plan_id", context_plan_id))
    if ((configured_app is not None and "app" in result and result["app"] != configured_app)
            or (configured_target is not None and "target" in result and result["target"] != configured_target)):
        return suggestion("owner_scope_changed",
                          "This historical receipt belongs to a different owner-configured app or target. It suggests no deployment or reconciliation in the current scope. Review the owner settings and run the doctor for the current app.",
                          "caprover_doctor")
    if status == "planned":
        expires = result.get("expires_at")
        if type(expires) in {int, float} and expires <= (time.time() if now is None else now):
            guidance = result_guidance({**result, "status": "expired"}, context_plan_id)
            guidance["code"] = "plan_validity_ended"
            guidance["message"] = "This plan's validity window ended. Review current state and create a new plan; approval does not extend its lifetime."
            return guidance
        return suggestion("review_then_request_approval",
                          "Review the target, image, baseline and expiry. Apply this saved plan only when you intend the deployment; Hermes will require a fresh native human approval.",
                          "caprover_apply", args)
    if status in {"preflight_pending", "sending", "reconcile_required"}:
        return suggestion("reconcile_same_operation",
                          "The deployment outcome is uncertain. Explicitly reconcile this same saved plan. Do not repeat apply, delete the ledger or create a replacement to bypass uncertainty.",
                          "caprover_reconcile" if args else "caprover_status", args)
    if status in {"expired", "consumed_expired", "consumed_drift"}:
        message = ("The app changed after planning; this plan sent no deployment POST. " if status == "consumed_drift"
                   else "This plan expired without a deployment POST. ")
        image = result.get("image")
        image_args = {"image": image} if isinstance(image, str) and len(image) <= 512 else {}
        return suggestion("create_new_plan", message + "Review the current app state and create a new plan for the intended immutable image. The old plan cannot be reused.",
                          "caprover_plan", image_args, required_input=None if image_args else ["image"])
    if status == "consumed_preflight_failed":
        return suggestion("diagnose_before_new_plan",
                          "Preflight evidence was unavailable; this plan sent no deployment POST and is consumed. Diagnose access before creating a new plan.",
                          "caprover_doctor")
    if status in {"metadata_http_observed", "deployment_recorded"}:
        return suggestion("observe_runtime_evidence",
                          "CapRover recorded the requested image, but that is not serving-image rollout proof. HTTP 200 can come from an older task. Explicitly reconcile later; verified also needs supported exact-image task evidence and a configured positive HTTP health check.",
                          "caprover_reconcile" if args else "caprover_status", args)
    if status == "observed_unhealthy":
        return suggestion("investigate_observed_health",
                          "Deployment metadata was observed, but health or replica evidence is unhealthy. Inspect the doctor and receipt, then explicitly reconcile after investigation. No rollback or retry is performed.",
                          "caprover_doctor")
    if status == "verified":
        return suggestion("inspect_dated_receipt",
                          "The dated task and HTTP evidence passed. Keep the receipt; it does not prove migrations, ongoing availability or exclusive causation.",
                          "caprover_status", args)
    if status == "superseded":
        return suggestion("inspect_later_deployment",
                          "A later different image superseded this recorded operation. Inspect current state before planning another change; this receipt authorizes no new request.",
                          "caprover_doctor")
    if status == "authorization_denied":
        return error_guidance("native_approval_required", result.get("plan_id", context_plan_id))
    if isinstance(result.get("operations"), list) and not result["operations"]:
        return suggestion("start_with_diagnostics",
                          "No saved operations were found for this target. Run the read-only doctor, then request a plan for your intended immutable image.",
                          "caprover_doctor")
    return suggestion("inspect_saved_operations",
                      "Choose the saved operation you want to inspect. Reconcile uncertain operations explicitly; suggestions never execute themselves.",
                      "caprover_status", required_input=["plan_id"])


def explain(result: Any, context_plan_id: Any = None, *, configured_app=None, configured_target=None, now=None) -> Any:
    """Add presentation-only guidance without mutating or persisting a receipt."""
    if not isinstance(result, dict):
        return result
    value = dict(result)
    if isinstance(value.get("operations"), list):
        value["operations"] = [explain(item, configured_app=configured_app, configured_target=configured_target, now=now) for item in value["operations"]]
    value["guidance"] = result_guidance(value, context_plan_id, configured_app=configured_app, configured_target=configured_target, now=now)
    return value

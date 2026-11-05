"""Bounded read-only checks; never opens a ledger or grants an approval."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .guidance import safe_error_code, suggestion


def new_report() -> dict:
    return {
        "status": "needs_configuration", "read_only": True,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "readiness_scope": "planning_only",
        "checks": {
            "configuration": {"status": "not_checked"},
            "credential": {"status": "not_checked"},
            "connection": {"status": "not_checked"},
            "app": {"status": "not_checked"},
            "runtime": {"status": "not_checked"},
            "health": {"status": "not_checked"},
            "approval": {"status": "not_tested", "required": "fresh_native_human",
                         "supported_isolation": "in_process", "deployment_authorized": False},
        },
        "limitations": ["local_storage_not_tested", "native_execution_approval_not_tested"],
    }


def _count(value: Any) -> int | None:
    return value if type(value) is int and 0 <= value <= 1_000_000 else None


def inspect_read_only(client, app: str, report: dict) -> dict:
    """Read the existing snapshot and fixed health URL once, with no retries.

    Only allowlisted summary fields leave this function; raw response strings,
    definitions, task identities/logs and exception messages never do.
    """
    checks = report["checks"]
    snapshot = None
    try:
        snapshot = client.snapshot(app)
        if not isinstance(snapshot, dict) or snapshot.get("app") != app:
            raise ValueError
    except Exception as exc:
        code = safe_error_code(getattr(exc, "code", None))
        responded = code in {"authentication_rejected", "authorization_denied", "app_not_found",
                             "response_invalid", "response_too_large", "endpoint_unavailable", "redirect_rejected"}
        checks["connection"] = {"status": "responded" if responded else "unavailable", "code": code}
        checks["app"] = {"status": "missing" if code == "app_not_found" else "not_confirmed"}
        if code in {"authentication_rejected", "authentication_unavailable"}:
            checks["credential"] = {"status": "rejected" if code != "authentication_unavailable" else "unavailable"}
        elif code == "app_not_found":
            checks["credential"] = {"status": "accepted_by_api"}
        report["status"] = "unavailable"
        message = "The configured app could not be confirmed. Check the owner settings, HTTPS connectivity and Hermes credential, then explicitly run the doctor again. No deployment was attempted."
        if code == "app_not_found":
            message = "The API accepted the credential, but the configured app was not found. Correct app_name in the owner settings or verify the server, then explicitly run the doctor again. This tool cannot create an app."
        elif code == "authorization_denied":
            message = "The server denied access. Review CapRover permissions and any proxy/access policy for the configured origin, then explicitly run the doctor again. This response alone does not prove that the credential is invalid."
        elif code in {"authentication_rejected", "authentication_unavailable"}:
            message = "The API credential was rejected or unavailable. Correct CAPROVER_OPERATOR_TOKEN through Hermes credentials for this profile and server, then explicitly run the doctor again. Never paste the token into a tool call."
        report["guidance"] = suggestion("check_api_access", message, "caprover_doctor")
        snapshot = None
    else:
        checks["connection"] = {"status": "responded"}
        checks["credential"] = {"status": "accepted_by_api"}
        checks["app"] = {"status": "found", "is_building": snapshot.get("is_building") is True,
                         "image_known": isinstance(snapshot.get("image"), str) and bool(snapshot["image"]),
                         "configured_replicas": _count(snapshot.get("replicas"))}
        runtime = snapshot.get("runtime")
        if isinstance(runtime, dict) and runtime.get("available") is True:
            checks["runtime"] = {"status": "available", "desired_instances": _count(runtime.get("desired_instances")),
                                 "running_instances": _count(runtime.get("running_instances")),
                                 "current_task_count": len(runtime["current_tasks"]) if isinstance(runtime.get("current_tasks"), list) else None}
        else:
            checks["runtime"] = {"status": "unavailable"}
            if isinstance(runtime, dict) and runtime.get("reason") in {"endpoint_unavailable", "response_invalid", "collection_unavailable"}:
                checks["runtime"]["reason"] = runtime["reason"]
            report["limitations"].append("runtime_rollout_evidence_unavailable")

    try:
        health = client.health(app)
        if not isinstance(health, dict) or type(health.get("configured")) is not bool:
            raise ValueError
        code = health.get("status_code")
        code = code if type(code) is int and 100 <= code <= 599 else None
        if not health["configured"]:
            checks["health"] = {"status": "not_configured"}
            report["limitations"].append("http_health_not_configured")
        else:
            checks["health"] = {"status": "responded" if health.get("ok") is True else "unhealthy_or_unavailable",
                                "status_code": code, "ok": health.get("ok") is True}
    except Exception:
        checks["health"] = {"status": "unavailable"}

    if snapshot is None:
        return report
    if checks["app"]["is_building"] or not checks["app"]["image_known"]:
        report["status"] = "needs_attention"
        report["guidance"] = suggestion(
            "inspect_app_before_planning",
            "The API accepted the credential, but the app is building or its deployed image is unknown. Inspect the app and explicitly run the doctor again before planning.",
            "caprover_doctor",
        )
    elif checks["health"]["status"] in {"unhealthy_or_unavailable", "unavailable"}:
        report["status"] = "needs_attention"
        report["guidance"] = suggestion(
            "investigate_health",
            "The app API is reachable, but the configured health check did not pass. Investigate that endpoint, then explicitly run the doctor again. This does not authorize deployment.",
            "caprover_doctor",
        )
    else:
        report["status"] = "ready_for_planning"
        message = "The app API accepted the credential and returned a known image. You can request a plan for an intended immutable image. Local ledger access, native human approval and in_process execution were not tested or authorized."
        if any(item in report["limitations"] for item in {"runtime_rollout_evidence_unavailable", "http_health_not_configured"}):
            message += " Runtime task evidence or HTTP health is unavailable; a later receipt may remain metadata-only."
        report["guidance"] = suggestion("request_immutable_image_plan", message, "caprover_plan", required_input=["image"])
    return report

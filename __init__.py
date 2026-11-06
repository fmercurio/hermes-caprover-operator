"""Native Hermes tools with a human-approved, per-call execution permit.

Registration is inert. Settings, credentials and storage are resolved lazily in
the active profile. The engine and HTTP client remain independent of Hermes.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import threading
import time
from typing import Any


PLUGIN_ID = "caprover-operator"
TOKEN_NAME = "CAPROVER_OPERATOR_TOKEN"
APPLY_TOOL = "caprover_apply"
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_PENDING_SECONDS = 300.0
_TICKET_SECONDS = 30.0
_MAX_PENDING = 128


class _Refused(Exception):
    def __init__(self, code: str):
        self.code = code


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _error(code: str, plan_id=None) -> str:
    from .caprover_operator.guidance import error_guidance, safe_error_code

    code = safe_error_code(code)
    messages = {
        "invalid_input": "Use only the arguments declared by this tool.",
        "configuration_required": "Configure the CapRover URL and app in this plugin's settings.",
        "token_missing": "Set CAPROVER_OPERATOR_TOKEN through Hermes credentials.",
        "native_approval_required": "Apply requires a fresh human approval in a compatible interactive Hermes session.",
        "approval_context_missing": "This Hermes call has no complete approval correlation context; nothing was applied.",
        "approval_pending": "An approval for this call is already pending; nothing was applied.",
        "configuration_changed": "The configured target or plan changed after approval. Inspect the plan before continuing.",
        "plan_not_applicable": "The plan is not pending or does not match the configured CapRover app.",
        "operation_failed": "The request could not be completed. Inspect its status before repeating an operation.",
    }
    return _json({"error": {"code": code, "message": messages.get(code, messages["operation_failed"])},
                  "guidance": error_guidance(code, plan_id)})


def _arguments(args: Any, required: set[str], optional: set[str] | None = None) -> dict:
    if not isinstance(args, dict) or not required.issubset(args) or set(args) - required - (optional or set()):
        raise _Refused("invalid_input")
    for key, value in args.items():
        if not isinstance(value, str) or not value or len(value) > (512 if key == "image" else 128):
            raise _Refused("invalid_input")
        if key == "plan_id" and not _ID.fullmatch(value):
            raise _Refused("invalid_input")
    return args


@dataclass(frozen=True)
class _Settings:
    url: str
    app: str
    health_url: str
    ttl: int


@dataclass(frozen=True)
class _Binding:
    scope: str
    target: str
    config_hash: str


@dataclass
class _Runtime:
    operator: Any
    settings: _Settings
    binding: _Binding


def _configuration_hash(settings: _Settings, target: str) -> str:
    public = {"target": target, "app": settings.app, "health_url": settings.health_url, "ttl": settings.ttl}
    return hashlib.sha256(_json(public).encode()).hexdigest()


def _token_provider() -> str:
    from agent.secret_scope import get_secret

    token = get_secret(TOKEN_NAME)
    if not isinstance(token, str) or not token:
        raise _Refused("token_missing")
    return token


def _prepare_private_directory(path: Path) -> Path:
    """Tighten Hermes' newly created 0755 directory without following links.

    Only the current user's directory with non-writable ancestry is eligible.
    The engine subsequently pins and checks the directory again before use.
    """
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "geteuid"):
        raise _Refused("secure_storage_unsupported")
    path = Path(os.path.abspath(os.fspath(path)))
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    current = -1
    try:
        current = os.open("/", flags)
        parts = path.parts[1:]
        if not parts:
            raise _Refused("unsafe_data_directory")
        for index, component in enumerate(parts):
            final = index == len(parts) - 1
            child = os.open(component, flags, dir_fd=current)
            os.close(current)
            current = child
            info = os.fstat(current)
            sticky_root = info.st_uid == 0 and bool(info.st_mode & stat.S_ISVTX) and not final
            if (info.st_uid not in {0, os.geteuid()}
                    or (info.st_mode & 0o022 and not sticky_root)
                    or (final and info.st_uid != os.geteuid())):
                raise _Refused("unsafe_data_directory")
        os.fchmod(current, 0o700)
        observed = os.stat(path, follow_symlinks=False)
        pinned = os.fstat(current)
        if (not stat.S_ISDIR(observed.st_mode)
                or (observed.st_dev, observed.st_ino) != (pinned.st_dev, pinned.st_ino)):
            raise _Refused("unsafe_data_directory")
        return path
    except OSError:
        raise _Refused("unsafe_data_directory") from None
    finally:
        if current != -1:
            os.close(current)


@contextmanager
def _make_runtime(settings: _Settings):
    from plugins.plugin_storage import plugin_data_dir
    from .caprover_operator.client import CapRoverClient
    from .caprover_operator.engine import Operator, load_fingerprint_key

    data_dir = _prepare_private_directory(Path(plugin_data_dir(PLUGIN_ID)))
    key = load_fingerprint_key(data_dir)
    client = CapRoverClient(
        settings.url,
        _token_provider,
        key,
        allowed_apps=[settings.app],
        health_urls={settings.app: settings.health_url} if settings.health_url else None,
        timeout=10.0,
    )
    binding = _Binding(str(data_dir.resolve()), client.target, _configuration_hash(settings, client.target))
    operator = Operator(data_dir, client)
    try:
        yield _Runtime(operator, settings, binding)
    finally:
        operator.close()


def _make_diagnostic_client(settings: _Settings, token_provider):
    """Validate settings and construct a read-only probe without persistent state."""
    import secrets
    from .caprover_operator.client import CapRoverClient

    return CapRoverClient(settings.url, token_provider, secrets.token_bytes(32),
                          allowed_apps=[settings.app],
                          health_urls={settings.app: settings.health_url} if settings.health_url else None,
                          timeout=10.0)


@dataclass(frozen=True)
class _Pending:
    plan_id: str
    plan_hash: str
    binding: _Binding
    rule_key: str
    deadline: float


@dataclass(frozen=True)
class _Ticket:
    pending: _Pending
    deadline: float


@dataclass
class _CallContext:
    key: tuple[str, str, str]
    plan_id: str
    plan_hash: str
    binding: _Binding
    used: bool = False


def _call_key(session_id: Any, turn_id: Any, tool_call_id: Any) -> tuple[str, str, str]:
    values = (session_id, turn_id, tool_call_id)
    if any(not isinstance(value, str) or not value or len(value) > 256 for value in values):
        raise _Refused("approval_context_missing")
    return values


class _Adapter:
    def __init__(self, ctx):
        self.ctx = ctx
        self._lock = threading.Lock()
        self._pending: dict[tuple[str, str, str], _Pending] = {}
        self._tickets: dict[tuple[str, str, str], _Ticket] = {}
        self._active_calls: set[tuple[str, str, str]] = set()
        self._call_context: ContextVar[_CallContext | None] = ContextVar("caprover_operator_call", default=None)

    def _configuration(self):
        url = self.ctx.get_config("url", default="")
        app = self.ctx.get_config("app_name", default="")
        health_url = self.ctx.get_config("health_url", default="")
        ttl = self.ctx.get_config("plan_ttl_seconds", default=300)
        missing = [key for key, value in (("url", url), ("app_name", app)) if isinstance(value, str) and not value.strip()]
        invalid = [key for key, value in (("url", url), ("app_name", app), ("health_url", health_url)) if not isinstance(value, str)]
        if type(ttl) is not int or not 30 <= ttl <= 3600:
            invalid.append("plan_ttl_seconds")
        if missing or invalid:
            return None, {"status": "missing_or_invalid", "missing_fields": missing, "invalid_fields": invalid}
        return _Settings(url.strip(), app.strip(), health_url.strip(), ttl), {"status": "present"}

    def _settings(self) -> _Settings:
        settings, _ = self._configuration()
        if settings is None:
            raise _Refused("configuration_required")
        return settings

    def doctor(self, args, **kwargs):
        from .caprover_operator.diagnostics import inspect_read_only, new_report
        from .caprover_operator.guidance import error_guidance, safe_error_code, suggestion

        try:
            _arguments(args, set())
        except _Refused as exc:
            return _error(exc.code)
        report = new_report()
        checks = report["checks"]
        try:
            settings, checks["configuration"] = self._configuration()
        except Exception:
            report["status"] = "unavailable"
            checks["configuration"] = {"status": "unavailable"}
            report["guidance"] = error_guidance("configuration_required")
            return _json(report)
        token = None
        try:
            token = _token_provider()
            checks["credential"] = {"status": "present_unverified"}
        except Exception as exc:
            code = safe_error_code(getattr(exc, "code", None))
            checks["credential"] = {"status": "missing" if code == "token_missing" else "unavailable"}
        if settings is None:
            report["guidance"] = error_guidance("configuration_required")
            return _json(report)
        try:
            client = _make_diagnostic_client(settings, lambda: token)
        except Exception as exc:
            code = safe_error_code(getattr(exc, "code", None))
            if code == "configuration_invalid":
                checks["configuration"] = {"status": "invalid"}
                report["guidance"] = error_guidance(code)
            else:
                report["status"] = "unavailable"
                report["guidance"] = suggestion("diagnostic_unavailable",
                    "The read-only diagnostic could not be initialized. Check the Hermes installation and owner settings, then explicitly run the doctor again. No deployment was attempted.",
                    "caprover_doctor")
            return _json(report)
        checks["configuration"] = {"status": "valid"}
        if token is None:
            missing = checks["credential"]["status"] == "missing"
            report["status"] = "needs_credentials" if missing else "unavailable"
            report["guidance"] = error_guidance("token_missing" if missing else "authentication_unavailable")
            return _json(report)
        return _json(inspect_read_only(client, settings.app, report))

    def _prune(self, now: float) -> None:
        for key in [key for key, item in self._pending.items() if item.deadline <= now]:
            self._pending.pop(key, None)
            self._tickets.pop(key, None)
        for key in [key for key, item in self._tickets.items() if item.deadline <= now]:
            self._tickets.pop(key, None)

    @staticmethod
    def _bound_plan(runtime: _Runtime, plan: Any, *, pending_only: bool = False) -> dict:
        if (
            not isinstance(plan, dict)
            or not isinstance(plan.get("plan_id"), str)
            or not _ID.fullmatch(plan["plan_id"])
            or not isinstance(plan.get("plan_hash"), str)
            or not _HASH.fullmatch(plan["plan_hash"])
            or plan.get("target") != runtime.binding.target
            or plan.get("app") != runtime.settings.app
            or (pending_only and plan.get("status") != "planned")
        ):
            raise _Refused("plan_not_applicable")
        return plan

    @staticmethod
    def _same_plan(pending: _Pending, runtime: _Runtime, plan: dict) -> bool:
        return (
            pending.binding == runtime.binding
            and pending.plan_id == plan.get("plan_id")
            and pending.plan_hash == plan.get("plan_hash")
        )

    def _run(self, operation, plan_id=None) -> str:
        from .caprover_operator.guidance import explain
        from .caprover_operator.presentation import present

        try:
            with _make_runtime(self._settings()) as runtime:
                result = explain(operation(runtime), plan_id, configured_app=runtime.settings.app,
                                 configured_target=runtime.binding.target)
                try:
                    result = present(result, data_dir=Path(runtime.binding.scope),
                                     app=runtime.settings.app, target=runtime.binding.target)
                except Exception:
                    # A display failure must never obscure the durable outcome
                    # of a possible deployment or invite repeating apply.
                    pass
                return _json(result)
        except _Refused as exc:
            return _error(exc.code, plan_id)
        except Exception as exc:
            return _error(getattr(exc, "code", None), plan_id)

    def plan(self, args, **kwargs):
        try:
            values = _arguments(args, {"image"})
        except _Refused as exc:
            return _error(exc.code)
        return self._run(lambda runtime: runtime.operator.plan(runtime.settings.app, values["image"], runtime.settings.ttl))

    def status(self, args, **kwargs):
        try:
            values = _arguments(args, set(), {"plan_id"})
        except _Refused as exc:
            return _error(exc.code)
        return self._run(lambda runtime: runtime.operator.status(values.get("plan_id")), values.get("plan_id"))

    def reconcile(self, args, **kwargs):
        try:
            values = _arguments(args, {"plan_id"})
        except _Refused as exc:
            return _error(exc.code)

        def observe(runtime):
            self._bound_plan(runtime, runtime.operator.get_plan(values["plan_id"]))
            return runtime.operator.reconcile(values["plan_id"])

        return self._run(observe, values["plan_id"])

    def apply(self, args, *, session_id="", **kwargs):
        try:
            values = _arguments(args, {"plan_id"})
            context = self._call_context.get()
            if context is None or context.key[0] != session_id or context.plan_id != values["plan_id"]:
                raise _Refused("native_approval_required")
            with self._lock:
                if context.used:
                    raise _Refused("native_approval_required")
                context.used = True
                ticket = self._tickets.pop(context.key, None)
                self._pending.pop(context.key, None)
            if ticket is None or ticket.deadline <= time.monotonic():
                raise _Refused("native_approval_required")
            if (context.plan_hash != ticket.pending.plan_hash
                    or context.binding != ticket.pending.binding
                    or context.plan_id != ticket.pending.plan_id):
                raise _Refused("configuration_changed")
        except _Refused as exc:
            return _error(exc.code, args.get("plan_id") if isinstance(args, dict) else None)

        def execute(runtime):
            plan = self._bound_plan(runtime, runtime.operator.get_plan(values["plan_id"]))
            if not self._same_plan(ticket.pending, runtime, plan):
                raise _Refused("configuration_changed")

            def authorize(actual_plan):
                return (
                    time.monotonic() < ticket.deadline
                    and self._same_plan(ticket.pending, runtime, actual_plan)
                    and self._settings() == runtime.settings
                )

            return runtime.operator.apply(values["plan_id"], authorize)

        return self._run(execute, values["plan_id"])

    def before_tool(self, tool_name, args, *, session_id="", turn_id="", tool_call_id="", **kwargs):
        if tool_name != APPLY_TOOL:
            return None
        try:
            values = _arguments(args, {"plan_id"})
            key = _call_key(session_id, turn_id, tool_call_id)
            with _make_runtime(self._settings()) as runtime:
                plan = self._bound_plan(runtime, runtime.operator.get_plan(values["plan_id"]), pending_only=True)
                rule_key = f"{PLUGIN_ID}:apply:{plan['plan_id']}:{plan['plan_hash']}"
                pending = _Pending(plan["plan_id"], plan["plan_hash"], runtime.binding, rule_key, time.monotonic() + _PENDING_SECONDS)
                baseline = plan.get("baseline", {})
                message = (
                    f"Apply plan {plan['plan_id']} to {plan['app']} at {plan['target']}: "
                    f"deploy {plan.get('image', '')}. Baseline generation {baseline.get('version', 'unknown')}, "
                    f"requested replicas {baseline.get('replicas', 'unknown')}. "
                    "This permits one deployment attempt. A lost reply requires reconciliation. "
                    "The image's startup behavior may run migrations or other side effects. "
                    "Image deployment does not restore environment variables, volumes or databases."
                )
            with self._lock:
                self._prune(time.monotonic())
                if key in self._pending or len(self._pending) >= _MAX_PENDING:
                    raise _Refused("approval_pending")
                self._pending[key] = pending
            return {"action": "approve", "message": message, "rule_key": rule_key}
        except _Refused as exc:
            return {"action": "block", "message": _error(exc.code, args.get("plan_id") if isinstance(args, dict) else None)}
        except Exception:
            return {"action": "block", "message": _error("operation_failed", args.get("plan_id") if isinstance(args, dict) else None)}

    def after_approval(
        self, *, pattern_key="", pattern_keys=None, choice="", decided_by=None,
        session_id="", turn_id="", tool_call_id="", **kwargs,
    ):
        try:
            key = _call_key(session_id, turn_id, tool_call_id)
            now = time.monotonic()
            with self._lock:
                self._prune(now)
                pending = self._pending.get(key)
                if pending is None:
                    return
                expected = f"plugin_rule:{pending.rule_key}"
                if pattern_key != expected or (pattern_keys is not None and pattern_keys != [expected]):
                    return
                self._tickets.pop(key, None)
                if choice not in {"once", "session", "always"} or decided_by not in {None, "", "human", "user"}:
                    self._pending.pop(key, None)
                    return
                self._tickets[key] = _Ticket(pending, min(pending.deadline, now + _TICKET_SECONDS))
        except Exception:
            # Observer failure must never manufacture approval. The handler also
            # requires the ticket and a trusted execution context.
            return

    def execution(self, tool_name, args, next_call, *, session_id="", turn_id="", tool_call_id="", **kwargs):
        if tool_name != APPLY_TOOL:
            return next_call(args)
        key, owns_call, context_token = None, False, None
        try:
            try:
                values = _arguments(args, {"plan_id"})
                key = _call_key(session_id, turn_id, tool_call_id)
                with self._lock:
                    self._prune(time.monotonic())
                    if key in self._active_calls:
                        raise _Refused("native_approval_required")
                    self._active_calls.add(key)
                    owns_call = True
                with _make_runtime(self._settings()) as runtime:
                    plan = self._bound_plan(runtime, runtime.operator.get_plan(values["plan_id"]), pending_only=True)
                    context = _CallContext(key, plan["plan_id"], plan["plan_hash"], runtime.binding)
            except _Refused as exc:
                return _error(exc.code, args.get("plan_id") if isinstance(args, dict) else None)
            except Exception:
                return _error("operation_failed", args.get("plan_id") if isinstance(args, dict) else None)

            # AIAgent wraps its approval gate inside execution middleware;
            # the direct dispatcher runs that gate before middleware. Bind
            # identity here, but let the handler consume a real post-approval
            # ticket only after next_call has traversed the host's gate.
            context_token = self._call_context.set(context)
            return next_call(args)
        finally:
            if context_token is not None:
                self._call_context.reset(context_token)
            if owns_call:
                with self._lock:
                    self._pending.pop(key, None)
                    self._tickets.pop(key, None)
                    self._active_calls.discard(key)


def _schema(name, description, properties, required):
    return {
        "name": name,
        "description": description,
        "parameters": {"type": "object", "properties": properties, "required": required, "additionalProperties": False},
    }


def register(ctx):
    adapter = _Adapter(ctx)
    tools = (
        ("caprover_doctor", "Diagnose first-use configuration, credential presence, API/app access and optional runtime/health using read-only checks. No ledger, plan, deployment or approval is created; readiness is for planning only.", {}, [], adapter.doctor),
        ("caprover_plan", "Plan an image deployment for the owner-configured app. Reads current state and saves a private plan; does not deploy.", {"image": {"type": "string", "description": "Immutable image reference ending in @sha256:<64 hex characters>."}}, ["image"], adapter.plan),
        (APPLY_TOOL, "Apply one saved plan after human approval in Hermes. Refuses changed state and repeated or uncertain attempts. Server and app come from owner settings.", {"plan_id": {"type": "string", "description": "Plan ID returned by caprover_plan."}}, ["plan_id"], adapter.apply),
        ("caprover_reconcile", "Observe an existing deployment attempt and update its receipt. Never sends a deployment request.", {"plan_id": {"type": "string"}}, ["plan_id"], adapter.reconcile),
        ("caprover_status", "Read one saved plan/receipt or list recent ones. Historical evidence includes its observation time.", {"plan_id": {"type": "string"}}, [], adapter.status),
    )
    for name, description, properties, required, handler in tools:
        if name != "caprover_doctor":
            description += " Results may include historical operation cards: display each available presentation.cards[].preview directive unchanged on its own line. Cards cannot execute or approve; never repeat an operation to repair a preview. JSON remains the fallback."
        ctx.register_tool(name=name, toolset="caprover_operator", schema=_schema(name, description, properties, required), handler=handler)
    ctx.register_hook("pre_tool_call", adapter.before_tool)
    ctx.register_hook("post_approval_response", adapter.after_approval)
    ctx.register_middleware("tool_execution", adapter.execution)

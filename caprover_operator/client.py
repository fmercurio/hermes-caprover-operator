"""Small, non-retrying CapRover API v2 transport.

The caller owns approvals and the durable operation ledger. This module never
interprets an accepted request as application health or as a running task count.
"""
from __future__ import annotations

import hashlib
import hmac
import http.client
import ipaddress
import json
import math
import queue
import re
import socket
import ssl
import threading
import time
from collections.abc import Callable, Mapping
from urllib.parse import quote, urlsplit


MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_APP = re.compile(r"[a-z0-9][a-z0-9-]{0,62}\Z", re.ASCII)
_IMAGE = re.compile(
    r"[a-z0-9][a-z0-9._:-]*(?:/[a-z0-9][a-z0-9._-]*)*"
    r"(?::[A-Za-z0-9_][A-Za-z0-9_.-]{0,127})?"
    r"@sha256:[a-f0-9]{64}\Z", re.ASCII
)
_RUNTIME_IMAGE = re.compile(
    r"[a-z0-9][a-z0-9._:-]*(?:/[a-z0-9][a-z0-9._-]*)*"
    r"(?::[A-Za-z0-9_][A-Za-z0-9_.-]{0,127})?(?:@sha256:[a-f0-9]{64})?\Z", re.ASCII
)
_TASK_STATES = frozenset({"new", "allocated", "pending", "assigned", "accepted", "ready",
                          "preparing", "starting", "running", "complete", "shutdown", "failed",
                          "rejected", "orphaned", "remove"})
MAX_RUNTIME_TASKS = 1024
MAX_RUNTIME_INSTANCES = 1_000_000


class ClientError(Exception):
    """Expected client failure, containing only a fixed, non-secret message."""

    def __init__(self, code: str = "request_failed") -> None:
        self.code = code
        super().__init__({
            "configuration_invalid": "CapRover client configuration is invalid.",
            "app_not_allowed": "The application is not configured for this client.",
            "app_not_found": "The configured application was not found on this server.",
            "image_invalid": "An immutable SHA-256 image reference is required.",
            "authentication_unavailable": "The configured credential is unavailable.",
            "authentication_rejected": "CapRover rejected authentication.",
            "authorization_denied": "CapRover denied the request.",
            "redirect_rejected": "The server redirected the request; it was not followed.",
            "response_invalid": "CapRover returned an unsupported response.",
            "response_too_large": "CapRover exceeded the response size limit.",
            "endpoint_unavailable": "The requested CapRover endpoint is unavailable.",
            "request_timeout": "The request deadline expired; observe before another write.",
            "request_failed": "The request did not complete; observe before another write.",
        }.get(code, "The request could not be completed."))


def _url(value: str, *, insecure_local: bool, origin_only: bool) -> tuple[str, str, int, str]:
    try:
        if (not isinstance(value, str) or not value or len(value) > 4096
                or any(ord(c) < 33 or ord(c) > 126 for c in value) or "\\" in value):
            raise ValueError
        parsed = urlsplit(value)
        if (parsed.scheme not in {"https", "http"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.fragment or parsed.query or "%" in parsed.netloc):
            raise ValueError
        host = parsed.hostname.lower()
        if host.endswith(".") or not re.fullmatch(r"[a-z0-9.:-]+", host):
            raise ValueError
        port = parsed.port if parsed.port is not None else (443 if parsed.scheme == "https" else 80)
        if not 1 <= port <= 65535:
            raise ValueError
        if parsed.scheme == "http":
            loopback = host == "localhost"
            try:
                loopback = loopback or ipaddress.ip_address(host).is_loopback
            except ValueError:
                pass
            if not insecure_local or not loopback:
                raise ValueError
        path = parsed.path or "/"
        if origin_only and path != "/":
            raise ValueError
        return parsed.scheme, host, port, path
    except (ValueError, TypeError):
        raise ClientError("configuration_invalid") from None


def _origin(parts: tuple[str, str, int, str]) -> str:
    scheme, host, port, _ = parts
    netloc = f"[{host}]" if ":" in host else host
    default = 443 if scheme == "https" else 80
    return f"{scheme}://{netloc}" + (f":{port}" if port != default else "")


def _json_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


class CapRoverClient:
    def __init__(
        self, base_url: str, token_provider: Callable[[], str], fingerprint_key: bytes,
        *, allowed_apps, health_urls: Mapping[str, str] | None = None,
        timeout: float = 10.0, allow_insecure_local: bool = False,
    ) -> None:
        if (not callable(token_provider) or not isinstance(fingerprint_key, bytes)
                or len(fingerprint_key) < 32 or isinstance(timeout, bool)
                or not isinstance(timeout, (float, int)) or not math.isfinite(timeout)
                or timeout <= 0 or timeout > 120 or type(allow_insecure_local) is not bool):
            raise ClientError("configuration_invalid")
        if isinstance(allowed_apps, (str, bytes)):
            raise ClientError("configuration_invalid")
        try:
            apps = frozenset(allowed_apps)
        except (TypeError, ValueError):
            raise ClientError("configuration_invalid") from None
        if not apps or any(not isinstance(a, str) or not _APP.fullmatch(a) for a in apps):
            raise ClientError("configuration_invalid")
        self._parts = _url(base_url, insecure_local=allow_insecure_local, origin_only=True)
        self.target = _origin(self._parts)
        self._token_provider = token_provider
        self._fingerprint_key = fingerprint_key
        self._apps = apps
        self._timeout = float(timeout)
        self._health = {}
        if health_urls is not None and not isinstance(health_urls, Mapping):
            raise ClientError("configuration_invalid")
        for app, url in (health_urls or {}).items():
            self._allow(app)
            self._health[app] = _url(url, insecure_local=allow_insecure_local, origin_only=False)

    def _allow(self, app: str) -> None:
        if not isinstance(app, str) or app not in self._apps:
            raise ClientError("app_not_allowed")

    def _token(self) -> str:
        try:
            token = self._token_provider()
        except Exception:
            raise ClientError("authentication_unavailable") from None
        if (not isinstance(token, str) or not token or len(token) > 8192
                or any(ord(c) < 33 or ord(c) > 126 for c in token)):
            raise ClientError("authentication_unavailable")
        return token

    def _http(self, parts, method: str, path: str, *, token=None, body=None, status_only=False):
        """One connection and request; a timer interrupts slow headers/body reads.

        Connect/DNS completion is checked again before sending. A deadline never
        launches a replacement request, refreshes credentials or follows a URL.
        """
        scheme, host, port, _ = parts
        connection = (
            http.client.HTTPSConnection(host, port, timeout=self._timeout, context=ssl.create_default_context())
            if scheme == "https" else http.client.HTTPConnection(host, port, timeout=self._timeout)
        )
        deadline = time.monotonic() + self._timeout
        expired = threading.Event()
        live_socket: list[socket.socket] = []

        def connect_socket(address, timeout=None, source_address=None, **_kwargs):
            # Resolve in a DNS-only daemon: a stuck system resolver cannot delay
            # the caller indefinitely or later send an abandoned POST.
            results = queue.Queue(maxsize=1)

            def resolve():
                try:
                    results.put(socket.getaddrinfo(address[0], address[1], type=socket.SOCK_STREAM))
                except Exception:
                    results.put(None)

            resolver = threading.Thread(target=resolve, daemon=True)
            resolver.start()
            try:
                addresses = results.get(timeout=max(0.001, deadline - time.monotonic()))
            except queue.Empty:
                raise ClientError("request_timeout") from None
            if not addresses:
                raise ClientError("request_failed")
            for family, kind, proto, _, destination in addresses:
                remaining = deadline - time.monotonic()
                if expired.is_set() or remaining <= 0:
                    raise ClientError("request_timeout")
                sock = socket.socket(family, kind, proto)
                live_socket.append(sock)
                try:
                    sock.settimeout(remaining)
                    if source_address:
                        sock.bind(source_address)
                    sock.connect(destination)
                    remaining = deadline - time.monotonic()
                    if expired.is_set() or remaining <= 0:
                        sock.close()
                        raise ClientError("request_timeout")
                    sock.settimeout(remaining)
                    return sock
                except OSError:
                    sock.close()
            raise ClientError("request_failed")

        connection._create_connection = connect_socket

        def abort():
            expired.set()
            for sock in live_socket:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                try:
                    sock.close()
                except OSError:
                    pass

        timer = threading.Timer(self._timeout, abort)
        timer.daemon = True
        timer.start()
        response = None
        try:
            connection.connect()
            if expired.is_set() or time.monotonic() >= deadline or connection.sock is None:
                raise ClientError("request_timeout")
            live_socket.append(connection.sock)
            if scheme == "http" and not ipaddress.ip_address(connection.sock.getpeername()[0]).is_loopback:
                raise ClientError("configuration_invalid")
            headers = {"Accept": "application/json", "User-Agent": "hermes-caprover-operator/0.1", "Connection": "close"}
            if token is not None:
                headers["x-captain-auth"] = token
            if body is not None:
                headers["Content-Type"] = "application/json"
            # There is deliberately no redirect/auth/retry handler here.
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            status = response.status
            if expired.is_set():
                raise ClientError("request_timeout")
            if 300 <= status < 400:
                raise ClientError("redirect_rejected")
            if status_only:
                return status, None
            if status == 401:
                raise ClientError("authentication_rejected")
            if status == 403:
                raise ClientError("authorization_denied")
            if status == 404:
                raise ClientError("endpoint_unavailable")
            if not 200 <= status < 300:
                raise ClientError("request_failed")
            length = response.getheader("Content-Length")
            if length is not None:
                if not re.fullmatch(r"[0-9]+", length):
                    raise ClientError("response_invalid")
                if len(length) > 12 or int(length) > MAX_RESPONSE_BYTES:
                    raise ClientError("response_too_large")
            if response.getheader("Content-Encoding", "identity").lower() != "identity":
                raise ClientError("response_invalid")
            data = bytearray()
            while True:
                chunk = response.read1(min(65536, MAX_RESPONSE_BYTES + 1 - len(data)))
                if expired.is_set() or time.monotonic() >= deadline:
                    raise ClientError("request_timeout")
                if not chunk:
                    break
                data.extend(chunk)
                if len(data) > MAX_RESPONSE_BYTES:
                    raise ClientError("response_too_large")
            if length is not None and len(data) != int(length):
                raise ClientError("response_invalid")
            try:
                result = json.loads(data, object_pairs_hook=_json_object,
                                    parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            except (ValueError, UnicodeError, RecursionError):
                raise ClientError("response_invalid") from None
            return status, result
        except ClientError:
            raise
        except Exception:
            code = "request_timeout" if expired.is_set() or time.monotonic() >= deadline else "request_failed"
            raise ClientError(code) from None
        finally:
            timer.cancel()
            if response is not None:
                response.close()
            connection.close()

    def _api(self, method: str, path: str, payload=None, *, require_data=True) -> dict:
        token = self._token()
        body = None if payload is None else json.dumps(payload, separators=(",", ":"), allow_nan=False).encode()
        _, envelope = self._http(self._parts, method, path, token=token, body=body)
        if not isinstance(envelope, dict) or type(envelope.get("status")) is not int:
            raise ClientError("response_invalid")
        code = envelope["status"]
        if code in {1105, 1106, 1112, 1113, 1115}:
            raise ClientError("authentication_rejected")
        if code == 1102:
            raise ClientError("authorization_denied")
        if code != 100:
            raise ClientError("response_invalid")
        data = envelope.get("data")
        if require_data and not isinstance(data, dict):
            raise ClientError("response_invalid")
        return data if require_data else {"accepted": True}

    def snapshot(self, app: str) -> dict:
        self._allow(app)
        data = self._api("GET", "/api/v2/user/apps/appDefinitions/")
        definitions = data.get("appDefinitions")
        if not isinstance(definitions, list) or any(not isinstance(v, dict) for v in definitions):
            raise ClientError("response_invalid")
        matches = [v for v in definitions if v.get("appName") == app]
        if not matches:
            raise ClientError("app_not_found")
        if len(matches) != 1:
            raise ClientError("response_invalid")
        definition = matches[0]
        version, replicas = definition.get("deployedVersion"), definition.get("instanceCount")
        if type(version) is not int or version < 0 or type(replicas) is not int or replicas < 0:
            raise ClientError("response_invalid")
        versions = definition.get("versions")
        if not isinstance(versions, list) or any(not isinstance(v, dict) for v in versions):
            raise ClientError("response_invalid")
        history = [v for v in versions if type(v.get("version")) is int and v["version"] == version]
        if len(history) > 1:
            raise ClientError("response_invalid")
        image = history[0].get("deployedImageName") if history else None
        if image is not None and (not isinstance(image, str) or not image or len(image) > 2048
                                  or any(ord(c) < 33 or ord(c) > 126 for c in image)):
            raise ClientError("response_invalid")
        build = self._api("GET", f"/api/v2/user/apps/appData/{quote(app, safe='')}/")
        if type(build.get("isAppBuilding")) is not bool or type(build.get("isBuildFailed")) is not bool:
            raise ClientError("response_invalid")
        definition_building = definition.get("isAppBuilding", False)
        if type(definition_building) is not bool:
            raise ClientError("response_invalid")
        try:
            canonical = json.dumps(definition, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()
        except (ValueError, RecursionError):
            raise ClientError("response_invalid") from None
        fingerprint = hmac.new(self._fingerprint_key, canonical, hashlib.sha256).hexdigest()
        runtime = self._runtime(app)
        return {"app": app, "version": version, "image": image, "replicas": replicas,
                "is_building": build["isAppBuilding"] or definition_building,
                "config_fingerprint": fingerprint, "runtime": runtime,
                "running_replicas": runtime["running_instances"] if runtime["available"] else None}

    @staticmethod
    def _runtime_unavailable(reason: str) -> dict:
        return {"available": False, "reason": reason, "desired_instances": None,
                "running_instances": None, "current_tasks": []}

    def _runtime(self, app: str) -> dict:
        """Optional capability: old servers and failed collectors stay unknown.

        Only current task identity/state is retained, never task error strings,
        node details, runtime logs, history or remote commands.
        """
        try:
            data = self._api("GET", f"/api/v2/user/apps/runtime/{quote(app, safe='')}")
        except ClientError as exc:
            return self._runtime_unavailable(exc.code)
        status = data.get("status")
        if (data.get("appName") != app or not isinstance(status, str)
                or status not in {"running", "degraded", "stopped", "unavailable"}):
            return self._runtime_unavailable("response_invalid")
        if status == "unavailable" or data.get("error") is not None:
            return self._runtime_unavailable("collection_unavailable")
        desired, running = data.get("desiredInstances"), data.get("runningInstances")
        if any(type(v) is not int or not 0 <= v <= MAX_RUNTIME_INSTANCES for v in (desired, running)):
            return self._runtime_unavailable("response_invalid")
        tasks = data.get("currentTasks")
        if not isinstance(tasks, list) or len(tasks) > MAX_RUNTIME_TASKS or running > len(tasks):
            return self._runtime_unavailable("response_invalid")
        current = []
        for task in tasks:
            if not isinstance(task, dict):
                return self._runtime_unavailable("response_invalid")
            slot, task_image = task.get("slot"), task.get("image")
            state, wanted = task.get("state"), task.get("desiredState")
            if (slot is not None and (type(slot) is not int or not 1 <= slot <= MAX_RUNTIME_INSTANCES)):
                return self._runtime_unavailable("response_invalid")
            if (not isinstance(task_image, str) or len(task_image) > 512
                    or not _RUNTIME_IMAGE.fullmatch(task_image) or "//" in task_image or "://" in task_image
                    or not isinstance(state, str) or not isinstance(wanted, str)
                    or state not in _TASK_STATES or wanted not in _TASK_STATES):
                return self._runtime_unavailable("response_invalid")
            current.append({"slot": slot, "image": task_image, "state": state, "desired_state": wanted})
        return {"available": True, "reason": "observed", "desired_instances": desired,
                "running_instances": running, "current_tasks": current}

    def deploy_image(self, app: str, image: str) -> dict:
        self._allow(app)
        if (not isinstance(image, str) or len(image) > 512 or not _IMAGE.fullmatch(image)
                or "//" in image or "://" in image):
            raise ClientError("image_invalid")
        qualified = image.split("@", 1)[0]
        prefix, separator, leaf = qualified.rpartition("/")
        # Consecutive dots are valid in an OCI tag (e.g. v1..2), but not
        # admitted in a repository/registry name by this client's contract.
        repository = (prefix + separator) + leaf.split(":", 1)[0]
        if ".." in repository:
            raise ClientError("image_invalid")
        captain = json.dumps({"schemaVersion": 2, "imageName": image}, separators=(",", ":"))
        return self._api("POST", f"/api/v2/user/apps/appData/{quote(app, safe='')}/",
                         {"captainDefinitionContent": captain}, require_data=False)

    def health(self, app: str) -> dict:
        self._allow(app)
        parts = self._health.get(app)
        if parts is None:
            return {"configured": False, "ok": None, "status_code": None, "reason": "not_configured"}
        try:
            status, _ = self._http(parts, "GET", parts[3], status_only=True)
            return {"configured": True, "ok": 200 <= status < 300, "status_code": status,
                    "reason": "http_ok" if 200 <= status < 300 else "http_unhealthy"}
        except ClientError as exc:
            return {"configured": True, "ok": False, "status_code": None, "reason": exc.code}

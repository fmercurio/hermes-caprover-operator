"""Disposable HTTP protocol fixture, NOT a real CapRover/Docker instance.

Only binds 127.0.0.1. It changes synthetic in-memory definitions and can drop
the acknowledgement *after* a deployment is applied. No images are executed.
"""
from __future__ import annotations

import copy
import http.server
import json
import socket
import socketserver
import threading
import time
from urllib.parse import urlsplit


OLD_IMAGE = "ghcr.io/example/demo@sha256:" + "1" * 64
NEW_IMAGE = "ghcr.io/example/demo@sha256:" + "2" * 64


class FakeCapRover:
    def __init__(self, app="demo-app", token="fixture-token"):
        self.app = app
        self.token = token
        self.definition = {
            "appName": app, "deployedVersion": 1, "instanceCount": 1,
            "versions": [{"version": 1, "deployedImageName": OLD_IMAGE, "gitHash": ""}],
            "envVars": [{"key": "SYNTHETIC_SECRET", "value": "synthetic-env-value"}],
            "volumes": [], "ports": [], "containerHttpPort": 80,
            "notExposeAsWebApp": False, "forceSsl": False,
            "websocketSupport": False, "isLegacyAppName": True,
        }
        self.additional_definitions = []
        self.build_status = {"isAppBuilding": False, "isBuildFailed": False, "logs": "synthetic build log"}
        self.health_status = 200
        self.health_body = b"synthetic health body must not enter receipts"
        self.trigger_count = 0
        self.requests = []
        self.deployments = []
        self.lose_next_ack = False
        self.post_delay = 0.0
        self.post_building = False
        self.apply_on_post = True
        self.runtime_available = False
        self.runtime = {"appName": app, "status": "running", "desiredInstances": 1,
                        "runningInstances": 1, "error": None, "latestFailure": None,
                        "currentTasks": [{"taskId": "synthetic-task-1", "slot": 1, "image": OLD_IMAGE,
                                          "state": "running", "desiredState": "running", "error": None,
                                          "message": "synthetic message not for receipts"}], "taskHistory": []}
        self.redirects = {}
        self.body_overrides = {}
        self.http_statuses = {}
        self.captain_statuses = {}
        self.delays = {}
        self.drip_interval = 0.0
        self._lock = threading.RLock()
        fixture = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):
                pass

            def respond(self, value, status=200):
                path = urlsplit(self.path).path
                delay = fixture.delays.get(path, 0)
                if delay:
                    time.sleep(delay)
                if path in fixture.redirects:
                    self.send_response(302)
                    self.send_header("Location", fixture.redirects[path])
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                body = fixture.body_overrides.get(path)
                if body is None:
                    body = value if isinstance(value, bytes) else json.dumps(value).encode()
                self.send_response(fixture.http_statuses.get(path, status))
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    if fixture.drip_interval:
                        for offset in range(0, len(body), 8):
                            self.wfile.write(body[offset:offset + 8])
                            self.wfile.flush()
                            time.sleep(fixture.drip_interval)
                    else:
                        self.wfile.write(body)
                except (OSError, ValueError):
                    pass

            def authenticated(self):
                return self.headers.get("x-captain-auth") == fixture.token

            def record(self):
                with fixture._lock:
                    fixture.requests.append({"method": self.command, "path": self.path,
                                             "auth_present": "x-captain-auth" in self.headers})

            def do_GET(self):
                self.record()
                path = urlsplit(self.path).path
                if path == "/health":
                    self.respond(fixture.health_body, fixture.health_status)
                    return
                if not self.authenticated():
                    self.respond({"status": 1106, "description": "synthetic-token-error"})
                    return
                with fixture._lock:
                    if path.rstrip("/") == "/api/v2/user/apps/appDefinitions":
                        data = {"appDefinitions": [copy.deepcopy(fixture.definition),
                                                   *copy.deepcopy(fixture.additional_definitions)]}
                    elif path.rstrip("/") == f"/api/v2/user/apps/appData/{fixture.app}":
                        data = copy.deepcopy(fixture.build_status)
                    elif path.rstrip("/") == f"/api/v2/user/apps/runtime/{fixture.app}" and fixture.runtime_available:
                        data = copy.deepcopy(fixture.runtime)
                    else:
                        self.respond({"status": 1110}, 404)
                        return
                self.respond({"status": fixture.captain_statuses.get(path, 100), "data": data})

            def do_POST(self):
                self.record()
                path = urlsplit(self.path).path
                if not self.authenticated():
                    self.respond({"status": 1106})
                    return
                if path.rstrip("/") != f"/api/v2/user/apps/appData/{fixture.app}":
                    self.respond({"status": 1110}, 404)
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length <= 0 or length > 8192:
                        raise ValueError
                    payload = json.loads(self.rfile.read(length))
                    definition = json.loads(payload["captainDefinitionContent"])
                    if set(payload) != {"captainDefinitionContent"} or definition.get("schemaVersion") != 2:
                        raise ValueError
                    image = definition["imageName"]
                    if not isinstance(image, str):
                        raise ValueError
                except (ValueError, KeyError, TypeError):
                    self.respond({"status": 1110})
                    return
                with fixture._lock:
                    fixture.trigger_count += 1
                    fixture.deployments.append(copy.deepcopy(definition))
                    if fixture.apply_on_post:
                        version = fixture.definition["deployedVersion"] + 1
                        fixture.definition["deployedVersion"] = version
                        fixture.definition["versions"].append({"version": version, "deployedImageName": image, "gitHash": ""})
                    fixture.build_status["isAppBuilding"] = fixture.post_building
                    lose_ack = fixture.lose_next_ack
                    fixture.lose_next_ack = False
                if lose_ack:
                    self.close_connection = True
                    try:
                        self.connection.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    self.connection.close()
                    return
                if fixture.post_delay:
                    time.sleep(fixture.post_delay)
                self.respond({"status": fixture.captain_statuses.get(path, 100),
                              "description": "Synthetic image request accepted"})

        class LoopbackServer(http.server.ThreadingHTTPServer):
            def server_bind(self):
                # HTTPServer's default getfqdn adds unnecessary reverse DNS to
                # a loopback-only fixture and can stall on a VPN resolver.
                socketserver.TCPServer.server_bind(self)
                self.server_name = "127.0.0.1"
                self.server_port = self.server_address[1]

        self._server = LoopbackServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self.base_url = f"http://127.0.0.1:{self._server.server_port}"
        self.url = self.base_url
        self.health_url = self.base_url + "/health"
        self._thread = None

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=lambda: self._server.serve_forever(poll_interval=0.02), daemon=True)
            self._thread.start()
        return self

    def close(self):
        if self._thread is not None:
            self._server.shutdown()
            self._thread.join(timeout=2)
        self._server.server_close()

    def client(self, *, timeout=1.0, health=True, fingerprint_key=b"f" * 32):
        from caprover_operator.client import CapRoverClient
        return CapRoverClient(self.base_url, lambda: self.token, fingerprint_key,
                             allowed_apps=[self.app], timeout=timeout,
                             health_urls={self.app: self.health_url} if health else None,
                             allow_insecure_local=True)

    def drift(self, **fields):
        with self._lock:
            self.definition.update(copy.deepcopy(fields))

    def sync_runtime_to_definition(self):
        """Explicit fixture convergence; metadata updates alone do NOT do this."""
        with self._lock:
            version = self.definition["deployedVersion"]
            image = next(v["deployedImageName"] for v in self.definition["versions"] if v["version"] == version)
            desired = self.definition["instanceCount"]
            self.runtime.update(status="running" if desired else "stopped", desiredInstances=desired,
                                runningInstances=desired, error=None)
            self.runtime["currentTasks"] = [
                {"slot": slot, "image": image, "state": "running", "desiredState": "running"}
                for slot in range(1, desired + 1)
            ]

    def __enter__(self):
        return self.start()

    def __exit__(self, *_args):
        self.close()

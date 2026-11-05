#!/usr/bin/env python3
"""Native Hermes smoke test for the ALREADY CREATED disposable local lab.

Fixed lab ports/app/images only. Approval is a SIMULATED LAB HOST CALLBACK.
The real default plugin/client uses HTTPS through a temporary local CA proxy.
No production endpoint, arbitrary URL, image, credentials or shell parameters.
This script neither creates nor stops the Docker/CapRover lab.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import http.client
import http.server
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import socketserver
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
API_PORT = 62735
HEALTH_PORT = 62734
APP = "operator-demo"
BASELINE_IMAGE = "nginx@sha256:a8b39bd9cf0f83869a2162827a0caf6137ddf759d50a171451b335cecc87d236"
DESIRED_IMAGE = "nginx@sha256:5616878291a2eed594aee8db4dade5878cf7edcb475e59193904b198d9b830de"
RESULT_PREFIX = "REAL_CAPROVER_NATIVE_LAB_RESULT="

NATIVE_DISPATCH = r'''
import json, os, sys
source, tool, arguments, call_id = sys.argv[1:]
assert tool in {"caprover_plan", "caprover_apply", "caprover_reconcile"}
sys.path.insert(0, source)
from hermes_cli.plugins import get_plugin_manager
manager = get_plugin_manager()
manager.discover_and_load()
from tools.registry import registry
assert registry.get_entry("caprover_apply") is not None
from tools.terminal_tool import set_approval_callback
from tools.approval_context import set_current_session_key
from model_tools import handle_function_call
set_current_session_key("real-caprover-disposable-lab-session")
approvals = []
def simulated_lab_human(command, description, **kwargs):
    assert tool == "caprover_apply"
    assert description.startswith("Apply plan ")
    assert os.environ["CAPROVER_NATIVE_LAB_ORIGIN"] in description
    approvals.append({"surface": "native_cli", "command": command,
                      "message": description, "choice": "once",
                      "decision_kind": "SIMULATED LAB HUMAN APPROVAL"})
    return "once"
set_approval_callback(simulated_lab_human)
result = handle_function_call(
    tool, json.loads(arguments), task_id="real-caprover-disposable-lab-task",
    session_id="real-caprover-disposable-lab-session", turn_id="real-caprover-disposable-lab-turn",
    tool_call_id=call_id,
)
print("REAL_CAPROVER_NATIVE_LAB_RESULT=" + json.dumps({
    "public_result": json.loads(result), "host_approvals": approvals,
    "native_dispatch": True, "fresh_hermes_process": True,
    "default_client_unchanged": True,
}))
'''


class LabFailure(RuntimeError):
    pass


def api(method, path, *, token=None, payload=None):
    """Fixed loopback lab API only; no retry, redirects or response logging."""
    connection = http.client.HTTPConnection("127.0.0.1", API_PORT, timeout=90)
    try:
        headers = {"Content-Type": "application/json", "Connection": "close"}
        if token:
            headers["x-captain-auth"] = token
        body = None if payload is None else json.dumps(payload).encode()
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        if response.status != 200:
            raise LabFailure("lab_api_http_failed")
        raw = response.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise LabFailure("lab_api_size_failed")
        value = json.loads(raw)
        if not isinstance(value, dict) or value.get("status") != 100:
            raise LabFailure("lab_api_envelope_failed")
        return value
    finally:
        connection.close()


def make_tls_files(directory: Path):
    """Keys are lab-only, owner-readable and deleted with the temp directory."""
    directory.mkdir(mode=0o700)
    directory.chmod(0o700)
    ca_key, ca_cert = directory / "ca.key", directory / "ca.crt"
    server_key, csr, cert = directory / "server.key", directory / "server.csr", directory / "server.crt"
    extensions = directory / "server.ext"
    extensions.write_text("basicConstraints=critical,CA:FALSE\nsubjectAltName=IP:127.0.0.1\n"
                          "extendedKeyUsage=serverAuth\nkeyUsage=critical,digitalSignature,keyEncipherment\n"
                          "subjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid,issuer\n")
    commands = [
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(ca_key),
         "-out", str(ca_cert), "-days", "1", "-subj", "/CN=CapRover disposable lab CA",
         "-addext", "basicConstraints=critical,CA:TRUE", "-addext", "keyUsage=critical,keyCertSign,cRLSign",
         "-addext", "subjectKeyIdentifier=hash"],
        ["openssl", "req", "-new", "-newkey", "rsa:2048", "-nodes", "-keyout", str(server_key),
         "-out", str(csr), "-subj", "/CN=127.0.0.1"],
        ["openssl", "x509", "-req", "-in", str(csr), "-CA", str(ca_cert), "-CAkey", str(ca_key),
         "-set_serial", "1", "-out", str(cert), "-days", "1", "-extfile", str(extensions)],
    ]
    for command in commands:
        result = subprocess.run(command, capture_output=True, timeout=30, check=False)
        if result.returncode:
            raise LabFailure("lab_tls_generation_failed")
    ca_key.chmod(0o600)
    server_key.chmod(0o600)
    return ca_cert, cert, server_key


class LocalTLSProxy:
    """Fixed lab routes. Lost ACK occurs only after the real API responds."""
    def __init__(self, cert: Path, key: Path):
        self.post_attempts = 0
        self.backend_acknowledgements = []
        self.drop_next_deploy_ack = True
        self.ack_dropped = threading.Event()
        self._lock = threading.Lock()
        proxy = self
        definitions = "/api/v2/user/apps/appDefinitions/"
        deployment = f"/api/v2/user/apps/appData/{APP}/"
        runtime = f"/api/v2/user/apps/runtime/{APP}"

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):
                pass

            def do_GET(self):
                self.forward()

            def do_POST(self):
                self.forward()

            def forward(self):
                # Neither arbitrary forwarding nor a production proxy.
                if self.path == "/health" and self.command == "GET":
                    port, path, health = HEALTH_PORT, "/", True
                elif self.command == "GET" and self.path in {definitions, deployment, runtime}:
                    port, path, health = API_PORT, self.path, False
                elif self.command == "POST" and self.path == deployment:
                    port, path, health = API_PORT, self.path, False
                else:
                    self.send_error(404)
                    return
                body = None
                if self.command == "POST":
                    try:
                        length = int(self.headers.get("Content-Length", "0"))
                        if not 0 < length <= 8192:
                            raise ValueError
                        body = self.rfile.read(length)
                        payload = json.loads(body)
                        captain = json.loads(payload["captainDefinitionContent"])
                        if set(payload) != {"captainDefinitionContent"} or captain != {"schemaVersion": 2, "imageName": DESIRED_IMAGE}:
                            raise ValueError
                    except (ValueError, TypeError, KeyError):
                        self.send_error(400)
                        return
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=90)
                try:
                    headers = {"Connection": "close"}
                    if not health:
                        headers["x-captain-auth"] = self.headers.get("x-captain-auth", "")
                        headers["Content-Type"] = "application/json"
                    if body is not None:
                        with proxy._lock:
                            proxy.post_attempts += 1
                    connection.request(self.command, path, body=body, headers=headers)
                    response = connection.getresponse()
                    if health:
                        output = b""
                    else:
                        output = response.read(2 * 1024 * 1024 + 1)
                        if len(output) > 2 * 1024 * 1024:
                            raise LabFailure("lab_proxy_response_too_large")
                    if body is not None:
                        parsed = json.loads(output)
                        acknowledgement = {"http_status": response.status,
                                           "captain_status": parsed.get("status") if isinstance(parsed, dict) else None}
                        with proxy._lock:
                            proxy.backend_acknowledgements.append(acknowledgement)
                            drop = proxy.drop_next_deploy_ack
                            proxy.drop_next_deploy_ack = False
                        if drop:
                            proxy.ack_dropped.set()
                            self.close_connection = True
                            try:
                                self.connection.shutdown(socket.SHUT_RDWR)
                            except OSError:
                                pass
                            self.connection.close()
                            return
                    self.send_response(response.status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(output)))
                    self.end_headers()
                    self.wfile.write(output)
                except (OSError, ValueError, LabFailure):
                    self.close_connection = True
                finally:
                    connection.close()

        class LoopbackServer(http.server.ThreadingHTTPServer):
            daemon_threads = True
            def server_bind(self):
                socketserver.TCPServer.server_bind(self)
                self.server_name = "127.0.0.1"
                self.server_port = self.server_address[1]

        self.server = LoopbackServer(("127.0.0.1", 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
        self.origin = f"https://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.05), daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


def run_real_native_lab() -> dict:
    hermes = importlib.util.find_spec("hermes_cli")
    if hermes is None or not hermes.origin:
        raise LabFailure("hermes_not_importable")
    source = Path(hermes.origin).parents[1]
    events = []
    def event(name, **evidence):
        events.append({"index": len(events) + 1, "event": name, **evidence})

    # This public default belongs only to the disposable lab created by root.
    token = api("POST", "/api/v2/login/", payload={"password": "captain42"})["data"]["token"]
    if not isinstance(token, str) or not token:
        raise LabFailure("lab_token_missing")
    with tempfile.TemporaryDirectory(prefix="real-native-lab-", dir=ROOT.parent) as temporary:
        temporary = Path(temporary).resolve()
        temporary.chmod(0o700)
        ca, cert, key = make_tls_files(temporary / "tls")
        home = temporary / "hermes-home"
        home.mkdir(mode=0o700)
        shutil.copytree(ROOT, home / "plugins" / "caprover-operator",
                        ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache", "tests", "docs", "demo", ".venv", "*.egg-info", "dist", "build"))
        with LocalTLSProxy(cert, key) as proxy:
            (home / "config.yaml").write_text(json.dumps({
                "plugins": {"enabled": ["caprover-operator"], "isolation": "in_process",
                            "entries": {"caprover-operator": {"settings": {
                                "url": proxy.origin, "app_name": APP, "health_url": proxy.origin + "/health",
                                "plan_ttl_seconds": 3600}}}}, "approvals": {"mode": "manual"},
            }))
            environment = {k: v for k, v in os.environ.items() if k in {"PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "SYSTEMROOT"}}
            environment.update({"HERMES_HOME": str(home), "HERMES_ENABLE_PROJECT_PLUGINS": "0",
                                "HERMES_INTERACTIVE": "1", "PYTHONDONTWRITEBYTECODE": "1",
                                "CAPROVER_OPERATOR_TOKEN": token, "CAPROVER_NATIVE_LAB_ORIGIN": proxy.origin,
                                "SSL_CERT_FILE": str(ca)})

            def call(tool, arguments, name):
                completed = subprocess.run([sys.executable, "-c", NATIVE_DISPATCH, str(source), tool,
                                            json.dumps(arguments), "real-lab-call-" + str(len(events) + 1)],
                                           env=environment, capture_output=True, text=True, timeout=55, check=False)
                if completed.returncode:
                    raise LabFailure("native_dispatch_failed_" + str(completed.returncode))
                lines = [line for line in completed.stdout.splitlines() if line.startswith(RESULT_PREFIX)]
                if len(lines) != 1:
                    raise LabFailure("native_result_missing")
                result = json.loads(lines[0][len(RESULT_PREFIX):])
                if token in json.dumps(result):
                    raise LabFailure("credential_in_public_result")
                event(name, tool_name=tool, tool_arguments=arguments, post_attempts=proxy.post_attempts, **result)
                return result

            first = call("caprover_plan", {"image": DESIRED_IMAGE}, "native_plan")["public_result"]
            if first.get("status") != "planned" or first.get("baseline", {}).get("image") != BASELINE_IMAGE:
                print(json.dumps({"phase": "native_plan", "public_result": first,
                                  "deployment_post_attempts": proxy.post_attempts}), file=sys.stderr)
                raise LabFailure("unexpected_lab_baseline")
            applied = call("caprover_apply", {"plan_id": first["plan_id"]}, "real_deploy_lost_ack")
            if applied["public_result"].get("status") != "reconcile_required" or len(applied["host_approvals"]) != 1:
                raise LabFailure("uncertain_outcome_not_preserved")
            if not proxy.ack_dropped.wait(timeout=50) or proxy.post_attempts != 1:
                raise LabFailure("real_ack_drop_not_observed")
            if proxy.backend_acknowledgements != [{"http_status": 200, "captain_status": 100}]:
                raise LabFailure("real_deployment_not_acknowledged")
            event("real_backend_acknowledged_before_connection_drop", acknowledgements=proxy.backend_acknowledgements,
                  post_attempts=proxy.post_attempts)
            replay = call("caprover_apply", {"plan_id": first["plan_id"]}, "new_process_replay_refused")
            if "error" not in replay["public_result"] or replay["host_approvals"] or proxy.post_attempts != 1:
                raise LabFailure("replay_not_refused")
            deadline = time.monotonic() + 180
            final = None
            while time.monotonic() < deadline:
                observation = call("caprover_reconcile", {"plan_id": first["plan_id"]}, "real_runtime_observation")["public_result"]
                if observation.get("status") == "verified":
                    final = observation
                    break
                if "error" in observation:
                    raise LabFailure("reconciliation_refused")
                time.sleep(1)
            if final is None or proxy.post_attempts != 1 or not final["checks"].get("runtime_rollout_proven"):
                raise LabFailure("real_rollout_not_verified")
            trace = {"demo_kind": "native_hermes_real_disposable_caprover", "real_caprover": True,
                     "caprover_version": "1.16.0", "generated_at": datetime.now(timezone.utc).isoformat(),
                     "scope": {"native_dispatch": True, "default_engine_and_client_unchanged": True,
                               "human_approval": "SIMULATED LAB HOST CALLBACK", "app": APP,
                               "production_traffic": False, "tls": "temporary lab CA trusted by child SSL_CERT_FILE only",
                               "baseline_image": BASELINE_IMAGE, "desired_image": DESIRED_IMAGE},
                     "events": events, "post_attempts": proxy.post_attempts,
                     "backend_acknowledgements": proxy.backend_acknowledgements, "result": final}
            if token in json.dumps(trace):
                raise LabFailure("credential_in_trace")
            return trace


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="Create-only sanitized JSON trace")
    args = parser.parse_args(argv)
    if args.output.exists() or args.output.is_symlink():
        parser.error("output already exists")
    old_umask = os.umask(0o077)
    try:
        trace = run_real_native_lab()
        with args.output.open("x", encoding="utf-8") as output:
            json.dump(trace, output, indent=2, sort_keys=True)
            output.write("\n")
        print(json.dumps({"real_caprover": True, "native_dispatch": True,
                          "post_attempts": trace["post_attempts"], "status": trace["result"]["status"],
                          "runtime_rollout_proven": trace["result"]["checks"]["runtime_rollout_proven"],
                          "trace_created": True}))
        return 0
    except (OSError, LabFailure, ValueError, KeyError, subprocess.TimeoutExpired) as exc:
        reason = str(exc) if isinstance(exc, LabFailure) else "lab_execution_failed"
        print(json.dumps({"error": reason, "real_caprover": True,
                          "hint": "Only the fixed, pre-created disposable lab is supported; no retry was performed."}), file=sys.stderr)
        return 1
    finally:
        os.umask(old_umask)


if __name__ == "__main__":
    raise SystemExit(main())

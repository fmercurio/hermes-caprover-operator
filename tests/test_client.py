"""Behavioral transport tests against a disposable loopback protocol fixture."""
import copy
import json
import socket
import time
import unittest
from unittest.mock import patch

from caprover_operator.client import CapRoverClient, ClientError, MAX_RESPONSE_BYTES
from caprover_operator.lab import FakeCapRover, NEW_IMAGE, OLD_IMAGE


class ClientTests(unittest.TestCase):
    def test_configuration_is_lazy_and_http_is_explicit_loopback_only(self):
        calls = []
        client = CapRoverClient("https://captain.example.com:443/", lambda: calls.append(1), b"k" * 32,
                               allowed_apps=["demo-app"])
        self.assertEqual(client.target, "https://captain.example.com")
        self.assertEqual(calls, [])
        invalid = ["http://captain.example.com", "http://127.0.0.1:9000",
                   "https://user:secret@captain.example.com", "https://captain.example.com/api",
                   "https://captain.example.com/?token=private", "https://captain.example.com/#part",
                   "https://captain.example.com:0", "https://captain.example.com\\@other.example"]
        for url in invalid:
            with self.subTest(url=url), self.assertRaises(ClientError):
                CapRoverClient(url, lambda: "token", b"k" * 32, allowed_apps=["demo-app"])
        with self.assertRaises(ClientError):
            CapRoverClient("http://example.com", lambda: "token", b"k" * 32,
                           allowed_apps=["demo-app"], allow_insecure_local=True)

    def test_allowlist_and_immutable_image_reject_before_token_or_network(self):
        with FakeCapRover() as lab:
            calls = []
            client = CapRoverClient(lab.base_url, lambda: calls.append(1), b"k" * 32,
                                   allowed_apps=[lab.app], allow_insecure_local=True)
            for invoke in [lambda: client.snapshot("other"), lambda: client.health("other"),
                           lambda: client.deploy_image("other", NEW_IMAGE),
                           lambda: client.deploy_image(lab.app, "ghcr.io/example/app:latest"),
                           lambda: client.deploy_image(lab.app, "https://bad/thing@sha256:" + "2" * 64)]:
                with self.assertRaises(ClientError):
                    invoke()
            self.assertEqual(calls, [])
            self.assertEqual(lab.requests, [])

    def test_snapshot_uses_exact_version_history_and_build_status(self):
        with FakeCapRover() as lab:
            lab.definition["imageName"] = "not-the-deployed-history"
            lab.definition["versions"].insert(0, {"version": 0, "deployedImageName": "wrong-history"})
            lab.build_status["isAppBuilding"] = True
            result = lab.client().snapshot(lab.app)
            self.assertEqual(result["version"], 1)
            self.assertEqual(result["image"], OLD_IMAGE)
            self.assertTrue(result["is_building"])
            self.assertEqual(result["replicas"], 1)
            self.assertIsNone(result["running_replicas"])
            self.assertNotIn("logs", result)
            self.assertEqual([r["path"] for r in lab.requests], [
                "/api/v2/user/apps/appDefinitions/", "/api/v2/user/apps/appData/demo-app/",
                "/api/v2/user/apps/runtime/demo-app"])

    def test_hmac_covers_secrets_unknown_fields_and_order_without_disclosing_values(self):
        with FakeCapRover() as lab:
            client = lab.client()
            first = client.snapshot(lab.app)
            lab.definition = dict(reversed(list(lab.definition.items())))
            self.assertEqual(first["config_fingerprint"], client.snapshot(lab.app)["config_fingerprint"])
            lab.definition["envVars"][0]["value"] = "changed-private-value"
            second = client.snapshot(lab.app)
            self.assertNotEqual(first["config_fingerprint"], second["config_fingerprint"])
            lab.definition["futureRelevantSetting"] = {"enabled": True}
            third = client.snapshot(lab.app)
            self.assertNotEqual(second["config_fingerprint"], third["config_fingerprint"])
            public = json.dumps([first, second, third])
            for secret in [lab.token, "synthetic-env-value", "changed-private-value", "SYNTHETIC_SECRET"]:
                self.assertNotIn(secret, public)
            other_key = lab.client(fingerprint_key=b"other-key" * 4).snapshot(lab.app)
            self.assertNotEqual(third["config_fingerprint"], other_key["config_fingerprint"])

    def test_bad_snapshot_shapes_fail_safely(self):
        mutations = [
            lambda lab: lab.definition.update(instanceCount=True),
            lambda lab: lab.definition.update(deployedVersion=True),
            lambda lab: lab.definition.update(versions=[{"version": 1, "deployedImageName": "x"}, {"version": 1, "deployedImageName": "x"}]),
            lambda lab: lab.additional_definitions.append(copy.deepcopy(lab.definition)),
            lambda lab: lab.build_status.update(isAppBuilding=1),
        ]
        for mutate in mutations:
            with self.subTest(mutate=mutate), FakeCapRover() as lab:
                mutate(lab)
                with self.assertRaises(ClientError) as caught:
                    lab.client().snapshot(lab.app)
                self.assertEqual(caught.exception.code, "response_invalid")

    def test_missing_history_is_unknown_image_not_top_level_guess(self):
        with FakeCapRover() as lab:
            lab.definition.update(versions=[], imageName="tempting-wrong-image")
            self.assertIsNone(lab.client().snapshot(lab.app)["image"])

    def test_runtime_is_optional_and_does_not_infer_running_from_desired(self):
        with FakeCapRover() as lab:
            snapshot = lab.client().snapshot(lab.app)
            self.assertIsNone(snapshot["running_replicas"])
            self.assertEqual(snapshot["runtime"], {"available": False, "reason": "endpoint_unavailable",
                                                   "desired_instances": None, "running_instances": None,
                                                   "current_tasks": []})

    def test_metadata_update_and_http_health_do_not_replace_old_task_image(self):
        with FakeCapRover() as lab:
            lab.runtime_available = True
            client = lab.client()
            client.deploy_image(lab.app, NEW_IMAGE)
            snapshot = client.snapshot(lab.app)
            self.assertEqual(snapshot["image"], NEW_IMAGE)
            self.assertTrue(client.health(lab.app)["ok"])
            self.assertEqual(snapshot["runtime"]["current_tasks"][0]["image"], OLD_IMAGE)
            self.assertEqual(snapshot["running_replicas"], 1)
            self.assertNotIn("synthetic message", json.dumps(snapshot))
            lab.sync_runtime_to_definition()
            converged = client.snapshot(lab.app)["runtime"]
            self.assertTrue(converged["available"])
            self.assertEqual(converged["current_tasks"], [{"slot": 1, "image": NEW_IMAGE,
                                                         "state": "running", "desired_state": "running"}])

    def test_malformed_or_failed_runtime_is_unknown_without_losing_snapshot(self):
        mutations = [
            lambda lab: lab.runtime.update(appName="other"),
            lambda lab: lab.runtime.update(status=[]),
            lambda lab: lab.runtime.update(status="unavailable", error="private collector error"),
            lambda lab: lab.runtime.update(desiredInstances=True),
            lambda lab: lab.runtime.update(runningInstances=2),
            lambda lab: lab.runtime.update(currentTasks=[{}]),
            lambda lab: lab.runtime["currentTasks"][0].update(slot=True),
            lambda lab: lab.runtime["currentTasks"][0].update(state="invented"),
            lambda lab: lab.runtime["currentTasks"][0].update(state=[]),
            lambda lab: lab.runtime["currentTasks"][0].update(image="private bad image\n"),
            lambda lab: lab.runtime.update(currentTasks=[{}] * 1025),
        ]
        for mutate in mutations:
            with self.subTest(mutate=mutate), FakeCapRover() as lab:
                lab.runtime_available = True
                mutate(lab)
                snapshot = lab.client().snapshot(lab.app)
                self.assertEqual(snapshot["image"], OLD_IMAGE)
                self.assertFalse(snapshot["runtime"]["available"])
                self.assertIsNone(snapshot["running_replicas"])
                self.assertNotIn("private", json.dumps(snapshot))

    def test_runtime_network_or_redirect_failure_is_unavailable_and_not_retried(self):
        with FakeCapRover() as lab, FakeCapRover() as other:
            lab.runtime_available = True
            path = "/api/v2/user/apps/runtime/demo-app"
            lab.redirects[path] = other.base_url + path
            snapshot = lab.client().snapshot(lab.app)
            self.assertFalse(snapshot["runtime"]["available"])
            self.assertEqual(snapshot["runtime"]["reason"], "redirect_rejected")
            self.assertEqual(other.requests, [])
            self.assertEqual(len([r for r in lab.requests if r["path"] == path]), 1)

    def test_native_image_deployment_sends_one_json_captain_definition(self):
        with FakeCapRover() as lab:
            result = lab.client().deploy_image(lab.app, NEW_IMAGE)
            self.assertEqual(result, {"accepted": True})
            self.assertEqual(lab.trigger_count, 1)
            self.assertEqual(lab.deployments, [{"schemaVersion": 2, "imageName": NEW_IMAGE}])
            self.assertEqual(lab.requests, [{"method": "POST", "path": "/api/v2/user/apps/appData/demo-app/", "auth_present": True}])
            snapshot = lab.client().snapshot(lab.app)
            self.assertEqual((snapshot["version"], snapshot["image"]), (2, NEW_IMAGE))

    def test_lost_ack_after_applying_is_error_without_transport_retry(self):
        with FakeCapRover() as lab:
            lab.lose_next_ack = True
            with self.assertRaises(ClientError):
                lab.client().deploy_image(lab.app, NEW_IMAGE)
            self.assertEqual(lab.trigger_count, 1)
            self.assertEqual(lab.definition["versions"][-1]["deployedImageName"], NEW_IMAGE)
            self.assertEqual(len([r for r in lab.requests if r["method"] == "POST"]), 1)

    def test_tag_beside_digest_is_immutable_and_preserved_on_wire(self):
        for tag in ["Release_2026", "v1..2"]:
            with self.subTest(tag=tag), FakeCapRover() as lab:
                image = "ghcr.io/example/demo:" + tag + "@sha256:" + "3" * 64
                self.assertEqual(lab.client().deploy_image(lab.app, image), {"accepted": True})
                self.assertEqual(lab.trigger_count, 1)
                self.assertEqual(lab.deployments, [{"schemaVersion": 2, "imageName": image}])
                self.assertEqual(lab.client().snapshot(lab.app)["image"], image)

    def test_timeout_after_applying_does_not_resend(self):
        with FakeCapRover() as lab:
            lab.post_delay = 0.3
            started = time.monotonic()
            with self.assertRaises(ClientError) as caught:
                lab.client(timeout=0.06).deploy_image(lab.app, NEW_IMAGE)
            self.assertLess(time.monotonic() - started, 0.25)
            self.assertEqual(caught.exception.code, "request_timeout")
            self.assertEqual(lab.trigger_count, 1)

    def test_redirect_never_forwards_token_or_repeats_post(self):
        with FakeCapRover() as lab, FakeCapRover() as other:
            path = "/api/v2/user/apps/appData/demo-app/"
            lab.redirects[path] = other.base_url + path
            with self.assertRaises(ClientError) as caught:
                lab.client().deploy_image(lab.app, NEW_IMAGE)
            self.assertEqual(caught.exception.code, "redirect_rejected")
            self.assertEqual(lab.trigger_count, 1)
            self.assertEqual(other.requests, [])
            self.assertEqual(len(lab.requests), 1)

    def test_health_is_fixed_url_status_only_without_auth_or_body(self):
        with FakeCapRover() as lab:
            calls = []
            client = CapRoverClient(lab.base_url, lambda: calls.append(1), b"k" * 32,
                                   allowed_apps=[lab.app], health_urls={lab.app: lab.health_url},
                                   allow_insecure_local=True)
            lab.health_body = b"private-health-body" * 1000
            result = client.health(lab.app)
            self.assertEqual(result, {"configured": True, "ok": True, "status_code": 200, "reason": "http_ok"})
            self.assertEqual(calls, [])
            self.assertEqual(lab.requests, [{"method": "GET", "path": "/health", "auth_present": False}])
            self.assertNotIn("private", json.dumps(result))
            lab.health_status = 503
            self.assertFalse(client.health(lab.app)["ok"])
            self.assertEqual(lab.client(health=False).health(lab.app), {
                "configured": False, "ok": None, "status_code": None, "reason": "not_configured"})

    def test_health_redirect_is_not_followed(self):
        with FakeCapRover() as lab, FakeCapRover() as other:
            lab.redirects["/health"] = other.health_url
            result = lab.client().health(lab.app)
            self.assertFalse(result["ok"])
            self.assertEqual(result["reason"], "redirect_rejected")
            self.assertEqual(other.requests, [])

    def test_error_messages_never_include_token_response_or_provider_exception(self):
        with FakeCapRover() as lab:
            path = "/api/v2/user/apps/appDefinitions/"
            lab.body_overrides[path] = json.dumps({"status": 1106, "description": "private-server-message " + lab.token}).encode()
            with self.assertRaises(ClientError) as caught:
                lab.client().snapshot(lab.app)
            self.assertEqual(caught.exception.code, "authentication_rejected")
            self.assertNotIn(lab.token, str(caught.exception))
            self.assertNotIn("private-server-message", str(caught.exception))
            def unavailable():
                raise RuntimeError("private-provider-exception")
            client = CapRoverClient(lab.base_url, unavailable, b"k" * 32,
                                   allowed_apps=[lab.app], allow_insecure_local=True)
            with self.assertRaises(ClientError) as caught:
                client.snapshot(lab.app)
            self.assertNotIn("private-provider", str(caught.exception))

    def test_api_rejects_duplicate_json_keys_and_async_or_invalid_envelopes(self):
        for raw in [b'{"status":100,"status":1106,"data":{}}',
                    b'{"status":true,"data":{}}', b'{"status":101}',
                    b'{"status":100,"data":NaN}', b'private-invalid-response']:
            with self.subTest(raw=raw), FakeCapRover() as lab:
                lab.body_overrides["/api/v2/user/apps/appDefinitions/"] = raw
                with self.assertRaises(ClientError):
                    lab.client().snapshot(lab.app)

    def test_response_bytes_and_slow_body_have_bounds(self):
        with FakeCapRover() as lab:
            path = "/api/v2/user/apps/appDefinitions/"
            lab.body_overrides[path] = b"x" * (MAX_RESPONSE_BYTES + 1)
            with self.assertRaises(ClientError) as caught:
                lab.client().snapshot(lab.app)
            self.assertEqual(caught.exception.code, "response_too_large")
            lab.body_overrides[path] = b" " * 1000
            lab.drip_interval = 0.04
            started = time.monotonic()
            with self.assertRaises(ClientError) as caught:
                lab.client(timeout=0.07).snapshot(lab.app)
            self.assertEqual(caught.exception.code, "request_timeout")
            self.assertLess(time.monotonic() - started, 0.3)

    def test_slow_dns_does_not_later_send_an_abandoned_write(self):
        with FakeCapRover() as lab:
            original = socket.getaddrinfo
            def delayed(*args, **kwargs):
                time.sleep(0.15)
                return original(*args, **kwargs)
            started = time.monotonic()
            with patch("socket.getaddrinfo", delayed), self.assertRaises(ClientError) as caught:
                lab.client(timeout=0.04).deploy_image(lab.app, NEW_IMAGE)
            self.assertEqual(caught.exception.code, "request_timeout")
            self.assertLess(time.monotonic() - started, 0.12)
            time.sleep(0.18)
            self.assertEqual(lab.requests, [])


if __name__ == "__main__":
    unittest.main()

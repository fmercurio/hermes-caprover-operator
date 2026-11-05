"""Historical cards must never strengthen evidence or become an execution path."""
from copy import deepcopy
from html.parser import HTMLParser
import json
from pathlib import Path
import tempfile
import unittest

from caprover_operator.presentation import card_data, present, render_html


APP = "example-notes"
TARGET = "https://captain.example.test"
IMAGE = "ghcr.io/example/notes@sha256:" + "b" * 64
NOW = 1_794_000_000


def receipt(status="planned"):
    return {"plan_id": "1" * 32, "plan_hash": "2" * 64, "app": APP, "target": TARGET,
            "image": IMAGE, "status": status, "created_at": NOW, "expires_at": NOW + 300,
            "baseline": {"image": "ghcr.io/example/notes@sha256:" + "a" * 64,
                         "version": 8, "replicas": 1, "fingerprint": "PRIVATE_FINGERPRINT"}}


class Tags(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


class OperationCardTests(unittest.TestCase):
    def data(self, value, **kwargs):
        return card_data(value, app=APP, target=TARGET, now=NOW, **kwargs)

    def test_metadata_and_http_do_not_paint_rollout_green(self):
        value = receipt("metadata_http_observed")
        value.update(observed_at=NOW + 8, checks={"deployment_recorded": True,
                     "http_health": {"configured": True, "ok": True, "status_code": 200},
                     "runtime_rollout_proven": False})
        card = self.data(value)
        self.assertEqual(card["receipt"]["tone"], "warning")
        self.assertEqual(card["evidence"]["http"], "Passed")
        self.assertEqual(card["evidence"]["runtime"], "Exact-image tasks not proven")
        self.assertEqual(card["next_step"]["tool"], "caprover_reconcile")
        value["status"] = "verified"
        self.assertEqual(self.data(value)["receipt"]["display_status"], "evidence_incomplete")
        value["checks"]["runtime_rollout_proven"] = True
        self.assertEqual(self.data(value)["receipt"]["tone"], "success")

    def test_expired_planned_receipt_never_suggests_apply(self):
        value = receipt()
        value["expires_at"] = NOW - 1
        card = self.data(value)
        self.assertEqual(card["receipt"]["status"], "planned")
        self.assertEqual(card["receipt"]["display_status"], "expired")
        self.assertEqual(card["next_step"]["tool"], "caprover_plan")
        self.assertEqual(value["status"], "planned")

    def test_attempt_marker_survives_reconciled_checks_without_claiming_delivery_or_approval(self):
        value = receipt("reconcile_required")
        card = self.data(value)
        self.assertFalse(card["attempt"]["recorded"])
        value.update(attempted_at=NOW + 1, consumed_at=NOW, checks={"runtime_rollout_proven": False})
        card = self.data(value)
        self.assertTrue(card["attempt"]["recorded"])
        self.assertIn("does not prove network delivery", card["attempt"]["meaning"])
        self.assertEqual(card["approval"]["record"], "Not separately stored in this receipt")
        self.assertFalse(card["next_step"]["automatic"])
        self.assertEqual(card["next_step"]["tool"], "caprover_reconcile")
        self.assertNotIn("POST count", render_html(card))

    def test_card_omits_private_raw_payloads_and_bounds_strings(self):
        value = receipt()
        value.update(token="SECRET_TOKEN", logs="SECRET_LOGS", observed={"env": "SECRET_ENV"},
                     approval={"approved": True, "token": "SECRET_APPROVAL"}, image="x" * 513)
        card = self.data(value)
        encoded = json.dumps(card)
        self.assertNotIn("SECRET", encoded)
        self.assertNotIn("PRIVATE_FINGERPRINT", encoded)
        self.assertIsNone(card["requested_image"]["full"])
        self.assertEqual(card["requested_image"]["short"], "Not recorded")

    def test_html_escapes_untrusted_fields_and_has_no_execution_or_network_elements(self):
        value = receipt()
        value["image"] = '</code><script src="https://evil.test"></script><img src=x onerror=alert(1)>'
        card = self.data(value)
        html = render_html(card)
        parser = Tags()
        parser.feed(html)
        for tag, attrs in parser.tags:
            self.assertNotIn(tag, {"script", "img", "iframe", "button", "form", "a", "link"})
            self.assertFalse(any(key.startswith("on") or key == "data-hermes-send" for key in attrs))
        self.assertIn("&lt;script", html)
        self.assertIn("connect-src 'none'", html)
        self.assertIn("Historical snapshot", html)

    def test_full_digest_remains_copyable_and_previous_image_is_not_observed_image(self):
        value = receipt()
        value["observed"] = {"image": IMAGE}
        card = self.data(value)
        self.assertEqual(card["requested_image"]["full"], IMAGE)
        self.assertLess(len(card["requested_image"]["short"]), len(IMAGE))
        self.assertTrue(card["previous_image"]["full"].endswith("a" * 64))
        self.assertIn(IMAGE, render_html(card))

    def test_other_owner_scope_is_not_exported(self):
        value = receipt()
        value["app"] = "another-app"
        self.assertIsNone(self.data(value))
        value = receipt()
        value["target"] = "https://another.example.test"
        self.assertIsNone(self.data(value))

    def test_immutable_snapshots_and_history_limit_without_touching_ledger(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            ledger = root / "state.sqlite3"
            ledger.write_bytes(b"opaque ledger must stay unchanged")
            value = receipt()
            first = present(value, data_dir=root, app=APP, target=TARGET, now=NOW)
            item = first["presentation"]["cards"][0]
            path = Path(item["file"])
            self.assertTrue(item["available"])
            self.assertEqual(item["preview"], '::preview{file=' + json.dumps(str(path)) + '}')
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
            before = path.stat().st_mtime_ns
            second = present(value, data_dir=root, app=APP, target=TARGET, now=NOW)
            self.assertEqual(second["presentation"]["cards"][0]["file"], str(path))
            self.assertEqual(before, path.stat().st_mtime_ns)
            changed = deepcopy(value)
            changed.update(status="reconcile_required", attempted_at=NOW + 1)
            other = present(changed, data_dir=root, app=APP, target=TARGET, now=NOW)
            self.assertNotEqual(other["presentation"]["cards"][0]["file"], str(path))
            many = present({"operations": [value] * 1000}, data_dir=root, app=APP, target=TARGET, now=NOW)
            self.assertEqual(len(many["presentation"]["cards"]), 5)
            self.assertEqual(ledger.read_bytes(), b"opaque ledger must stay unchanged")

    def test_unsafe_storage_never_overwrites_or_changes_operation_outcome(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            destination = root / "outside"
            destination.mkdir(mode=0o700)
            (root / "cards").symlink_to(destination, target_is_directory=True)
            value = receipt("reconcile_required")
            output = present(value, data_dir=root, app=APP, target=TARGET, now=NOW)
            self.assertEqual(output["status"], "reconcile_required")
            self.assertFalse(output["presentation"]["cards"][0]["available"])
            self.assertEqual(list(destination.iterdir()), [])

    def test_tampered_existing_snapshot_is_not_overwritten_or_linked(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            value = receipt()
            item = present(value, data_dir=root, app=APP, target=TARGET, now=NOW)["presentation"]["cards"][0]
            path = Path(item["file"])
            path.write_text("tampered", encoding="utf-8")
            updated = present(value, data_dir=root, app=APP, target=TARGET, now=NOW)["presentation"]["cards"][0]
            self.assertFalse(updated["available"])
            self.assertNotIn("file", updated)
            self.assertEqual(path.read_text(), "tampered")


if __name__ == "__main__":
    unittest.main()

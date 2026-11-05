"""Behavioral ledger tests, using synthetic clients and private temp storage."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import copy
import json
import multiprocessing
import os
from pathlib import Path
import sqlite3
import threading

import pytest

from caprover_operator.engine import Operator, OperatorError, load_fingerprint_key


OLD = "registry.example/test@sha256:" + "a" * 64
NEW = "registry.example/test@sha256:" + "b" * 64
SECRET = "never-record-this-private-service-token"


class FakeClient:
    target = "https://captain.example.invalid"

    def __init__(self, post_log: Path | None = None):
        self.state = {"app": "example", "version": 4, "image": OLD, "replicas": 1,
                      "is_building": False, "config_fingerprint": "c" * 64}
        self.health_result = {"configured": True, "ok": True, "status_code": 200, "reason": SECRET}
        self.snapshots = 0
        self.posts = 0
        self.post_error = None
        self.snapshot_error = None
        self.post_log = post_log
        self.crash = False

    def snapshot(self, app):
        self.snapshots += 1
        if self.snapshot_error:
            raise self.snapshot_error
        return {**copy.deepcopy(self.state), "definition": {"token": SECRET}}

    def deploy_image(self, app, image):
        self.posts += 1
        if self.post_log:
            fd = os.open(self.post_log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.write(fd, b"POST\n")
                os.fsync(fd)
            finally:
                os.close(fd)
        if self.crash:
            os._exit(17)
        if self.post_error:
            raise self.post_error
        return {"accepted": True, "token": SECRET}

    def health(self, app):
        return copy.deepcopy(self.health_result)


class Clock:
    def __init__(self):
        self.value = 1000.0

    def __call__(self):
        return self.value


@pytest.fixture
def setup_operator(tmp_path):
    client, clock = FakeClient(), Clock()
    directory = tmp_path.resolve() / "private"
    op = Operator(directory, client, clock=clock)
    yield op, client, clock, directory
    op.close()


def _concurrent_apply(directory, plan_id, post_log, barrier, queue):
    client = FakeClient(Path(post_log))
    operator = Operator(Path(directory), client)
    try:
        result = operator.apply(plan_id, lambda plan: (barrier.wait(timeout=10), True)[1])
        queue.put(result["status"])
    except OperatorError as exc:
        queue.put(exc.code)
    finally:
        operator.close()


def _crash_after_possible_post(directory, plan_id, post_log):
    client = FakeClient(Path(post_log))
    client.crash = True
    operator = Operator(Path(directory), client)
    operator.apply(plan_id, lambda plan: True)


def test_authorization_then_fresh_drift_consumes_without_post(setup_operator):
    operator, client, clock, directory = setup_operator
    plan = operator.plan("example", NEW)

    def authorize(summary):
        assert summary["plan_hash"] == plan["plan_hash"]
        client.state["config_fingerprint"] = "d" * 64
        return True

    result = operator.apply(plan["plan_id"], authorize)
    assert result["status"] == "consumed_drift"
    assert result["checks"]["post_attempted"] is False
    assert client.posts == 0
    # Consumed plans cannot be reused, even after the configuration is restored.
    client.state["config_fingerprint"] = "c" * 64
    assert operator.apply(plan["plan_id"], lambda _: True)["status"] == "consumed_drift"
    assert client.posts == 0


def test_denied_expired_and_nonboolean_authorization_never_post(setup_operator):
    operator, client, clock, directory = setup_operator
    plan = operator.plan("example", NEW, ttl_seconds=5)
    assert operator.apply(plan["plan_id"], lambda _: "approved")["status"] == "authorization_denied"
    assert operator.apply(plan["plan_id"], lambda _: False)["status"] == "authorization_denied"
    assert operator.get_plan(plan["plan_id"])["status"] == "planned"
    clock.value += 5
    assert operator.apply(plan["plan_id"], lambda _: pytest.fail("expired plan asked for approval"))["status"] == "expired"
    assert client.posts == 0


def test_expiry_and_unavailable_state_after_approval_do_not_post(setup_operator):
    operator, client, clock, directory = setup_operator
    plan = operator.plan("example", NEW, ttl_seconds=5)

    def authorize(summary):
        clock.value += 5
        return True

    assert operator.apply(plan["plan_id"], authorize)["status"] == "expired"
    plan = operator.plan("example", NEW)

    def unavailable(summary):
        client.snapshot_error = TimeoutError(SECRET)
        return True

    assert operator.apply(plan["plan_id"], unavailable)["status"] == "consumed_preflight_failed"
    assert client.posts == 0


def test_timeout_claim_blocks_replay_new_plan_and_other_existing_plan_after_reopen(setup_operator):
    operator, client, clock, directory = setup_operator
    first = operator.plan("example", NEW)
    second = operator.plan("example", NEW)
    client.post_error = TimeoutError(SECRET)
    result = operator.apply(first["plan_id"], lambda _: True)
    assert result["status"] == "reconcile_required"
    assert client.posts == 1
    operator.close()
    reopened = Operator(directory, client, clock=clock)
    try:
        assert reopened.apply(first["plan_id"], lambda _: pytest.fail("retry asked for a new approval"))["status"] == "reconcile_required"
        with pytest.raises(OperatorError) as exc:
            reopened.plan("example", NEW)
        assert exc.value.code == "operation_in_progress"
        with pytest.raises(OperatorError) as exc:
            reopened.apply(second["plan_id"], lambda _: True)
        assert exc.value.code == "operation_in_progress"
        assert client.posts == 1
        assert SECRET not in json.dumps(reopened.status())
    finally:
        reopened.close()


def test_two_processes_apply_one_plan_at_most_one_post(tmp_path):
    directory = tmp_path.resolve() / "state"
    operator = Operator(directory, FakeClient())
    plan = operator.plan("example", NEW)
    operator.close()
    ctx = multiprocessing.get_context("spawn")
    barrier, queue = ctx.Barrier(2), ctx.Queue()
    post_log = tmp_path.resolve() / "attempts"
    processes = [ctx.Process(target=_concurrent_apply, args=(str(directory), plan["plan_id"], str(post_log), barrier, queue)) for _ in range(2)]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=20)
        assert process.exitcode == 0
    results = [queue.get(timeout=2) for _ in processes]
    assert all(value in {"preflight_pending", "sending", "reconcile_required", "operation_in_progress"} for value in results), results
    assert post_log.read_text().splitlines() == ["POST"]


def test_process_death_at_transport_boundary_cannot_replay(tmp_path):
    directory, post_log = tmp_path.resolve() / "state", tmp_path.resolve() / "attempts"
    operator = Operator(directory, FakeClient())
    plan = operator.plan("example", NEW)
    operator.close()
    ctx = multiprocessing.get_context("spawn")
    process = ctx.Process(target=_crash_after_possible_post, args=(str(directory), plan["plan_id"], str(post_log)))
    process.start()
    process.join(timeout=20)
    assert process.exitcode == 17
    client = FakeClient(post_log)
    reopened = Operator(directory, client)
    try:
        assert reopened.get_plan(plan["plan_id"])["status"] == "sending"
        assert reopened.apply(plan["plan_id"], lambda _: True)["status"] == "sending"
        assert client.posts == 0
        assert post_log.read_text().splitlines() == ["POST"]
        assert reopened.reconcile(plan["plan_id"])["status"] == "reconcile_required"
    finally:
        reopened.close()


def test_reconcile_cannot_release_a_slot_while_its_local_writer_is_in_flight(setup_operator):
    operator, client, clock, directory = setup_operator
    plan = operator.plan("example", NEW)
    entered, release = threading.Event(), threading.Event()
    original = client.deploy_image

    def paused(app, image):
        entered.set()
        assert release.wait(timeout=10)
        return original(app, image)

    client.deploy_image = paused
    observer = Operator(directory, client, clock=clock)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(operator.apply, plan["plan_id"], lambda _: True)
            try:
                assert entered.wait(timeout=5)
                # Even if a matching remote generation is visible, an observer
                # cannot release the original process's still-live writer slot.
                client.state.update(version=5, image=NEW)
                with pytest.raises(OperatorError) as exc:
                    observer.reconcile(plan["plan_id"])
                assert exc.value.code == "operation_in_progress"
                assert observer.get_plan(plan["plan_id"])["status"] == "sending"
                with pytest.raises(OperatorError):
                    observer.plan("example", OLD)
            finally:
                release.set()
            assert pending.result(timeout=5)["status"] == "reconcile_required"
        assert observer.reconcile(plan["plan_id"])["status"] == "metadata_http_observed"
        assert client.posts == 1
    finally:
        observer.close()


@pytest.mark.parametrize("version,image,building", [(4, NEW, False), (5, OLD, False), (5, NEW, True)])
def test_incomplete_or_contradictory_deployment_evidence_remains_uncertain(setup_operator, version, image, building):
    operator, client, clock, directory = setup_operator
    plan = operator.plan("example", NEW)
    operator.apply(plan["plan_id"], lambda _: True)
    client.state.update(version=version, image=image, is_building=building)
    result = operator.reconcile(plan["plan_id"])
    assert result["status"] == "reconcile_required"
    assert result["checks"]["deployment_recorded"] is False
    assert result["checks"]["runtime_rollout_proven"] is False
    assert client.posts == 1


@pytest.mark.parametrize("health,actual,expected", [
    ({"configured": False, "ok": None, "status_code": None}, None, "deployment_recorded"),
    ({"configured": True, "ok": False, "status_code": 503}, 1, "observed_unhealthy"),
    ({"configured": True, "ok": True, "status_code": 200}, None, "metadata_http_observed"),
    ({"configured": True, "ok": True, "status_code": 200}, 0, "observed_unhealthy"),
])
def test_reconciliation_separates_deployment_replicas_and_explicit_health(setup_operator, health, actual, expected):
    operator, client, clock, directory = setup_operator
    plan = operator.plan("example", NEW)
    assert operator.apply(plan["plan_id"], lambda _: True)["status"] == "reconcile_required"
    client.state.update(version=5, image=NEW, running_replicas=actual)
    client.health_result = {**health, "reason": SECRET, "body": SECRET}
    result = operator.reconcile(plan["plan_id"])
    assert result["status"] == expected
    assert result["checks"]["configured_replicas"] == 1
    assert result["checks"]["running_replicas"] == actual
    assert result["checks"]["runtime_rollout_proven"] is False
    assert SECRET not in json.dumps(result)
    # Historical receipts do not pretend to monitor the service continuously.
    calls = client.snapshots
    client.state["version"] = 6
    assert operator.status(plan["plan_id"])["observed"]["version"] == 5
    assert client.snapshots == calls


def test_new_metadata_and_http_200_from_old_container_never_prove_rollout(setup_operator):
    operator, client, clock, directory = setup_operator
    plan = operator.plan("example", NEW)
    operator.apply(plan["plan_id"], lambda _: True)
    # CapRover may record/enqueue the desired image before replacing tasks.
    client.state.update(version=5, image=NEW)
    client.runtime_image = OLD  # Deliberately unobservable through this API.
    client.health_result = {"configured": True, "ok": True, "status_code": 200}
    result = operator.reconcile(plan["plan_id"])
    assert client.runtime_image == OLD
    assert result["status"] == "metadata_http_observed"
    assert result["checks"]["deployment_recorded"] is True
    assert result["checks"]["running_replicas"] is None
    assert result["checks"]["runtime_rollout_proven"] is False
    assert "verified" not in json.dumps(result)


@pytest.mark.parametrize("health,expected", [
    ({"configured": True, "ok": True, "status_code": None}, "deployment_recorded"),
    ({"configured": True, "ok": True, "status_code": 503}, "observed_unhealthy"),
    ({"configured": True, "ok": True, "status_code": True}, "deployment_recorded"),
    ({"configured": False, "ok": True, "status_code": 200}, "deployment_recorded"),
])
def test_health_success_requires_configured_positive_http_status(setup_operator, health, expected):
    operator, client, clock, directory = setup_operator
    plan = operator.plan("example", NEW)
    operator.apply(plan["plan_id"], lambda _: True)
    client.state.update(version=5, image=NEW)
    client.health_result = health
    result = operator.reconcile(plan["plan_id"])
    assert result["status"] == expected
    assert result["checks"]["http_health"]["ok"] is not True
    assert result["checks"]["runtime_rollout_proven"] is False


def test_legacy_receipt_labels_are_not_exposed_as_rollout_verification(setup_operator):
    operator, client, clock, directory = setup_operator
    plan = operator.plan("example", NEW)
    legacy = {"observed": {"version": 5, "image": NEW}, "checks": {"deployment_observed": True}}
    with sqlite3.connect(directory / "ledger.sqlite3") as db:
        db.execute("UPDATE plans SET status='verified', receipt=? WHERE plan_id=?", (json.dumps(legacy), plan["plan_id"]))
    result = operator.get_plan(plan["plan_id"])
    assert result["status"] == "deployment_recorded"
    assert result["checks"] == {"deployment_recorded": True, "runtime_rollout_proven": False}


def _runtime_evidence(images, *, desired=None, running=None):
    count = len(images)
    return {"available": True, "desired_instances": count if desired is None else desired,
            "running_instances": count if running is None else running,
            "current_tasks": [{"slot": index, "image": image, "state": "running", "desired_state": "running"}
                              for index, image in enumerate(images, 1)]}


def test_only_current_new_image_tasks_and_positive_http_can_be_verified(setup_operator):
    operator, client, clock, directory = setup_operator
    client.state["replicas"] = 2
    plan = operator.plan("example", NEW)
    operator.apply(plan["plan_id"], lambda _: True)
    client.state.update(version=5, image=NEW, running_replicas=2,
                        runtime=_runtime_evidence([NEW, NEW.replace("@", ":release@")]))
    result = operator.reconcile(plan["plan_id"])
    assert result["status"] == "verified"
    assert result["checks"]["runtime_rollout_proven"] is True
    assert operator.get_plan(plan["plan_id"])["checks"]["runtime_rollout_proven"] is True
    calls = client.snapshots
    client.state["runtime"] = _runtime_evidence([OLD, OLD])
    assert operator.reconcile(plan["plan_id"])["status"] == "verified"
    assert client.snapshots == calls  # Verified receipt is dated historical evidence.


@pytest.mark.parametrize("runtime", [
    _runtime_evidence([OLD, NEW]),
    _runtime_evidence([NEW]),
    _runtime_evidence([NEW, NEW], desired=1),
    _runtime_evidence([NEW, NEW], running=1),
    {"available": False},
    {"available": True, "desired_instances": 2, "running_instances": 2, "current_tasks": "malformed"},
    {"available": True, "desired_instances": 2, "running_instances": 2,
     "current_tasks": [{"slot": 1, "image": NEW, "state": [], "desired_state": "running"}]},
    {"available": True, "desired_instances": 2, "running_instances": 2,
     "current_tasks": [{"slot": 1, "image": NEW, "state": "running", "desired_state": "running"},
                       {"slot": 1, "image": NEW, "state": "running", "desired_state": "running"}]},
    {"available": True, "desired_instances": 2, "running_instances": 2,
     "current_tasks": [{"slot": 1, "image": NEW, "state": "running", "desired_state": "running"},
                       {"slot": 2, "image": NEW, "state": "starting", "desired_state": "running"}]},
])
def test_old_mixed_missing_or_malformed_tasks_do_not_prove_rollout(setup_operator, runtime):
    operator, client, clock, directory = setup_operator
    client.state["replicas"] = 2
    plan = operator.plan("example", NEW)
    operator.apply(plan["plan_id"], lambda _: True)
    client.state.update(version=5, image=NEW, runtime=runtime)
    result = operator.reconcile(plan["plan_id"])
    # A well-formed explicit shortage may additionally report unhealthy; an
    # unknown/malformed task list can never manufacture rollout verification.
    assert result["status"] in {"metadata_http_observed", "observed_unhealthy"}
    assert result["checks"]["http_health"]["ok"] is True
    assert result["checks"]["runtime_rollout_proven"] is False


def test_explicit_reobservation_can_prove_later_convergence_with_bounded_history(setup_operator):
    operator, client, clock, directory = setup_operator
    plan = operator.plan("example", NEW)
    operator.apply(plan["plan_id"], lambda _: True)
    client.state.update(version=5, image=NEW, runtime=_runtime_evidence([OLD]))
    first = operator.reconcile(plan["plan_id"])
    assert first["status"] == "metadata_http_observed"
    assert first["checks"]["runtime_rollout_proven"] is False
    first_metadata = first["first_metadata_observed"]
    for _ in range(10):
        clock.value += 1
        operator.reconcile(plan["plan_id"])
    client.state["runtime"] = _runtime_evidence([NEW])
    clock.value += 1
    result = operator.reconcile(plan["plan_id"])
    assert result["status"] == "verified"
    assert result["checks"]["runtime_rollout_proven"] is True
    assert result["checks"]["running_replicas"] == 1
    assert result["checks"]["replica_evidence"] == "observed"
    assert result["first_metadata_observed"] == first_metadata
    assert len(result["observation_history"]) == 8
    assert all("observation_history" not in item and "baseline" not in item for item in result["observation_history"])
    assert result["observation_history"][-1]["status"] == "verified"
    assert client.posts == 1


def test_known_record_superseded_without_reclaiming_or_releasing_other_operation(setup_operator):
    operator, client, clock, directory = setup_operator
    plan = operator.plan("example", NEW)
    operator.apply(plan["plan_id"], lambda _: True)
    client.state.update(version=5, image=NEW, runtime=_runtime_evidence([OLD]))
    first = operator.reconcile(plan["plan_id"])
    assert first["status"] == "metadata_http_observed"
    other_image = "registry.example/test@sha256:" + "d" * 64
    other = operator.plan("example", other_image)
    operator.apply(other["plan_id"], lambda _: True)
    client.state.update(version=6, image=other_image, runtime=_runtime_evidence([other_image]))
    calls = client.posts
    result = operator.reconcile(plan["plan_id"])
    assert result["status"] == "superseded"
    assert result["first_metadata_observed"] == first["first_metadata_observed"]
    assert result["checks"]["runtime_rollout_proven"] is False
    assert client.posts == calls
    with pytest.raises(OperatorError) as exc:
        operator.plan("example", NEW)
    assert exc.value.code == "operation_in_progress"  # Other operation still owns its claim.


def test_unavailable_reobservation_preserves_known_record_without_new_claim(setup_operator):
    operator, client, clock, directory = setup_operator
    plan = operator.plan("example", NEW)
    operator.apply(plan["plan_id"], lambda _: True)
    client.state.update(version=5, image=NEW)
    first = operator.reconcile(plan["plan_id"])
    clock.value += 1
    client.snapshot_error = TimeoutError(SECRET)
    result = operator.reconcile(plan["plan_id"])
    assert result["status"] == "metadata_http_observed"
    assert result["observed_at"] == first["observed_at"]
    assert result["last_observation_attempt_at"] == clock.value
    assert result["observation_history"][-1]["observation"] == "unavailable"
    client.snapshot_error = None
    operator.plan("example", OLD)  # Historical metadata-only receipt does not retain a pending claim.


@pytest.mark.parametrize("image", ["repo:latest", "repo@sha256:bad", "https://user:password@example/repo@sha256:" + "a" * 64, "../repo@sha256:" + "a" * 64])
def test_invalid_or_mutable_images_fail_before_network(setup_operator, image):
    operator, client, clock, directory = setup_operator
    with pytest.raises(OperatorError):
        operator.plan("example", image)
    assert client.snapshots == client.posts == 0


def test_noop_building_and_unknown_images_cannot_be_planned(setup_operator):
    operator, client, clock, directory = setup_operator
    with pytest.raises(OperatorError) as exc:
        operator.plan("example", OLD)
    assert exc.value.code == "no_op"
    client.state["is_building"] = True
    with pytest.raises(OperatorError) as exc:
        operator.plan("example", NEW)
    assert exc.value.code == "app_building"
    client.state.update(is_building=False, image=None)
    with pytest.raises(OperatorError):
        operator.plan("example", NEW)


def test_errors_and_local_receipts_never_include_remote_secrets(setup_operator):
    operator, client, clock, directory = setup_operator
    plan = operator.plan("example", NEW)

    def failed_authorization(summary):
        raise ValueError(SECRET)

    with pytest.raises(OperatorError) as exc:
        operator.apply(plan["plan_id"], failed_authorization)
    assert SECRET not in str(exc.value)
    count = client.snapshots
    assert SECRET not in json.dumps(operator.get_plan(plan["plan_id"]))
    assert SECRET not in json.dumps(operator.status())
    assert client.snapshots == count


def test_private_key_creation_is_consistent_under_concurrency(tmp_path):
    directory = tmp_path.resolve() / "private"
    with ThreadPoolExecutor(max_workers=8) as pool:
        values = list(pool.map(lambda _: load_fingerprint_key(directory), range(20)))
    assert len(set(values)) == 1
    assert len(values[0]) == 32
    assert (directory.stat().st_mode & 0o777) == 0o700
    assert ((directory / "fingerprint.key").stat().st_mode & 0o777) == 0o600


def test_unsafe_or_symlinked_storage_is_rejected_without_touching_victim(tmp_path):
    directory = tmp_path.resolve() / "insecure"
    directory.mkdir(mode=0o755)
    with pytest.raises(OperatorError):
        Operator(directory, FakeClient())
    with pytest.raises(OperatorError):
        load_fingerprint_key(directory)
    private = tmp_path.resolve() / "private"
    private.mkdir(mode=0o700)
    victim = tmp_path.resolve() / "victim"
    victim.write_text("untouched")
    victim.chmod(0o600)
    (private / "ledger.sqlite3").symlink_to(victim)
    with pytest.raises(OperatorError):
        Operator(private, FakeClient())
    (private / "fingerprint.key").symlink_to(victim)
    with pytest.raises(OperatorError):
        load_fingerprint_key(private)
    alias = tmp_path.resolve() / "alias"
    alias.symlink_to(private, target_is_directory=True)
    with pytest.raises(OperatorError):
        Operator(alias, FakeClient())
    assert victim.read_text() == "untouched"


def test_storage_replacement_and_plan_corruption_fail_closed(setup_operator):
    operator, client, clock, directory = setup_operator
    plan = operator.plan("example", NEW)
    with sqlite3.connect(directory / "ledger.sqlite3") as db:
        db.execute("UPDATE plans SET intent=? WHERE plan_id=?", ('{"token":"' + SECRET + '"}', plan["plan_id"]))
    with pytest.raises(OperatorError) as exc:
        operator.get_plan(plan["plan_id"])
    assert SECRET not in str(exc.value)
    directory.rename(directory.with_name("renamed"))
    directory.mkdir(mode=0o700)
    with pytest.raises(OperatorError):
        operator.status()
    assert client.posts == 0

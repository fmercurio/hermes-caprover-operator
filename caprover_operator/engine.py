"""Durable, single-attempt image deployment intents; independent of Hermes.

The ledger prevents this plugin from replaying a possible POST. It is not a
distributed lock over CapRover, nor a remote compare-and-swap primitive.
"""
from __future__ import annotations

from contextlib import contextmanager
import copy
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import threading
import time
from typing import Any, Callable
from urllib.parse import urlsplit
import uuid


class OperatorError(RuntimeError):
    """An expected failure whose message contains no remote or secret data."""

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        super().__init__(message or code.replace("_", " "))


_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_PLAN_ID = re.compile(r"[0-9a-f]{32}\Z")
_APP = re.compile(r"[a-z0-9][a-z0-9-]{0,62}\Z")
_COMPONENT = re.compile(r"[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*\Z")
_HOST = re.compile(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?\Z")
_TAG = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}\Z")
_ACTIVE = frozenset({"preflight_pending", "sending", "reconcile_required"})
_TERMINAL = frozenset({
    "expired", "consumed_expired", "consumed_drift", "consumed_preflight_failed",
    "verified", "metadata_http_observed", "deployment_recorded", "observed_unhealthy", "superseded",
})
_REOBSERVABLE = frozenset({"metadata_http_observed", "deployment_recorded", "observed_unhealthy"})
_INTENT_KEYS = ("plan_id", "target", "app", "image", "baseline", "created_at", "expires_at")


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _image(value: Any, *, immutable: bool) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 512 or value != value.strip():
        raise OperatorError("invalid_image")
    if any(ch.isspace() for ch in value) or value.count("@") > 1:
        raise OperatorError("invalid_image")
    name, marker, digest = value.partition("@")
    if marker and not (digest.startswith("sha256:") and _HEX64.fullmatch(digest[7:])):
        raise OperatorError("invalid_image")
    if immutable and not marker:
        raise OperatorError("immutable_image_required")
    # A tag beside a digest is still immutable; the digest determines the image.
    final = name.rsplit("/", 1)[-1]
    if ":" in final:
        leaf, tag = final.rsplit(":", 1)
        if not _TAG.fullmatch(tag):
            raise OperatorError("invalid_image")
        name = name[:len(name) - len(final)] + leaf
    components = name.split("/")
    if len(components) > 1 and ("." in components[0] or ":" in components[0] or components[0] == "localhost"):
        registry = components.pop(0)
        host, colon, port = registry.partition(":")
        if not _HOST.fullmatch(host) or ".." in host:
            raise OperatorError("invalid_image")
        if colon and (not port.isdecimal() or not 1 <= int(port) <= 65535):
            raise OperatorError("invalid_image")
    if not components or not all(_COMPONENT.fullmatch(part) for part in components):
        raise OperatorError("invalid_image")
    return value


def _app(value: Any) -> str:
    if not isinstance(value, str) or not _APP.fullmatch(value):
        raise OperatorError("invalid_app")
    return value


def _runtime(value: Any) -> dict[str, Any]:
    """Normalize optional task evidence, never guessing it from app metadata."""
    unavailable = {"available": False, "desired_instances": None, "running_instances": None, "current_tasks": []}
    if not isinstance(value, dict) or value.get("available") is not True:
        return unavailable
    desired, running, tasks = value.get("desired_instances"), value.get("running_instances"), value.get("current_tasks")
    if (type(desired) is not int or type(running) is not int or not 0 <= desired <= 1_000_000
            or not 0 <= running <= 1_000_000 or not isinstance(tasks, list) or len(tasks) > 512):
        return unavailable
    normalized = []
    states = {"new", "allocated", "pending", "assigned", "accepted", "preparing", "ready", "starting",
              "running", "complete", "shutdown", "failed", "rejected", "remove", "orphaned"}
    for task in tasks:
        if (not isinstance(task, dict) or type(task.get("slot")) is not int or not 1 <= task["slot"] <= 1_000_000
                or not isinstance(task.get("state"), str) or not isinstance(task.get("desired_state"), str)
                or task["state"] not in states or task["desired_state"] not in states):
            return unavailable
        try:
            image = _image(task.get("image"), immutable=False)
        except OperatorError:
            return unavailable
        normalized.append({"slot": task["slot"], "image": image, "state": task["state"], "desired_state": task["desired_state"]})
    return {"available": True, "desired_instances": desired, "running_instances": running, "current_tasks": normalized}


def _image_identity(value: Any) -> tuple[str, str] | None:
    try:
        image = _image(value, immutable=True)
    except OperatorError:
        return None
    repository, digest = image.split("@")
    prefix, slash, leaf = repository.rpartition("/")
    leaf = leaf.split(":", 1)[0]
    return ((prefix + slash + leaf), digest)


def _runtime_rollout_proven(observed: Any, desired_image: str) -> bool:
    if not isinstance(observed, dict):
        return False
    runtime = _runtime(observed.get("runtime"))
    count = observed.get("replicas")
    if (runtime["available"] is not True or type(count) is not int or count <= 0
            or runtime["desired_instances"] != count or runtime["running_instances"] < count
            or len(runtime["current_tasks"]) != count):
        return False
    actual = observed.get("running_replicas")
    if actual is not None and (type(actual) is not int or actual != runtime["running_instances"]):
        return False
    expected = _image_identity(desired_image)
    if expected is None:
        return False
    slots = set()
    for task in runtime["current_tasks"]:
        if (task["state"] != "running" or task["desired_state"] != "running"
                or task["slot"] in slots or _image_identity(task["image"]) != expected):
            return False
        slots.add(task["slot"])
    return slots == set(range(1, count + 1))


def _target(client: Any) -> str:
    value = getattr(client, "target", None)
    if not isinstance(value, str) or not 1 <= len(value) <= 512:
        raise OperatorError("invalid_target")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise OperatorError("invalid_target") from None
    if (parsed.scheme not in {"https", "http"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
        raise OperatorError("invalid_target")
    if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise OperatorError("invalid_target")
    if port is not None and not 1 <= port <= 65535:
        raise OperatorError("invalid_target")
    return value.rstrip("/")


def _snapshot(value: Any, app: str, *, known_image: bool = False) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("app") != app:
        raise OperatorError("invalid_snapshot")
    result: dict[str, Any] = {"app": app}
    for field in ("version", "replicas"):
        item = value.get(field)
        if type(item) is not int or not 0 <= item <= 2**63 - 1:
            raise OperatorError("invalid_snapshot")
        result[field] = item
    image = value.get("image")
    if image is None and not known_image:
        result["image"] = None
    else:
        try:
            result["image"] = _image(image, immutable=False)
        except OperatorError:
            raise OperatorError("invalid_snapshot") from None
    if type(value.get("is_building")) is not bool:
        raise OperatorError("invalid_snapshot")
    result["is_building"] = value["is_building"]
    fingerprint = value.get("config_fingerprint")
    if not isinstance(fingerprint, str) or not _HEX64.fullmatch(fingerprint):
        raise OperatorError("invalid_snapshot")
    result["config_fingerprint"] = fingerprint
    if "running_replicas" in value:
        actual = value["running_replicas"]
        if actual is not None and (type(actual) is not int or not 0 <= actual <= 2**63 - 1):
            raise OperatorError("invalid_snapshot")
        result["running_replicas"] = actual
    if "runtime" in value:
        result["runtime"] = _runtime(value["runtime"])
        if result["runtime"]["available"] and result.get("running_replicas") is None:
            # This is actual task-API evidence, never requested replica count.
            result["running_replicas"] = result["runtime"]["running_instances"]
    return result


def _health(value: Any) -> dict[str, Any]:
    """Keep evidence, never a response body, URL or arbitrary remote reason."""
    if not isinstance(value, dict) or type(value.get("configured")) is not bool:
        return {"configured": False, "ok": None, "status_code": None, "reason": "health_unavailable"}
    configured = value["configured"]
    ok = value.get("ok")
    code = value.get("status_code")
    if ok is not None and type(ok) is not bool:
        ok = None
    if code is not None and (type(code) is not int or not 100 <= code <= 599):
        code = None
    if not configured:
        ok, code = None, None
    elif ok is True and (code is None or not 200 <= code < 300):
        # A claimed success without a positive HTTP status is not evidence.
        ok = None if code is None else False
    return {
        "configured": configured, "ok": ok, "status_code": code,
        "reason": "health_passed" if ok is True else "health_failed" if ok is False else "health_not_proven",
    }


class _PrivateDirectory:
    """Pin an owner-only directory; reject symlink components and unsafe files.

    POSIX owner permissions are the boundary. Other programs running as this
    same OS user are not isolated from this ledger.
    """

    def __init__(self, path: Path) -> None:
        if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "geteuid"):
            raise OperatorError("secure_storage_unsupported")
        self.path = Path(os.path.abspath(os.fspath(path)))
        self.fd = -1
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        current = os.open("/", flags)
        try:
            for index, component in enumerate(self.path.parts[1:]):
                final = index == len(self.path.parts[1:]) - 1
                try:
                    os.mkdir(component, 0o700, dir_fd=current)
                except FileExistsError:
                    pass
                child = os.open(component, flags, dir_fd=current)
                info = os.fstat(child)
                try:
                    if info.st_uid not in {0, os.geteuid()}:
                        raise OperatorError("unsafe_data_directory")
                    sticky_root = info.st_uid == 0 and bool(info.st_mode & stat.S_ISVTX) and not final
                    if info.st_mode & 0o022 and not sticky_root:
                        raise OperatorError("unsafe_data_directory")
                    if final and (info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700):
                        raise OperatorError("unsafe_data_directory")
                except BaseException:
                    os.close(child)
                    raise
                os.close(current)
                current = child
            if self.path == Path("/"):
                raise OperatorError("unsafe_data_directory")
            self.fd = current
            current = -1
            self.identity = self._identity(os.fstat(self.fd))
            self.check_binding()
        except OperatorError:
            self.close()
            raise
        except OSError:
            self.close()
            raise OperatorError("unsafe_data_directory") from None
        finally:
            if current != -1:
                os.close(current)

    @staticmethod
    def _identity(info: os.stat_result) -> tuple[int, int]:
        return info.st_dev, info.st_ino

    def check_binding(self) -> None:
        try:
            info = os.stat(self.path, follow_symlinks=False)
            if (not stat.S_ISDIR(info.st_mode) or self._identity(info) != self.identity
                    or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700):
                raise OperatorError("unsafe_data_directory")
            # Re-check ancestry; a renamed or symlinked parent must not redirect SQLite.
            current = Path("/")
            for part in self.path.parts[1:]:
                current /= part
                if current.is_symlink():
                    raise OperatorError("unsafe_data_directory")
        except OSError:
            raise OperatorError("unsafe_data_directory") from None

    def file(self, name: str, *, create: bool = False) -> int:
        self.check_binding()
        flags = os.O_RDWR | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        try:
            if create:
                try:
                    fd = os.open(name, flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=self.fd)
                except FileExistsError:
                    fd = os.open(name, flags, dir_fd=self.fd)
            else:
                fd = os.open(name, flags, dir_fd=self.fd)
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
                os.close(fd)
                raise OperatorError("unsafe_state_file")
            return fd
        except OSError:
            raise OperatorError("unsafe_state_file") from None

    def close(self) -> None:
        if self.fd != -1:
            os.close(self.fd)
            self.fd = -1


def load_fingerprint_key(data_dir: Path) -> bytes:
    directory = _PrivateDirectory(Path(data_dir))
    lock_fd = -1
    temporary: str | None = None
    try:
        lock_fd = directory.file("fingerprint.lock", create=True)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        directory.check_binding()
        try:
            fd = directory.file("fingerprint.key")
        except OperatorError:
            # Missing is the only admissible reason to create a new key. Never
            # replace a symlink, unsafe file or damaged previously written key.
            try:
                os.stat("fingerprint.key", dir_fd=directory.fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise
        else:
            try:
                key = os.read(fd, 33)
            finally:
                os.close(fd)
            if len(key) != 32:
                raise OperatorError("invalid_fingerprint_key")
            return key
        temporary = ".fingerprint-" + uuid.uuid4().hex
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory.fd)
        try:
            key = secrets.token_bytes(32)
            if os.write(fd, key) != len(key):
                raise OperatorError("state_write_failed")
            os.fsync(fd)
        finally:
            os.close(fd)
        os.link(temporary, "fingerprint.key", src_dir_fd=directory.fd, dst_dir_fd=directory.fd, follow_symlinks=False)
        os.unlink(temporary, dir_fd=directory.fd)
        temporary = None
        os.fsync(directory.fd)
        directory.check_binding()
        return key
    except OSError:
        raise OperatorError("state_write_failed") from None
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary, dir_fd=directory.fd)
            except OSError:
                pass
        if lock_fd != -1:
            os.close(lock_fd)
        directory.close()


class Operator:
    def __init__(self, data_dir: Path, client: Any, *, clock: Callable[[], float] | None = None) -> None:
        self.client = client
        self.target = _target(client)
        self.clock = clock or time.time
        self._lock = threading.RLock()
        self._closed = False
        self._directory = _PrivateDirectory(Path(data_dir))
        self._db: sqlite3.Connection | None = None
        try:
            fd = self._directory.file("ledger.sqlite3", create=True)
            self._db_identity = _PrivateDirectory._identity(os.fstat(fd))
            os.close(fd)
            self._check_files()
            self._db = sqlite3.connect(str(self._directory.path / "ledger.sqlite3"), timeout=10, isolation_level=None, check_same_thread=False)
            self._db.row_factory = sqlite3.Row
            self._db.execute("PRAGMA busy_timeout=10000")
            self._db.execute("PRAGMA journal_mode=DELETE")
            self._db.execute("PRAGMA synchronous=FULL")
            self._db.execute("PRAGMA foreign_keys=ON")
            with self._transaction() as db:
                version = db.execute("PRAGMA user_version").fetchone()[0]
                if version not in {0, 1}:
                    raise OperatorError("unsupported_ledger")
                db.execute("CREATE TABLE IF NOT EXISTS plans (plan_id TEXT PRIMARY KEY, intent TEXT NOT NULL, plan_hash TEXT NOT NULL, target TEXT NOT NULL, app TEXT NOT NULL, created_at REAL NOT NULL, status TEXT NOT NULL, receipt TEXT NOT NULL)")
                db.execute("CREATE TABLE IF NOT EXISTS active_claims (target TEXT NOT NULL, app TEXT NOT NULL, plan_id TEXT NOT NULL UNIQUE REFERENCES plans(plan_id), PRIMARY KEY(target, app))")
                db.execute("PRAGMA user_version=1")
        except sqlite3.Error:
            self.close()
            raise OperatorError("ledger_unavailable") from None
        except BaseException:
            self.close()
            raise

    def _now(self) -> float:
        value = self.clock()
        if type(value) not in {float, int} or not math.isfinite(value):
            raise OperatorError("invalid_clock")
        return float(value)

    def _check_files(self) -> None:
        self._directory.check_binding()
        fd = self._directory.file("ledger.sqlite3")
        try:
            if _PrivateDirectory._identity(os.fstat(fd)) != self._db_identity:
                raise OperatorError("ledger_replaced")
        finally:
            os.close(fd)
        for name in ("ledger.sqlite3-journal", "ledger.sqlite3-wal", "ledger.sqlite3-shm"):
            try:
                fd = os.open(name, os.O_RDWR | os.O_NOFOLLOW, dir_fd=self._directory.fd)
            except FileNotFoundError:
                continue
            except OSError:
                raise OperatorError("unsafe_state_file") from None
            try:
                info = os.fstat(fd)
                # SQLite can unlink its journal as another process opens it.
                # A safely opened, now-unlinked regular journal is harmless.
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                        or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink not in {0, 1}):
                    raise OperatorError("unsafe_state_file")
            finally:
                os.close(fd)

    @contextmanager
    def _transaction(self):
        with self._lock:
            if self._closed or self._db is None:
                raise OperatorError("operator_closed")
            self._check_files()
            try:
                self._db.execute("BEGIN IMMEDIATE")
                yield self._db
                self._check_files()
                self._db.execute("COMMIT")
            except BaseException as exc:
                if self._db.in_transaction:
                    try:
                        self._db.execute("ROLLBACK")
                    except sqlite3.Error:
                        pass
                if isinstance(exc, sqlite3.Error):
                    raise OperatorError("ledger_unavailable") from None
                raise

    def _read(self, db: sqlite3.Connection, plan_id: str) -> dict[str, Any]:
        if not isinstance(plan_id, str) or not _PLAN_ID.fullmatch(plan_id):
            raise OperatorError("invalid_plan_id")
        row = db.execute("SELECT * FROM plans WHERE plan_id=?", (plan_id,)).fetchone()
        if row is None:
            raise OperatorError("plan_not_found")
        try:
            intent = json.loads(row["intent"])
            expected = hashlib.sha256(_canonical(intent).encode()).hexdigest()
            if (set(intent) != set(_INTENT_KEYS) or intent["plan_id"] != plan_id
                    or expected != row["plan_hash"] or intent["target"] != row["target"]
                    or intent["app"] != row["app"] or intent["target"] != self.target):
                raise OperatorError("invalid_plan")
            receipt = json.loads(row["receipt"])
            # Pre-release ledgers may contain optimistic labels. Historical
            # API/HTTP evidence must not be exposed as runtime rollout proof.
            status = {"deployment_observed": "deployment_recorded"}.get(row["status"], row["status"])
            if "checks" in receipt or "observed" in receipt:
                checks = dict(receipt.get("checks", {}))
                if "deployment_observed" in checks:
                    checks["deployment_recorded"] = checks.pop("deployment_observed")
                observed = receipt.get("observed", {})
                proof = (checks.get("runtime_rollout_proven") is True and isinstance(observed, dict)
                         and type(observed.get("version")) is int and observed["version"] > intent["baseline"]["version"]
                         and observed.get("image") == intent["image"] and observed.get("is_building") is False
                         and _runtime_rollout_proven(observed, intent["image"]))
                receipt["checks"] = {**checks, "runtime_rollout_proven": proof}
                if status == "verified" and not (proof and _health(checks.get("http_health"))["ok"] is True):
                    health = _health(checks.get("http_health"))
                    status = "metadata_http_observed" if health["ok"] is True else "deployment_recorded"
            elif status == "verified":
                status = "deployment_recorded"
            return {**intent, "plan_hash": row["plan_hash"], **receipt, "status": status}
        except (ValueError, TypeError, KeyError):
            raise OperatorError("invalid_plan") from None

    @staticmethod
    def _save(db: sqlite3.Connection, plan: dict[str, Any], status: str, **details: Any) -> dict[str, Any]:
        receipt = {key: value for key, value in plan.items() if key not in {*_INTENT_KEYS, "plan_hash", "status"}}
        receipt.update(details)
        checks = receipt.get("checks", {})
        receipt["checks"] = {**checks, "runtime_rollout_proven": checks.get("runtime_rollout_proven") is True}
        db.execute("UPDATE plans SET status=?, receipt=? WHERE plan_id=?", (status, _canonical(receipt), plan["plan_id"]))
        if status in _TERMINAL:
            db.execute("DELETE FROM active_claims WHERE plan_id=?", (plan["plan_id"],))
        return {**plan, **receipt, "status": status}

    def _stable_target(self) -> None:
        if _target(self.client) != self.target:
            raise OperatorError("target_changed")

    @contextmanager
    def _operation_lock(self, app: str):
        # A persistent SQLite claim handles crashes. This OS lock additionally
        # prevents another local process reconciling/releasing a claim while
        # its writer is still in the transport boundary. It dies with a process.
        name = "operation-" + hashlib.sha256(_canonical([self.target, app]).encode()).hexdigest() + ".lock"
        fd = self._directory.file(name, create=True)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise OperatorError("operation_in_progress") from None
            self._directory.check_binding()
            yield
        finally:
            os.close(fd)

    def plan(self, app: str, image: str, ttl_seconds: int = 300) -> dict[str, Any]:
        app, image = _app(app), _image(image, immutable=True)
        if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 3600:
            raise OperatorError("invalid_ttl")
        self._stable_target()
        with self._transaction() as db:
            if db.execute("SELECT 1 FROM active_claims WHERE target=? AND app=?", (self.target, app)).fetchone():
                raise OperatorError("operation_in_progress")
        try:
            baseline = _snapshot(self.client.snapshot(app), app, known_image=True)
        except Exception:
            raise OperatorError("snapshot_unavailable") from None
        if baseline["is_building"]:
            raise OperatorError("app_building")
        if baseline["image"] == image:
            raise OperatorError("no_op")
        self._stable_target()
        now = self._now()
        intent = {"plan_id": uuid.uuid4().hex, "target": self.target, "app": app, "image": image,
                  "baseline": baseline, "created_at": now, "expires_at": now + ttl_seconds}
        digest = hashlib.sha256(_canonical(intent).encode()).hexdigest()
        with self._transaction() as db:
            if db.execute("SELECT 1 FROM active_claims WHERE target=? AND app=?", (self.target, app)).fetchone():
                raise OperatorError("operation_in_progress")
            db.execute("INSERT INTO plans VALUES(?,?,?,?,?,?,?,?)", (intent["plan_id"], _canonical(intent), digest, self.target, app, now, "planned", "{}"))
        return {**intent, "plan_hash": digest, "status": "planned"}

    def get_plan(self, plan_id: str) -> dict[str, Any]:
        with self._transaction() as db:
            return self._read(db, plan_id)

    def apply(self, plan_id: str, authorize: Callable[[dict[str, Any]], bool]) -> dict[str, Any]:
        self._stable_target()
        with self._transaction() as db:
            plan = self._read(db, plan_id)
            if plan["status"] != "planned":
                return plan
            if self._now() >= plan["expires_at"]:
                return self._save(db, plan, "expired")
        try:
            authorized = callable(authorize) and authorize(copy.deepcopy(plan)) is True
        except Exception:
            raise OperatorError("authorization_failed") from None
        if not authorized:
            return {**plan, "status": "authorization_denied"}
        with self._operation_lock(plan["app"]):
            return self._apply_authorized(plan_id)

    def _apply_authorized(self, plan_id: str) -> dict[str, Any]:
        self._stable_target()
        with self._transaction() as db:
            plan = self._read(db, plan_id)
            if plan["status"] != "planned":
                return plan
            now = self._now()
            if now >= plan["expires_at"]:
                return self._save(db, plan, "expired")
            if db.execute("SELECT 1 FROM active_claims WHERE target=? AND app=?", (self.target, plan["app"])).fetchone():
                raise OperatorError("operation_in_progress")
            db.execute("INSERT INTO active_claims VALUES(?,?,?)", (self.target, plan["app"], plan_id))
            plan = self._save(db, plan, "preflight_pending", consumed_at=now, requested={"image": plan["image"]})
        try:
            self._stable_target()
            fresh = _snapshot(self.client.snapshot(plan["app"]), plan["app"], known_image=True)
            self._stable_target()
        except Exception:
            with self._transaction() as db:
                current = self._read(db, plan_id)
                if current["status"] != "preflight_pending":
                    return current
                return self._save(db, current, "consumed_preflight_failed", checks={"preflight": "unavailable", "post_attempted": False})
        with self._transaction() as db:
            current = self._read(db, plan_id)
            if current["status"] != "preflight_pending":
                return current
            if fresh != plan["baseline"]:
                return self._save(db, current, "consumed_drift", observed=fresh, observed_at=self._now(), checks={"preflight": "drift", "post_attempted": False})
            if self._now() >= plan["expires_at"]:
                return self._save(db, current, "consumed_expired", checks={"preflight": "expired", "post_attempted": False})
            claim = db.execute("SELECT plan_id FROM active_claims WHERE target=? AND app=?", (self.target, plan["app"])).fetchone()
            if claim is None or claim[0] != plan_id:
                raise OperatorError("claim_lost")
            plan = self._save(db, current, "sending", attempted_at=self._now(), checks={"preflight": "matched", "post_attempted": True})
        # There is intentionally no retry around this boundary. BaseException
        # (including process termination) leaves the durable status as sending.
        try:
            self._stable_target()
            response = self.client.deploy_image(plan["app"], plan["image"])
            outcome = "accepted" if isinstance(response, dict) else "uncertain"
        except Exception:
            outcome = "uncertain"
        with self._transaction() as db:
            current = self._read(db, plan_id)
            if current["status"] in _TERMINAL:
                return current
            return self._save(db, current, "reconcile_required", trigger_outcome=outcome)

    def reconcile(self, plan_id: str) -> dict[str, Any]:
        plan = self.get_plan(plan_id)
        if plan["status"] in _TERMINAL and plan["status"] not in _REOBSERVABLE:
            return plan
        if plan["status"] not in _ACTIVE | _REOBSERVABLE:
            raise OperatorError("reconciliation_not_required")
        with self._operation_lock(plan["app"]):
            return self._reconcile_locked(plan_id)

    def _reconcile_locked(self, plan_id: str) -> dict[str, Any]:
        self._stable_target()
        plan = self.get_plan(plan_id)
        if plan["status"] in _TERMINAL and plan["status"] not in _REOBSERVABLE:
            return plan
        if plan["status"] not in _ACTIVE | _REOBSERVABLE:
            raise OperatorError("reconciliation_not_required")
        try:
            observed = _snapshot(self.client.snapshot(plan["app"]), plan["app"])
            self._stable_target()
        except Exception:
            with self._transaction() as db:
                current = self._read(db, plan_id)
                if current["status"] in _TERMINAL and current["status"] not in _REOBSERVABLE:
                    return current
                now = self._now()
                status = current["status"] if current["status"] in _REOBSERVABLE else "reconcile_required"
                history = self._observation_history(current, status, now, None, {"runtime_rollout_proven": False}, "unavailable")
                return self._save(db, current, status, last_observation_attempt_at=now,
                                  observation="unavailable", observation_history=history)
        newer = observed["version"] > plan["baseline"]["version"]
        exact = observed["image"] == plan["image"]
        deployed = newer and exact and not observed["is_building"]
        health = {"configured": False, "ok": None, "status_code": None, "reason": "deployment_not_observed"}
        if deployed:
            try:
                health = _health(self.client.health(plan["app"]))
            except Exception:
                health = _health(None)
        actual = observed.get("running_replicas")
        replicas_contradict = observed["replicas"] == 0 or (actual is not None and actual < observed["replicas"])
        runtime_proven = deployed and _runtime_rollout_proven(observed, plan["image"])
        checks = {
            "newer_version": newer, "exact_image": exact, "not_building": not observed["is_building"],
            "deployment_recorded": deployed, "configured_replicas": observed["replicas"],
            "running_replicas": actual, "replica_evidence": "unavailable" if actual is None else "observed",
            "http_health": health, "runtime_rollout_proven": runtime_proven,
        }
        result = "reconcile_required"
        if deployed:
            if health["ok"] is False or replicas_contradict:
                result = "observed_unhealthy"
            elif health["configured"] and health["ok"] is True:
                result = "verified" if runtime_proven else "metadata_http_observed"
            else:
                result = "deployment_recorded"
        elif plan["status"] in _REOBSERVABLE:
            # We already observed the operation recorded. A later observation
            # never turns that historical fact into a new pending write claim.
            first = plan.get("first_metadata_observed") or plan.get("observed", {})
            first_version = first.get("version") if isinstance(first, dict) else None
            result = ("superseded" if type(first_version) is int and observed["version"] > first_version
                      and isinstance(observed["image"], str) and not exact else "deployment_recorded")
        with self._transaction() as db:
            current = self._read(db, plan_id)
            if current["status"] in _TERMINAL and current["status"] not in _REOBSERVABLE:
                return current
            now = self._now()
            details = {"observed": observed, "observed_at": now, "observation": "available", "checks": checks,
                       "observation_history": self._observation_history(current, result, now, observed, checks, "available")}
            if deployed and "first_metadata_observed" not in current:
                details["first_metadata_observed"] = {"version": observed["version"], "image": observed["image"], "observed_at": now}
            return self._save(db, current, result, **details)

    @staticmethod
    def _observation_history(plan: dict[str, Any], status: str, now: float, observed: dict[str, Any] | None,
                             checks: dict[str, Any], availability: str) -> list[dict[str, Any]]:
        previous = plan.get("observation_history", [])
        if not isinstance(previous, list):
            previous = []
        # Only flat summaries produced here are retained, never whole receipts.
        previous = [item for item in previous if isinstance(item, dict)
                    and set(item) == {"status", "observed_at", "version", "image", "checks", "observation"}]
        return previous[-7:] + [{"status": status, "observed_at": now,
                                 "version": observed.get("version") if observed else None,
                                 "image": observed.get("image") if observed else None,
                                 "checks": checks, "observation": availability}]

    def status(self, plan_id: str | None = None, limit: int = 20) -> dict[str, Any]:
        if plan_id is not None:
            return self.get_plan(plan_id)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise OperatorError("invalid_limit")
        with self._transaction() as db:
            ids = db.execute("SELECT plan_id FROM plans WHERE target=? ORDER BY created_at DESC, plan_id DESC LIMIT ?", (self.target, limit)).fetchall()
            return {"target": self.target, "operations": [self._read(db, row[0]) for row in ids], "limit": limit}

    def close(self) -> None:
        with self._lock:
            if self._db is not None:
                self._db.close()
                self._db = None
            self._directory.close()
            self._closed = True

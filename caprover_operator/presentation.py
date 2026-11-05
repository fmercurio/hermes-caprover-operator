"""Bounded, historical operation cards. No client, ledger or approval access.

The only write is an immutable HTML presentation artifact in an existing private
profile directory. A card never refreshes itself or offers an execution action.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from html import escape
import json
import math
import os
from pathlib import Path
import secrets
import stat
import time
from typing import Any
from urllib.parse import urlsplit

from .guidance import result_guidance


_MAX_CARDS = 5
_MAX_BYTES = 64 * 1024
_STATUS = {
    "planned": ("Ready for review", "neutral"),
    "expired": ("Plan expired", "warning"),
    "consumed_expired": ("Plan expired", "warning"),
    "consumed_drift": ("Change blocked · app changed", "warning"),
    "consumed_preflight_failed": ("Change blocked · evidence unavailable", "warning"),
    "preflight_pending": ("Outcome uncertain", "warning"),
    "sending": ("Outcome uncertain", "warning"),
    "reconcile_required": ("Outcome uncertain", "warning"),
    "metadata_http_observed": ("Image recorded · runtime unproven", "warning"),
    "deployment_recorded": ("Image recorded · runtime unproven", "warning"),
    "observed_unhealthy": ("Unhealthy evidence observed", "danger"),
    "verified": ("Rollout evidence verified", "success"),
    "superseded": ("Superseded by a later image", "neutral"),
    "authorization_denied": ("Native approval required", "warning"),
}


def _text(value: Any, limit: int = 512) -> str | None:
    if not isinstance(value, str) or not 0 < len(value) <= limit:
        return None
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        return None
    return value


def _timestamp(value: Any) -> str | None:
    if type(value) not in {int, float} or not math.isfinite(value):
        return None
    try:
        return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    except (ValueError, OverflowError, OSError):
        return None


def _count(value: Any) -> int | None:
    return value if type(value) is int and 0 <= value <= 1_000_000 else None


def _origin(value: Any) -> str | None:
    value = _text(value, 2048)
    if value is None:
        return None
    try:
        parts = urlsplit(value)
        if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
            return None
        if parts.path not in {"", "/"} or parts.query or parts.fragment:
            return None
        return f"{parts.scheme}://{parts.netloc}"
    except ValueError:
        return None


def _image(value: Any) -> dict:
    full = _text(value)
    if full is None:
        return {"full": None, "short": "Not recorded"}
    name, separator, digest = full.partition("@sha256:")
    short_name = name if len(name) <= 55 else "…" + name[-54:]
    short = f"{short_name}@sha256:{digest[:12]}…" if separator and len(digest) == 64 else short_name
    return {"full": full, "short": short}


def card_data(receipt: Any, *, app: str, target: str, now: float | None = None) -> dict | None:
    """Select public facts, never raw task/log/config payloads or fingerprints."""
    if not isinstance(receipt, dict) or receipt.get("app") != app or receipt.get("target") != target:
        return None
    plan_id = _text(receipt.get("plan_id"), 64)
    if plan_id is None:
        return None
    baseline = receipt.get("baseline") if isinstance(receipt.get("baseline"), dict) else {}
    checks = receipt.get("checks") if isinstance(receipt.get("checks"), dict) else {}
    observed = receipt.get("observed") if isinstance(receipt.get("observed"), dict) else {}
    health = checks.get("http_health") if isinstance(checks.get("http_health"), dict) else {}
    raw_status = receipt.get("status") if isinstance(receipt.get("status"), str) else "unknown"
    status = raw_status if raw_status in _STATUS else "unknown"
    current = time.time() if now is None else now
    expires = receipt.get("expires_at")
    if status == "planned" and _timestamp(expires) is not None and expires <= current:
        status = "expired"
    observed_at = _timestamp(receipt.get("observed_at"))
    runtime = checks.get("runtime_rollout_proven") is True
    http_ok = health.get("configured") is True and health.get("ok") is True
    # Never paint a contradictory or incomplete imported receipt green.
    if status == "verified" and not (runtime and http_ok and observed_at
                                       and checks.get("deployment_recorded") is True):
        status = "evidence_incomplete"
    title, tone = _STATUS.get(status, ("Evidence incomplete · inspect receipt", "warning"))
    attempt_at = _timestamp(receipt.get("attempted_at"))
    attempted = attempt_at is not None or checks.get("post_attempted") is True
    if attempted:
        attempt_label = "Attempt marker recorded"
    elif checks.get("post_attempted") is False:
        attempt_label = "No deployment request attempted"
    else:
        attempt_label = "No attempt marker recorded"
    health_label = "Passed" if http_ok else ("Failed" if health.get("ok") is False else "Not proven")
    guidance = result_guidance(receipt, configured_app=app, configured_target=target, now=current)
    if status == "evidence_incomplete":
        guidance = {"message": "Inspect the saved receipt before relying on it; the displayed evidence is incomplete.",
                    "next_step": {"tool": "caprover_status"}}
    return {
        "schema": 1, "historical": True,
        "app": _text(app, 128), "target": _origin(target),
        "plan": {"id": plan_id, "hash": _text(receipt.get("plan_hash"), 64),
                 "created_at": _timestamp(receipt.get("created_at")), "expires_at": _timestamp(expires)},
        "previous_image": _image(baseline.get("image")), "requested_image": _image(receipt.get("image")),
        "receipt": {"status": raw_status if raw_status in _STATUS else "unknown",
                    "display_status": status, "title": title, "tone": tone, "observed_at": observed_at},
        "approval": {"requirement": "Fresh native human approval for every apply",
                     "record": "Not separately stored in this receipt"},
        "attempt": {"recorded": attempted, "recorded_at": attempt_at, "label": attempt_label,
                    "meaning": "A durable marker precedes the possible POST; it does not prove network delivery or count server requests."},
        "evidence": {
            "metadata": "Requested image recorded" if checks.get("deployment_recorded") is True else "Not proven",
            "runtime": "Exact-image tasks proven" if runtime else "Exact-image tasks not proven",
            "http": health_label, "http_status": _count(health.get("status_code")),
            "running_replicas": _count(checks.get("running_replicas", observed.get("running_replicas"))),
            "configured_replicas": _count(checks.get("configured_replicas", observed.get("replicas", baseline.get("replicas")))),
            "baseline_version": _count(baseline.get("version")), "observed_version": _count(observed.get("version")),
        },
        "next_step": {"text": guidance["message"], "tool": guidance["next_step"]["tool"], "automatic": False},
    }


_CSS = """
*{box-sizing:border-box}body{margin:0;font-family:Inter,ui-sans-serif,system-ui,-apple-system,sans-serif;color:#eef5f9;background:transparent}
.card{max-width:640px;background:#101c25;border:1px solid #31434d;border-radius:18px;overflow:hidden}
.top{padding:18px 24px;border-bottom:1px solid #2c3e49;display:flex;gap:12px;align-items:center;justify-content:space-between}
.brand{font-weight:700;font-size:13px;letter-spacing:.02em}.stamp{font-size:10px;font-weight:600;color:#a8bbc6;letter-spacing:.08em;text-transform:uppercase}
.body{padding:24px}.status{display:inline-block;font-size:14px;line-height:1.4;padding:5px 9px;border-radius:6px;font-weight:650;background:#263d49;color:#d9edf5}
.warning{color:#ffdd9d;background:#443722}.danger{color:#ffc1bd;background:#492d30}.success{color:#a2eec8;background:#1c4338}
h1{font-size:29px;letter-spacing:-.04em;line-height:1.15;margin:14px 0 6px;overflow-wrap:anywhere}.target{font-size:12px;color:#a9bdc9;overflow-wrap:anywhere}
.images{margin:22px 0 18px;padding:15px 16px;border:1px solid #30434e;border-radius:10px;display:grid;gap:15px}
.label{font-size:10px;text-transform:uppercase;letter-spacing:.09em;color:#93aab7;font-weight:650;margin-bottom:5px}
code{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:11px;overflow-wrap:anywhere;user-select:all}.requested{color:#b9edff}
.facts{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:1px;background:#30434e;border:1px solid #30434e;border-radius:10px;overflow:hidden}
.fact{padding:13px 12px;background:#13232e}.value{font-size:12px;font-weight:600;line-height:1.45;overflow-wrap:anywhere}.note{color:#a8bcc8;font-size:11px;line-height:1.5}
.audit{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin:19px 0}.audit .value{font-size:11px}.next{border-left:3px solid #87dbed;background:#192d37;padding:13px 15px;border-radius:0 8px 8px 0;margin-top:18px}
.next p{font-size:12px;line-height:1.55;margin:5px 0 8px}.next code{font-size:11px;color:#b9edff}.foot{border-top:1px solid #30434e;padding:15px 24px;color:#a4b7c2;font-size:10px;line-height:1.6}
details{margin-top:16px;color:#bdd0dc;font-size:11px}summary{cursor:pointer;user-select:none}dl{margin:10px 0 0}dt{color:#91aab9;margin-top:10px;font-size:10px}dd{margin:4px 0;line-height:1.6}
@media(max-width:450px){.body{padding:18px}.top,.foot{padding:15px 18px}.top{align-items:flex-start}.facts{grid-template-columns:1fr}.audit{grid-template-columns:1fr}h1{font-size:25px}}
"""


def render_html(card: dict) -> str:
    """Render only our normalized card. All data is escaped as text."""
    def text(value):
        return escape(str(value) if value is not None else "Not recorded", quote=True)

    receipt, evidence, plan = card["receipt"], card["evidence"], card["plan"]
    tone = receipt["tone"] if receipt["tone"] in {"neutral", "warning", "danger", "success"} else "neutral"
    replicas = f"{evidence['running_replicas'] if evidence['running_replicas'] is not None else '?'} / {evidence['configured_replicas'] if evidence['configured_replicas'] is not None else '?'} replicas observed"
    http = evidence["http"] + (f" · HTTP {evidence['http_status']}" if evidence["http_status"] else "")
    details = [("Plan ID", plan["id"]), ("Intent hash", plan["hash"]),
               ("Created (UTC)", plan["created_at"]), ("Plan expires (UTC)", plan["expires_at"]),
               ("Previous image · select to copy", card["previous_image"]["full"]),
               ("Requested image · select to copy", card["requested_image"]["full"]),
               ("Attempt marker (UTC)", card["attempt"]["recorded_at"])]
    detail_html = "".join(f"<dt>{text(label)}</dt><dd><code>{text(value)}</code></dd>" for label, value in details)
    # Hermes adds its own sizing bridge to inline HTML. Permit that inline script,
    # while this document contains none and cannot load assets or make requests.
    csp = "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'none'; img-src 'none'; base-uri 'none'; form-action 'none'"
    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="{csp}"><title>CapRover operation receipt</title><style>{_CSS}</style></head>
<body><article class="card" aria-label="Historical CapRover operation receipt">
<header class="top"><span class="brand">CAPROVER OPERATOR</span><span class="stamp">Historical snapshot</span></header>
<div class="body"><span class="status {tone}">{text(receipt['title'])}</span>
<h1>{text(card['app'])}</h1><div class="target">{text(card['target'])}</div>
<section class="images" aria-label="Planned image change"><div><div class="label">Previous image</div><code>{text(card['previous_image']['short'])}</code></div>
<div><div class="label">Requested image</div><code class="requested">{text(card['requested_image']['short'])}</code></div></section>
<section class="facts" aria-label="Recorded evidence"><div class="fact"><div class="label">CapRover metadata</div><div class="value">{text(evidence['metadata'])}</div></div>
<div class="fact"><div class="label">Runtime</div><div class="value">{text(evidence['runtime'])}</div><div class="note">{text(replicas)}</div></div>
<div class="fact"><div class="label">HTTP health</div><div class="value">{text(http)}</div></div></section>
<section class="audit"><div><div class="label">Native approval</div><div class="value">Required for every apply</div><div class="note">Not separately stored in this receipt</div></div>
<div><div class="label">Deployment attempt</div><div class="value">{text(card['attempt']['label'])}</div><div class="note">A marker is not proof of server delivery</div></div></section>
<div class="label">Evidence observed (UTC)</div><div class="value">{text(receipt['observed_at'])}</div>
<aside class="next"><div class="label">Next step · manual</div><p>{text(card['next_step']['text'])}</p><code>{text(card['next_step']['tool'])}</code></aside>
<details><summary>Plan details &amp; full image references</summary><dl>{detail_html}</dl></details></div>
<footer class="foot">Saved evidence, not live availability. This card cannot approve, deploy, retry or roll back. Metadata and HTTP alone do not prove the serving image.</footer>
</article></body></html>'''


def _private_directory(path: Path) -> int:
    """Open an existing directory without following a symlink in any component."""
    if not path.is_absolute() or any(part in {".", ".."} for part in path.parts):
        raise OSError("invalid card directory")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open("/", flags)
    try:
        for component in path.parts[1:]:
            child = os.open(component, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise OSError("card directory must be private")
        return fd
    except BaseException:
        os.close(fd)
        raise


def _write_snapshot(data_dir: Path, html: str) -> Path:
    content = html.encode("utf-8")
    if len(content) > _MAX_BYTES:
        raise ValueError("card is too large")
    name = hashlib.sha256(content).hexdigest() + ".html"
    root = _private_directory(data_dir)
    cards = None
    temporary = None
    try:
        try:
            os.mkdir("cards", mode=0o700, dir_fd=root)
        except FileExistsError:
            pass
        cards = os.open("cards", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
        info = os.fstat(cards)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise OSError("card directory must be private")
        temporary = "." + secrets.token_hex(16) + ".tmp"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=cards)
        with os.fdopen(fd, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        try:
            os.link(temporary, name, src_dir_fd=cards, dst_dir_fd=cards, follow_symlinks=False)
        except FileExistsError:
            pass
        os.unlink(temporary, dir_fd=cards)
        temporary = None
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=cards)
        with os.fdopen(fd, "rb") as existing:
            info = os.fstat(existing.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1
                    or info.st_size != len(content) or existing.read(_MAX_BYTES + 1) != content):
                raise OSError("invalid existing card")
        os.fsync(cards)
        return data_dir / "cards" / name
    finally:
        if temporary is not None and cards is not None:
            os.unlink(temporary, dir_fd=cards)
        if cards is not None:
            os.close(cards)
        os.close(root)


def present(result: Any, *, data_dir: Path, app: str, target: str, now: float | None = None) -> Any:
    """Add card facts and best-effort HTML; never alter operation success/failure.

    Repeated results reuse the same content hash. History exports at most five
    cards and never exports receipts from another configured scope.
    """
    if not isinstance(result, dict):
        return result
    receipts = result.get("operations") if isinstance(result.get("operations"), list) else [result]
    cards = []
    for receipt in receipts[:100]:
        card = card_data(receipt, app=app, target=target, now=now)
        if card is None:
            continue
        item = {"card": card, "available": False}
        try:
            path = _write_snapshot(Path(data_dir), render_html(card))
            item.update(available=True, file=str(path), preview=f"::preview{{file={json.dumps(str(path))}}}")
        except (OSError, ValueError, TypeError):
            item["reason"] = "card_export_unavailable"
        cards.append(item)
        if len(cards) == _MAX_CARDS:
            break
    if not cards:
        return result
    return {**result, "presentation": {"kind": "caprover_operation_cards", "historical": True,
                                      "instruction": "Display each available preview directive unchanged on its own line. These are historical, read-only operation cards. If HTML is unavailable, summarize card facts; do not repeat an operation to repair its presentation.",
                                      "cards": cards, "history_limit": _MAX_CARDS}}

#!/usr/bin/env python3
"""Generate clearly synthetic cards for visual review; no Hermes or CapRover I/O."""
from copy import deepcopy
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from caprover_operator.presentation import present


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="Private directory for synthetic HTML snapshots")
    args = parser.parse_args()
    root = args.output.resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    now = 1_793_880_000
    base = {"app": "example-notes", "target": "https://captain.example.test",
            "plan_id": "1" * 32, "plan_hash": "2" * 64,
            "image": "ghcr.io/example/notes@sha256:" + "b" * 64,
            "baseline": {"image": "ghcr.io/example/notes@sha256:" + "a" * 64,
                         "version": 8, "replicas": 1},
            "created_at": now, "expires_at": now + 300}
    scenarios = []
    for status in ("planned", "reconcile_required", "metadata_http_observed", "observed_unhealthy", "verified"):
        value = deepcopy(base)
        value["status"] = status
        if status != "planned":
            value["attempted_at"] = now + 20
        if status in {"metadata_http_observed", "observed_unhealthy", "verified"}:
            value.update(observed_at=now + 35, observed={"version": 9, "replicas": 1, "image": base["image"]},
                         checks={"deployment_recorded": True, "configured_replicas": 1,
                                 "running_replicas": 0 if status == "observed_unhealthy" else 1,
                                 "runtime_rollout_proven": status == "verified",
                                 "http_health": {"configured": True, "ok": status != "observed_unhealthy",
                                                 "status_code": 503 if status == "observed_unhealthy" else 200}})
        output = present(value, data_dir=root, app=base["app"], target=base["target"], now=now + 40)
        scenarios.append({"scenario": status, **output["presentation"]["cards"][0]})
    manifest = {"synthetic": True, "purpose": "Visual fixtures only; not deployment or native-runtime evidence",
                "cards": scenarios}
    print(json.dumps(manifest, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

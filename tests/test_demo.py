"""The example executes actual protocol faults on synthetic data."""
from caprover_operator.demo import run_demo


def test_demo_drift_restart_lost_ack_and_task_evidence():
    evidence = run_demo(emit=None)
    assert evidence["real_caprover"] is False
    assert evidence["native_hermes_approval"] is False
    assert evidence["post_attempts"] == 1
    assert evidence["result"]["status"] == "verified"
    assert evidence["result"]["checks"]["runtime_rollout_proven"] is True
    by_name = {event["event"]: event for event in evidence["events"]}
    assert by_name["drift"]["post_count"] == 0
    assert by_name["old_tasks"]["receipt"]["checks"]["runtime_rollout_proven"] is False

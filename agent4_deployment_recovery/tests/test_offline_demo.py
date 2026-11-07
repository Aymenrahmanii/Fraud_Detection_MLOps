from agent4_deployment_recovery.demo import run_demo


def test_offline_demo_covers_success_rollback_restart_and_audit(tmp_path):
    report = run_demo(tmp_path)

    assert report["mode"] == "OFFLINE_SIMULATION_ONLY"
    assert report["cluster_contacted"] is False
    assert report["production_authorization_used"] is False
    scenarios = {item["scenario"]: item for item in report["scenarios"]}
    assert scenarios["successful deployment"]["final_state"] == "SUCCEEDED"
    assert scenarios["failed rollout and exact rollback"]["final_state"] == "ROLLED_BACK"
    assert scenarios["failed rollout and exact rollback"]["mutation_directions"] == [
        "FORWARD",
        "ROLLBACK",
    ]
    assert scenarios["restart reconciliation"]["final_state"] == "SUCCEEDED"
    assert scenarios["restart reconciliation"]["resumed_after_interruption"] is True
    assert scenarios["restart reconciliation"]["mutation_directions"] == ["FORWARD"]
    assert all(item["audit_chain_valid"] for item in scenarios.values())

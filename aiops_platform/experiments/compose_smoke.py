"""Distributed smoke test of `compose.platform.yaml` (standard library only).

    python aiops_platform/experiments/compose_smoke.py [--start] [--stop]

Injects the v5 memory-leak scenario through the Dashboard, waits for the
approval, approves it as the SRE operator, then waits until the incident is
RESOLVED with a valid audit chain. Exits non-zero on any failure.
"""

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request

DASHBOARD = "http://127.0.0.1:8090"
TOKEN = "dev-sre-token"
COMPOSE = ["docker", "compose", "-f", "compose.platform.yaml"]


def call(method: str, path: str, body: dict | None = None):
    request = urllib.request.Request(
        DASHBOARD + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 - fixed local URL
        return json.loads(response.read())


def wait(what: str, predicate, timeout: float, interval: float = 3.0):
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        try:
            value = predicate()
            if value:
                return value
        except (urllib.error.URLError, ConnectionError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = exc
        time.sleep(interval)
    raise SystemExit(f"timed out waiting for {what} (last error: {last_error})")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", action="store_true", help="docker compose up -d first")
    parser.add_argument("--stop", action="store_true", help="docker compose down -v at the end")
    args = parser.parse_args()
    started = time.monotonic()
    try:
        if args.start:
            subprocess.run([*COMPOSE, "up", "-d", "--build"], check=True)
        wait("the Dashboard", lambda: call("GET", "/healthz"), 300)
        wait("an operator session", lambda: call("GET", "/api/me"), 120)
        call("POST", "/api/scenarios/reset")
        call("POST", "/api/scenarios/memory_leak_after_deploy")
        print("injected: memory leak after v2.7", flush=True)

        def approval():
            rows = call("GET", "/api/approvals")
            return next((r for r in rows if r["request"]["action"] == "ROLLBACK"), None)

        pending = wait("Agent 1 detection -> RCA -> Agent 4 plan -> approval request", approval, 240)
        incident = pending["incident_id"]
        print(f"approval requested for {incident}: {pending['request']['reason']}", flush=True)
        call(
            "POST",
            f"/api/approvals/{pending['approval_id']}/resolve",
            {"decision": "APPROVE", "comment": "compose smoke test"},
        )

        def resolved():
            detail = call("GET", f"/api/incidents/{incident}")
            stage = detail["workflow"].get("stage")
            if stage in ("ESCALATED",):
                raise SystemExit(f"workflow escalated: {detail['workflow'].get('notes')}")
            return detail if stage == "RESOLVED" else None

        detail = wait("Agent 4 rollback + Agent 1 verification", resolved, 420)
        report = {
            "incident": incident,
            "stage": detail["workflow"]["stage"],
            "outcome": detail["workflow"]["outcome"],
            "root_cause": (detail["workflow"].get("consensus") or {}).get("primary_root_cause"),
            "action": (detail["workflow"].get("selected") or {}).get("type"),
            "audit_records": len(detail["audit"]),
            "audit_chain_valid": detail["audit_chain_valid"],
            "agents": sorted({r["agent_id"] for r in detail["context"]["agent_results"]}),
            "seconds": round(time.monotonic() - started, 1),
        }
        print(json.dumps(report, indent=2))
        ok = report["outcome"] == "RECOVERED" and report["audit_chain_valid"] and report["action"] == "ROLLBACK"
        return 0 if ok else 1
    finally:
        if args.stop:
            subprocess.run([*COMPOSE, "logs", "--tail", "40"], check=False)
            subprocess.run([*COMPOSE, "down", "-v"], check=False)


if __name__ == "__main__":
    sys.exit(main())

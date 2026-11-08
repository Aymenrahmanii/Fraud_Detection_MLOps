# Docker timing run: docker-timing-v1

One memory-leak incident (payment-service) through the rebuilt Docker stack, approved by the SRE token.
Recorded 2026-11-08 at commit of the verification fix (32d2150) plus the ablation and seed runners.

| Phase | Time (UTC) | Notes |
|---|---|---|
| Detected, diagnosing | 12:46:34.8 | incident open about 9 s after the fault was injected |
| Planning | 12:46:35.2 | |
| Waiting for approval | 12:46:35.3 | approved 2 s later (by the operator, in this run) |
| Executing | 12:46:37.3 | rollback applied |
| Verifying | 12:47:22.7 | rollout observation of about 45 s |
| Resolved | 12:48:07.8 | verification window of about 45 s |

Total 93.0 s from the first audit record to the last. Outcome RECOVERED, audit chain valid, 35 records.

Verification checks (monitoring agent): error_rate PASS, latency_p95_ms PASS, memory_slope_mb_min PASS,
cpu_avg_pct PASS, cpu_cost_per_request PASS. The CPU checks are active on the rebuilt containers.

Note: one run only. The detection delay here (about 9 s) is shorter than the one-minute window described
in DEMO.md, because the simulated cluster had no prior history at this point; treat it as an observation.

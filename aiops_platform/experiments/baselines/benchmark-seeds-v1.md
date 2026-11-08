# Benchmark over seeds: benchmark-seeds-v1

Recorded 2026-11-08 13:45. Seeds: 7, 11, 23, 42, 101. Each cell is median (min to max) over the seeds.

## clean-100

| Measure | Median (min to max) |
|---|---|
| resolved | 100.0 (100.0 to 100.0) |
| escalated | 0.0 (0.0 to 0.0) |
| one_change_per_resolved | 1.0 (1.0 to 1.0) |
| audit_valid | 1.0 (1.0 to 1.0) |
| rca_accuracy | 1.0 (1.0 to 1.0) |
| dead_letters | 0.0 (0.0 to 0.0) |
| throughput_per_min | 66.01 (58.39 to 68.04) |
| detect_to_decision_p50_s | 22.722 (15.964 to 27.783) |
| detect_to_decision_p95_s | 33.408 (26.66 to 42.958) |

## faults-100-5pct

| Measure | Median (min to max) |
|---|---|
| resolved | 99.0 (99.0 to 100.0) |
| escalated | 1.0 (0.0 to 1.0) |
| one_change_per_resolved | 1.0 (1.0 to 1.0) |
| audit_valid | 1.0 (1.0 to 1.0) |
| rca_accuracy | 1.0 (1.0 to 1.0) |
| dead_letters | 1.0 (0.0 to 1.0) |
| throughput_per_min | 57.7 (55.06 to 68.96) |
| detect_to_decision_p50_s | 24.309 (20.967 to 28.622) |
| detect_to_decision_p95_s | 37.315 (32.095 to 40.807) |

## faults-100-15pct

| Measure | Median (min to max) |
|---|---|
| resolved | 75.0 (69.0 to 82.0) |
| escalated | 25.0 (18.0 to 31.0) |
| one_change_per_resolved | 1.0 (1.0 to 1.0) |
| audit_valid | 1.0 (1.0 to 1.0) |
| rca_accuracy | 1.0 (1.0 to 1.0) |
| dead_letters | 25.0 (18.0 to 31.0) |
| throughput_per_min | 47.56 (43.43 to 51.92) |
| detect_to_decision_p50_s | 24.427 (22.648 to 27.753) |
| detect_to_decision_p95_s | 44.348 (43.155 to 45.24) |

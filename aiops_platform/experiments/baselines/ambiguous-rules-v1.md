# Hard cases: ambiguous-rules-v1

Recorded 2026-11-08 12:24. Deterministic rules only unless the name says otherwise.
Ground truth is fixed in advance. The operator approves every request (worst case).

## Outcomes by family

| Family | Outcomes |
|---|---|
| leak | correct: 4 |
| cpu_burn | WRONG change executed, reported RESOLVED (false recovery): 2; wrong action shown to a human (nothing executed): 2 |
| load_memory | missed (no incident): 1; wrong action shown to a human (nothing executed): 3 |
| traffic | correct: 4 |
| control | quiet (correct): 2 |

## Cases

| Family | Variant | Truth | Proposed | Executed | Outcome |
|---|---|---|---|---|---|
| leak | 8 min after deploy | ROLLBACK | ROLLBACK | ROLLBACK | correct |
| leak | 9 min after deploy | ROLLBACK | ROLLBACK | ROLLBACK | correct |
| leak | 10 min after deploy | ROLLBACK | ROLLBACK | ROLLBACK | correct |
| leak | 11 min after deploy | ROLLBACK | ROLLBACK | ROLLBACK | correct |
| cpu_burn | 260 rps | ROLLBACK | SCALE_REPLICAS | SCALE_REPLICAS | WRONG change executed, reported RESOLVED (false recovery) |
| cpu_burn | 300 rps | ROLLBACK | SCALE_REPLICAS | SCALE_REPLICAS | WRONG change executed, reported RESOLVED (false recovery) |
| cpu_burn | 340 rps | ROLLBACK | SCALE_REPLICAS | — | wrong action shown to a human (nothing executed) |
| cpu_burn | 380 rps | ROLLBACK | SCALE_REPLICAS | — | wrong action shown to a human (nothing executed) |
| load_memory | 680 rps after deploy | SCALE_REPLICAS | — | — | missed (no incident) |
| load_memory | 720 rps after deploy | SCALE_REPLICAS | ROLLBACK | — | wrong action shown to a human (nothing executed) |
| load_memory | 760 rps after deploy | SCALE_REPLICAS | ROLLBACK | — | wrong action shown to a human (nothing executed) |
| load_memory | 800 rps after deploy | SCALE_REPLICAS | ROLLBACK | — | wrong action shown to a human (nothing executed) |
| traffic | 600 rps, no deploy | SCALE_REPLICAS | SCALE_REPLICAS | SCALE_REPLICAS | correct |
| traffic | 640 rps, no deploy | SCALE_REPLICAS | SCALE_REPLICAS | SCALE_REPLICAS | correct |
| traffic | 680 rps, no deploy | SCALE_REPLICAS | SCALE_REPLICAS | SCALE_REPLICAS | correct |
| traffic | 720 rps, no deploy | SCALE_REPLICAS | SCALE_REPLICAS | SCALE_REPLICAS | correct |
| control | steady traffic, 9 min | — | — | — | quiet (correct) |
| control | healthy deploy, 9 min | — | — | — | quiet (correct) |

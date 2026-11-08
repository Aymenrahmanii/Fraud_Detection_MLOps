# Hard cases: ambiguous-rules-v2 (after the verification fix)

Recorded 2026-11-08 12:52. Rules only.
Ground truth is fixed in advance.

## Outcomes by family

| Family | rules (worst-case operator) |
|---|---|
| leak | correct: 4 |
| cpu_burn | WRONG change attempted, caught by verification and reverted: 4 |
| load_memory | missed (no incident): 1; WRONG change attempted, caught by verification and reverted: 3 |
| traffic | correct: 4 |
| control | quiet (correct): 2 |

## Review summary


## Cases

| Family | Variant | Truth | Rules: proposed / outcome |  |
|---|---|---|---|
| leak | 8 min after deploy | ROLLBACK | ROLLBACK / correct |
| leak | 9 min after deploy | ROLLBACK | ROLLBACK / correct |
| leak | 10 min after deploy | ROLLBACK | ROLLBACK / correct |
| leak | 11 min after deploy | ROLLBACK | ROLLBACK / correct |
| cpu_burn | 260 rps | ROLLBACK | SCALE_REPLICAS / WRONG change attempted, caught by verification and reverted |
| cpu_burn | 300 rps | ROLLBACK | SCALE_REPLICAS / WRONG change attempted, caught by verification and reverted |
| cpu_burn | 340 rps | ROLLBACK | SCALE_REPLICAS / WRONG change attempted, caught by verification and reverted |
| cpu_burn | 380 rps | ROLLBACK | SCALE_REPLICAS / WRONG change attempted, caught by verification and reverted |
| load_memory | 680 rps after deploy | SCALE_REPLICAS | — / missed (no incident) |
| load_memory | 720 rps after deploy | SCALE_REPLICAS | ROLLBACK / WRONG change attempted, caught by verification and reverted |
| load_memory | 760 rps after deploy | SCALE_REPLICAS | ROLLBACK / WRONG change attempted, caught by verification and reverted |
| load_memory | 800 rps after deploy | SCALE_REPLICAS | ROLLBACK / WRONG change attempted, caught by verification and reverted |
| traffic | 600 rps, no deploy | SCALE_REPLICAS | SCALE_REPLICAS / correct |
| traffic | 640 rps, no deploy | SCALE_REPLICAS | SCALE_REPLICAS / correct |
| traffic | 680 rps, no deploy | SCALE_REPLICAS | SCALE_REPLICAS / correct |
| traffic | 720 rps, no deploy | SCALE_REPLICAS | SCALE_REPLICAS / correct |
| control | steady traffic, 9 min | — | — / quiet (correct) |
| control | healthy deploy, 9 min | — | — / quiet (correct) |

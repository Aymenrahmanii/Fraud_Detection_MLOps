# Hard cases: ambiguous-groq-120b-v1

Recorded 2026-11-08 12:32. Agent 2 review: `openai/gpt-oss-120b`.
Ground truth is fixed in advance.

## Outcomes by family

| Family | rules (worst-case operator) | review, worst operator | review, informed operator |
|---|---|---|---|
| leak | correct: 4 | correct: 4 | correct: 4 |
| cpu_burn | WRONG change executed, reported RESOLVED (false recovery): 2; wrong action shown to a human (nothing executed): 2 | WRONG change executed, reported RESOLVED (false recovery): 2; wrong action shown to a human (nothing executed): 2 | WRONG change executed, reported RESOLVED (false recovery): 2; wrong action shown to a human (nothing executed): 2 |
| load_memory | missed (no incident): 1; wrong action shown to a human (nothing executed): 3 | missed (no incident): 1; wrong action shown to a human (nothing executed): 3 | missed (no incident): 1; wrong action shown to a human (nothing executed): 3 |
| traffic | correct: 4 | correct: 4 | correct: 4 |
| control | quiet (correct): 2 | quiet (correct): 2 | quiet (correct): 2 |

## Review summary

- worst operator: 15 reviews, 1 answered, 0 challenged, 14 unavailable, 0 invalid.
- informed operator: 15 reviews, 0 answered, 0 challenged, 15 unavailable, 0 invalid.

## Cases

| Family | Variant | Truth | Rules: proposed / outcome | Review (worst): answer / outcome | Review (informed): answer / outcome |
|---|---|---|---|---|---|
| leak | 8 min after deploy | ROLLBACK | ROLLBACK / correct | UNAVAILABLE / correct | UNAVAILABLE / correct |
| leak | 9 min after deploy | ROLLBACK | ROLLBACK / correct | UNAVAILABLE / correct | UNAVAILABLE / correct |
| leak | 10 min after deploy | ROLLBACK | ROLLBACK / correct | MEMORY_LEAK_POST_DEPLOY / correct | UNAVAILABLE / correct |
| leak | 11 min after deploy | ROLLBACK | ROLLBACK / correct | UNAVAILABLE / correct | UNAVAILABLE / correct |
| cpu_burn | 260 rps | ROLLBACK | SCALE_REPLICAS / WRONG change executed, reported RESOLVED (false recovery) | UNAVAILABLE / WRONG change executed, reported RESOLVED (false recovery) | UNAVAILABLE / WRONG change executed, reported RESOLVED (false recovery) |
| cpu_burn | 300 rps | ROLLBACK | SCALE_REPLICAS / WRONG change executed, reported RESOLVED (false recovery) | UNAVAILABLE / WRONG change executed, reported RESOLVED (false recovery) | UNAVAILABLE / WRONG change executed, reported RESOLVED (false recovery) |
| cpu_burn | 340 rps | ROLLBACK | SCALE_REPLICAS / wrong action shown to a human (nothing executed) | UNAVAILABLE / wrong action shown to a human (nothing executed) | UNAVAILABLE / wrong action shown to a human (nothing executed) |
| cpu_burn | 380 rps | ROLLBACK | SCALE_REPLICAS / wrong action shown to a human (nothing executed) | UNAVAILABLE / wrong action shown to a human (nothing executed) | UNAVAILABLE / wrong action shown to a human (nothing executed) |
| load_memory | 680 rps after deploy | SCALE_REPLICAS | — / missed (no incident) | OFF / missed (no incident) | OFF / missed (no incident) |
| load_memory | 720 rps after deploy | SCALE_REPLICAS | ROLLBACK / wrong action shown to a human (nothing executed) | UNAVAILABLE / wrong action shown to a human (nothing executed) | UNAVAILABLE / wrong action shown to a human (nothing executed) |
| load_memory | 760 rps after deploy | SCALE_REPLICAS | ROLLBACK / wrong action shown to a human (nothing executed) | UNAVAILABLE / wrong action shown to a human (nothing executed) | UNAVAILABLE / wrong action shown to a human (nothing executed) |
| load_memory | 800 rps after deploy | SCALE_REPLICAS | ROLLBACK / wrong action shown to a human (nothing executed) | UNAVAILABLE / wrong action shown to a human (nothing executed) | UNAVAILABLE / wrong action shown to a human (nothing executed) |
| traffic | 600 rps, no deploy | SCALE_REPLICAS | SCALE_REPLICAS / correct | UNAVAILABLE / correct | UNAVAILABLE / correct |
| traffic | 640 rps, no deploy | SCALE_REPLICAS | SCALE_REPLICAS / correct | UNAVAILABLE / correct | UNAVAILABLE / correct |
| traffic | 680 rps, no deploy | SCALE_REPLICAS | SCALE_REPLICAS / correct | UNAVAILABLE / correct | UNAVAILABLE / correct |
| traffic | 720 rps, no deploy | SCALE_REPLICAS | SCALE_REPLICAS / correct | UNAVAILABLE / correct | UNAVAILABLE / correct |
| control | steady traffic, 9 min | — | — / quiet (correct) | OFF / quiet (correct) | OFF / quiet (correct) |
| control | healthy deploy, 9 min | — | — / quiet (correct) | OFF / quiet (correct) | OFF / quiet (correct) |

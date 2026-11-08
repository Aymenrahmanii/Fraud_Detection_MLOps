# Ablation: ablation-v1

Recorded 2026-11-08 13:23. Rules only, 18 hard cases, worst-case operator.

| Measure | full | no-cpu-check | no-human-gate | no-gates |
|---|---|---|---|---|
| correct | 8 | 8 | 0 | 0 |
| wrong change applied and verified as recovered (false recovery) | 0 | 2 | 0 | 0 |
| wrong change applied, not verified | 0 | 0 | 0 | 0 |
| wrong change applied, caught by verification and reverted | 7 | 5 | 0 | 0 |
| refused by Agent 4 (nothing applied) | 0 | 0 | 15 | 15 |
| wrong action shown to a human | 0 | 0 | 0 | 0 |
| control false alarms | 0 | 0 | 0 | 0 |
| approvals requested | 15 | 15 | 0 | 0 |

Reading the table: removing the CPU check lets wrong changes be verified as recovered. Removing the
platform's policy gate alone changes nothing here: Agent 4 refuses automated production actions,
because they need a signed human permit (its provenance check, independent of the policy). The human
gate's own protection depends on the operator's judgement, which a simulation cannot measure: the
worst-case operator approves everything.

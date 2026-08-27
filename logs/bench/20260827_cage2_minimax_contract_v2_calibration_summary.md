# CAGE-2 MiniMax post-contract resource calibration

Status: **resource-only h30 subset complete; publication_v2 proposed, not frozen**.

This artifact uses clean source `2a0c7063b5c4528213b95d061c7f5d41b3e61c68`,
`openai/MiniMax-M3`, temperature 0, thinking disabled, and segmented protocol
`cage2_segmented_v1`. Budget selection did not read or use reward fields.

The final-contract sample contains one paired B_lineAgent/h30 episode for each of
Single, Orchestrator-only, and Full: 90 environment steps in 9 committed 10-step
segments. The user requested stopping after this episode group, so final-contract
h50/h100 resource-tail coverage was not run. Earlier 30/50/100 diagnostics exposed
the residual structured-contract bug and are not used to freeze the ceiling.

| Arm | provider tokens p50/p90/p95/p99/max | calls p50/p90/p95/p99/max | tools max | dispatch max | wall p50/p90/p95/p99/max (s) |
| --- | --- | --- | ---: | ---: | --- |
| Single | 4387 / 4468 / 4523 / 4534 / 4534 | 1 / 1 / 1 / 1 / 1 | 1 | 0 | 2.7976 / 4.3454 / 5.4712 / 5.7109 / 5.7109 |
| Orchestrator-only | 4448 / 4519 / 4532 / 4572 / 4572 | 1 / 1 / 1 / 1 / 1 | 1 | 0 | 2.8490 / 3.8826 / 5.5224 / 6.6933 / 6.6933 |
| Full | 4518 / 9826 / 10978 / 12000 / 12000 | 1 / 3 / 3 / 3 / 3 | 1 | 1 | 3.4149 / 13.2048 / 20.1642 / 27.6829 / 27.6829 |

Full naturally dispatched seven specialists (watcher 6, analyst 1). Mechanical
trace counts after the final contract fix are: DispatchNotAllowed 0, InvalidRole 0,
ToolNotAvailable 0. The maximum observed result-segment duration was 64.675 seconds,
well below the ten-minute target.

The numeric publication_v2 proposal retains publication_v1's equal per-step limits:
7 runtime steps, 5 LLM calls, 4 tools, 3 dispatches, 4 role steps, 24,576 accounted
tokens, and 90 seconds. This is at least 2.048x the observed token maximum and 3.25x
the observed wall-time maximum. It is **not frozen** because h50/h100 tails under the
final action contract are missing. No publication_v2 performance run may start until
that resource-only coverage is completed and the profile is committed.

Historical error comparison: the immutable publication_v1 Full runs recorded 197
and 195 dispatch errors (mostly DispatchNotAllowed). The first partial fix reduced a
540-step diagnostic to 3 DispatchNotAllowed and 1 InvalidRole, which exposed that the
JSON example still advertised role/mission fields. After removing those fields, the
final 30-step Full calibration recorded zero contract errors.

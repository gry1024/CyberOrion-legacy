# CAGE-2 publication_v1 paired summary (MiniMax-M3)

- Six complete raw artifacts: two executions (first and repeat1) of the exact same 27-episode seed manifest; repeat1 is sensitivity, not additional independent seeds.
- Source HEAD `1e65eae28bbb100409787b5c21fcd31981ffd1ea`, clean; model `openai/MiniMax-M3`, temperature=0, thinking disabled.
- publication_v1 per-step budget: 24,576 accounted tokens / 5 LLM calls / 4 tools / 3 dispatches / 7 runtime steps / 4 role steps / 90s.
- Reaching a documented limit with fallback is allowed by the repository validity contract; exceeding it is invalid. All six pass hard-limit validity, but Full exhaustion is reported explicitly.

## Raw results

| replicate | arm | mean reward | elapsed s | fallback | exhaustion | dispatches | provider tokens | validity |
|---|---|---:|---:|---:|---:|---:|---:|---|
| first | single | -211.1926 | 8633.19 | 0 | 0 | 0 | 6916236 | True |
| first | orchestrator_only | -227.4000 | 9387.12 | 0 | 0 | 0 | 6926975 | True |
| first | full | -282.2852 | 13426.91 | 38 | 37 | 298 | 8835125 | True |
| repeat1 | single | -283.0481 | 8401.95 | 0 | 0 | 0 | 6880102 | True |
| repeat1 | orchestrator_only | -246.2074 | 8656.41 | 0 | 0 | 0 | 6924030 | True |
| repeat1 | full | -227.6481 | 13362.81 | 36 | 36 | 305 | 8835551 | True |

## Paired effects (matched condition + episode)

| replicate | Full−Single | Full−Orchestrator-only | Orchestrator-only−Single |
|---|---:|---:|---:|
| first | -71.0926 [-152.3815, 12.9000] | -54.8852 [-123.5185, 4.5444] | -16.2074 [-100.6963, 72.7778] |
| repeat1 | 55.4000 [-34.6963, 146.9111] | 18.5593 [-54.5444, 83.5852] | 36.8407 [-42.5148, 137.2148] |

## Horizon effects (first / repeat1 mean paired delta)

| comparison | 30 | 50 | 100 |
|---|---:|---:|---:|
| Full−Single | -23.06/22.49 | -31.87/107.71 | -158.36/36.00 |
| Full−Orchestrator-only | -9.13/36.26 | 18.32/98.81 | -173.84/-79.39 |
| Orchestrator-only−Single | -13.92/-13.77 | -50.19/8.90 | 15.49/115.39 |

## Audit

- All six: done, n=27, exact clean source/model/settings/budget, 1,620 traces and 27 episode resources.
- All six: no evaluator/reward keys in model-visible memory; all 100-step episodes have 100 actions and valid final-10 actions; no permanent early Sleep episode.
- Single/Orchestrator-only: zero fallback/exhaustion. Full: first 38/37 and repeat1 36/36 fallback/exhaustion (2.35%/2.22% of 1,620 steps); no over-limit violation.
- Full dispatch role totals and detailed trace diagnostics are in the JSON. Do not tune prompts or budget from these scores.
- Paired CIs bootstrap matched episode deltas within each 27-episode run. Repeated runs reuse the same seeds, so report replicate means/ranges rather than pooled n=54 CI.

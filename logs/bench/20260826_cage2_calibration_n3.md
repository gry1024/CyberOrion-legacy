# CAGE-2 per-step budget diagnostic calibration

This calibration is diagnostic evidence only, not a performance comparison or
publication result. It ran one real 30-step episode for each of
`B_lineAgent`, `RedMeanderAgent`, and `SleepAgent`, separately for Single and
Full CyberOrion.

- Source SHA: `04592f98b20c9503eddacb46afd32d9b18231087`
- Source worktree: clean (`git_dirty=false` in both raw runs)
- Model: `openai/deepseek-v4-flash`
- Decoding: temperature `0`, max output tokens `8192`, thinking disabled
- Diagnostic per-step ceilings: 10 LLM calls, 8 tools, 4 dispatches,
  32768 tokens, 300 seconds
- Outcome integrity: each arm has 3 episodes / 90 decision traces; all 90
  steps selected valid non-Sleep actions; no fallback or exhaustion occurred
- Provider token usage: available on all 180 steps
- Full dispatch: 3 dispatches on 3/90 steps, all to `watcher`
- Memory: step 1 through 30 observed; no serialized memory contains `reward`

## Per-step resource distribution

| Arm | Metric | p50 | p90 | p95 | max |
|---|---|---:|---:|---:|---:|
| Single | provider total tokens | 4998.5 | 5191.3 | 5201.65 | 5287 |
| Single | estimated tokens | 3246.5 | 3369.0 | 3386.1 | 3438 |
| Single | LLM calls | 1 | 1 | 1 | 1 |
| Single | tool calls | 1 | 1 | 1 | 1 |
| Single | wall seconds | 1.878 | 2.479 | 2.646 | 2.849 |
| Full | provider total tokens | 5119.5 | 5292.4 | 5354.4 | 11933 |
| Full | estimated tokens | 3360.5 | 3479.1 | 3564.15 | 8031 |
| Full | LLM calls | 1 | 1 | 1 | 3 |
| Full | tool calls | 1 | 1 | 1 | 1 |
| Full | wall seconds | 1.965 | 2.558 | 3.045 | 9.148 |

## Frozen pilot ceiling decision

The pilot profile is frozen before running or inspecting any pilot result:

- token budget: 16384 per step (37% over the observed provider-token maximum,
  and more than 2x the estimated-token maximum);
- max LLM calls: 4 (one repair call above the observed maximum of 3);
- max tool calls: 3 (three times the observed maximum);
- max dispatches: 2 (twice the observed maximum);
- max runtime steps: 6; max role steps: 4;
- wall time: 60 seconds (over 6.5x the observed maximum).

These values are a resource ceiling, not a target. They must not be changed in
response to which architecture wins the pilot.

## Artifact hashes

- `20260826_cage2_calibration_single_n3.json`:
  `fea96db58fb179f2f697e06c27bda24d1e1dd95e76b51905e53fc23398c12620`
- `20260826_cage2_calibration_single_n3.sample.json`:
  `e393dacc544dbef46836577af6dae16df32b0584c4199d6157442127aa520955`
- `20260826_cage2_calibration_agent_n3.json`:
  `1811c0f4b101d92a2c67dda07d75d5750f96319c96dea0a656fac9e02f680617`
- `20260826_cage2_calibration_agent_n3.sample.json`:
  `e343d1e2c0be77319734a5624783b388deea127e225d6f69f24bbc8f33dcab9a`

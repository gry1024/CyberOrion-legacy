# ExCyTIn / SABER Resource-Limit Correctness Fix

## Verdict

PASS. The pinned SABER solver now enters its one-shot, tool-free final-answer
fallback only for Inspect's actual `tool_call` limit. Every other
`LimitExceededError` propagates unchanged. No ExCyTIn arm, scorer, task
environment, or performance experiment was run.

## Exact pins

- ACESEvals checkout: `17135140d0fdf52c2264a1fc248cf01e16b23a79`
- ACESEvals tree: `4d5dea0db0073189210d5cdf1d56eab86780ce0e`
- Inspect package version: `0.1.dev5935+gf94a85b6b`
- Inspect commit: `f94a85b6b3a246d2e4417b49bdda96fd8f04b93a`
- SABER package version: `0.2.0`
- SABER commit: `a9bdce1343fd1c331aafda3119cbf0d48f215382`

On 2026-08-28, `refs/heads/main` of `https://github.com/microsoft/ACES.git`
was still exactly `a9bdce1343fd1c331aafda3119cbf0d48f215382`.
There was therefore no compatible newer upstream commit containing an
equivalent fix. This runtime is accurately described as:

> upstream SABER + resource-limit correctness patch

It is not byte-identical to upstream SABER.

## Deterministic Inspect limit identities

The offline reproduction is `scripts/repro_saber_resource_limits.py` and uses
the pinned Inspect implementation's real limit contexts and accounting calls.

| Field | Tool-call limit | Token limit |
| --- | --- | --- |
| exception class | `inspect_ai.util._limit.LimitExceededError` | `inspect_ai.util._limit.LimitExceededError` |
| `exc.type` | `tool_call` | `token` |
| `exc.value` | `2` | `11` |
| `exc.limit` | `1` | `10` |
| `exc.message` | `Tool call limit exceeded. value: 2; limit: 1` | `Token limit exceeded. value: 11; limit: 10` |
| source class | `inspect_ai.util._limit._ToolCallLimit` | `inspect_ai.util._limit._TokenLimit` |

The stable, explicit discriminator is `exc.type`; no message matching is used.

## Bug and minimal fix

Pinned SABER previously caught every `LimitExceededError` and always called
`get_model().generate(..., tools=[])`. Consequently token, time, working-time,
cost, message, operator, custom, and other hard limits could incorrectly cause
additional model work after termination.

The patch changes only the exception handler:

```python
except LimitExceededError as exc:
    if exc.type != "tool_call":
        raise
    # existing one-shot tool-free fallback follows unchanged
```

Unknown types fail closed because every value other than the exact
`tool_call` discriminator is re-raised. An exception raised by the permitted
fallback is outside the protected `try` body and therefore propagates without
recursive fallback.

Patch artifact:

- `benchmarks/patches/saber-resource-limit-correctness.patch`
- patch SHA-256:
  `93d847dfa0c3f0976f412ae74652df139c5415351e395b9a9031e08db8d8fe6f`
- reconstructed upstream solver SHA-256:
  `8af5f648d01496b2b1844dcd5841078d82238736586cbf6e5befef15646cf024`
- patched solver SHA-256:
  `53fce535f3e58d07526348a133b5180c903e5e2b5bdb150dae5edfbb9edb8894`

## Regression evidence

Command:

```text
benchmarks/external/excytin/.venv/bin/python -m pytest \
  tests/test_saber_resource_limits.py -q
```

Result: `10 passed`.

The tests prove:

- attempted tool call 2/1 is checked before execution;
- only the first tool action executes;
- the existing final-answer fallback runs exactly once with `tools=[]`;
- token, time, working, cost, message, operator, custom, and a future unknown
  limit do not invoke fallback;
- the future unknown type is propagated and rejected by pinned Inspect's
  closed persisted-limit schema;
- a hard limit raised by the one permitted fallback propagates after exactly
  that one attempted generation, with no recursive fallback.

## Minimal runtime reproduction

An offline mock model completed one call with 11 accounted tokens under a
10-token hard limit. The relevant event sequence was:

```text
event[5] ModelEvent
event[6] SampleLimitEvent(type="token", limit=10)
termination
```

Observed counters:

- total model callback calls: `1` (the initial completed call);
- post-limit `ModelEvent` count: `0`;
- persisted sample limit: `token`, limit `10`;
- sample error: none (Inspect represents the propagated hard limit as
  `sample.limit`).

Raw offline `.eval` evidence is preserved under:

`/tmp/cyberorion_cage_runs/excytin_limit_fix_20260828/`

## Scope

No changes were made in this patch to ExCyTIn bridge semantics, commander
tools, role prompts, delegation policy, shared state, official tasks/tools,
scoring, or benchmark resource settings. The earlier uncommitted architecture
work remains separate and untouched.

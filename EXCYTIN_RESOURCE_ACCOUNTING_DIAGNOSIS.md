# ExCyTIn Resource-Accounting Diagnosis

Date: 2026-08-28<br>
Verdict: **A correctness bug exists; STOP**

This diagnosis uses only native Inspect/SABER events and pinned source. It does
not inspect scorer output, reward, or answer quality.

## 26/25 tool usage

Classification: **B — the over-limit attempt was rejected before execution.**

For `full_attempt2 / incident_38_latest_cleaned_test_set_task_2`:

- native ToolEvents: 25, all successful `bash` calls;
- event 124: a completed model response requested further tool work;
- event 125: `SampleLimitEvent(type="tool_call")` with
  `value: 26; limit: 25`;
- no 26th ToolEvent exists.

Pinned Inspect `_execute_tools_impl` calls `record_tool_call_usage(count)` and
then `check_tool_call_limit()` before constructing/executing tool tasks. Thus
the usage value includes the rejected request, while environment work did not
execute beyond the ceiling. The ceiling was binding, but this event alone is
not an execution-accounting bug.

## 1,006,504/1,000,000 token usage

The first overage is **B**, but SABER's subsequent fallback is **A**.

For `full_attempt3 / incident_39_latest_cleaned_test_set_task_92`:

1. event 235 is a completed model response;
2. Inspect records provider usage after the response and emits event 236:
   `Token limit exceeded. value: 1,006,504; limit: 1,000,000`;
3. this initial overshoot cannot be rejected in advance because exact provider
   token usage is known only after the call completes;
4. SABER `agents/solver_factory.py::create_saber_solver` catches every
   `LimitExceededError`, logs it as a tool-call limit, and unconditionally calls
   `get_model().generate(..., tools=[])` for a final answer;
5. event 237 is that additional completed ModelEvent with 2,084 tokens;
6. event 238 records a second token-limit violation at 1,008,588 tokens.

The extra event-237 model request happened after the hard token ceiling had
already fired. This is execution beyond a hard limit, not delayed accounting.
It satisfies the task's category A and is a correctness blocker.

The misleading SABER log line `Tool call limit exceeded for agent ...` is
caused by the same broad exception handler. The native SampleLimitEvent type is
authoritative: this was a token limit.

## Consequence

No further bridge semantic changes, scripted recovery run, real-model smoke,
other arm, scorer, or performance run is permitted in this continuation. The
pinned SABER limit handler requires review so that only a true tool-call limit
may receive the tool-free final-answer fallback; token/time/cost/working limits
must not trigger additional model work.

All prior evidence remains preserved under
`/tmp/cyberorion_cage_runs/excytin_architecture_validity_20260828/`.

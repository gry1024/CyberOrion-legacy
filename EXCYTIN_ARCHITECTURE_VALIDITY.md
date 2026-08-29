# ExCyTIn Architecture Validity

Final verdict: **FAIL**. No performance experiment was started, and no
publication budget was frozen.

This verdict uses mechanism and resource evidence only. All real-model smoke
runs used the official ACESEvals Task, SABER Docker/MySQL environment, native
Inspect tools, and `openai/MiniMax-M3` with temperature 0 and thinking disabled.
The official scorer stayed attached to the Task but was not executed
(`score=false`), and no reward or answer quality was inspected.

## Provenance

- CyberOrion HEAD/tree: `60438fbd0ea7dbdba71a0da13241e11c8af18a37` / `eb89fc8071b2794a81ea6154a515dc2a304fb78c`
- Runtime dirty diff SHA-256: `1afbc876a7f99b04d6978a326642d2426ecd288eaa5bcda9dac6ab663553ab7e`
- Final bridge SHA-256: `061b304e9d77c7997d3d8abe4593d602b702cd2cb83f9618907844598014fed9`
- ACESEvals HEAD/tree: `17135140d0fdf52c2264a1fc248cf01e16b23a79` / `4d5dea0db0073189210d5cdf1d56eab86780ce0e`
- SABER pin: `a9bdce1343fd1c331aafda3119cbf0d48f215382`
- Inspect pin: `f94a85b6b3a246d2e4417b49bdda96fd8f04b93a`
- Upstream tracked task/tool/scorer/config changes: none

## Final bridge architecture

The official ExCyTIn bridge no longer imports `superagent_runtime` and no
longer converts Inspect tools to `ToolSpec` or runs a parallel JSON action
protocol. All three arms use native Inspect `react` and receive the official
Tool objects unchanged.

- Single is a strong monolithic investigator with schema-discovery,
  correlation, hypothesis-testing, evidence, and verification SOP.
- Orchestrator-only uses the same commander investigation interface and full
  official environment-tool union, with dispatch disabled.
- Full adds only four coordination tools: `delegate_watcher`,
  `delegate_analyst`, `delegate_hunter`, and `delegate_responder`.
- A dispatched specialist receives the official task context, bounded mission,
  current shared notebook, prior visible evidence, official prompts, and every
  official environment tool.
- Specialists run isolated native conversations and must submit deterministic
  structured reports. Reports are classified as successful, empty, parse
  failure, role-budget exhaustion, or tool failure.
- The shared notebook contains only model-visible task/evidence data and
  provenance. It contains no target, gold, scorer output, judge prompt, hidden
  database object, or sandbox state.
- Multiple dispatch tools are marked parallel-safe. A concurrency-safe global
  model-call gate and Inspect sample-root token/tool/time limits account for
  child use globally.

Removed custom layers: Inspect Tool-to-`ToolSpec` conversion, handler-direct
execution, custom tool/action JSON parser, reserialized official task prompt,
and fixed 4,000/12,000-character bridge clipping. Retained layers:
arm registration, role prompts, native coordination tools, explicit shared
state, structured reports, and audit/accounting metadata.

## A–M gate

| Item | Result | Evidence |
| --- | --- | --- |
| A. Official task/scorer/tools unchanged | PASS | Pinned upstream tracked diff is empty |
| B. No scorer/gold leakage | PASS | Static AST guard and context audit |
| C. Single complete official tools | PASS | Mechanical native tool contract |
| D. Full team union equals Single | PASS | Same official Tool objects for every role |
| E. Commander sees four roles/dispatch | PASS | Prompt and exact native tool schemas tested |
| F. Child receives context/mission/state/tools | PASS | Native mechanism probe |
| G. Child executes real official tool | PASS (offline) | Actual Inspect `bash` implementation |
| H. Commander receives report and continues | PASS (offline) | Three intact reports before final submit |
| I. Independent/parallel specialists | PASS (offline) | watcher/analyst execution overlap |
| J. Child usage counted globally | PASS (offline) | 9 model calls, 10 root tool calls, 1,350 root tokens |
| K. No truncation/budget binding | **FAIL** | 16 KiB tool truncation and 1M-token limit hit |
| L. Contract/parser errors zero | PASS | No malformed/unknown tools or report parser errors |
| M. No extra hidden information | PASS | Shared-state schema and AST guard |

The offline native probe proves that the bridge can execute a real Inspect
tool, propagate shared evidence, return structured reports, run two children
in parallel, and charge child usage to the root sample. It does not substitute
for the required real-model team-behavior gate.

## Real-model mechanism evidence

Three fresh manifests were frozen before their first model call. Nine Full
samples across distinct incidents completed; Single and Orchestrator-only were
not run because Full failed the early qualitative gate.

| Attempt | Manifest SHA | Samples | Natural dispatches | Key resource result |
| --- | --- | ---: | ---: | --- |
| 1 | `6bf4a923…9590c` | 3/3 | 0 | 33 official tool calls; two outputs truncated |
| 2 | `84d47cc…5b21c` | 3/3 | 0 | root tool limit bound at 26/25; five outputs truncated |
| 3 | `8e741da6…8b510` | 3/3 | 0 | token limit bound at 1,006,504/1,000,000; three outputs truncated |

Raw assistant requests and native ToolEvents prove this was not a swallowed or
failed delegation: across all nine samples there were zero `delegate_*`
requests, zero unknown tool names, and zero ToolEvent errors. The limit-hit
samples used only `bash`. Thus Full behaved as a monolithic commander despite
the role roster and valid dispatch implementation.

The apparent repeated 16,515-character messages are confirmed native Inspect
truncations: ToolEvents report original outputs of 23,127–251,888 bytes clipped
to the default 16,384-byte maximum. Eight of nine real samples experienced at
least one such truncation. This directly fails the requested no-binding-
truncation condition.

## Traces requested before a large run

- Real specialist investigation trace: **unavailable**, because the real model
  never dispatched a specialist. Supplying the offline mock trace as if it were
  a real-model trace would be misleading.
- Multi-dispatch trace: available only in the deterministic native mechanism
  probe (watcher and analyst parallel, then hunter after shared evidence).
- Contract error count: 0.
- Proposed frozen performance budget: **none**. Resource evidence is censored,
  so selecting a publication budget would violate the calibration rule.

## Test status and stop

- ExCyTIn architecture + external benchmark tests: **54 passed**.
- `git diff --check`: **passed**.
- Full repository suite: **600 passed, 1 skipped, 10 failed**. The failures are
  outside this bridge: five require missing sibling `cai-latest` files, and the
  remainder are existing server/docs/reportlab/scenario assertions. They were
  preserved and not patched as part of this benchmark work.

`EXCYTIN_ARCHITECTURE_VALIDITY = FAIL`. All raw logs, manifests, stdout, exit
codes, provenance, and intermediate evidence are preserved under
`/tmp/cyberorion_cage_runs/excytin_architecture_validity_20260828/`. Execution
stops here before Single/Orchestrator-only calibration and before any expensive
performance run.

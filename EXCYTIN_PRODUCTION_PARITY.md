# ExCyTIn Production-Parity Audit

Date: 2026-08-28<br>
CyberOrion HEAD/tree: `60438fbd0ea7dbdba71a0da13241e11c8af18a37` / `eb89fc8071b2794a81ea6154a515dc2a304fb78c`<br>
Verdict before mechanism recovery: **FAIL**

## Which implementation is current

The repository contains three generations that must not be conflated:

1. `cyberorion/agents/blue_team.py` is the legacy watcher/analyst/responder/
   hunter implementation used by `core/controller.py`. It is not the blue path
   selected by the current `server.py` controller.
2. The repository-contained production arena path is
   `server.py -> ControllerV2 -> agents/v2/blue_orchestrator.py`. This path is
   executable and fully auditable in the current worktree.
3. The user-facing `/ws/cai` terminal defaults to the newer external
   `cyberorion_agent`. Its documented and tested contract is one
   `dispatch_agent` tool that selects existing CAI specialist Agents. The
   sibling `cai-latest/src/cai/agents/cyberorion_agent.py` source is absent in
   this checkout, so implementation details beyond the repository's contract
   tests and `docs/FRAMEWORK.md` cannot be claimed as directly inspected.

ExCyTIn is a blue-team active-investigation benchmark. The closest fully
inspectable production topology is therefore the current V2 blue orchestrator.
The external top-level CyberOrion contract independently confirms its central
primitive: the commander delegates; it does not copy specialist execution
tools onto itself.

## Current V2 production topology

### Commander responsibilities

The V2 commander owns the investigation lifecycle: read the current alert and
investigation summary, route bounded work, inspect worker-written state,
schedule follow-up work, and submit the final investigation summary. Its prompt
explicitly says that it does not directly investigate.

Exact commander tools:

- state queries: `get_alerts`, `get_investigation_summary`
- delegation: `dispatch_triage`, `dispatch_threat_hunter`,
  `dispatch_lateral_analyst`, `dispatch_escalation`
- finalization: `complete_investigation`, `task_complete`
- control callbacks: `request_assistance`, `end_turn`

It does not receive `query_logs`, detection, process, file, network, response,
shell, or Python execution tools.

### Worker types, prompts, and tools

- TRIAGE: initial alert assessment, severity routing, first-pass IoCs, and data
  source discovery. Production tools include log/alert query, one detection
  query, ATT&CK lookup, evidence/timeline writes, and finding submission.
- THREAT_HUNTER: deep investigation, detection templates, time-window
  correlation, process/network/file inspection, ATT&CK mapping, evidence, and
  attack-chain reconstruction.
- LATERAL_ANALYST: cross-host authentication/network/process correlation and
  lateral-movement graph/range tracking.
- ESCALATION: high/critical review, cross-investigation correlation, escalation
  decisions, and evidence-backed response recommendations/actions.

Every worker has an isolated agent loop, a role prompt, role-scoped tools, and
callback tools. The orchestrator's dispatch handler automatically builds the
worker prompt/tool set, renders the task with an `OpState` snapshot, executes
the worker, writes a timeline event, and returns structured outcome fields to
the commander.

### Shared workspace and communication

Production uses two explicit shared-state mechanisms:

- concurrency-safe `OpState` snapshots and timeline;
- the blue investigation ledger for evidence, techniques, hosts, and alerts.

Workers write findings/evidence into this state. The commander reads bounded
summaries through `get_*` tools rather than receiving every raw tool transcript.
Worker outcomes return through the dispatch tool result. There is no scorer or
ground-truth channel.

### Parallelism and final ownership

`core/agent_loop.py` executes multiple external tool calls from one model turn
with `asyncio.gather`. Multiple independent dispatch calls can therefore run in
parallel. Parallelism is available but not forced.

The commander owns final synthesis: workers submit investigation findings;
the commander calls `complete_investigation` and then `task_complete`.

## External top-level CyberOrion contract

`server.py` defaults `/ws/cai` to `CAI_AGENT_TYPE=cyberorion_agent`.
`docs/FRAMEWORK.md` and `tests/test_cyberorion_contracts.py` require exactly one
commander tool, `dispatch_agent(task, context, preferred_agent, phase)`. It
dynamically selects Knowledge Agent or an existing CAI specialist, while the
specialist retains its own tools. CyberOrion owns evidence aggregation and the
answer; Report Agent is a separate final system action. This reinforces, rather
than contradicts, the V2 rule that environment execution belongs to workers.

## Required ExCyTIn parity topology

The benchmark adaptation should preserve the V2 blue primitives while using
only official ExCyTIn environment truth:

- Full commander environment tools: **none** (`bash`/`python` removed).
- Full commander coordination tools: bounded workspace summary, four V2
  dispatch tools, and official final submit.
- Worker identities: TRIAGE, THREAT_HUNTER, LATERAL_ANALYST, ESCALATION.
- Every worker receives the official task, mission, bounded shared-state
  snapshot, relevant compact prior evidence, and official `bash`/`python` tools.
- Full environment-tool union: official `bash` + `python`.
- Single environment-tool union: the same official `bash` + `python` directly.
- Orchestrator-only, when later authorized: the same official union directly,
  with the commander planning prompt and dispatch disabled.
- Multiple independent dispatch calls remain parallel-safe.
- Full commander remains the sole owner of the official final answer.

Role adaptation must remove unavailable production concepts such as active
alert mutation or host isolation. ESCALATION becomes evidence review and
response-oriented recommendation only; it must not invent environment actions.
This changes surface tools, not role ownership or organization.

## Historical ExCyTIn parity gaps (before mechanism recovery)

- The bridge at that time gave official `bash`/`python` directly to Full commander.
- It uses legacy watcher/analyst/hunter/responder names and semantics.
- Its shared state stores full raw official outputs, amplifying 16 KiB
  truncation and context growth instead of exposing a bounded summary.
- Four role-specific `delegate_*` tools do not match the current V2 dispatch
  API or the external single-dispatch contract.

Therefore that bridge did not reproduce current production CyberOrion. The
mechanism-recovery work below corrected these topology gaps before another
real-model call.

## 2026-08-29 resource-delegation protocol addendum

The native bridge now preserves the topology above and adds explicit,
auditable resource delegation without changing official ExCyTIn tools, task,
database, or scorer:

- Single and Orchestrator-only receive the complete official investigation-tool
  union directly. Full commander receives only coordination and final-submit
  tools; Full workers receive the same official investigation-tool union.
- Every root-agent model request gets an ephemeral global balance containing
  provider tokens, root tool calls, model calls, wall/working time, messages,
  and cost when Inspect exposes them. In Full, only the commander sees this
  global balance.
- Every Full dispatch requires a positive worker-local token, tool-call,
  model-call, and wall-time allocation. The allocation is a nested hard limit,
  not additional compute, and all child usage remains counted globally.
- Full atomically reserves additive worker allocations under a lock and protects
  a fixed commander finishing reserve. Independent dispatch tools remain
  parallel-capable under the bounded semaphore; unsafe over-allocation is
  rejected before a worker starts.
- Workers receive only their local balance on every model request. A worker that
  exhausts it stops with `role_budget_exhaustion`; the bridge preserves any
  native tool evidence but does not synthesize a report the worker failed to
  submit.
- TRIAGE, THREAT_HUNTER, LATERAL_ANALYST, and ESCALATION have non-overlapping
  mission contracts covering routing, targeted hypothesis testing, cross-entity
  spread, and adversarial claim verification respectively. The Full commander
  retains final answer ownership.

The offline native-Inspect mechanism probe covers dynamic balance visibility,
real nested limits, report return, local exhaustion without forced reporting,
pre-start rejection of unsafe allocations, and overlapping independent worker
execution. This addendum describes a new protocol version and does not make old
performance samples comparable with future runs.

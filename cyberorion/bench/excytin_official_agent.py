"""CyberOrion agents for the official ACESEvals/SABER ExCyTIn task.

SABER supplies the rendered official prompts and native Inspect tools. This
bridge adds only arm-specific investigation instructions, delegation, shared
evidence state, and audit metadata. It never reads scorer, target, or sandbox
state and never reimplements the native model/tool loop.

Inspect/SABER imports stay inside runtime functions because CyberOrion's main
test environment does not install the pinned upstream environment.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import asdict, dataclass, field
from types import TracebackType
from typing import Any, Iterable, Mapping, Sequence


ROLES = ("triage", "threat_hunter", "lateral_analyst", "escalation")
_COORDINATION_TOOL_PREFIX = "dispatch_"
_REPORT_TOOL_PREFIX = "submit_"
_MAX_COMPACT_TEXT = 1200
_MAX_MODEL_VISIBLE_SNAPSHOT_CHARS = 12_000
# Native Inspect keeps the complete transcript in its audit log, while this
# bounded compaction keeps a long but legitimate investigation from paying for
# an ever-growing model context.  These are interface/resource controls, not
# changes to the official task, tools, or database.
_NATIVE_CONTEXT_COMPACTION_THRESHOLD_TOKENS = 48_000
_NATIVE_CONTEXT_COMPACTION_PRESERVE = 0.55
_MAX_COMMANDS = 48
_MAX_EVIDENCE = 64
_MAX_REPORTS = 24
_MAX_PROVENANCE = 64
_RESOURCE_BALANCE_MARKER = "CYBERORION RUNTIME RESOURCE BALANCE"
# Full must never let one child allocation consume the resources needed for
# commander synthesis and final submission. These are scheduler reserves, not
# extra resources: all usage still counts against the shared sample ceilings.
_COMMANDER_MODEL_CALL_RESERVE = 4
_COMMANDER_TOOL_CALL_RESERVE = 4
_COMMANDER_TOKEN_RESERVE = 16_384
_COMMANDER_WALL_TIME_RESERVE_SEC = 15.0

ROLE_DESCRIPTIONS: dict[str, str] = {
    "triage": (
        "Map only the relevant schema and data sources, identify the first "
        "bounded set of incident entities and time pivots, and hand off a "
        "prioritized investigation map. Do not reconstruct the whole incident."
    ),
    "threat_hunter": (
        "Test one explicit evidence-backed hypothesis at a time, correlate "
        "targeted records across the already identified sources, and return "
        "the supported or refuted incident-chain segment."
    ),
    "lateral_analyst": (
        "Start from named pivots and trace only cross-host, account, address, "
        "process, and time relationships needed to establish spread or scope. "
        "Do not repeat initial schema triage."
    ),
    "escalation": (
        "Adversarially verify a small set of candidate-answer claims, challenge "
        "contradictions or missing provenance, and report whether the answer is "
        "supported. Do not restart broad investigation or invent environment actions."
    ),
}

ROLE_MISSION_CONTRACTS: dict[str, str] = {
    "triage": (
        "Deliver the relevant table/field map, bounded time range, initial "
        "entities, and no more than three prioritized next hypotheses. Stop "
        "when that routing map is sufficient; leave deep correlation to hunters."
    ),
    "threat_hunter": (
        "Receive one named hypothesis and its pivots. Deliver concrete supporting "
        "or refuting records and the incident-chain implication. Stop after the "
        "hypothesis is resolved or two targeted branches add no evidence."
    ),
    "lateral_analyst": (
        "Receive explicit source and destination pivots. Deliver confirmed cross-"
        "entity links, affected scope, and unresolved gaps. Stop without performing "
        "general schema discovery or unrelated hunting."
    ),
    "escalation": (
        "Receive candidate claims and their provenance. Verify only the highest-"
        "impact unsupported or conflicting claims, then return a pass/challenge "
        "assessment and any narrowly required correction."
    ),
}


@dataclass(frozen=True)
class WorkerAllocation:
    """Commander-assigned hard limits for one isolated worker run."""

    token_limit: int
    tool_call_limit: int
    model_call_limit: int
    wall_time_sec: float

    def validate(self) -> None:
        values = {
            "token_limit": self.token_limit,
            "tool_call_limit": self.tool_call_limit,
            "model_call_limit": self.model_call_limit,
            "wall_time_sec": self.wall_time_sec,
        }
        invalid = [name for name, value in values.items() if value <= 0]
        if invalid:
            raise ValueError(
                "worker allocation values must be positive: "
                + ", ".join(invalid))

    def as_dict(self) -> dict[str, int | float]:
        self.validate()
        return asdict(self)


class _WorkerModelCallLimit:
    """Inspect-compatible worker-local model-call limit.

    The global model gate remains authoritative. This nested source is passed
    to ``inspect_ai.agent.run`` so only this worker's exhaustion is caught and
    classified locally; unknown or root/global limit exceptions still escape.
    """

    def __init__(self, limit: int) -> None:
        if limit <= 0:
            raise ValueError("worker model-call limit must be positive")
        self._limit = limit
        self._usage = 0
        self._entered = False

    @property
    def limit(self) -> int:
        return self._limit

    @property
    def usage(self) -> int:
        return self._usage

    @property
    def remaining(self) -> int:
        return max(0, self._limit - self._usage)

    def __enter__(self) -> "_WorkerModelCallLimit":
        if self._entered:
            raise RuntimeError("worker model-call limit cannot be reused")
        self._entered = True
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None,
        exc_val: BaseException | None, exc_tb: TracebackType | None,
    ) -> None:
        return None

    def consume(self) -> None:
        if self._usage >= self._limit:
            from inspect_ai.util import LimitExceededError
            raise LimitExceededError(
                "custom", value=self._usage, limit=self._limit,
                message=(
                    "CyberOrion worker-local model-call ceiling reached: "
                    f"{self._usage}/{self._limit}"),
                source=self,
            )
        self._usage += 1


def _resource_row(*, limit: int | float | None,
                  usage: int | float | None) -> dict[str, Any]:
    remaining = None
    if limit is not None and usage is not None:
        remaining = max(0, limit - usage)
    return {"limit": limit, "used": usage, "remaining": remaining}


def _sample_resource_rows(
    *, token_limit_value: int, tool_call_limit_value: int,
    wall_time_limit_value: float,
) -> dict[str, dict[str, Any]]:
    """Read root Inspect balances, falling back only for unavailable usage."""
    declared = {
        "provider_tokens": token_limit_value,
        "tool_calls": tool_call_limit_value,
        "wall_time_sec": wall_time_limit_value,
        "working_time_sec": None,
        "messages": None,
        "cost_usd": None,
    }
    try:
        from inspect_ai.util import sample_limits
        limits = sample_limits()
        mapped = {
            "provider_tokens": limits.token,
            "tool_calls": limits.tool_call,
            "wall_time_sec": limits.time,
            "working_time_sec": limits.working,
            "messages": limits.message,
            "cost_usd": limits.cost,
        }
        rows = {}
        for name, item in mapped.items():
            try:
                usage = item.usage
            except (NotImplementedError, RuntimeError):
                usage = None
            rows[name] = _resource_row(
                limit=item.limit if item.limit is not None else declared[name],
                usage=usage,
            )
        return rows
    except RuntimeError:
        return {
            name: _resource_row(limit=limit, usage=None)
            for name, limit in declared.items()
        }

_COMMON_INVESTIGATION_SOP = """
CYBERORION EXCYTIN INVESTIGATION SOP:
- Work only from the official task context and results returned by the official
  tools. Never infer hidden database or scoring state.
- Begin with schema discovery, then narrow queries using incident entities and
  time bounds. Correlate across tables when the question requires multiple hops.
- Prefer SHOW/DESCRIBE/INFORMATION_SCHEMA before data queries. Never use
  SELECT * or table dumps. Select only needed columns and start with LIMIT 5
  or LIMIT 10, COUNT, GROUP BY, targeted WHERE clauses, and time/IP/host/event
  filters. Avoid wide JSON/text columns such as AdditionalFields, Entities,
  Parameters, and Description until needed; use length/count or targeted
  extraction before retrieving their contents.
- A row LIMIT does not make a wide projection safe. Never select raw
  AdditionalFields, Entities, Parameters, Description, UrlChain, or comparable
  JSON/text columns across multiple rows. First select identifiers/counts or
  LEFT(column, 500) for at most one or five targeted records; retrieve a full
  wide field only for one exact record when it is indispensable.
- Every non-aggregate SELECT must have a bounded LIMIT. If a result is large,
  narrow the columns and predicate instead of repeating or expanding the query.
- Use Python to summarize already retrieved bounded data when appropriate; do
  not repeatedly copy raw tool output or the full database into context.
- Every query must resolve a named open question. After two consecutive empty
  or non-informative queries, stop broadening that branch and report the
  uncertainty. Once the assigned mission has sufficient evidence, submit the
  result immediately. Treat the runtime resource balance attached to every
  model request as authoritative and preserve enough balance to submit before
  any hard ceiling is reached.
- Keep hypotheses separate from verified evidence. Cite concrete table names,
  commands/queries, timestamps, identifiers, and returned values.
- Treat tool errors and empty results as evidence about the investigation, not
  as success. Change the query or report the uncertainty.
- Before submitting, verify that the answer directly addresses the official
  question and is supported by observed evidence.
- Use native tool calls. Do not emit a second JSON action protocol.
""".strip()

_COMMANDER_DISPATCH_GUIDANCE = """
RESOURCE-AWARE DISPATCH PLAYBOOK (from prior mechanism/resource traces; not
from reward or answer quality):
- A triage worker once broadened bash exploration across many records and
  consumed its local allocation before submitting any report. Treat triage as
  a bounded map-making mission, not as ownership of the whole incident.
- Another worker spent all of its available model turns on discovery and had
  no turn left for the required structured report. A worker allocation must
  support investigation plus report submission; do not spend the last call or
  token on another query.
- Concrete prior allocation failures show why a superficially positive
  allocation can still be unusable: a worker assigned 3,000 provider tokens
  consumed about 5,833 in its first provider call and produced no tool call or
  report; other workers assigned 3,000, 4,000, or 3,500 tokens consumed about
  8,398, 6,906, or 7,896 respectively before they could report. These are
  resource-planning observations, not task-specific instructions or score
  targets. Leave enough room for at least one useful investigation turn and
  the final structured report.
- The same failure occurs with model-call ceilings: workers capped at 1/1,
  2/2, 3/3, or 4/4 model calls exhausted the ceiling while investigating and
  had no structured report recorded. A model-call allocation is not merely a
  query budget; it must include a deliberate final report turn. If the
  remaining model-call balance cannot cover that turn, stop investigating and
  submit the report immediately.
- After a useful triage report, a later escalation worker reopened a broad
  investigation and consumed the remaining sample capacity instead of doing a
  narrow adversarial check. Use the existing evidence and only delegate a
  distinct, evidence-backed follow-up.
- A worker can also finish useful investigation while its report arguments
  fail schema validation. Keep missions and expected evidence concise, inspect
  the returned status, and do not blindly repeat the same failed delegation.

For every dispatch, follow this procedure:
1. Read the current global balance and safe worker capacity first. Preserve a
   protected commander reserve for evidence review, final synthesis, and
   submit. Never allocate the last shared resources to a worker.
2. Define one bounded mission with named pivots, an explicit stopping
   condition, the evidence required, and the report deliverable. Assign a
   worker enough local token, tool-call, model-call, and wall-time capacity to
   complete both its investigation and its structured report. The report call
   is part of the allocation and is never optional.
3. Route schema/source mapping to triage, one hypothesis to threat_hunter,
   explicit cross-entity links to lateral_analyst, and high-impact claim
   checking to escalation. Do not ask one worker to perform all roles, and do
   not issue duplicate or decorative missions.
4. Dispatch independent missions in parallel when they do not depend on one
   another; wait for prerequisite evidence before dispatching dependent work.
   Account for the aggregate reservations and reduce scope instead of
   over-allocating.
5. When a worker report arrives, inspect it and the shared summary before
   dispatching again. If a worker is exhausted, empty, or has a report parse
   failure, preserve that fact, use the evidence already available, and only
   retry with a materially narrower mission when the remaining balance can
   support a complete report.
6. When the balance is shrinking, stop opening new investigations and finish
   from the bounded evidence. The commander owns final reconciliation and
   must submit a supported answer before its own reserve is consumed.
""".strip()

SINGLE_PROMPT = f"""
You are CyberOrion's strong monolithic ExCyTIn investigator. You own planning,
schema discovery, multi-table correlation, targeted hunting, adversarial final
verification, and the final answer. You have the complete official environment
tool set. Maintain an internal evidence notebook and carry the investigation to
a supported conclusion. Every model request includes the current GLOBAL
resource balance. Use it to control investigation depth and reserve calls,
tokens, tools, and time for final verification and submit.

{_COMMON_INVESTIGATION_SOP}
""".strip()

COMMANDER_PROMPT = f"""
You are CyberOrion's ExCyTIn investigation commander. You own investigation
planning, worker routing, shared-state review, and the final answer. As in the
current production architecture, you do not directly execute environment
investigation tools: all bash/python investigation is performed by workers.
Delegation is natural work routing, not an optional decoration. Reports are
bounded evidence summaries, not hidden truth.

CYBERORION TEAM AVAILABLE IN FULL MODE:
- dispatch_triage(mission, token_limit, tool_call_limit, model_call_limit,
  wall_time_sec): {ROLE_DESCRIPTIONS["triage"]}
- dispatch_threat_hunter(mission, token_limit, tool_call_limit,
  model_call_limit, wall_time_sec): {ROLE_DESCRIPTIONS["threat_hunter"]}
- dispatch_lateral_analyst(mission, token_limit, tool_call_limit,
  model_call_limit, wall_time_sec): {ROLE_DESCRIPTIONS["lateral_analyst"]}
- dispatch_escalation(mission, token_limit, tool_call_limit, model_call_limit,
  wall_time_sec): {ROLE_DESCRIPTIONS["escalation"]}
- get_investigation_summary(): Read the bounded shared workspace.

DELEGATION SEMANTICS:
- Every commander model request includes the current GLOBAL resource balance,
  including safely allocatable worker capacity after active reservations and
  the protected commander finishing reserve. Workers never see that global
  balance.
- You control each worker's hard local token, native-tool-call, model-call, and
  wall-time ceilings through the dispatch arguments. The tool-call allocation
  includes the worker's final report-tool call, so reserve at least one. Allocate enough for the
  bounded mission but keep sufficient global balance for dependent work,
  evidence review, final synthesis, and submit. These allocations are subsets
  of—not additions to—the global sample resources.
- A delegated specialist automatically receives the official task context, its
  bounded mission, its assigned LOCAL resource balance, the current shared
  investigation state, and all official environment tools. Explicitly make the
  mission small enough that the worker can submit its report before its local
  balance is exhausted. Do not manually copy the whole evidence history.
- Begin by routing initial schema/data-source discovery to triage. Route deep
  hypothesis testing and incident reconstruction to threat_hunter; cross-entity
  or cross-host spread to lateral_analyst; and high-impact final challenge or
  response-oriented review to escalation.
- Independent missions may be dispatched together as parallel tool calls in one
  response. Their allocations are reserved atomically and each conversation is
  isolated; dependent missions must wait for the evidence they need. Never
  over-allocate the available worker balance.
- After dispatch, inspect the complete bounded reports returned by the tools or
  call get_investigation_summary. Do not ask workers to repeat full raw output.
- Do not dispatch decorative or duplicate missions. You remain responsible for
  checking reports and producing the official final answer with submit.

{_COMMANDER_DISPATCH_GUIDANCE}

{_COMMON_INVESTIGATION_SOP}
""".strip()

ORCHESTRATOR_ONLY_PROMPT = (
    "You are CyberOrion's strong monolithic ExCyTIn commander/investigator. "
    "Dispatch is disabled in this arm. You directly receive the complete "
    "official environment tool set and own planning, investigation, evidence "
    "verification, and the final answer. Every model request includes the "
    "current GLOBAL resource balance; preserve enough for final verification "
    "and submit.\n\n" + _COMMON_INVESTIGATION_SOP
)


def specialist_prompt(role: str) -> str:
    """Return the ExCyTIn adaptation of a production CyberOrion role."""
    if role not in ROLE_DESCRIPTIONS:
        raise ValueError(f"unknown specialist role: {role}")
    return f"""
You are CyberOrion's ExCyTIn {role} specialist.

ROLE DUTY:
{ROLE_DESCRIPTIONS[role]}

MISSION BOUNDARY:
{ROLE_MISSION_CONTRACTS[role]}

{_COMMON_INVESTIGATION_SOP}

Complete only the assigned mission independently with the official tools. Every
model request includes your assigned LOCAL resource balance, never the global
team balance. Plan backward from those hard limits and call the report tool
before tokens, official-tool calls, model calls, or wall time are exhausted. If
you consume the allocation first, execution stops and no report is synthesized.
Finish by
calling the provided structured report tool. Report actual findings, evidence,
commands/queries, confidence, uncertainties, recommended next investigation,
and candidate answer implications. An empty report is not acceptable.
""".strip()


def build_official_context(*, instruction_prompt: str, assistant_prompt: str,
                           task_input: str) -> tuple[str, dict[str, Any]]:
    """Hash complete model-visible official context without adding gold.

    The serialized value is retained for backward-compatible audit tests. It is
    not used as task input by the native bridge.
    """
    context = {
        "instruction_prompt": str(instruction_prompt),
        "assistant_prompt": str(assistant_prompt),
        "task_input": str(task_input),
    }
    serialized = json.dumps(context, ensure_ascii=False, sort_keys=True)
    audit = {
        "schema": "official_model_visible_context_v2_native",
        "instruction_prompt_present": bool(instruction_prompt),
        "assistant_prompt_present": bool(assistant_prompt),
        "task_input_present": bool(task_input),
        "instruction_prompt_sha256": hashlib.sha256(
            str(instruction_prompt).encode()).hexdigest(),
        "assistant_prompt_sha256": hashlib.sha256(
            str(assistant_prompt).encode()).hexdigest(),
        "task_input_sha256": hashlib.sha256(str(task_input).encode()).hexdigest(),
        "effective_context_sha256": hashlib.sha256(serialized.encode()).hexdigest(),
        "gold_or_scorer_context_added": False,
        "native_inspect_tool_execution": True,
        "custom_json_action_protocol": False,
    }
    return serialized, audit


def _tool_name(tool: Any) -> str:
    """Resolve a model-visible native Inspect tool name without wrapping it."""
    try:
        from inspect_ai._util.registry import registry_info
        return str(registry_info(tool).name).rsplit("/", 1)[-1]
    except Exception:  # noqa: BLE001 - pure metadata fallback
        for value in (
            getattr(tool, "name", None), getattr(tool, "__name__", None)
        ):
            if value:
                return str(value)
        return type(tool).__name__


def official_tool_names(tools: Sequence[Any] | None) -> tuple[str, ...]:
    """Return exact official tool names supplied by SABER."""
    return tuple(_tool_name(tool) for tool in (tools or ()))


def arm_tool_contract(arm: str, tools: Sequence[Any] | None) -> dict[str, Any]:
    """Mechanically expose environment-tool fairness for tests and artifacts."""
    if arm not in {"single", "orchestrator_only", "full"}:
        raise ValueError(f"unknown arm: {arm}")
    names = official_tool_names(tools)
    full = arm == "full"
    return {
        "arm": arm,
        "official_environment_tools": list(names),
        "commander_environment_tools": [] if full else list(names),
        "worker_environment_tools": (
            {role: list(names) for role in ROLES} if full else {}
        ),
        "delegation_tools": (
            [f"{_COORDINATION_TOOL_PREFIX}{role}" for role in ROLES]
            if full else []
        ),
        "commander_state_tools": (
            ["get_investigation_summary"] if full else []
        ),
        "final_answer_tool": "submit",
        "investigation_tool_union": list(names),
        "official_tool_union": list(names),
    }


def _message_content(message: Any) -> str:
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for item in content or ():
        text = getattr(item, "text", None)
        if text:
            parts.append(str(text))
    return "\n".join(parts)


def _task_input(state: Any) -> str:
    """Read only sample input already visible to every official arm."""
    visible = getattr(state, "input", None)
    if visible:
        return str(visible)
    for message in getattr(state, "messages", ()):
        if str(getattr(message, "role", "")) == "user":
            text = _message_content(message)
            if text:
                return text
    return ""


def _compact_text(value: Any, limit: int = _MAX_COMPACT_TEXT) -> dict[str, Any]:
    """Create deterministic bounded evidence while preserving raw audit identity."""
    text = str(value or "")
    if len(text) <= limit:
        compact = text
        truncated = False
    else:
        head = limit * 3 // 4
        tail = limit - head
        compact = text[:head] + "\n...[bounded shared-state omission]...\n" + text[-tail:]
        truncated = True
    return {
        "text": compact,
        "raw_chars": len(text),
        "raw_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "shared_state_truncated": truncated,
    }


def _bounded_arguments(value: Any) -> dict[str, Any]:
    """Bound command/query arguments without altering the official tool call."""
    serialized = json.dumps(value or {}, ensure_ascii=False, sort_keys=True,
                            default=str)
    return _compact_text(serialized, 800)


def _tail(rows: list[Any], limit: int) -> list[Any]:
    return rows[-limit:]


@dataclass
class SpecialistReport:
    role: str
    findings: list[str]
    evidence: list[dict[str, str]]
    commands_or_queries: list[str]
    confidence: str
    uncertainties: list[str]
    recommended_next_investigation: list[str]
    candidate_answer_implications: list[str]

    def validate(self) -> None:
        if self.role not in ROLES:
            raise ValueError(f"invalid report role: {self.role}")
        if self.confidence not in {"low", "medium", "high"}:
            raise ValueError("confidence must be low, medium, or high")
        if not any((self.findings, self.evidence, self.commands_or_queries,
                    self.uncertainties, self.recommended_next_investigation,
                    self.candidate_answer_implications)):
            raise ValueError("empty specialist report")


def parse_specialist_report(
    raw: Any, role: str,
) -> tuple[str, dict[str, Any] | None, str | None]:
    """Classify a specialist result without accepting malformed output."""
    try:
        value = raw if isinstance(raw, Mapping) else json.loads(str(raw or ""))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        return "parse_failure", None, f"{type(exc).__name__}: {exc}"
    if not isinstance(value, Mapping):
        return "parse_failure", None, "report is not an object"
    try:
        def text_list(name: str) -> list[str]:
            raw_field = value.get(name)
            if raw_field is None:
                return []
            if isinstance(raw_field, str):
                return [raw_field] if raw_field.strip() else []
            if isinstance(raw_field, list):
                return [str(item) for item in raw_field]
            raise ValueError(f"{name} must be text or a text list")

        evidence = []
        for item in value.get("evidence") or []:
            if not isinstance(item, Mapping):
                raise ValueError("evidence item is not an object")
            evidence.append({str(k): str(v) for k, v in item.items()})
        report = SpecialistReport(
            role=role,
            findings=text_list("findings"),
            evidence=evidence,
            commands_or_queries=text_list("commands_or_queries"),
            confidence=str(value.get("confidence") or "").lower(),
            uncertainties=text_list("uncertainties"),
            recommended_next_investigation=text_list(
                "recommended_next_investigation"),
            candidate_answer_implications=text_list(
                "candidate_answer_implications"),
        )
        report.validate()
    except (TypeError, ValueError) as exc:
        status = (
            "empty_report" if "empty specialist report" in str(exc)
            else "parse_failure"
        )
        return status, None, f"{type(exc).__name__}: {exc}"
    return "successful_report", asdict(report), None


@dataclass
class InvestigationState:
    """Shared state containing only model-visible investigation data."""

    task_context_sha256: str
    discovered_schema: list[dict[str, Any]] = field(default_factory=list)
    executed_commands: list[dict[str, Any]] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    hypotheses: list[dict[str, Any]] = field(default_factory=list)
    unresolved_questions: list[dict[str, Any]] = field(default_factory=list)
    specialist_reports: list[dict[str, Any]] = field(default_factory=list)
    provenance: list[dict[str, Any]] = field(default_factory=list)
    report_counts: dict[str, int] = field(default_factory=lambda: {
        "successful_report": 0,
        "empty_report": 0,
        "parse_failure": 0,
        "role_budget_exhaustion": 0,
        "tool_failure": 0,
    })
    model_calls: int = 0
    official_tool_calls: int = 0
    dispatches: int = 0
    workspace_omissions: dict[str, int] = field(default_factory=lambda: {
        "commands": 0, "evidence": 0, "reports": 0, "provenance": 0,
    })
    _seen_messages: set[str] = field(default_factory=set, repr=False)
    _pending_calls: dict[str, dict[str, Any]] = field(
        default_factory=dict, repr=False)
    _sequence: int = field(default=0, repr=False)

    def _next(self) -> int:
        self._sequence += 1
        return self._sequence

    def ingest_messages(self, messages: Iterable[Any], *, role: str,
                        official_names: set[str]) -> None:
        """Extract real native model/tool messages into the notebook."""
        for message in messages:
            message_id = str(getattr(message, "id", "") or id(message))
            if message_id in self._seen_messages:
                continue
            self._seen_messages.add(message_id)
            message_role = str(getattr(message, "role", ""))
            if message_role == "assistant":
                self.model_calls += 1
                for call in getattr(message, "tool_calls", None) or ():
                    self._pending_calls[str(getattr(call, "id", ""))] = {
                        "tool": str(getattr(call, "function", "")),
                        "arguments": getattr(call, "arguments", {}) or {},
                    }
            elif message_role == "tool":
                call_id = str(getattr(message, "tool_call_id", ""))
                function = str(getattr(message, "function", ""))
                call = self._pending_calls.pop(
                    call_id, {"tool": function, "arguments": {}})
                if call["tool"] not in official_names:
                    continue
                error = getattr(message, "error", None)
                row = {
                    "id": f"CMD-{self._next()}",
                    "sequence": self._sequence,
                    "role": role,
                    "tool": call["tool"],
                    "arguments": _bounded_arguments(call["arguments"]),
                    "tool_call_id": call_id,
                    "error": None if error is None else str(error),
                }
                self.executed_commands.append(row)
                if len(self.executed_commands) > _MAX_COMMANDS:
                    self.workspace_omissions["commands"] += 1
                    self.executed_commands = _tail(
                        self.executed_commands, _MAX_COMMANDS)
                self.official_tool_calls += 1
                if error is not None:
                    self.report_counts["tool_failure"] += 1
                else:
                    compact = _compact_text(_message_content(message))
                    self.evidence.append({
                        "id": f"E-{self._next()}",
                        "sequence": self._sequence,
                        "role": role,
                        "source": call["tool"],
                        "tool_call_id": call_id,
                        "query_or_command": row["arguments"],
                        "snippet": compact["text"],
                        "raw_chars": compact["raw_chars"],
                        "raw_sha256": compact["raw_sha256"],
                        "shared_state_truncated": compact[
                            "shared_state_truncated"],
                    })
                    if len(self.evidence) > _MAX_EVIDENCE:
                        self.workspace_omissions["evidence"] += 1
                        self.evidence = _tail(self.evidence, _MAX_EVIDENCE)

    def record_dispatch(self, role: str, mission: str,
                        allocation: WorkerAllocation) -> str:
        self.dispatches += 1
        dispatch_id = f"D-{self._next()}"
        self.provenance.append({
            "id": dispatch_id, "sequence": self._sequence,
            "kind": "dispatch", "role": role,
            "mission": _compact_text(mission, 800),
            "worker_allocation": allocation.as_dict(),
        })
        if len(self.provenance) > _MAX_PROVENANCE:
            self.workspace_omissions["provenance"] += 1
            self.provenance = _tail(self.provenance, _MAX_PROVENANCE)
        return dispatch_id

    def record_report(self, *, status: str, role: str, mission: str,
                      report: dict[str, Any] | None, error: str | None,
                      raw: str | None = None,
                      allocation: WorkerAllocation | None = None,
                      local_usage: Mapping[str, Any] | None = None,
                      dispatch_id: str | None = None) -> None:
        if status not in self.report_counts:
            self.report_counts[status] = 0
        self.report_counts[status] += 1
        bounded_report = None
        if report is not None:
            bounded_report = {
                "role": role,
                "findings": [
                    _compact_text(item)["text"]
                    for item in report.get("findings", [])[:16]
                ],
                "evidence": [
                    {
                        str(key): _compact_text(value, 800)["text"]
                        for key, value in item.items()
                    }
                    for item in report.get("evidence", [])[:24]
                ],
                "commands_or_queries": [
                    _compact_text(item, 800)["text"]
                    for item in report.get("commands_or_queries", [])[:24]
                ],
                "confidence": report.get("confidence"),
                "uncertainties": [
                    _compact_text(item)["text"]
                    for item in report.get("uncertainties", [])[:16]
                ],
                "recommended_next_investigation": [
                    _compact_text(item)["text"]
                    for item in report.get(
                        "recommended_next_investigation", [])[:16]
                ],
                "candidate_answer_implications": [
                    _compact_text(item)["text"]
                    for item in report.get(
                        "candidate_answer_implications", [])[:16]
                ],
            }
            self.hypotheses.extend({
                "id": f"H-{self._next()}", "sequence": self._sequence,
                "role": role, "text": item,
            } for item in bounded_report["candidate_answer_implications"])
            self.unresolved_questions.extend({
                "id": f"Q-{self._next()}", "sequence": self._sequence,
                "role": role, "text": item,
            } for item in (
                bounded_report["uncertainties"]
                + bounded_report["recommended_next_investigation"]
            ))
            self.hypotheses = _tail(self.hypotheses, 48)
            self.unresolved_questions = _tail(self.unresolved_questions, 48)
        raw_identity = (
            {
                "raw_chars": len(raw),
                "raw_sha256": hashlib.sha256(raw.encode()).hexdigest(),
            }
            if raw else None
        )
        self.specialist_reports.append({
            "id": f"R-{self._next()}", "sequence": self._sequence,
            "status": status, "role": role,
            "mission": _compact_text(mission, 800),
            "report": bounded_report, "error": error,
            "raw_report_identity": raw_identity,
            "dispatch_id": dispatch_id,
            "worker_allocation": (
                allocation.as_dict() if allocation is not None else None),
            "worker_local_usage": dict(local_usage or {}),
        })
        if len(self.specialist_reports) > _MAX_REPORTS:
            self.workspace_omissions["reports"] += 1
            self.specialist_reports = _tail(
                self.specialist_reports, _MAX_REPORTS)

    def public_snapshot(self) -> dict[str, Any]:
        """Small recency window safe to provide to another agent."""
        visible = {
            "discovered_schema": 8,
            "executed_commands": 5,
            "evidence": 5,
            "hypotheses": 8,
            "unresolved_questions": 8,
            "specialist_reports": 2,
            "provenance": 12,
        }

        def report_summary(row: dict[str, Any]) -> dict[str, Any]:
            report = row.get("report") or {}
            return {
                "id": row.get("id"),
                "sequence": row.get("sequence"),
                "status": row.get("status"),
                "role": row.get("role"),
                "mission": row.get("mission"),
                "report": {
                    "findings": [
                        _compact_text(item, 300)["text"]
                        for item in report.get("findings", [])[:3]
                    ],
                    "evidence": [
                        {
                            str(key): _compact_text(value, 250)["text"]
                            for key, value in item.items()
                        }
                        for item in report.get("evidence", [])[:3]
                    ],
                    "commands_or_queries": [
                        _compact_text(item, 250)["text"]
                        for item in report.get("commands_or_queries", [])[:3]
                    ],
                    "confidence": report.get("confidence"),
                    "uncertainties": [
                        _compact_text(item, 300)["text"]
                        for item in report.get("uncertainties", [])[:2]
                    ],
                    "recommended_next_investigation": [
                        _compact_text(item, 300)["text"]
                        for item in report.get(
                            "recommended_next_investigation", [])[:2]
                    ],
                    "candidate_answer_implications": [
                        _compact_text(item, 300)["text"]
                        for item in report.get(
                            "candidate_answer_implications", [])[:3]
                    ],
                } if report else None,
                "error": row.get("error"),
            }

        def command_summary(row: dict[str, Any]) -> dict[str, Any]:
            value = dict(row)
            arguments = value.get("arguments") or {}
            value["arguments"] = {
                **arguments,
                "text": _compact_text(arguments.get("text"), 400)["text"],
            }
            return value

        def evidence_summary(row: dict[str, Any]) -> dict[str, Any]:
            value = dict(row)
            value["snippet"] = _compact_text(value.get("snippet"), 600)["text"]
            arguments = value.get("query_or_command") or {}
            value["query_or_command"] = {
                **arguments,
                "text": _compact_text(arguments.get("text"), 400)["text"],
            }
            return value

        def text_fact_summary(row: dict[str, Any]) -> dict[str, Any]:
            value = dict(row)
            value["text"] = _compact_text(value.get("text"), 300)["text"]
            return value
        omissions = dict(self.workspace_omissions)
        omissions.update({
            "model_visible_commands": max(
                0, len(self.executed_commands) - visible["executed_commands"]),
            "model_visible_evidence": max(
                0, len(self.evidence) - visible["evidence"]),
            "model_visible_reports": max(
                0, len(self.specialist_reports) - visible["specialist_reports"]),
            "model_visible_provenance": max(
                0, len(self.provenance) - visible["provenance"]),
        })
        snapshot = {
            "task_context_sha256": self.task_context_sha256,
            "discovered_schema": _tail(
                self.discovered_schema, visible["discovered_schema"]),
            "executed_commands": _tail(
                [command_summary(row) for row in self.executed_commands],
                visible["executed_commands"]),
            "evidence": _tail(
                [evidence_summary(row) for row in self.evidence],
                visible["evidence"]),
            "hypotheses": _tail(
                [text_fact_summary(row) for row in self.hypotheses],
                visible["hypotheses"]),
            "unresolved_questions": _tail(
                [text_fact_summary(row) for row in self.unresolved_questions],
                visible["unresolved_questions"]),
            "specialist_reports": _tail(
                [report_summary(row) for row in self.specialist_reports],
                visible["specialist_reports"]),
            "provenance": _tail(self.provenance, visible["provenance"]),
            "workspace_omissions": omissions,
        }
        def snapshot_size() -> int:
            return len(json.dumps(snapshot, ensure_ascii=False, default=str))

        # Keep the model-visible notebook below the native Inspect prompt/tool
        # scale. Raw evidence remains in audit_snapshot and the Inspect log;
        # this only drops the oldest compact rows from the shared view.
        bound_applied = False
        for key in (
            "provenance", "unresolved_questions", "hypotheses",
            "specialist_reports", "evidence", "executed_commands",
            "discovered_schema",
        ):
            rows = snapshot[key]
            while snapshot_size() > _MAX_MODEL_VISIBLE_SNAPSHOT_CHARS and rows:
                rows.pop(0)
                bound_applied = True
            if snapshot_size() <= _MAX_MODEL_VISIBLE_SNAPSHOT_CHARS:
                break
        if bound_applied:
            snapshot["workspace_omissions"][
                "model_visible_snapshot_bound_applied"] = True
        if snapshot_size() > _MAX_MODEL_VISIBLE_SNAPSHOT_CHARS:
            for key in (
                "provenance", "unresolved_questions", "hypotheses",
                "specialist_reports", "evidence", "executed_commands",
                "discovered_schema",
            ):
                snapshot[key] = []
                snapshot["workspace_omissions"][
                    "model_visible_snapshot_bound_applied"] = True
                if snapshot_size() <= _MAX_MODEL_VISIBLE_SNAPSHOT_CHARS:
                    break
        return snapshot

    def audit_snapshot(self) -> dict[str, Any]:
        public = self.public_snapshot()
        value = {
            "task_context_sha256": self.task_context_sha256,
            "discovered_schema": self.discovered_schema,
            "executed_commands": self.executed_commands,
            "evidence": self.evidence,
            "hypotheses": self.hypotheses,
            "unresolved_questions": self.unresolved_questions,
            "specialist_reports": self.specialist_reports,
            "provenance": self.provenance,
            "workspace_omissions": dict(self.workspace_omissions),
        }
        value.update({
            "report_counts": dict(self.report_counts),
            "model_calls": self.model_calls,
            "official_tool_calls": self.official_tool_calls,
            "dispatches": self.dispatches,
            "bounds": {
                "max_compact_text_chars": _MAX_COMPACT_TEXT,
                "max_commands": _MAX_COMMANDS,
                "max_evidence": _MAX_EVIDENCE,
                "max_reports": _MAX_REPORTS,
                "max_provenance": _MAX_PROVENANCE,
                "max_model_visible_snapshot_chars": (
                    _MAX_MODEL_VISIBLE_SNAPSHOT_CHARS),
                "model_visible_snapshot_chars": len(json.dumps(
                    public, ensure_ascii=False, default=str)),
            },
        })
        return value


def _combined_instructions(official: str, addition: str) -> str:
    """Preserve official instructions verbatim and append arm semantics."""
    return f"{official}\n\n{addition}" if official else addition


def _native_context_compaction() -> Any:
    """Use pinned Inspect's deterministic, non-LLM transcript compaction."""
    from inspect_ai.model import CompactionTrim

    return CompactionTrim(
        threshold=_NATIVE_CONTEXT_COMPACTION_THRESHOLD_TOKENS,
        preserve=_NATIVE_CONTEXT_COMPACTION_PRESERVE,
        memory=False,
    )


def _report_tool(role: str) -> Any:
    """Build a native Inspect submit tool with deterministic report schema."""
    from inspect_ai.tool import ToolDef

    async def submit_report(
        findings: list[str],
        evidence: list[dict[str, str]],
        commands_or_queries: list[str],
        confidence: str,
        uncertainties: list[str],
        recommended_next_investigation: list[str],
        candidate_answer_implications: list[str],
    ) -> str:
        """Submit the specialist's structured investigation report.

        Args:
          findings: Concise verified findings as separate text items.
          evidence: Claim/source/snippet records from official tool results.
          commands_or_queries: Commands/queries actually executed.
          confidence: One of low, medium, or high.
          uncertainties: Remaining uncertainty and unsupported claims.
          recommended_next_investigation: Concrete bounded next steps.
          candidate_answer_implications: Evidence-bearing answer implications.
        """
        return json.dumps({
            "role": role,
            "findings": findings,
            "evidence": evidence,
            "commands_or_queries": commands_or_queries,
            "confidence": confidence,
            "uncertainties": uncertainties,
            "recommended_next_investigation": recommended_next_investigation,
            "candidate_answer_implications": candidate_answer_implications,
        }, ensure_ascii=False)

    return ToolDef(
        submit_report,
        name=f"{_REPORT_TOOL_PREFIX}{role}_report",
        description=f"Submit the required structured {role} report.",
    )


class _DispatchController:
    """Sample-scoped native dispatch and shared-state controller."""

    def __init__(self, *, task_context: str, instruction_prompt: str,
                 assistant_prompt: str, official_tools: Sequence[Any],
                 commander_messages: list[Any], max_dispatches: int,
                 max_parallel_dispatches: int,
                 model_gate: "_GlobalModelGate") -> None:
        self.task_context = task_context
        self.instruction_prompt = instruction_prompt
        self.assistant_prompt = assistant_prompt
        self.official_tools = list(official_tools)
        self.official_names = set(official_tool_names(official_tools))
        self.commander_messages = commander_messages
        self.max_dispatches = max_dispatches
        self.max_parallel_dispatches = max_parallel_dispatches
        self.model_gate = model_gate
        self.state = InvestigationState(
            hashlib.sha256(task_context.encode()).hexdigest())
        self._lock = asyncio.Lock()
        self._parallel = asyncio.Semaphore(max_parallel_dispatches)
        self._active_allocations: dict[str, WorkerAllocation] = {}

    def _active_reserved(self) -> dict[str, int | float]:
        allocations = tuple(self._active_allocations.values())
        return {
            "provider_tokens": sum(item.token_limit for item in allocations),
            "tool_calls": sum(
                item.tool_call_limit for item in allocations),
            "model_calls": sum(item.model_call_limit for item in allocations),
            # Concurrent allocations overlap in wall-clock time, so their wall
            # ceilings are not additive. The maximum remains auditable.
            "wall_time_sec": max(
                (item.wall_time_sec for item in allocations), default=0.0),
        }

    def commander_balance(self) -> dict[str, Any]:
        """Global balance visible only to the Full commander."""
        balance = self.model_gate.global_balance()
        active = self._active_reserved()
        reserves = {
            "provider_tokens": _COMMANDER_TOKEN_RESERVE,
            "tool_calls": _COMMANDER_TOOL_CALL_RESERVE,
            "model_calls": _COMMANDER_MODEL_CALL_RESERVE,
            "wall_time_sec": _COMMANDER_WALL_TIME_RESERVE_SEC,
        }
        allocatable = {}
        for name, reserve in reserves.items():
            remaining = balance["resources"][name]["remaining"]
            if remaining is None:
                allocatable[name] = None
            else:
                # Wall allocations overlap; all other active allocations are
                # additive against the common sample budget.
                reserved = 0 if name == "wall_time_sec" else active[name]
                allocatable[name] = max(0, remaining - reserve - reserved)
        balance.update({
            "commander_finishing_reserve": reserves,
            "active_worker_reservations": active,
            "safe_worker_allocatable": allocatable,
            "dispatches": {
                "limit": self.max_dispatches,
                "used": self.state.dispatches,
                "remaining": max(
                    0, self.max_dispatches - self.state.dispatches),
                "max_parallel": self.max_parallel_dispatches,
                "active": len(self._active_allocations),
            },
        })
        return balance

    async def _reserve(
        self, role: str, mission: str, allocation: WorkerAllocation,
    ) -> tuple[str, dict[str, Any]]:
        from inspect_ai.tool import ToolError
        try:
            allocation.validate()
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        async with self._lock:
            self.state.ingest_messages(
                self.commander_messages, role="orchestrator",
                official_names=self.official_names)
            if self.state.dispatches >= self.max_dispatches:
                raise ToolError(
                    f"global dispatch ceiling reached: {self.max_dispatches}")
            available = self.commander_balance()["safe_worker_allocatable"]
            requested = {
                "provider_tokens": allocation.token_limit,
                "tool_calls": allocation.tool_call_limit,
                "model_calls": allocation.model_call_limit,
                "wall_time_sec": allocation.wall_time_sec,
            }
            excessive = {
                name: {"requested": value, "available": available[name]}
                for name, value in requested.items()
                if available[name] is not None and value > available[name]
            }
            if excessive:
                raise ToolError(
                    "worker allocation exceeds safely allocatable global "
                    "balance: " + json.dumps(excessive, sort_keys=True))
            dispatch_id = self.state.record_dispatch(
                role, mission, allocation)
            self._active_allocations[dispatch_id] = allocation
            snapshot = json.loads(json.dumps(
                self.state.public_snapshot(), ensure_ascii=False, default=str))
            return dispatch_id, snapshot

    async def _release(self, dispatch_id: str) -> None:
        async with self._lock:
            self._active_allocations.pop(dispatch_id, None)

    @staticmethod
    def _worker_balance(
        *, role: str, allocation: WorkerAllocation, token_budget: Any,
        tool_budget: Any, time_budget: Any,
        model_budget: _WorkerModelCallLimit,
    ) -> dict[str, Any]:
        return {
            "scope": "worker_allocation",
            "role": role,
            "resources": {
                "provider_tokens": _resource_row(
                    limit=allocation.token_limit, usage=token_budget.usage),
                "tool_calls": _resource_row(
                    limit=allocation.tool_call_limit,
                    usage=tool_budget.usage),
                "model_calls": _resource_row(
                    limit=allocation.model_call_limit,
                    usage=model_budget.usage),
                "wall_time_sec": _resource_row(
                    limit=allocation.wall_time_sec, usage=time_budget.usage),
            },
            "instruction": (
                "This is your complete local allocation. Submit the structured "
                "report before any remaining value reaches zero."),
        }

    async def dispatch(self, role: str, mission: str,
                       allocation: WorkerAllocation) -> str:
        """Run one isolated native Inspect specialist and return its report."""
        from inspect_ai.agent import AgentPrompt, AgentSubmit, react, run
        from inspect_ai.util import time_limit, token_limit, tool_call_limit

        status = "parse_failure"
        report: dict[str, Any] | None = None
        error: str | None = None
        raw = ""
        child_state = None
        dispatch_id = ""
        local_usage: dict[str, Any] = {}
        async with self._parallel:
            try:
                dispatch_id, shared_snapshot = await self._reserve(
                    role, mission, allocation)
                report_tool = _report_tool(role)
                token_budget = token_limit(allocation.token_limit)
                tool_budget = tool_call_limit(allocation.tool_call_limit)
                time_budget = time_limit(allocation.wall_time_sec)
                model_budget = _WorkerModelCallLimit(
                    allocation.model_call_limit)
                balance_provider = lambda: self._worker_balance(
                    role=role, allocation=allocation,
                    token_budget=token_budget, tool_budget=tool_budget,
                    time_budget=time_budget, model_budget=model_budget)
                child = react(
                    name=f"cyberorion_excytin_{role}",
                    description=ROLE_DESCRIPTIONS[role],
                    prompt=AgentPrompt(
                        instructions=_combined_instructions(
                            self.instruction_prompt, specialist_prompt(role)),
                        assistant_prompt=self.assistant_prompt or None,
                        handoff_prompt=None,
                        submit_prompt=None,
                    ),
                    tools=self.official_tools,
                    model=self.model_gate.agent(
                        role, balance_provider=balance_provider,
                        local_model_limit=model_budget),
                    submit=AgentSubmit(
                        tool=report_tool,
                        name=f"{_REPORT_TOOL_PREFIX}{role}_report",
                        description=f"Submit the structured {role} report.",
                        answer_only=True,
                        keep_in_messages=True,
                    ),
                    compaction=_native_context_compaction(),
                    truncation="auto",
                )
                child_input = (
                    "OFFICIAL TASK CONTEXT (identical information available "
                    "to Single):\n"
                    f"{self.task_context}\n\n"
                    f"SPECIALIST MISSION:\n{mission}\n\n"
                    "HARD LOCAL WORKER ALLOCATION:\n"
                    + json.dumps(allocation.as_dict(), ensure_ascii=False)
                    + "\nSubmit the structured report before any local limit "
                    "is exhausted. No report will be synthesized for you.\n\n"
                    "CURRENT SHARED INVESTIGATION STATE:\n"
                    + json.dumps(
                        shared_snapshot, ensure_ascii=False, default=str)
                )
                child_state, limit_error = await run(
                    child, input=child_input, name=role,
                    limits=[token_budget, tool_budget, time_budget, model_budget],
                )
                local_usage = balance_provider()["resources"]
                if limit_error is not None:
                    status = "role_budget_exhaustion"
                    error = json.dumps({
                        "type": limit_error.type,
                        "value": limit_error.value,
                        "limit": limit_error.limit,
                        "message": limit_error.message,
                    }, ensure_ascii=False)
                else:
                    raw = str(child_state.output.completion or "")
                    status, report, error = parse_specialist_report(raw, role)
            except Exception as exc:  # noqa: BLE001 - classified for audit
                # Root/global Inspect limit errors have no worker-local source,
                # are not caught by run(... limits=...), and must propagate.
                from inspect_ai.util import LimitExceededError
                from inspect_ai.tool import ToolError
                if isinstance(exc, LimitExceededError):
                    raise
                if isinstance(exc, ToolError):
                    raise
                status = "parse_failure"
                error = f"{type(exc).__name__}: {exc}"
            finally:
                if dispatch_id:
                    await self._release(dispatch_id)
        async with self._lock:
            if child_state is not None:
                self.state.ingest_messages(
                    child_state.messages, role=role,
                    official_names=self.official_names)
            self.state.record_report(
                status=status, role=role, mission=mission, report=report,
                error=error, raw=raw if status != "successful_report" else None,
                allocation=allocation, local_usage=local_usage,
                dispatch_id=dispatch_id or None)
            commander_report = self.state.specialist_reports[-1]["report"]
        return json.dumps({
            "status": status,
            "role": role,
            "mission": mission,
            "worker_allocation": allocation.as_dict(),
            "worker_local_usage": local_usage,
            "report": commander_report,
            "error": error,
            "audit": {
                "official_tool_calls": self.state.official_tool_calls,
                "tool_failures": self.state.report_counts["tool_failure"],
            },
        }, ensure_ascii=False, default=str)

    def tools(self) -> list[Any]:
        """Create production-parity state-query and dispatch tools."""
        from inspect_ai.tool import ToolDef

        async def get_investigation_summary() -> str:
            """Read the current bounded shared investigation workspace."""
            async with self._lock:
                return json.dumps(
                    self.state.public_snapshot(), ensure_ascii=False,
                    default=str)

        result = [ToolDef(
            get_investigation_summary,
            name="get_investigation_summary",
            description=(
                "Read compact shared findings, evidence provenance, prior "
                "specialist reports, hypotheses, and unresolved questions."
            ),
        )]
        for role in ROLES:
            def make_delegate(selected_role: str):
                async def delegate(
                    mission: str, token_limit: int, tool_call_limit: int,
                    model_call_limit: int, wall_time_sec: float,
                ) -> str:
                    """Delegate an independent investigation mission.

                    Args:
                      mission: Bounded specialist mission and expected evidence.
                      token_limit: Hard provider-token ceiling for this worker.
                      tool_call_limit: Hard native tool-call ceiling, including report.
                      model_call_limit: Hard LLM-call ceiling for this worker.
                      wall_time_sec: Hard wall-clock ceiling in seconds.
                    """
                    return await self.dispatch(
                        selected_role, mission, WorkerAllocation(
                            token_limit=token_limit,
                            tool_call_limit=tool_call_limit,
                            model_call_limit=model_call_limit,
                            wall_time_sec=wall_time_sec,
                        ))

                return delegate

            result.append(ToolDef(
                make_delegate(role),
                name=f"{_COORDINATION_TOOL_PREFIX}{role}",
                description=(
                    f"Dispatch the production {role} worker: "
                    f"{ROLE_DESCRIPTIONS[role]} "
                    "The specialist automatically receives official task context, "
                    "bounded shared evidence, official bash/python tools, and only "
                    "the hard local resource balance assigned in this call. "
                    "The tool-call allocation includes the report call. "
                    f"Mission contract: {ROLE_MISSION_CONTRACTS[role]} "
                    "Independent delegate calls in one response run concurrently."
                ),
                parallel=True,
            ))
        return result


class _GlobalModelGate:
    """Concurrency-safe hard model-call ceiling shared by commander/children."""

    def __init__(self, limit: int, *, token_limit_value: int,
                 tool_call_limit_value: int,
                 wall_time_limit_value: float) -> None:
        if limit <= 0:
            raise ValueError("model-call limit must be positive")
        self.limit = limit
        self.token_limit_value = token_limit_value
        self.tool_call_limit_value = tool_call_limit_value
        self.wall_time_limit_value = wall_time_limit_value
        self.calls = 0
        self.by_role: dict[str, int] = {}
        self._lock = asyncio.Lock()

    def global_balance(self) -> dict[str, Any]:
        resources = _sample_resource_rows(
            token_limit_value=self.token_limit_value,
            tool_call_limit_value=self.tool_call_limit_value,
            wall_time_limit_value=self.wall_time_limit_value,
        )
        resources["model_calls"] = _resource_row(
            limit=self.limit, usage=self.calls)
        return {
            "scope": "global",
            "resources": resources,
            "model_calls_by_role": dict(sorted(self.by_role.items())),
            "instruction": (
                "These are shared hard sample limits. Preserve enough balance "
                "for verification and final submit."),
        }

    @staticmethod
    def _attach_balance(input_value: Any,
                        balance: Mapping[str, Any]) -> list[Any]:
        """Attach an ephemeral system balance to exactly this provider call."""
        from inspect_ai.model import ChatMessageSystem, ChatMessageUser

        message = ChatMessageSystem(content=(
            f"{_RESOURCE_BALANCE_MARKER}\n"
            + json.dumps(balance, ensure_ascii=False, sort_keys=True,
                         default=str)
        ))
        if isinstance(input_value, str):
            return [message, ChatMessageUser(content=input_value)]
        messages = list(input_value)
        insert_at = 0
        while (insert_at < len(messages)
               and str(getattr(messages[insert_at], "role", "")) == "system"):
            insert_at += 1
        messages.insert(insert_at, message)
        return messages

    def agent(self, role: str, *, balance_provider=None,
              local_model_limit: _WorkerModelCallLimit | None = None):
        """Return a transparent Inspect Model with shared call accounting.

        Returning an Inspect ``Model`` (rather than an Agent-shaped callable)
        is important: pinned ``react`` otherwise treats the callable as a
        custom agent and intentionally disables its native compaction handler.
        """
        from inspect_ai._util.notgiven import NOT_GIVEN
        from inspect_ai.model import GenerateConfig, Model, get_model

        gate = self
        underlying = get_model()

        class _BudgetedModel(Model):
            def __init__(self) -> None:
                # Reuse the active provider/client; this proxy must not create
                # a second credentialed model or alter its wire configuration.
                super().__init__(underlying.api, underlying.config,
                                 underlying.model_args)
                self._underlying = underlying

            async def generate(
                self, input, tools=(), tool_choice=None,
                config: GenerateConfig | None = None, cache=NOT_GIVEN,
            ):
                async with gate._lock:
                    if gate.calls >= gate.limit:
                        from inspect_ai.util import LimitExceededError
                        raise LimitExceededError(
                            "custom", value=gate.calls, limit=gate.limit,
                            message=(
                                "CyberOrion global model-call ceiling reached: "
                                f"{gate.calls}/{gate.limit}"),
                        )
                    if local_model_limit is not None:
                        local_model_limit.consume()
                    gate.calls += 1
                    gate.by_role[role] = gate.by_role.get(role, 0) + 1
                    balance = (
                        balance_provider() if balance_provider is not None
                        else gate.global_balance())
                    model_input = gate._attach_balance(input, balance)
                return await self._underlying.generate(
                    input=model_input, tools=tools, tool_choice=tool_choice,
                    config=config or GenerateConfig(), cache=cache,
                )

        return _BudgetedModel()

    def snapshot(self) -> dict[str, Any]:
        return {
            "limit": self.limit,
            "calls": self.calls,
            "remaining": max(0, self.limit - self.calls),
            "by_role": dict(sorted(self.by_role.items())),
        }


def _usage_snapshot() -> dict[str, Any]:
    """Read Inspect global sample usage without exposing model responses."""
    try:
        from inspect_ai.model._model import sample_model_usage, sample_role_usage
        from inspect_ai.util import sample_limits

        limits = sample_limits()
        limit_usage = {}
        for name in ("token", "tool_call", "time", "working"):
            item = getattr(limits, name)
            try:
                usage = item.usage
            except (NotImplementedError, RuntimeError):
                usage = None
            limit_usage[name] = {
                "limit": item.limit,
                "usage": usage,
            }
        return {
            "model_usage": {
                name: usage.model_dump()
                for name, usage in sample_model_usage().items()
            },
            "role_usage": {
                name: usage.model_dump()
                for name, usage in sample_role_usage().items()
            },
            "sample_root_limits": limit_usage,
        }
    except Exception as exc:  # noqa: BLE001 - audit cannot break official run
        return {"usage_error": f"{type(exc).__name__}: {exc}"}


def create_agent(*, arm: str = "single", **factory_kwargs: Any):
    """Return the two-level factory expected by ``create_saber_solver``."""
    if arm not in {"single", "orchestrator_only", "full"}:
        raise ValueError(f"unknown arm: {arm}")
    max_dispatches = int(factory_kwargs.get("max_dispatches", 16))
    max_parallel = int(factory_kwargs.get("max_parallel_dispatches", 4))
    max_model_calls = int(factory_kwargs.get("max_model_calls", 64))
    global_tool_call_limit = int(
        factory_kwargs.get("global_tool_call_limit", 64))
    global_token_limit = int(
        factory_kwargs.get("global_token_limit", 1_000_000))
    global_time_limit = float(
        factory_kwargs.get("global_time_limit", 300.0))
    if any(value <= 0 for value in (
            max_dispatches, max_parallel, max_model_calls,
            global_tool_call_limit, global_token_limit, global_time_limit)):
        raise ValueError("resource and dispatch limits must be positive")

    def create_with_prompts(*, instruction_prompt: str = "",
                            assistant_prompt: str = "", tools=None,
                            max_steps: int = 25, **_kwargs: Any):
        official_tools = list(tools or ())

        async def solve(state, _generate):
            from inspect_ai.agent import AgentPrompt, AgentState, react
            from inspect_ai.util import store

            started = time.perf_counter()
            # SABER's task metadata keeps the pinned upstream max_steps intact.
            # CyberOrion applies one explicit global diagnostic/publication
            # tool ceiling uniformly to all three arms so coordination and
            # child official-tool calls cannot bypass accounting.
            state.tool_call_limit = global_tool_call_limit
            task_context = _task_input(state)
            _, context_audit = build_official_context(
                instruction_prompt=instruction_prompt,
                assistant_prompt=assistant_prompt,
                task_input=task_context,
            )
            contract = arm_tool_contract(arm, official_tools)
            model_gate = _GlobalModelGate(
                max_model_calls,
                token_limit_value=global_token_limit,
                tool_call_limit_value=global_tool_call_limit,
                wall_time_limit_value=global_time_limit,
            )
            shared = InvestigationState(
                hashlib.sha256(task_context.encode()).hexdigest())
            agent_state = AgentState(messages=state.messages)
            arm_addition = SINGLE_PROMPT if arm == "single" else (
                ORCHESTRATOR_ONLY_PROMPT if arm == "orchestrator_only"
                else COMMANDER_PROMPT)
            native_tools = list(official_tools)
            if arm == "full":
                controller = _DispatchController(
                    task_context=task_context,
                    instruction_prompt=instruction_prompt,
                    assistant_prompt=assistant_prompt,
                    official_tools=official_tools,
                    commander_messages=agent_state.messages,
                    max_dispatches=max_dispatches,
                    max_parallel_dispatches=max_parallel,
                    model_gate=model_gate,
                )
                shared = controller.state
                # Production V2 commander delegates investigation and does not
                # directly own environment investigation tools.
                native_tools = controller.tools()

            root_balance_provider = (
                controller.commander_balance if arm == "full" else None)

            native_agent = react(
                name=f"cyberorion_excytin_{arm}",
                prompt=AgentPrompt(
                    instructions=_combined_instructions(
                        instruction_prompt, arm_addition),
                    assistant_prompt=assistant_prompt or None,
                    handoff_prompt=None,
                ),
                tools=native_tools,
                model=model_gate.agent(
                    "single" if arm == "single" else "orchestrator",
                    balance_provider=root_balance_provider),
                submit=True,
                compaction=_native_context_compaction(),
                truncation="auto",
            )
            agent_state = await native_agent(agent_state)
            state.messages = agent_state.messages
            state.output = agent_state.output
            shared.ingest_messages(
                state.messages,
                role="single" if arm == "single" else "orchestrator",
                official_names=set(contract["official_environment_tools"]),
            )
            trace = {
                "schema": "cyberorion_excytin_native_trace_v2_resource_delegation",
                "arm": arm,
                "max_steps_from_official_task": int(max_steps),
                "global_tool_call_limit": global_tool_call_limit,
                "global_token_limit": global_token_limit,
                "global_time_limit": global_time_limit,
                "official_tool_contract": contract,
                "full_tool_topology": {
                    "commander_environment_tools": contract[
                        "commander_environment_tools"],
                    "commander_coordination_tools": (
                        contract["commander_state_tools"]
                        + contract["delegation_tools"]
                    ),
                    "worker_environment_tools": contract[
                        "worker_environment_tools"],
                    "final_answer_owner": "commander",
                    "final_answer_tool": "submit",
                },
                "context_audit": context_audit,
                "shared_investigation_state": shared.audit_snapshot(),
                "inspect_usage": _usage_snapshot(),
                "global_model_call_budget": model_gate.snapshot(),
                "final_global_resource_balance": (
                    controller.commander_balance()
                    if arm == "full" else model_gate.global_balance()),
                "wall_clock_sec": time.perf_counter() - started,
                "native_inspect_tool_execution": True,
                "custom_json_action_protocol": False,
                "output_or_context_truncation_by_bridge": False,
                "native_context_management": {
                    "strategy": "CompactionTrim",
                    "threshold_tokens": (
                        _NATIVE_CONTEXT_COMPACTION_THRESHOLD_TOKENS),
                    "preserve_fraction": _NATIVE_CONTEXT_COMPACTION_PRESERVE,
                    "raw_inspect_transcript_retained": True,
                },
                "bounded_workspace": {
                    "raw_outputs_retained_in_inspect_audit": True,
                    "model_visible_raw_transcripts": False,
                    "compacted_evidence_count": sum(
                        bool(item.get("shared_state_truncated"))
                        for item in shared.evidence
                    ),
                    "max_raw_tool_output_chars": max(
                        (int(item.get("raw_chars", 0))
                         for item in shared.evidence), default=0),
                },
            }
            store().set("cyberorion_arm", arm)
            store().set("cyberorion_runtime_trace", trace)
            store().set("cyberorion_context_audit", context_audit)
            return state

        return solve

    return create_with_prompts


def register_official_agents() -> None:
    """Register three CyberOrion arms without changing SABER itself."""
    from saber.agents import AgentRegistry

    for name, arm in (
        ("cyberorion_single", "single"),
        ("cyberorion_orchestrator_only", "orchestrator_only"),
        ("cyberorion_full", "full"),
    ):
        if AgentRegistry.get(name) is None:
            AgentRegistry.register(
                name, lambda _arm=arm, **kwargs: create_agent(
                    arm=_arm, **kwargs))


__all__ = [
    "COMMANDER_PROMPT", "InvestigationState", "ORCHESTRATOR_ONLY_PROMPT",
    "ROLE_DESCRIPTIONS", "ROLE_MISSION_CONTRACTS", "ROLES", "SINGLE_PROMPT",
    "SpecialistReport", "WorkerAllocation",
    "arm_tool_contract", "build_official_context", "create_agent",
    "official_tool_names", "parse_specialist_report",
    "register_official_agents", "specialist_prompt",
]

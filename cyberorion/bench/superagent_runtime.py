"""Auditable JSON tool-loop for the SuperAgent blue benchmark.

The runtime is deliberately small and dependency-free. The benchmark injects
already-scoped tools and an async LLM callable; this module only enforces the
shared budget, JSON decision contract, role dispatch, and audit trace.
"""

from __future__ import annotations

import inspect
import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol

ROLES = ("watcher", "analyst", "responder", "hunter")
_ACTIONS = {"tool", "dispatch", "complete"}
_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.I | re.S)
_PRIVATE_KEYS = {
    "analysis", "chain_of_thought", "cot", "reasoning",
    "reasoning_content", "thought", "thoughts", "raw",
}


class AsyncLLM(Protocol):
    async def __call__(self, **kwargs: Any) -> Any: ...


ToolHandler = Callable[..., Any]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    handler: ToolHandler
    description: str = ""
    input_schema: Mapping[str, Any] = field(
        default_factory=lambda: {"type": "object", "properties": {}}
    )
    terminal: bool = False


@dataclass(frozen=True)
class RuntimeConfig:
    max_steps: int = 30
    max_llm_calls: int = 30
    max_tool_calls: int = 24
    max_dispatches: int = 12
    max_role_steps: int = 6
    max_invalid_decisions: int = 3
    max_observation_chars: int = 4000
    max_trace_text_chars: int = 1200

    def __post_init__(self) -> None:
        for name, value in self.__dict__.items():
            if value <= 0:
                raise ValueError(f"{name} must be positive")


@dataclass
class _Budget:
    config: RuntimeConfig
    steps: int = 0
    llm_calls: int = 0
    tool_calls: int = 0
    dispatches: int = 0

    def can_call_llm(self) -> bool:
        return (self.steps < self.config.max_steps
                and self.llm_calls < self.config.max_llm_calls)

    def can_call_tool(self) -> bool:
        return self.tool_calls < self.config.max_tool_calls

    def snapshot(self) -> dict[str, Any]:
        limits = {
            "steps": self.config.max_steps,
            "llm_calls": self.config.max_llm_calls,
            "tool_calls": self.config.max_tool_calls,
            "dispatches": self.config.max_dispatches,
            "role_steps": self.config.max_role_steps,
        }
        used = {
            "steps": self.steps,
            "llm_calls": self.llm_calls,
            "tool_calls": self.tool_calls,
            "dispatches": self.dispatches,
        }
        return {
            **used,
            "limits": limits,
            "remaining": {
                key: max(0, int(limits[key]) - int(used.get(key, 0)))
                for key in ("steps", "llm_calls", "tool_calls", "dispatches")
            },
        }


def _clip(value: Any, limit: int) -> str:
    text = value if isinstance(value, str) else json.dumps(
        value, ensure_ascii=False, default=str, sort_keys=True)
    return text if len(text) <= limit else text[:limit] + f"...[{len(text)} chars]"


def _json_safe(value: Any) -> Any:
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        return str(value)


def _audit_safe(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(k): _audit_safe(v)
            for k, v in value.items()
            if str(k).lower() not in _PRIVATE_KEYS
        }
    if isinstance(value, (list, tuple, set)):
        return [_audit_safe(v) for v in value]
    return _json_safe(value)


def _normalise_tools(tools: Mapping[str, Any]) -> dict[str, ToolSpec]:
    out: dict[str, ToolSpec] = {}
    for key, raw in tools.items():
        name = str(key)
        if isinstance(raw, ToolSpec):
            out[name] = raw if raw.name == name else ToolSpec(
                name, raw.handler, raw.description, raw.input_schema,
                raw.terminal)
        elif callable(raw):
            out[name] = ToolSpec(
                name=name,
                handler=raw,
                description=str(getattr(raw, "description", "")
                                or inspect.getdoc(raw) or ""),
                input_schema=getattr(raw, "input_schema", None)
                or getattr(raw, "params_json_schema", None)
                or {"type": "object", "properties": {}},
            )
        elif callable(getattr(raw, "on_invoke_tool", None)):
            async def cai_handler(_tool: Any = raw, **kwargs: Any) -> Any:
                result = _tool.on_invoke_tool(
                    None, json.dumps(kwargs, ensure_ascii=False))
                return await result if inspect.isawaitable(result) else result

            out[name] = ToolSpec(
                name=name,
                handler=cai_handler,
                description=str(getattr(raw, "description", "") or ""),
                input_schema=getattr(raw, "params_json_schema", None)
                or {"type": "object", "properties": {}},
            )
        elif isinstance(raw, Mapping) and callable(raw.get("handler")):
            out[name] = ToolSpec(
                name=name,
                handler=raw["handler"],
                description=str(raw.get("description") or ""),
                input_schema=raw.get("input_schema") or raw.get("parameters")
                or {"type": "object", "properties": {}},
                terminal=bool(raw.get("terminal", False)),
            )
        else:
            raise TypeError(f"tool {name!r} is not callable")
    if not out:
        raise ValueError("tools must not be empty")
    return out


def _tool_schema(spec: ToolSpec) -> dict[str, Any]:
    return {
        "name": spec.name,
        "description": spec.description,
        "parameters": _json_safe(dict(spec.input_schema)),
    }


def _virtual_tool_schemas(allow_dispatch: bool) -> list[dict[str, Any]]:
    rows = []
    if allow_dispatch:
        rows.append({
            "name": "dispatch_task",
            "description": "dispatch watcher/analyst/responder/hunter",
            "parameters": {
                "type": "object",
                "properties": {
                    "role": {"type": "string", "enum": list(ROLES)},
                    "mission": {"type": "string"},
                },
                "required": ["role", "mission"],
            },
        })
    rows.append({
        "name": "task_complete",
        "description": "submit final summary",
        "parameters": {
            "type": "object",
            "properties": {"summary": {"type": ["string", "object"]}},
            "required": ["summary"],
        },
    })
    return rows


def _system_prompt(role: str, allow_dispatch: bool) -> str:
    duties = {
        "reference": "You are the single reference blue-team agent.",
        "orchestrator": "You are the CyberOrion blue-team commander.",
        "watcher": "You perform broad telemetry review.",
        "analyst": "You correlate evidence and reconstruct chains.",
        "responder": "You perform minimal response and read failures.",
        "hunter": "You hunt residual compromise and verify cleanup.",
    }
    dispatch = "You may dispatch roles." if allow_dispatch else "You may not dispatch roles."
    return (
        f"{duties.get(role, duties['reference'])}\n{dispatch}\n"
        "Return exactly one JSON object per step. Do not include hidden "
        "reasoning. Shape: {\"hypothesis\":\"...\",\"evidence_ids\":[],"
        "\"action\":{\"type\":\"tool|dispatch|complete\",\"tool\":\"name\","
        "\"arguments\":{},\"role\":\"watcher\",\"mission\":\"...\","
        "\"summary\":\"...\"},\"replan_reason\":\"...\"}. Tools can fail; "
        "observe failures and adapt or report them honestly."
    )


def _extract_value(raw: Any) -> Any:
    if isinstance(raw, (str, bytes, Mapping)):
        return raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
    choices = getattr(raw, "choices", None)
    if choices:
        message = getattr(choices[0], "message", None)
        calls = getattr(message, "tool_calls", None) or []
        if calls:
            fn = getattr(calls[0], "function", None)
            name = str(getattr(fn, "name", "") or "")
            try:
                args = json.loads(getattr(fn, "arguments", "{}") or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                args = {}
            if name == "dispatch_task":
                return {"action": {"type": "dispatch", **args}}
            if name == "task_complete":
                return {"action": {"type": "complete", **args}}
            return {"action": {"type": "tool", "tool": name, "arguments": args}}
        return getattr(message, "content", "") or ""
    return getattr(raw, "content", raw)


def _parse_decision(raw: Any, config: RuntimeConfig) -> dict[str, Any]:
    value = _extract_value(raw)
    if isinstance(value, Mapping):
        parsed = dict(value)
    else:
        text = str(value or "").strip()
        match = _JSON_FENCE_RE.search(text)
        text = match.group(1) if match else text
        if not text.startswith("{"):
            start, end = text.find("{"), text.rfind("}")
            text = text[start:end + 1] if start >= 0 and end > start else text
        parsed = json.loads(text)
    action = parsed.get("action")
    if isinstance(action, str):
        action = {"type": action}
    elif not isinstance(action, Mapping):
        action = {k: parsed[k] for k in (
            "type", "tool", "arguments", "role", "mission", "summary")
            if k in parsed}
    action = dict(action or {})
    kind = str(action.get("type") or "").lower()
    aliases = {"call_tool": "tool", "tool_call": "tool",
               "dispatch_task": "dispatch", "finish": "complete",
               "task_complete": "complete"}
    kind = aliases.get(kind, kind)
    if not kind:
        kind = "tool" if action.get("tool") else (
            "dispatch" if action.get("role") else "complete")
    # Some OpenAI-compatible providers serialize native virtual-tool calls as
    # JSON actions instead of returning ``message.tool_calls``.  Treat those
    # representations identically; otherwise the runtime tries to invoke a
    # non-existent injected tool named task_complete/dispatch_task (the exact
    # failure recorded in the 20260825_234948 SecAlertBench smoke).
    virtual_tool = str(action.get("tool") or "").lower()
    if kind == "tool" and virtual_tool in {"task_complete", "dispatch_task"}:
        args = action.get("arguments") or {}
        args = dict(args) if isinstance(args, Mapping) else {}
        if virtual_tool == "task_complete":
            kind = "complete"
            action = {"type": "complete", "summary": args.get("summary", args)}
        else:
            kind = "dispatch"
            action = {
                "type": "dispatch",
                "role": args.get("role", action.get("role", "")),
                "mission": args.get("mission", action.get("mission", "")),
            }
    if kind not in _ACTIONS:
        raise ValueError(f"unknown action type: {kind}")
    if kind == "tool":
        args = action.get("arguments") or {}
        action = {
            "type": "tool",
            "tool": str(action.get("tool") or ""),
            "arguments": dict(args) if isinstance(args, Mapping) else {"value": args},
        }
    elif kind == "dispatch":
        action = {
            "type": "dispatch",
            "role": str(action.get("role") or ""),
            "mission": _clip(action.get("mission") or "", config.max_trace_text_chars),
        }
    else:
        action = {"type": "complete", "summary": _audit_safe(action.get("summary") or "")}
    evidence = parsed.get("evidence_ids") or []
    if not isinstance(evidence, (list, tuple, set)):
        evidence = [evidence]
    return {
        "hypothesis": _clip(parsed.get("hypothesis") or "", config.max_trace_text_chars),
        "evidence_ids": [_clip(item, 200) for item in list(evidence)[:100]],
        "action": _json_safe(action),
        "replan_reason": _clip(parsed.get("replan_reason") or "", config.max_trace_text_chars),
    }


async def _invoke_llm(llm: Any, *, messages: list[dict[str, Any]],
                      tools: list[dict[str, Any]], role: str,
                      budget: dict[str, Any]) -> Any:
    target = getattr(llm, "complete", None) or llm
    if not callable(target):
        raise TypeError("llm must be callable")
    request = {"messages": messages, "tools": tools, "role": role, "budget": budget}
    try:
        sig = inspect.signature(target)
        params = sig.parameters
    except (TypeError, ValueError):
        result = target(**request)
    else:
        names = set(params)
        has_kwargs = any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())
        if {"system", "user"}.issubset(names):
            result = target(str(messages[0].get("content") if messages else ""),
                            json.dumps({
                                "role": role, "conversation": messages[1:],
                                "available_tools": tools, "budget": budget,
                            }, ensure_ascii=False))
        elif has_kwargs or names & set(request):
            result = target(**request) if has_kwargs else target(
                **{k: v for k, v in request.items() if k in names})
        else:
            result = target(request)
    return await result if inspect.isawaitable(result) else result


async def _call_tool(spec: ToolSpec, arguments: Mapping[str, Any]) -> tuple[str, str]:
    try:
        result = spec.handler(**dict(arguments))
        if inspect.isawaitable(result):
            result = await result
        return "ok", _clip(result, 1_000_000)
    except Exception as exc:  # noqa: BLE001
        return "error", f"ERROR[{type(exc).__name__}]: {exc}"


@dataclass
class _State:
    llm: Any
    tools: dict[str, ToolSpec]
    config: RuntimeConfig
    budget: _Budget
    role_tools: dict[str, set[str]]
    decision_trace: list[dict[str, Any]] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    role_events: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    invalid_decisions: int = 0

    def available(self, role: str) -> dict[str, ToolSpec]:
        names = self.role_tools.get(role)
        return self.tools if names is None else {
            key: tool for key, tool in self.tools.items() if key in names}

    def trace(self, role: str, event: str, decision: Mapping[str, Any] | None,
              observation: Any) -> None:
        decision = decision or {}
        self.decision_trace.append({
            "seq": len(self.decision_trace) + 1,
            "role": role,
            "event": event,
            "hypothesis": str(decision.get("hypothesis") or ""),
            "evidence_ids": list(decision.get("evidence_ids") or []),
            "action": _audit_safe(decision.get("action") or {}),
            "observation": _clip(observation, self.config.max_observation_chars),
            "replan_reason": str(decision.get("replan_reason") or ""),
        })


def _role_access(tools: Mapping[str, ToolSpec],
                 role_tools: Mapping[str, Any] | None) -> dict[str, set[str]]:
    all_names = set(tools)
    access = {"reference": set(all_names), "orchestrator": set(all_names)}
    if role_tools:
        for role, names in role_tools.items():
            selected = set(map(str, names))
            unknown = selected - all_names
            if unknown:
                raise ValueError(f"unknown tools for {role}: {sorted(unknown)}")
            access[role] = selected
    for role in ROLES:
        access.setdefault(role, set(all_names))
    if access["reference"] != set().union(*(access[r] for r in ("orchestrator", *ROLES))):
        raise ValueError("reference and superagent tool union must match")
    return access


async def _run_role(state: _State, *, role: str, task: str,
                    allow_dispatch: bool) -> tuple[str, str]:
    tools = state.available(role)
    schemas = [_tool_schema(spec) for spec in tools.values()]
    schemas.extend(_virtual_tool_schemas(allow_dispatch))
    messages = [
        {"role": "system", "content": _system_prompt(role, allow_dispatch)},
        {"role": "user", "content": task},
    ]
    local_steps = 0
    while state.budget.can_call_llm():
        if role in ROLES and local_steps >= state.config.max_role_steps:
            summary = f"{role} reached role step budget"
            state.trace(role, "role_budget_exhausted", None, summary)
            return "budget_exhausted", summary
        local_steps += 1
        state.budget.steps += 1
        state.budget.llm_calls += 1
        try:
            raw = await _invoke_llm(
                state.llm, messages=messages, tools=schemas, role=role,
                budget=state.budget.snapshot())
            decision = _parse_decision(raw, state.config)
        except Exception as exc:  # noqa: BLE001
            state.invalid_decisions += 1
            err = f"INVALID_LLM_DECISION[{type(exc).__name__}]: {exc}"
            state.errors.append(err)
            state.trace(role, "invalid_decision", None, err)
            if state.invalid_decisions >= state.config.max_invalid_decisions:
                return "model_error", err
            messages.append({"role": "user", "content": err})
            continue
        action = decision["action"]
        kind = action["type"]
        if kind == "complete":
            summary = _clip(action.get("summary") or "", state.config.max_trace_text_chars)
            state.trace(role, "complete", decision, summary)
            return "complete", summary
        if kind == "tool":
            name = str(action.get("tool") or "")
            if name not in tools:
                status, observation = "error", f"ERROR[ToolNotAvailable]: {name}"
            elif not state.budget.can_call_tool():
                status, observation = "error", "ERROR[BudgetExceeded]: tool budget exhausted"
            else:
                state.budget.tool_calls += 1
                status, observation = await _call_tool(
                    tools[name], action.get("arguments") or {})
            call = {
                "seq": len(state.tool_calls) + 1,
                "role": role,
                "tool": name,
                "arguments": _audit_safe(action.get("arguments") or {}),
                "status": status,
                "output": _clip(observation, state.config.max_observation_chars),
            }
            state.tool_calls.append(call)
            state.trace(role, "tool", decision, observation)
            if status == "ok" and tools[name].terminal:
                return "complete", observation
            messages.append({"role": "assistant", "content": json.dumps(decision, ensure_ascii=False)})
            messages.append({"role": "tool", "name": name, "content": observation})
            continue
        if not allow_dispatch:
            observation = "ERROR[DispatchNotAllowed]"
            state.trace(role, "dispatch_error", decision, observation)
            messages.append({"role": "user", "content": observation})
            continue
        target_role = str(action.get("role") or "")
        mission = str(action.get("mission") or "")
        if target_role not in ROLES:
            observation = f"ERROR[InvalidRole]: {target_role}"
            state.trace(role, "dispatch_error", decision, observation)
            messages.append({"role": "user", "content": observation})
            continue
        if state.budget.dispatches >= state.config.max_dispatches:
            observation = "ERROR[BudgetExceeded]: dispatch budget exhausted"
            state.trace(role, "dispatch_error", decision, observation)
            messages.append({"role": "user", "content": observation})
            continue
        state.budget.dispatches += 1
        state.role_events.append({
            "event": "spawn", "role": target_role, "mission": mission,
            "seq": len(state.role_events) + 1,
        })
        status, report = await _run_role(
            state, role=target_role, task=mission, allow_dispatch=False)
        state.role_events.append({
            "event": "done", "role": target_role, "mission": mission,
            "status": status, "report": _clip(report, state.config.max_trace_text_chars),
            "seq": len(state.role_events) + 1,
        })
        observation = f"{target_role} {status}: {report}"
        state.trace(role, "dispatch", decision, observation)
        messages.append({"role": "assistant", "content": json.dumps(decision, ensure_ascii=False)})
        messages.append({"role": "tool", "name": "dispatch_task", "content": observation})
    summary = "global budget exhausted"
    state.trace(role, "budget_exhausted", None, summary)
    return "budget_exhausted", summary


async def run_arm(
    mode: str,
    *,
    task: str,
    llm: AsyncLLM | Any,
    tools: Mapping[str, ToolSpec | ToolHandler | Mapping[str, Any]],
    config: RuntimeConfig | None = None,
    role_tools: Mapping[str, list[str] | tuple[str, ...] | set[str]] | None = None,
) -> dict[str, Any]:
    if mode not in {"reference", "orchestrator_only", "superagent"}:
        raise ValueError("mode must be reference, orchestrator_only or superagent")
    config = config or RuntimeConfig()
    specs = _normalise_tools(tools)
    state = _State(llm, specs, config, _Budget(config), _role_access(specs, role_tools))
    role = "reference" if mode == "reference" else "orchestrator"
    status, output = await _run_role(
        state, role=role, task=task, allow_dispatch=mode == "superagent")
    return {
        "mode": mode,
        "status": status,
        "output": output,
        "decision_trace": state.decision_trace,
        "tool_calls": state.tool_calls,
        "role_events": state.role_events,
        "budget": state.budget.snapshot(),
        "errors": state.errors,
        "valid": status != "model_error",
    }


async def run_reference(**kwargs: Any) -> dict[str, Any]:
    return await run_arm("reference", **kwargs)


async def run_superagent(**kwargs: Any) -> dict[str, Any]:
    return await run_arm("superagent", **kwargs)


async def run_orchestrator_only(**kwargs: Any) -> dict[str, Any]:
    return await run_arm("orchestrator_only", **kwargs)


__all__ = [
    "AsyncLLM", "ROLES", "RuntimeConfig", "ToolHandler", "ToolSpec",
    "run_arm", "run_orchestrator_only", "run_reference", "run_superagent",
]

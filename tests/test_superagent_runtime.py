"""SUPER-AGENT benchmark runtime 审计轨迹与共享预算测试。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from cyberorion.bench.superagent_runtime import (
    RuntimeConfig, ToolSpec, run_orchestrator_only, run_reference,
    run_superagent,
)


def test_reference_records_real_tool_call() -> None:
    async def llm(**request):
        messages = request["messages"]
        if not any(m.get("role") == "tool" for m in messages):
            return {"action": {"type": "tool", "tool": "read", "arguments": {}}}
        return {"action": {"type": "complete", "summary": {"verdict": "attack"}}}

    result = asyncio.run(run_reference(
        task="triage", llm=llm, tools={"read": lambda: "E1 malicious"},
        config=RuntimeConfig(max_steps=4, max_llm_calls=4, max_tool_calls=2,
                             max_dispatches=1, max_role_steps=2)))
    assert result["status"] == "complete"
    assert result["tool_calls"][0]["tool"] == "read"
    assert result["decision_trace"][0]["event"] == "tool"


def test_terminal_tool_completes_without_redundant_llm_call() -> None:
    calls = 0

    async def llm(**_request):
        nonlocal calls
        calls += 1
        return {"action": {"type": "tool", "tool": "select",
                           "arguments": {"action_id": 2}}}

    result = asyncio.run(run_reference(
        task="choose", llm=llm,
        tools={"select": ToolSpec(
            "select", lambda action_id: f"selected {action_id}", terminal=True)},
        config=RuntimeConfig(max_steps=3, max_llm_calls=3, max_tool_calls=2,
                             max_dispatches=1, max_role_steps=1)))
    assert calls == 1
    assert result["status"] == "complete"
    assert result["output"] == "selected 2"
    assert result["budget"]["llm_calls"] == 1
    assert result["budget"]["tool_calls"] == 1


def test_terminal_required_rejects_task_complete_and_retries_selector() -> None:
    calls = 0

    async def llm(**request):
        nonlocal calls
        calls += 1
        names = {tool["name"] for tool in request["tools"]}
        assert "task_complete" not in names
        if calls == 1:
            return {"action": {"type": "complete", "summary": "select 2"}}
        assert "TerminalToolRequired" in str(request["messages"])
        return {"action": {"type": "tool", "tool": "select",
                           "arguments": {"action_id": 2}}}

    result = asyncio.run(run_reference(
        task="choose", llm=llm,
        tools={"select": ToolSpec(
            "select", lambda action_id: f"selected {action_id}", terminal=True)},
        config=RuntimeConfig(max_steps=3, max_llm_calls=3, max_tool_calls=2,
                             max_dispatches=1, max_role_steps=1,
                             require_terminal_tool=True)))
    assert calls == 2
    assert result["status"] == "complete"
    assert [row["event"] for row in result["decision_trace"]] == [
        "complete_rejected", "tool"]
    assert result["output"] == "selected 2"


def test_cage_single_contract_is_tool_only() -> None:
    async def llm(**request):
        assert [tool["name"] for tool in request["tools"]] == ["select"]
        prompt = request["messages"][0]["content"]
        assert '{"type":"tool","tool":' in prompt
        assert '"type":"dispatch"' not in prompt
        assert '"type":"complete"' not in prompt
        return {"action": {"type": "tool", "tool": "select",
                           "arguments": {"action_id": 1}}}

    result = asyncio.run(run_reference(
        task="choose", llm=llm,
        tools={"select": ToolSpec("select", lambda action_id: action_id,
                                  terminal=True)},
        config=RuntimeConfig(require_terminal_tool=True)))
    assert result["status"] == "complete"


def test_cage_full_contracts_match_orchestrator_and_specialist_permissions() -> None:
    seen = {}

    async def llm(**request):
        role = request["role"]
        seen.setdefault(role, []).append({
            "tools": [tool["name"] for tool in request["tools"]],
            "prompt": request["messages"][0]["content"],
        })
        if role == "orchestrator" and len(seen[role]) == 1:
            return {"action": {"type": "dispatch", "role": "watcher",
                               "mission": "inspect"}}
        if role == "watcher":
            return {"action": {"type": "complete", "summary": "clear"}}
        return {"action": {"type": "tool", "tool": "select",
                           "arguments": {"action_id": 1}}}

    result = asyncio.run(run_superagent(
        task="choose", llm=llm,
        tools={"select": ToolSpec("select", lambda action_id: action_id,
                                  terminal=True)},
        role_tools={role: () for role in (
            "watcher", "analyst", "responder", "hunter")},
        config=RuntimeConfig(max_steps=4, max_llm_calls=4,
                             require_terminal_tool=True)))
    assert seen["orchestrator"][0]["tools"] == ["select", "dispatch_task"]
    assert '{"type":"tool","tool":' in seen["orchestrator"][0]["prompt"]
    assert '"select"' in seen["orchestrator"][0]["prompt"]
    assert "authorized_tool_name" not in seen["orchestrator"][0]["prompt"]
    assert '{"type":"dispatch","role":' in seen["orchestrator"][0]["prompt"]
    assert '"type":"complete"' not in seen["orchestrator"][0]["prompt"]
    assert seen["watcher"][0]["tools"] == ["task_complete"]
    specialist_contract = seen["watcher"][0]["prompt"].split(
        "permitted variants", 1)[1]
    assert '{"type":"complete","summary":' in specialist_contract
    assert '"type":"tool"' not in specialist_contract
    assert '"type":"dispatch"' not in specialist_contract
    assert '"role"' not in specialist_contract
    assert '"mission"' not in specialist_contract
    assert '"type":"tool"' not in specialist_contract
    assert result["status"] == "complete"


def test_model_visible_cage_tool_contract_uses_real_orchestrator_tool() -> None:
    seen: dict[str, str] = {}

    async def llm(**request):
        role = request["role"]
        if role == "orchestrator":
            seen[role] = request["messages"][0]["content"]
            return {"action": {"type": "tool", "tool": "select_blue_action",
                                "arguments": {"action_id": 2}}}
        return {"action": {"type": "complete", "summary": "done"}}

    result = asyncio.run(run_superagent(
        task="choose", llm=llm,
        tools={"select_blue_action": ToolSpec(
            "select_blue_action", lambda action_id: f"selected {action_id}",
            input_schema={"type": "object", "properties": {
                "action_id": {"type": "integer"}},
                "required": ["action_id"], "additionalProperties": False},
            terminal=True)},
        role_tools={role: () for role in (
            "watcher", "analyst", "responder", "hunter")},
        config=RuntimeConfig(require_terminal_tool=True)))
    prompt = seen["orchestrator"]
    assert "select_blue_action" in prompt
    assert "authorized_tool_name" not in prompt
    assert '"type":"tool"' in prompt
    assert '"type":"dispatch"' in prompt
    assert '"type":"complete"' not in prompt
    assert result["status"] == "complete"


def test_model_visible_specialist_without_real_tools_has_no_tool_action() -> None:
    seen: dict[str, str] = {}

    async def llm(**request):
        seen[request["role"]] = request["messages"][0]["content"]
        if request["role"] == "orchestrator":
            if not any(message.get("name") == "dispatch_task"
                       for message in request["messages"]):
                return {"action": {"type": "dispatch", "role": "watcher",
                                    "mission": "inspect"}}
            return {"action": {"type": "tool", "tool": "select_blue_action",
                                "arguments": {"action_id": 0}}}
        return {"action": {"type": "complete", "summary": "done"}}

    result = asyncio.run(run_superagent(
        task="defend", llm=llm, tools={"select_blue_action": ToolSpec(
            "select_blue_action", lambda action_id: action_id, terminal=True)},
        role_tools={role: () for role in (
            "watcher", "analyst", "responder", "hunter")},
        config=RuntimeConfig(max_steps=4, max_llm_calls=4,
                             max_dispatches=1, max_role_steps=1)))
    prompt = seen["watcher"]
    assert '"type":"tool"' not in prompt
    assert "actual_real_tools" not in prompt
    assert '"type":"complete"' in prompt
    assert result["status"] == "complete"


def test_model_visible_generic_multi_tool_contract_lists_only_visible_tools() -> None:
    seen: dict[str, str] = {}

    async def llm(**request):
        seen["prompt"] = request["messages"][0]["content"]
        return {"action": {"type": "complete", "summary": "done"}}

    result = asyncio.run(run_reference(
        task="triage", llm=llm,
        tools={
            "read_alert": ToolSpec(
                "read_alert", lambda alert_id: alert_id,
                input_schema={"type": "object", "properties": {
                    "alert_id": {"type": "string"}},
                    "required": ["alert_id"]}),
            "list_hosts": ToolSpec(
                "list_hosts", lambda: [],
                input_schema={"type": "object", "properties": {}}),
        },
        config=RuntimeConfig(max_steps=2, max_llm_calls=2,
                             max_tool_calls=1, max_dispatches=1,
                             max_role_steps=1)))
    prompt = seen["prompt"]
    assert "read_alert" in prompt
    assert "list_hosts" in prompt
    assert "authorized_tool_name" not in prompt
    assert "tool_name" not in prompt
    assert "example_tool" not in prompt
    assert result["status"] == "complete"


def test_excytin_like_specialist_contract_is_tool_and_complete() -> None:
    seen = {}

    async def llm(**request):
        role = request["role"]
        seen[role] = request
        if role == "orchestrator" and not any(
                message.get("name") == "dispatch_task"
                for message in request["messages"]):
            return {"action": {"type": "dispatch", "role": "analyst",
                               "mission": "query"}}
        return {"action": {"type": "complete", "summary": "done"}}

    asyncio.run(run_superagent(
        task="investigate", llm=llm, tools={"query_sql": lambda: "row"},
        role_tools={"analyst": ("query_sql",)},
        config=RuntimeConfig(max_steps=3, max_llm_calls=3)))
    request = seen["analyst"]
    assert [tool["name"] for tool in request["tools"]] == [
        "query_sql", "task_complete"]
    prompt = request["messages"][0]["content"]
    assert '{"type":"tool","tool":' in prompt
    assert '{"type":"complete","summary":' in prompt
    assert '"type":"dispatch"' not in prompt


def test_forbidden_specialist_dispatch_still_fails_closed() -> None:
    watcher_calls = 0

    async def llm(**request):
        nonlocal watcher_calls
        if request["role"] == "orchestrator" and not any(
                message.get("name") == "dispatch_task"
                for message in request["messages"]):
            return {"action": {"type": "dispatch", "role": "watcher",
                               "mission": "inspect"}}
        if request["role"] == "watcher":
            watcher_calls += 1
            if watcher_calls == 1:
                return {"action": {"type": "dispatch", "role": "analyst",
                                   "mission": "illegal"}}
            return {"action": {"type": "complete", "summary": "repaired"}}
        return {"action": {"type": "complete", "summary": "done"}}

    result = asyncio.run(run_superagent(
        task="defend", llm=llm, tools={"read": lambda: "ok"},
        role_tools={role: () for role in (
            "watcher", "analyst", "responder", "hunter")},
        config=RuntimeConfig(max_steps=5, max_llm_calls=5,
                             max_role_steps=2)))
    errors = [row for row in result["decision_trace"]
              if row["event"] == "dispatch_error"]
    assert len(errors) == 1
    assert "DispatchNotAllowed" in errors[0]["observation"]
    assert result["budget"]["dispatches"] == 1


def test_orchestrator_only_uses_commander_path_without_dispatch() -> None:
    async def llm(**request):
        assert request["role"] == "orchestrator"
        assert all(tool["name"] != "dispatch_task" for tool in request["tools"])
        return {"action": {"type": "complete", "summary": "done"}}

    result = asyncio.run(run_orchestrator_only(
        task="choose", llm=llm, tools={"read": lambda: "ok"},
        config=RuntimeConfig(max_steps=2, max_llm_calls=2, max_tool_calls=1,
                             max_dispatches=1, max_role_steps=1)))
    assert result["mode"] == "orchestrator_only"
    assert result["role_events"] == []


def test_cage_orchestrator_only_contract_is_tool_only() -> None:
    async def llm(**request):
        assert request["role"] == "orchestrator"
        assert [tool["name"] for tool in request["tools"]] == ["select"]
        prompt = request["messages"][0]["content"]
        assert '{"type":"tool","tool":' in prompt
        assert '"type":"dispatch"' not in prompt
        assert '"type":"complete"' not in prompt
        return {"action": {"type": "tool", "tool": "select",
                           "arguments": {"action_id": 1}}}

    result = asyncio.run(run_orchestrator_only(
        task="choose", llm=llm,
        tools={"select": ToolSpec("select", lambda action_id: action_id,
                                  terminal=True)},
        config=RuntimeConfig(require_terminal_tool=True)))
    assert result["status"] == "complete"


def test_superagent_dispatch_is_runtime_event_and_uses_shared_budget() -> None:
    async def llm(**request):
        role = request["role"]
        messages = request["messages"]
        if role == "orchestrator" and not any(
                m.get("name") == "dispatch_task" for m in messages):
            return {"action": {"type": "dispatch", "role": "watcher",
                               "mission": "inspect"}}
        return {"action": {"type": "complete", "summary": "done"}}

    result = asyncio.run(run_superagent(
        task="defend", llm=llm, tools={"read": lambda: "E1"},
        config=RuntimeConfig(max_steps=5, max_llm_calls=5, max_tool_calls=2,
                             max_dispatches=2, max_role_steps=2)))
    assert [e["event"] for e in result["role_events"]] == ["spawn", "done"]
    assert result["budget"]["dispatches"] == 1
    assert result["budget"]["llm_calls"] == 3


def test_specialist_cannot_invoke_orchestrator_only_terminal_tool() -> None:
    attempts = 0

    async def llm(**request):
        nonlocal attempts
        role = request["role"]
        messages = request["messages"]
        if role == "orchestrator" and not any(
                message.get("name") == "dispatch_task" for message in messages):
            return {"action": {"type": "dispatch", "role": "watcher",
                               "mission": "analyze"}}
        if role == "watcher" and not any(
                "ToolNotAvailable" in str(message.get("content"))
                for message in messages):
            attempts += 1
            return {"action": {"type": "tool", "tool": "select",
                               "arguments": {"action_id": 2}}}
        if role == "watcher":
            return {"action": {"type": "complete", "summary": "analysis only"}}
        return {"action": {"type": "tool", "tool": "select",
                           "arguments": {"action_id": 2}}}

    selected = []
    result = asyncio.run(run_superagent(
        task="choose", llm=llm,
        tools={"select": ToolSpec(
            "select", lambda action_id: selected.append(action_id) or "selected",
            terminal=True)},
        role_tools={role: () for role in (
            "watcher", "analyst", "responder", "hunter")},
        config=RuntimeConfig(max_steps=6, max_llm_calls=6, max_tool_calls=2,
                             max_dispatches=1, max_role_steps=3)))
    assert attempts == 1
    assert selected == [2]
    assert result["tool_calls"][0]["role"] == "watcher"
    assert result["tool_calls"][0]["status"] == "error"
    assert result["tool_calls"][-1]["role"] == "orchestrator"
    assert result["tool_calls"][-1]["status"] == "ok"


def test_json_virtual_task_complete_matches_20260825_failure_shape() -> None:
    async def llm(**_request):
        return {"action": {"type": "tool", "tool": "task_complete",
                           "arguments": {"verdict": "attack",
                                         "attack_probability": .9}}}

    result = asyncio.run(run_reference(
        task="triage", llm=llm, tools={"get_alert": lambda: {}},
        config=RuntimeConfig(max_steps=2, max_llm_calls=2, max_tool_calls=1,
                             max_dispatches=1, max_role_steps=1)))
    assert result["status"] == "complete"
    assert result["output"] == '{"attack_probability": 0.9, "verdict": "attack"}'
    assert not result["tool_calls"]
    assert "ToolNotAvailable" not in str(result)


def test_json_virtual_dispatch_task_is_dispatched_not_called_as_tool() -> None:
    calls = 0

    async def llm(**request):
        nonlocal calls
        calls += 1
        if request["role"] == "orchestrator" and calls == 1:
            return {"action": {"type": "tool", "tool": "dispatch_task",
                               "arguments": {"role": "watcher", "mission": "inspect"}}}
        return {"action": {"type": "tool", "tool": "task_complete",
                           "arguments": {"summary": "done"}}}

    result = asyncio.run(run_superagent(
        task="triage", llm=llm, tools={"get_alert": lambda: {}},
        config=RuntimeConfig(max_steps=4, max_llm_calls=4, max_tool_calls=1,
                             max_dispatches=1, max_role_steps=2)))
    assert result["status"] == "complete"
    assert [event["event"] for event in result["role_events"]] == ["spawn", "done"]
    assert not result["tool_calls"]


def test_native_virtual_tool_call_representation_is_preserved() -> None:
    async def llm(**_request):
        function = SimpleNamespace(
            name="task_complete",
            arguments='{"summary":{"verdict":"benign","attack_probability":0.1}}')
        message = SimpleNamespace(tool_calls=[SimpleNamespace(function=function)], content=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    result = asyncio.run(run_reference(
        task="triage", llm=llm, tools={"get_alert": lambda: {}},
        config=RuntimeConfig(max_steps=2, max_llm_calls=2, max_tool_calls=1,
                             max_dispatches=1, max_role_steps=1)))
    assert result["status"] == "complete"
    assert '"verdict": "benign"' in result["output"]
    assert not result["tool_calls"]

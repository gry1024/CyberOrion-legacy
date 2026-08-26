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

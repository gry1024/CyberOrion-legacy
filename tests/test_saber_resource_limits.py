"""Regression tests for the pinned SABER resource-limit correctness patch."""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("inspect_ai")
pytest.importorskip("saber")

from inspect_ai import Task, eval
from inspect_ai.dataset import Sample
from inspect_ai.event import ModelEvent, SampleLimitEvent
from inspect_ai.model import ChatMessageAssistant, ModelOutput, ModelUsage, get_model
from inspect_ai.util import LimitExceededError
from inspect_ai.util._limit import check_tool_call_limit, record_tool_call_usage
from saber.agents.solver_factory import create_saber_solver


def _output(content: str = "fallback", *, tokens: int = 2) -> ModelOutput:
    output = ModelOutput.from_message(ChatMessageAssistant(content=content))
    output.usage = ModelUsage(
        input_tokens=tokens - 1, output_tokens=1, total_tokens=tokens)
    return output


def _factory(agent_solver: Callable[..., Any]):
    def agent_factory(**_factory_kwargs: Any):
        def create_with_prompts(**_prompt_kwargs: Any):
            return agent_solver
        return create_with_prompts
    return agent_factory


def _run(agent_solver, *, model_callback, tool_limit=10, token_limit=100):
    log_dir = Path(
        "/tmp/cyberorion_cage_runs/excytin_limit_fix_20260828/regression")
    log_dir.mkdir(parents=True, exist_ok=True)
    solver = create_saber_solver(
        "react", _factory(agent_solver))
    task = Task(
        dataset=[Sample(
            input="offline limit test",
            metadata={"max_steps": tool_limit},
        )],
        solver=solver,
        sandbox="local",
        tool_call_limit=tool_limit,
        token_limit=token_limit,
    )
    model = get_model(
        "mockllm/model", custom_outputs=model_callback, memoize=False)
    log = eval(
        task, model=model, log_dir=str(log_dir), display="none")[0]
    return log.samples[0]


def test_tool_limit_rejects_extra_tool_and_runs_one_tool_free_fallback() -> None:
    tool_executions = 0
    fallback_tool_sets: list[list[str]] = []

    async def agent_solver(state, _generate):
        nonlocal tool_executions
        record_tool_call_usage(1)
        check_tool_call_limit()
        tool_executions += 1

        # Inspect records the attempted call before checking the hard limit.
        record_tool_call_usage(1)
        check_tool_call_limit()
        tool_executions += 1  # pragma: no cover - must never execute
        return state

    def fallback(_messages, tools, _tool_choice, _config):
        fallback_tool_sets.append([tool.name for tool in tools])
        return _output()

    sample = _run(
        agent_solver, model_callback=fallback, tool_limit=1,
        token_limit=100)

    assert sample.error is None
    assert tool_executions == 1
    assert fallback_tool_sets == [[]]
    assert sum(isinstance(event, ModelEvent) for event in sample.events) == 1


@pytest.mark.parametrize("limit_type", [
    "token", "time", "working", "cost", "message", "operator", "custom",
    "future_limit",
])
def test_non_tool_limits_propagate_without_fallback(limit_type: str) -> None:
    model_calls = 0

    async def agent_solver(state, _generate):
        if limit_type == "token":
            await get_model().generate(input=state.messages, tools=[])
            raise AssertionError("token limit should terminate the generation")
        raise LimitExceededError(  # type: ignore[arg-type]
            limit_type, value=2, limit=1,
            message=f"deterministic {limit_type} limit")

    def fallback(_messages, _tools, _tool_choice, _config):
        nonlocal model_calls
        model_calls += 1
        return _output(tokens=11 if limit_type == "token" else 2)

    sample = _run(
        agent_solver, model_callback=fallback, tool_limit=10,
        token_limit=10 if limit_type == "token" else 100)

    assert model_calls == (1 if limit_type == "token" else 0)
    model_event_indexes = [
        index for index, event in enumerate(sample.events)
        if isinstance(event, ModelEvent)
    ]
    if limit_type == "future_limit":
        # The patched SABER solver propagates this unknown type unchanged.
        # Pinned Inspect then fails closed because its persisted limit schema
        # intentionally accepts only its known Literal values.
        assert sample.error is not None
        assert sample.limit is None
        assert model_event_indexes == []
        return
    assert sample.error is None
    assert sample.limit is not None
    assert sample.limit.type == limit_type
    limit_events = [
        event for event in sample.events
        if isinstance(event, SampleLimitEvent)
    ]
    if limit_type == "token":
        assert [(event.type, event.limit) for event in limit_events] == [
            ("token", 10)]
        limit_index = next(
            index for index, event in enumerate(sample.events)
            if isinstance(event, SampleLimitEvent) and event.type == "token"
        )
        assert len(model_event_indexes) == 1
        assert model_event_indexes[0] < limit_index
        assert not any(
            isinstance(event, ModelEvent)
            for event in sample.events[limit_index + 1:]
        )
    else:
        assert model_event_indexes == []


def test_fallback_hard_limit_propagates_without_second_fallback() -> None:
    fallback_calls = 0

    async def agent_solver(_state, _generate):
        raise LimitExceededError(
            "tool_call", value=2, limit=1,
            message="deterministic tool-call limit")

    def fallback(_messages, _tools, _tool_choice, _config):
        nonlocal fallback_calls
        fallback_calls += 1
        raise LimitExceededError(
            "token", value=11, limit=10,
            message="fallback token limit")

    sample = _run(
        agent_solver, model_callback=fallback, tool_limit=10,
        token_limit=100)

    assert fallback_calls == 1
    assert sample.error is None
    assert sample.limit is not None
    assert sample.limit.type == "token"
    # Exactly the one permitted tool-free fallback was attempted. Its own hard
    # limit propagated; the handler did not recurse into another generation.
    assert sum(isinstance(event, ModelEvent) for event in sample.events) == 1

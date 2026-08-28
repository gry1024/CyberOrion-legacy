#!/usr/bin/env python
"""Deterministically reproduce pinned Inspect resource-limit identities.

Run with ``benchmarks/external/excytin/.venv/bin/python``. This script is
offline: it does not call a provider, start ExCyTIn, or invoke a scorer.
"""
from __future__ import annotations

import json
from typing import Any, Callable

from inspect_ai.model import ModelUsage
from inspect_ai.util import LimitExceededError, token_limit, tool_call_limit
from inspect_ai.util._limit import (
    check_token_limit,
    check_tool_call_limit,
    record_model_usage,
    record_tool_call_usage,
)


def _describe(trigger: Callable[[], None]) -> dict[str, Any]:
    try:
        trigger()
    except LimitExceededError as exc:
        return {
            "exception_class": f"{type(exc).__module__}.{type(exc).__qualname__}",
            "type": exc.type,
            "value": exc.value,
            "limit": exc.limit,
            "message": exc.message,
            "source_class": (
                None if exc.source is None
                else f"{type(exc.source).__module__}.{type(exc.source).__qualname__}"
            ),
        }
    raise AssertionError("expected LimitExceededError")


def _tool_call_limit() -> None:
    with tool_call_limit(1):
        record_tool_call_usage(2)
        check_tool_call_limit()


def _token_limit() -> None:
    with token_limit(10):
        record_model_usage(ModelUsage(
            input_tokens=6, output_tokens=5, total_tokens=11))
        check_token_limit()


def reproduce() -> dict[str, Any]:
    return {
        "tool_call": _describe(_tool_call_limit),
        "token": _describe(_token_limit),
    }


if __name__ == "__main__":
    print(json.dumps(reproduce(), indent=2, sort_keys=True))

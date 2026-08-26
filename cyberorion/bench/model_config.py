"""Benchmark 模型调用与 provenance 共用的非敏感配置。"""

from __future__ import annotations

import os

DEFAULT_MAX_OUTPUT_TOKENS = 8192


def max_output_tokens() -> int:
    """返回有效输出 token 上限；非法环境配置立即明确失败。"""
    raw = os.getenv("CO_BENCH_MAX_TOKENS")
    if raw is None or raw == "":
        return DEFAULT_MAX_OUTPUT_TOKENS
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(
            f"CO_BENCH_MAX_TOKENS 必须是正整数，收到 {raw!r}") from exc
    if value <= 0:
        raise ValueError(
            f"CO_BENCH_MAX_TOKENS 必须是正整数，收到 {raw!r}")
    return value


__all__ = ["DEFAULT_MAX_OUTPUT_TOKENS", "max_output_tokens"]

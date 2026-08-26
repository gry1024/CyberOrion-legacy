"""CAGE-2 官方环境 benchmark 适配器与持久化入口。"""

from __future__ import annotations

import asyncio
import json
import statistics
import time
from pathlib import Path
from typing import Any

from .assets import ASSETS, BenchmarkAssetMissing, require_asset
from .external_common import (
    DEFAULT_LOG_DIR, FAIR_ARM_BUDGET, LLMBudgetExceeded, MeteredLLM, _model_name,
    bootstrap_ci, make_llm, new_run_id, persist_run, provenance,
)
from .superagent_runtime import RuntimeConfig, ToolSpec, run_reference, run_superagent

SUITE = "cage2"
MODES = ("base", "single", "agent")
ARM_OF_MODE = {"base": "bare", "single": "single", "agent": "framework"}
METHODOLOGY_STATUS = "external_track"
TRIAL_STEPS = (30, 50, 100)
RED_AGENTS = ("B_lineAgent", "RedMeanderAgent", "SleepAgent")


def _safe_observation(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, default=str)[:12000]
    except Exception:
        return str(value)[:12000]


async def run_bench(n: int | None = None, mode: str = "base", seed: int = 42,
                    profile: str = "daily", dataset_version: str | None = None,
                    log_dir: str | Path = DEFAULT_LOG_DIR, llm=None,
                    on_progress=None, run_id: str | None = None,
                    source_provenance: dict | None = None, **_: Any) -> dict:
    if mode not in MODES:
        raise ValueError(f"cage2 mode 必须是 {'/'.join(MODES)}")
    root, files = require_asset(SUITE)
    total_episode_budget = max(9, int(n or (9 if profile == "daily" else 900)))
    matrix = [(step, red) for step in TRIAL_STEPS for red in RED_AGENTS]
    quotient, remainder = divmod(total_episode_budget, len(matrix))
    allocations = [quotient + (1 if index < remainder else 0)
                   for index in range(len(matrix))]
    started_at = time.time()
    from cyberorion.eval.benchmarks import run_cage2, run_cage2_async

    if mode != "base":
        llm = llm or make_llm(timeout=180.0)
        audit_traces: list[dict] = []
        episode_resources: list[dict] = []
        episode_states: dict[int, dict] = {}
        current_condition = ""

        async def choose(observation: Any, *, episode: int = 1, step: int = 1,
                         available_actions: list[dict] | None = None) -> dict:
            state = episode_states.setdefault(episode, {
                "meter": MeteredLLM(llm), "tool_calls": 0,
                "history": [], "started": time.perf_counter(),
                "budget_exhausted_steps": 0,
                "budget_exhausted_reason": None,
                "budget_exhaustion_reasons": set(),
                "budget_limit_violations": set(),
            })
            meter: MeteredLLM = state["meter"]

            def sleep_id() -> int:
                return next((row["action_id"] for row in (available_actions or [])
                             if row.get("action_type") == "Sleep"), -1)

            def fallback_trace(reason: str) -> dict:
                return {
                    "condition": current_condition, "episode": episode, "step": step,
                    "requested_blue_action": None,
                    "action": {"action_id": sleep_id()},
                    "decision_trace": [],
                    "tool_calls": [], "role_events": [], "trace_source": "runtime",
                    "budget_exhausted": True,
                    "budget_exhaustion_reason": reason,
                    "episode_budget_used": {"llm_calls": meter.calls,
                                            "tool_calls": state["tool_calls"],
                                            "estimated_tokens": meter.estimated_tokens},
                }

            # 预算一旦被判定耗尽（token/调用数），后续环境步直接执行文档化
            # fallback Sleep，不再反复调用 runtime 制造大量相同的
            # LLMBudgetExceeded 无效决策。
            if state["budget_exhausted_reason"]:
                state["budget_exhausted_steps"] += 1
                audit_traces.append(fallback_trace(state["budget_exhausted_reason"]))
                return {"action_id": sleep_id()}
            remaining_llm = FAIR_ARM_BUDGET["max_llm_calls"] - meter.calls
            remaining_tools = FAIR_ARM_BUDGET["max_tool_calls"] - state["tool_calls"]
            if remaining_llm <= 0 or remaining_tools <= 0:
                reason = "llm_calls" if remaining_llm <= 0 else "tool_calls"
                state["budget_exhausted_reason"] = reason
                state["budget_exhaustion_reasons"].add(reason)
                state["budget_exhausted_steps"] += 1
                audit_traces.append(fallback_trace(reason))
                return {"action_id": sleep_id()}
            selected: list[dict] = []

            safe_actions = list(available_actions or [])
            safe_ids = [int(row["action_id"]) for row in safe_actions]

            def select_blue_action(action_id: int) -> str:
                selected.append({"action_id": int(action_id)})
                return f"selected canonical Blue action_id={int(action_id)}"

            tools = {
                "select_blue_action": ToolSpec(
                    "select_blue_action", select_blue_action,
                    "Select exactly one currently valid safe ChallengeWrapper Blue action ID.",
                    {"type": "object",
                     "properties": {"action_id": {"type": "integer", "enum": safe_ids}},
                     "required": ["action_id"], "additionalProperties": False}),
            }

            async def wrapped(system: str, user: str) -> Any:
                raw = await meter(system, user)
                try:
                    parsed = json.loads(str(raw))
                except (ValueError, TypeError, json.JSONDecodeError):
                    return raw
                # Preserve direct JSON selection as a normal audited tool call.
                if isinstance(parsed, dict) and parsed.get("action_id") is not None:
                    return {"action": {"type": "tool", "tool": "select_blue_action",
                                       "arguments": {"action_id": parsed["action_id"]}}}
                return parsed

            task = json.dumps({
                "goal": "Choose exactly one defensive action, observe tool result, then complete.",
                "observation": _safe_observation(observation),
                "canonical_safe_blue_actions": safe_actions,
                "recent_actions": state["history"][-8:],
            }, ensure_ascii=False)
            config = RuntimeConfig(max_steps=max(1, remaining_llm),
                                   max_llm_calls=max(1, remaining_llm),
                                   max_tool_calls=max(1, remaining_tools),
                                   max_dispatches=3, max_role_steps=3)
            try:
                runtime = await (run_superagent if mode == "agent" else run_reference)(
                    task=task, llm=wrapped, tools=tools, config=config,
                    role_tools={
                        "watcher": ("select_blue_action",),
                        "analyst": ("select_blue_action",),
                        "responder": ("select_blue_action",),
                        "hunter": ("select_blue_action",),
                    })
            except LLMBudgetExceeded as exc:
                # meter 抛出的预算超限（token 或调用数）直接传播到这里。
                reason = ("token_budget" if "token" in str(exc).lower()
                          else "llm_calls")
                state["budget_exhausted_reason"] = reason
                state["budget_exhaustion_reasons"].add(reason)
                if meter.estimated_tokens > int(FAIR_ARM_BUDGET["token_budget"]):
                    # after-response 超限：响应已被记账，实际消耗超过硬上限。
                    state["budget_limit_violations"].add("estimated_tokens")
                state["budget_exhausted_steps"] += 1
                audit_traces.append(fallback_trace(reason))
                return {"action_id": sleep_id()}
            # runtime 捕获 meter 异常时会输出 INVALID_LLM_DECISION[...]；
            # 同时 meter 记账可能已超过 token 硬上限（after-response 超限），
            # 这属于实际违规，与"达到上限后走文档化 fallback"必须区分。
            output_text = str(runtime.get("output") or "")
            if "LLMBudgetExceeded" in output_text:
                reason = ("token_budget" if "token" in output_text.lower()
                          else "llm_calls")
                state["budget_exhausted_reason"] = reason
                state["budget_exhaustion_reasons"].add(reason)
            if meter.estimated_tokens > int(FAIR_ARM_BUDGET["token_budget"]):
                state["budget_exhausted_reason"] = "token_budget"
                state["budget_exhaustion_reasons"].add("token_budget")
                state["budget_limit_violations"].add("estimated_tokens")
            action = selected[-1] if selected else {"action_id": sleep_id()}
            state["history"].append(action)
            state["tool_calls"] += int(runtime["budget"].get("tool_calls", 0))
            audit_traces.append({
                "condition": current_condition, "episode": episode, "step": step,
                "requested_blue_action": action, "action": action,
                "decision_trace": runtime["decision_trace"],
                "tool_calls": runtime["tool_calls"],
                "role_events": runtime["role_events"],
                "budget": runtime["budget"], "trace_source": "runtime",
                "estimated_tokens": meter.estimated_tokens,
                "episode_budget_used": {"llm_calls": meter.calls,
                                        "tool_calls": state["tool_calls"],
                                        "estimated_tokens": meter.estimated_tokens},
            })
            return action

    all_episodes: list[dict] = []
    conditions: list[dict] = []
    for condition_index, (steps, red_agent) in enumerate(matrix):
        if mode != "base":
            current_condition = f"{red_agent}:{steps}"
            episode_states.clear()
        if mode == "base":
            result = run_cage2(allocations[condition_index], steps, False, None,
                               None, red_agent, seed + condition_index, True)
        else:
            result = await run_cage2_async(
                allocations[condition_index], steps, choose, None,
                red_agent, seed + condition_index, True)
        if result.get("error"):
            raise BenchmarkAssetMissing(SUITE, str(result["error"]))
        rows = result.get("episodes") or []
        for row in rows:
            row["condition"] = f"{red_agent}:{steps}"
            row["condition_seed"] = seed + condition_index
        all_episodes.extend(rows)
        if mode != "base":
            for episode, state in sorted(episode_states.items()):
                meter = state["meter"]
                episode_resources.append({
                    "condition": current_condition, "episode": episode,
                    "limits": dict(FAIR_ARM_BUDGET),
                    "used": {"llm_calls": meter.calls,
                             "tool_calls": state["tool_calls"],
                             "estimated_tokens": meter.estimated_tokens,
                             "wall_clock_sec": round(time.perf_counter() - state["started"], 4)},
                    "budget_exhausted_steps": state["budget_exhausted_steps"],
                    "budget_exhaustion_reasons": sorted(
                        state["budget_exhaustion_reasons"]),
                    "token_budget_exhausted": "token_budget" in
                        state["budget_exhaustion_reasons"],
                    "budget_limit_violations": sorted(
                        state["budget_limit_violations"]),
                })
        rewards_for_condition = [float(row.get("reward", 0.0)) for row in rows]
        conditions.append({
            "red_agent": red_agent, "steps": steps, "episodes": len(rows),
            "mean_reward": round(statistics.fmean(rewards_for_condition), 4)
            if rewards_for_condition else None,
            "reward_std": round(statistics.stdev(rewards_for_condition), 4)
            if len(rewards_for_condition) > 1 else 0.0,
        })
    finished_at = time.time()
    episode_rows = all_episodes
    rewards = [float(row.get("reward", 0)) for row in episode_rows]
    mean = statistics.fmean(rewards) if rewards else 0.0
    scores = {
        "n": len(rewards), "mean_reward": round(mean, 4),
        "reward_std": round(statistics.pstdev(rewards), 4) if len(rewards) > 1 else 0.0,
        "correct_mc_pct": 0.0, "avg_score": round(mean, 4), "parse_fail": 0,
        "llm_errors": 0, "by_difficulty": {}, "by_topic": {},
        "confidence_intervals": {"mean_reward": bootstrap_ci(rewards, seed)},
        "host_compromise_events": None,
        "host_compromise_metric_status": "not_exposed_by_official_ChallengeWrapper",
        # restore_cost_proxy 是 CyberOrion 自定义的非原生代理
        # （-Restore 次数），不是官方 CAGE availability 组件。
        "restore_cost_proxy": round(sum(float(r.get("restore_cost_proxy", 0.0))
                                         for r in episode_rows), 4),
        "restore_cost_proxy_status": "non_native_proxy",
        "restore_actions": sum(int(r.get("restore_actions", 0)) for r in episode_rows),
        "illegal_actions": sum(int(r.get("illegal_actions", 0)) for r in episode_rows),
    }
    spec = ASSETS[SUITE]
    run = {
        "schema_version": 3, "run_id": run_id or new_run_id(SUITE, mode, len(rewards)),
        "suite": SUITE, "mode": mode, "arm": ARM_OF_MODE[mode], "profile": profile,
        "n": len(rewards), "seed": seed, "model": _model_name(),
        "started_at": started_at, "finished_at": finished_at,
        "elapsed_sec": round(finished_at - started_at, 2), "scores": scores,
        "results": episode_rows, "llm_errors": 0, "status": "done", "error": None,
        "methodology_status": METHODOLOGY_STATUS,
        "methodology": {
            "official_alignment": "Scenario2, ChallengeWrapper, native cumulative reward, 3 step lengths x 3 red agents",
            "official_evaluation": "100 episodes per condition; leaderboard validation used 1000 and random.seed(153)",
            "differences": [
                "episode budget can be a representative subset",
                "condition seeds are explicit and independent for reproducibility",
                "LLM policies are callbacks rather than submitted BaseAgent classes",
                "host compromise event counts are unavailable from ChallengeWrapper and remain null",
                "restore_cost_proxy is a CyberOrion-defined non-native proxy (-Restore count), not an official CAGE availability component",
                "not directly comparable to the official leaderboard",
            ],
            "arm_budget": dict(FAIR_ARM_BUDGET),
        },
        "conditions": conditions,
        "benchmark_provenance": provenance(
            suite=SUITE, title=spec.title, upstream_url=spec.upstream_url,
            version=dataset_version or spec.version, files=files,
            selected_ids=[f"{r['condition']}:episode-{r['episode']}" for r in episode_rows],
            total=900, protocol="Scenario2 official 3x3 evaluation matrix", comparable=False),
        "asset_root": str(root),
    }
    if mode != "base":
        run["agent_traces"] = audit_traces
        run["episode_resource_usage"] = episode_resources
        run["budget_limit_violation_dimensions"] = sorted({
            dim for resource in episode_resources
            for dim in resource.get("budget_limit_violations", [])})
        run["budget_limit_violation"] = bool(
            run["budget_limit_violation_dimensions"])
    else:
        # 启发式基线不经过 LLM/工具预算，不存在预算违规。
        run["budget_limit_violation"] = False
        run["budget_limit_violation_dimensions"] = []
    return persist_run(run, log_dir, source_provenance=source_provenance)

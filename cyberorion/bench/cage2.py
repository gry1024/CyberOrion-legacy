"""CAGE-2 官方环境 benchmark：逐环境步公平预算与有状态蓝队策略。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import statistics
import time
from collections import Counter
from pathlib import Path
from typing import Any

from .assets import ASSETS, BenchmarkAssetMissing, require_asset
from .external_common import (
    DEFAULT_LOG_DIR, LLMBudgetExceeded, MeteredLLM, _model_name, bootstrap_ci, make_llm,
    new_run_id, persist_run, provenance,
)
from .superagent_runtime import (
    RuntimeConfig, ToolSpec, run_orchestrator_only, run_reference,
    run_superagent,
)

SUITE = "cage2"
MODES = ("base", "single", "orchestrator_only", "agent")
ARM_OF_MODE = {
    "base": "bare",
    "single": "single",
    "orchestrator_only": "orchestrator_only",
    "agent": "framework",
}
METHODOLOGY_STATUS = "external_track"
TRIAL_STEPS = (30, 50, 100)
RED_AGENTS = ("B_lineAgent", "RedMeanderAgent", "SleepAgent")

CAGE_MEMORY_SCHEMA = "cage_observable_transition_v1"
CAGE_MEMORY_WINDOW = 12
CAGE_MEMORY_MAX_CHARS = 32_000
CAGE_OBSERVATION_MAX_CHARS = 12_000
CAGE_EPISODE_SAFETY_MULTIPLIER = 1.25
_EVALUATOR_ONLY_KEYS = frozenset({
    "reward", "cumulative_reward", "final_score", "score",
    "scorer", "scorer_feedback", "evaluator_feedback",
})

# 校准阶段专用的宽松逐步上限。正式 pilot 值必须在真实校准 artifact 之后
# 另行冻结；不得用旧 episode-global smoke 反推。
CAGE_STEP_BUDGETS: dict[str, dict[str, int | float]] = {
    "diagnostic": {
        "max_steps": 10,
        "max_llm_calls": 10,
        "max_tool_calls": 8,
        "max_dispatches": 4,
        "max_role_steps": 4,
        "token_budget": 32_768,
        "wall_clock_sec": 300.0,
    },
    # 由 20260826 三种 30-step 条件的真实诊断校准冻结；不得按 pilot
    # performance 差异调整。决策证据见 logs/bench/...calibration_n3.md。
    "pilot_v1": {
        "max_steps": 6,
        "max_llm_calls": 4,
        "max_tool_calls": 3,
        "max_dispatches": 2,
        "max_role_steps": 4,
        "token_budget": 16_384,
        "wall_clock_sec": 60.0,
    },
    # publication_v1 仅依据不可发布 pilot_v1 的 1,620 条逐步资源轨迹冻结，
    # 未读取 reward。完整分位数与 headroom 见 calibration_v2 artifact。
    "publication_v1": {
        "max_steps": 7,
        "max_llm_calls": 5,
        "max_tool_calls": 4,
        "max_dispatches": 3,
        "max_role_steps": 4,
        "token_budget": 24_576,
        "wall_clock_sec": 90.0,
    },
}


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), default=str)


def _without_evaluator_fields(value: Any) -> Any:
    """递归剥离意外混入 observation 的 evaluator-only 字段。"""
    if isinstance(value, dict):
        return {
            str(key): _without_evaluator_fields(item)
            for key, item in value.items()
            if str(key).lower() not in _EVALUATOR_ONLY_KEYS
        }
    if isinstance(value, (list, tuple)):
        return [_without_evaluator_fields(item) for item in value]
    return value


def _safe_observation(value: Any) -> str:
    raw = _canonical_json(_without_evaluator_fields(value))
    if len(raw) <= CAGE_OBSERVATION_MAX_CHARS:
        return raw
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    marker = _canonical_json({
        "clipped": True,
        "original_chars": len(raw),
        "sha256": digest,
    })
    prefix_limit = max(0, CAGE_OBSERVATION_MAX_CHARS - len(marker) - 1)
    return raw[:prefix_limit] + "\n" + marker


def _model_visible_transition(value: dict[str, Any]) -> dict[str, Any]:
    """仅保留策略当时已观察到的 transition；明确排除 reward/scorer。"""
    return {
        "step": int(value.get("step") or 0),
        "observation_before": _safe_observation(
            value.get("observation_before")),
        "requested_blue_action": value.get("requested_blue_action"),
        "executed_blue_action": value.get("executed_blue_action"),
        "controller_status": str(value.get("controller_status") or "unknown"),
        "fallback_reason": value.get("fallback_reason"),
        "valid": bool(value.get("valid")),
        "invalid_reason": value.get("invalid_reason"),
        "done": bool(value.get("done")),
    }


def build_episode_memory(*, episode: int, step: int, horizon: int,
                         transitions: list[dict[str, Any]]) -> tuple[dict, str, str]:
    """构建确定性、有界、无 reward 的 model-visible episode memory。"""
    normalized = [_model_visible_transition(row) for row in transitions]
    recent = normalized[-CAGE_MEMORY_WINDOW:]
    omitted = normalized[:-len(recent)] if recent else normalized

    def payload() -> dict[str, Any]:
        omitted_raw = _canonical_json(omitted).encode("utf-8") if omitted else b""
        return {
            "schema_version": CAGE_MEMORY_SCHEMA,
            "episode": int(episode),
            "current_step": int(step),
            "horizon": int(horizon),
            "recent_transitions": recent,
            "omitted_prefix_count": len(omitted),
            "omitted_prefix_sha256": (
                hashlib.sha256(omitted_raw).hexdigest() if omitted else None),
        }

    memory = payload()
    serialized = _canonical_json(memory)
    while len(serialized) > CAGE_MEMORY_MAX_CHARS and recent:
        omitted.append(recent.pop(0))
        memory = payload()
        serialized = _canonical_json(memory)
    digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    return memory, serialized, digest


def _step_budget(profile: str,
                 override: dict[str, int | float] | None = None) -> dict[str, int | float]:
    budget = (dict(override) if override is not None
              else dict(CAGE_STEP_BUDGETS.get(profile) or {}))
    if not budget:
        raise ValueError(
            f"未知 CAGE budget profile {profile!r}；可用 {sorted(CAGE_STEP_BUDGETS)}")
    required = {
        "max_steps", "max_llm_calls", "max_tool_calls", "max_dispatches",
        "max_role_steps", "token_budget", "wall_clock_sec",
    }
    missing = sorted(required - budget.keys())
    if missing or any(float(budget[key]) <= 0 for key in required):
        raise ValueError(f"CAGE step budget 非法；missing={missing}, budget={budget}")
    return budget


def _episode_safety_ceiling(horizon: int,
                            step_budget: dict[str, int | float]) -> dict[str, int | float]:
    factor = CAGE_EPISODE_SAFETY_MULTIPLIER
    return {
        "budget_accounted_tokens": int(
            horizon * int(step_budget["token_budget"]) * factor),
        "llm_calls": int(horizon * int(step_budget["max_llm_calls"]) * factor),
        "tool_calls": int(horizon * int(step_budget["max_tool_calls"]) * factor),
        "wall_clock_sec": float(horizon * float(step_budget["wall_clock_sec"]) * factor),
    }


async def run_bench(
        n: int | None = None, mode: str = "base", seed: int = 42,
        profile: str = "daily", dataset_version: str | None = None,
        log_dir: str | Path = DEFAULT_LOG_DIR, llm=None,
        on_progress=None, run_id: str | None = None,
        source_provenance: dict | None = None,
        cage_budget_profile: str = "publication_v1",
        cage_step_budget: dict[str, int | float] | None = None,
        condition_steps: tuple[int, ...] | None = None,
        red_agents: tuple[str, ...] | None = None,
        **_: Any) -> dict:
    if mode not in MODES:
        raise ValueError(f"cage2 mode 必须是 {'/'.join(MODES)}")
    root, files = require_asset(SUITE)
    selected_steps = tuple(condition_steps or TRIAL_STEPS)
    selected_red = tuple(red_agents or RED_AGENTS)
    if not selected_steps or any(int(value) <= 0 for value in selected_steps):
        raise ValueError("CAGE condition_steps 必须是非空正整数序列")
    if not selected_red or any(value not in RED_AGENTS for value in selected_red):
        raise ValueError(f"CAGE red_agents 必须来自 {RED_AGENTS}")
    matrix = [(int(steps), red) for steps in selected_steps for red in selected_red]
    requested_episodes = int(n or (len(matrix) if profile == "daily" else 900))
    total_episode_budget = max(len(matrix), requested_episodes)
    quotient, remainder = divmod(total_episode_budget, len(matrix))
    allocations = [quotient + (1 if index < remainder else 0)
                   for index in range(len(matrix))]
    step_budget = _step_budget(cage_budget_profile, cage_step_budget)
    started_at = time.time()
    from cyberorion.eval.benchmarks import run_cage2, run_cage2_async

    audit_traces: list[dict] = []
    episode_resources: list[dict] = []
    episode_states: dict[int, dict] = {}
    current_condition = ""

    if mode != "base":
        # CAGE publication protocol fixes deterministic decoding.  Pass it
        # explicitly here so a caller cannot accidentally fall back to the
        # provider default when CO_BENCH_TEMPERATURE is unset.
        llm = llm or make_llm(
            timeout=max(180.0, float(step_budget["wall_clock_sec"])),
            temperature=0.0)

        async def choose(
                observation: Any, *, episode: int = 1, step: int = 1,
                horizon: int = 100, available_actions: list[dict] | None = None,
                previous_transition: dict[str, Any] | None = None) -> dict:
            state = episode_states.setdefault(episode, {
                "transitions": [], "seen_transition_steps": set(),
                "started": time.perf_counter(), "totals": Counter(),
                "provider_tokens": Counter(), "provider_usage_steps": 0,
                "roles": Counter(), "step_count": 0, "fallback_count": 0,
                "budget_exhaustion_count": 0, "steps_with_dispatch": 0,
                "budget_limit_violations": set(),
                "episode_safety_ceiling_reached": None,
            })
            if previous_transition is not None:
                previous_step = int(previous_transition.get("step") or 0)
                if previous_step not in state["seen_transition_steps"]:
                    state["transitions"].append(
                        _model_visible_transition(previous_transition))
                    state["seen_transition_steps"].add(previous_step)

            safe_actions = list(available_actions or [])
            safe_ids = [int(row["action_id"]) for row in safe_actions]
            sleep = next((row for row in safe_actions
                          if row.get("action_type") == "Sleep"), None)
            if sleep is None:
                raise RuntimeError("current canonical action table has no Sleep")
            sleep_id = int(sleep["action_id"])
            memory, memory_serialized, memory_hash = build_episode_memory(
                episode=episode, step=step, horizon=horizon,
                transitions=state["transitions"])
            current_observation = _safe_observation(observation)
            ceiling = _episode_safety_ceiling(horizon, step_budget)

            def append_trace(*, runtime: dict | None, action: dict,
                             fallback: bool, fallback_reason: str | None,
                             budget_status: str, meter: MeteredLLM | None,
                             wall_clock_sec: float) -> None:
                runtime = runtime or {
                    "decision_trace": [], "tool_calls": [], "role_events": [],
                    "budget": {"llm_calls": 0, "tool_calls": 0,
                               "dispatches": 0}, "errors": [],
                }
                provider = meter.usage()["provider"] if meter else None
                used = {
                    "llm_calls": meter.calls if meter else 0,
                    "tool_calls": int(runtime["budget"].get("tool_calls", 0)),
                    "estimated_tokens": meter.estimated_tokens if meter else 0,
                    "budget_accounted_tokens": (
                        meter.budget_accounted_tokens if meter else 0),
                    "budget_accounting_source": (
                        meter.budget_accounting_source if meter else "none"),
                    "wall_clock_sec": round(wall_clock_sec, 4),
                    "provider_prompt_tokens": int(provider.get("prompt_tokens", 0))
                    if provider else None,
                    "provider_completion_tokens": int(
                        provider.get("completion_tokens", 0)) if provider else None,
                    "provider_total_tokens": int(provider.get("total_tokens", 0))
                    if provider else None,
                    "provider_usage_status": "available" if provider else "unavailable",
                    "dispatches": int(runtime["budget"].get("dispatches", 0)),
                }
                roles = sorted({str(event.get("role"))
                                for event in runtime.get("role_events", [])
                                if event.get("event") == "spawn" and event.get("role")})
                audit_traces.append({
                    "condition": current_condition, "episode": episode,
                    "step": step, "horizon": horizon,
                    "requested_blue_action": action, "action": action,
                    "selected_or_fallback": "fallback" if fallback else "selected",
                    "fallback_reason": fallback_reason,
                    "decision_trace": runtime.get("decision_trace", []),
                    "tool_calls": runtime.get("tool_calls", []),
                    "role_events": runtime.get("role_events", []),
                    "trace_source": "runtime", "step_budget": dict(step_budget),
                    "step_resource_usage": used, "budget_status": budget_status,
                    "dispatch_count": used["dispatches"],
                    "dispatched_roles": roles, "memory_schema": CAGE_MEMORY_SCHEMA,
                    "model_visible_memory": memory_serialized,
                    "memory_sha256": memory_hash,
                    "memory_omitted_prefix_count": memory["omitted_prefix_count"],
                    "memory_omitted_prefix_sha256": memory["omitted_prefix_sha256"],
                })
                state["step_count"] += 1
                state["totals"].update({
                    "llm_calls": used["llm_calls"], "tool_calls": used["tool_calls"],
                    "estimated_tokens": used["estimated_tokens"],
                    "budget_accounted_tokens": used["budget_accounted_tokens"],
                    "wall_clock_sec": used["wall_clock_sec"],
                    "dispatches": used["dispatches"],
                })
                if provider:
                    state["provider_usage_steps"] += 1
                    state["provider_tokens"].update(provider)
                state["roles"].update(roles)
                state["fallback_count"] += int(fallback)
                state["budget_exhaustion_count"] += int(
                    budget_status in {"exhausted", "violation", "timeout"})
                state["steps_with_dispatch"] += int(bool(roles))

            safety_reason = state["episode_safety_ceiling_reached"]
            if safety_reason:
                action = {"action_id": sleep_id}
                append_trace(runtime=None, action=action, fallback=True,
                             fallback_reason=f"episode_safety_ceiling:{safety_reason}",
                             budget_status="episode_safety_ceiling", meter=None,
                             wall_clock_sec=0.0)
                return {**action, "_cyberorion": {
                    "status": "fallback",
                    "fallback_reason": f"episode_safety_ceiling:{safety_reason}"}}

            selected: list[dict[str, int]] = []

            def select_blue_action(action_id: int) -> str:
                candidate = int(action_id)
                if candidate not in safe_ids:
                    raise ValueError(
                        f"action_id {candidate} is not in current canonical safe set")
                if selected:
                    raise RuntimeError("a final Blue action was already selected")
                selected.append({"action_id": candidate})
                return f"selected canonical Blue action_id={candidate}"

            tools = {"select_blue_action": ToolSpec(
                "select_blue_action", select_blue_action,
                "Select exactly one currently valid safe ChallengeWrapper Blue action ID.",
                {"type": "object",
                 "properties": {"action_id": {"type": "integer", "enum": safe_ids}},
                 "required": ["action_id"], "additionalProperties": False},
                terminal=True)}
            meter = MeteredLLM(llm, budget=step_budget)

            async def wrapped(system: str, user: str) -> Any:
                raw = await meter(system, user)
                try:
                    parsed = json.loads(str(raw))
                except (ValueError, TypeError, json.JSONDecodeError):
                    return raw
                if isinstance(parsed, dict) and parsed.get("action_id") is not None:
                    return {"action": {"type": "tool", "tool": "select_blue_action",
                                       "arguments": {"action_id": parsed["action_id"]}}}
                return parsed

            architecture = {
                "single": "Reference is the only final action selector.",
                "orchestrator_only": (
                    "Orchestrator is the only final selector; dispatch is disabled."),
                "agent": (
                    "Specialists may analyze and return reports, but only the "
                    "orchestrator may select the final action."),
            }[mode]
            task = _canonical_json({
                "goal": ("Choose exactly one defensive action. A valid "
                         "select_blue_action call immediately ends this environment step."),
                "architecture": architecture,
                "current_observation": current_observation,
                "canonical_safe_blue_actions": safe_actions,
                "episode_memory": memory,
            })
            config = RuntimeConfig(
                max_steps=int(step_budget["max_steps"]),
                max_llm_calls=int(step_budget["max_llm_calls"]),
                max_tool_calls=int(step_budget["max_tool_calls"]),
                max_dispatches=int(step_budget["max_dispatches"]),
                max_role_steps=int(step_budget["max_role_steps"]),
            )
            runtime = None
            failure_reason = None
            started_step = time.perf_counter()
            try:
                async with asyncio.timeout(float(step_budget["wall_clock_sec"])):
                    runner = {"single": run_reference,
                              "orchestrator_only": run_orchestrator_only,
                              "agent": run_superagent}[mode]
                    runtime = await runner(
                        task=task, llm=wrapped, tools=tools, config=config,
                        # Design B：specialist 只分析，不能看到 terminal selector。
                        role_tools={role: () for role in (
                            "watcher", "analyst", "responder", "hunter")})
            except TimeoutError:
                failure_reason = "step_wall_time_exhausted"
            except LLMBudgetExceeded:
                failure_reason = "step_resource_budget_exhausted"
            wall = time.perf_counter() - started_step
            runtime = runtime or {
                "output": "", "decision_trace": [], "tool_calls": [],
                "role_events": [], "budget": {"llm_calls": meter.calls,
                "tool_calls": 0, "dispatches": 0}, "errors": []}
            if selected:
                action, fallback, budget_status = selected[0], False, "ok"
            else:
                action, fallback = {"action_id": sleep_id}, True
                diagnostic = " ".join([
                    str(runtime.get("output") or ""),
                    " ".join(map(str, runtime.get("errors") or []))])
                if failure_reason == "step_wall_time_exhausted":
                    budget_status = "timeout"
                elif failure_reason == "step_resource_budget_exhausted":
                    budget_status = "exhausted"
                elif ("BudgetExceeded" in diagnostic
                      or "budget exhausted" in diagnostic.lower()
                      or meter.calls >= int(step_budget["max_llm_calls"])
                      or meter.estimated_tokens >= int(step_budget["token_budget"])):
                    budget_status = "exhausted"
                    failure_reason = "step_resource_budget_exhausted"
                else:
                    budget_status = "no_valid_selection"
                    failure_reason = "no_valid_selection"
            if meter.budget_accounted_tokens > int(step_budget["token_budget"]):
                budget_status = "violation"
                state["budget_limit_violations"].add("budget_accounted_tokens")
            append_trace(runtime=runtime, action=action, fallback=fallback,
                         fallback_reason=failure_reason, budget_status=budget_status,
                         meter=meter, wall_clock_sec=wall)

            for dimension, limit in ceiling.items():
                if float(state["totals"].get(dimension, 0)) > float(limit):
                    state["episode_safety_ceiling_reached"] = dimension
                    state["budget_limit_violations"].add(
                        f"episode_safety_{dimension}")
                    break
            return {**action, "_cyberorion": {
                "status": "fallback" if fallback else "selected",
                "fallback_reason": failure_reason}}

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
                decision_steps = int(state["step_count"])
                episode_resources.append({
                    "condition": current_condition, "episode": episode,
                    "step_budget": dict(step_budget),
                    "episode_safety_ceiling": _episode_safety_ceiling(
                        steps, step_budget),
                    "used": {
                        "llm_calls": int(state["totals"]["llm_calls"]),
                        "tool_calls": int(state["totals"]["tool_calls"]),
                        "estimated_tokens": int(state["totals"]["estimated_tokens"]),
                        "budget_accounted_tokens": int(
                            state["totals"]["budget_accounted_tokens"]),
                        "wall_clock_sec": round(
                            float(state["totals"]["wall_clock_sec"]), 4),
                        "provider_prompt_tokens": int(
                            state["provider_tokens"]["prompt_tokens"]),
                        "provider_completion_tokens": int(
                            state["provider_tokens"]["completion_tokens"]),
                        "provider_total_tokens": int(
                            state["provider_tokens"]["total_tokens"]),
                        "provider_usage_steps": int(state["provider_usage_steps"]),
                        "dispatches": int(state["totals"]["dispatches"])},
                    "decision_steps": decision_steps,
                    "fallback_count": int(state["fallback_count"]),
                    "budget_exhaustion_count": int(
                        state["budget_exhaustion_count"]),
                    "dispatch_utilization": {
                        "dispatches": int(state["totals"]["dispatches"]),
                        "steps_with_dispatch": int(state["steps_with_dispatch"]),
                        "step_rate": round(state["steps_with_dispatch"] / decision_steps, 6)
                        if decision_steps else 0.0,
                        "roles": dict(sorted(state["roles"].items()))},
                    "episode_safety_ceiling_reached": state[
                        "episode_safety_ceiling_reached"],
                    "budget_limit_violations": sorted(
                        state["budget_limit_violations"]),
                    "elapsed_sec": round(
                        time.perf_counter() - state["started"], 4)})
        rewards_for_condition = [float(row.get("reward", 0.0)) for row in rows]
        conditions.append({
            "red_agent": red_agent, "steps": steps, "episodes": len(rows),
            "mean_reward": round(statistics.fmean(rewards_for_condition), 4)
            if rewards_for_condition else None,
            "reward_std": round(statistics.stdev(rewards_for_condition), 4)
            if len(rewards_for_condition) > 1 else 0.0})

    finished_at = time.time()
    rewards = [float(row.get("reward", 0)) for row in all_episodes]
    mean = statistics.fmean(rewards) if rewards else 0.0
    scores = {
        "n": len(rewards), "mean_reward": round(mean, 4),
        "reward_std": round(statistics.pstdev(rewards), 4) if len(rewards) > 1 else 0.0,
        "correct_mc_pct": 0.0, "avg_score": round(mean, 4), "parse_fail": 0,
        "llm_errors": 0, "by_difficulty": {}, "by_topic": {},
        "confidence_intervals": {"mean_reward": bootstrap_ci(rewards, seed)},
        "host_compromise_events": None,
        "host_compromise_metric_status": "not_exposed_by_official_ChallengeWrapper",
        "restore_cost_proxy": round(sum(
            float(row.get("restore_cost_proxy", 0.0)) for row in all_episodes), 4),
        "restore_cost_proxy_status": "non_native_proxy",
        "restore_actions": sum(int(row.get("restore_actions", 0))
                               for row in all_episodes),
        "illegal_actions": sum(int(row.get("illegal_actions", 0))
                               for row in all_episodes)}
    spec = ASSETS[SUITE]
    run = {
        "schema_version": 4,
        "run_id": run_id or new_run_id(SUITE, mode, len(rewards)),
        "suite": SUITE, "mode": mode, "arm": ARM_OF_MODE[mode],
        "profile": profile, "n": len(rewards), "seed": seed,
        "model": _model_name(), "started_at": started_at,
        "finished_at": finished_at,
        "elapsed_sec": round(finished_at - started_at, 2), "scores": scores,
        "results": all_episodes, "llm_errors": 0, "status": "done", "error": None,
        "methodology_status": METHODOLOGY_STATUS,
        "methodology": {
            "official_alignment": (
                "Scenario2, ChallengeWrapper, native cumulative reward, "
                "selected horizon x red-agent matrix"),
            "official_evaluation": (
                "100 episodes per condition; leaderboard validation used "
                "1000 and random.seed(153)"),
            "differences": [
                "episode count can be a representative pilot subset",
                "condition seeds are explicit and independent for reproducibility",
                "LLM policies are callbacks rather than submitted BaseAgent classes",
                "host compromise event counts are unavailable from ChallengeWrapper and remain null",
                "restore_cost_proxy is a non-native proxy (-Restore count)",
                "not directly comparable to the official leaderboard"],
            "fairness_scope": "per_environment_step",
            "budget_profile": cage_budget_profile, "step_budget": dict(step_budget),
            "episode_safety_multiplier": CAGE_EPISODE_SAFETY_MULTIPLIER,
            "terminal_action_semantics": (
                "reference/orchestrator only; first valid selection terminates step"),
            "episode_memory_schema": CAGE_MEMORY_SCHEMA,
            "model_visible_reward": False},
        "conditions": conditions,
        "benchmark_provenance": provenance(
            suite=SUITE, title=spec.title, upstream_url=spec.upstream_url,
            version=dataset_version or spec.version, files=files,
            selected_ids=[f"{row['condition']}:episode-{row['episode']}"
                          for row in all_episodes],
            total=900, protocol="Scenario2 official selected condition matrix",
            comparable=False),
        "asset_root": str(root)}
    if mode != "base":
        run["agent_traces"] = audit_traces
        run["episode_resource_usage"] = episode_resources
        run["budget_limit_violation_dimensions"] = sorted({
            dimension for resource in episode_resources
            for dimension in resource.get("budget_limit_violations", [])})
        run["budget_limit_violation"] = bool(
            run["budget_limit_violation_dimensions"])
    else:
        run["budget_limit_violation"] = False
        run["budget_limit_violation_dimensions"] = []
    return persist_run(run, log_dir, source_provenance=source_provenance)

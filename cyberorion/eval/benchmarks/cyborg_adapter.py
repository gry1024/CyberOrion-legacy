"""CybORG CAGE-2 benchmark 适配器（懒加载、可选依赖）。

导入本模块【不需要】安装 CybORG —— 所有 CybORG 相关的 import 都在
函数内部。未安装时 :func:`run_cage2` 返回带安装提示的 error 字典，
绝不抛 ImportError，因此调用方与测试可以无条件导入本模块。

PyPI 上存在 CybORG 0.2（``pip install CybORG``）；CAGE-2 挑战官方推荐
从 GitHub 安装完整环境。

llm_driven=False 时使用一个简单的启发式基线蓝队策略：
优先 Restore 被入侵主机，其次 Analyse，否则 Sleep —— 仅作 sanity
baseline，不追求高分。llm_driven=True（接入我们的蓝队 agent 决策回路）
暂未实现，返回明确的 NotImplemented 说明。
"""

from __future__ import annotations

import os
import inspect
import random
import json
from pathlib import Path
from typing import Any, Awaitable, Callable

# 未安装 CybORG 时返回的安装提示（已核实 PyPI 有 CybORG 0.2）。
_INSTALL_HINT = (
    "pip install CybORG  # PyPI 0.2；CAGE-2 完整环境推荐 "
    "pip install git+https://github.com/cage-challenge/cage-challenge-2.git"
)

_SAFE_BLUE_ACTION_TYPES = frozenset({"Sleep", "Monitor", "Analyse", "Remove", "Restore"})


def _heuristic_policy(observation: Any) -> Any:
    """启发式基线蓝队策略：Restore 可疑主机，否则 Sleep。

    CAGE-2 observation 中每个主机有 ``compromised`` 标记（在给了
    Decoy/Analyse 信息后可观测）；这里采用保守策略：发现 compromised
    就 Restore，否则 Sleep。策略仅作 baseline 占位。
    """
    from CybORG.Shared.Actions import Sleep, Restore

    try:
        for hostname, info in (observation or {}).items():
            if hostname == "success":
                continue
            compromised = str(getattr(info, "get", lambda *_: "")(
                "compromised", "")) if isinstance(info, dict) else ""
            if compromised and compromised.lower() not in ("no", "none", ""):
                return Restore(hostname=hostname, agent="Blue")
    except Exception:
        pass
    return Sleep()


def _mapped_action(spec: Any) -> tuple[Any, bool, str | None]:
    """把审计过的高层动作规格映射为 CAGE-2 原生动作。"""
    from CybORG.Shared.Actions import Sleep
    if not isinstance(spec, dict):
        return Sleep(), False, "action spec is not an object"
    action = str(spec.get("action") or spec.get("type") or "sleep").lower()
    host = str(spec.get("hostname") or spec.get("host") or "")
    if action == "sleep" or not host:
        return (Sleep(), action == "sleep",
                None if action == "sleep" else "hostname is required")
    try:
        from CybORG.Shared import Actions
        cls = {"analyse": getattr(Actions, "Analyse", None),
               "remove": getattr(Actions, "Remove", None),
               "restore": getattr(Actions, "Restore", None)}.get(action)
        if cls is None:
            module_name = {"analyse": "Analyse", "remove": "Remove",
                           "restore": "Restore"}.get(action)
            if module_name:
                module = __import__(
                    f"CybORG.Shared.Actions.{module_name}",
                    fromlist=[module_name])
                cls = getattr(module, module_name, None)
        if cls is not None:
            try:
                return cls(hostname=host, agent="Blue"), True, None
            except TypeError:
                return cls(session=0, agent="Blue", hostname=host), True, None
    except Exception as exc:
        return Sleep(), False, f"{type(exc).__name__}: {exc}"[:200]
    return Sleep(), False, f"unsupported action: {action}"


def _action_from_spec(spec: Any) -> Any:
    """兼容旧调用方：非法规格安全降级为 Sleep。"""
    return _mapped_action(spec)[0]


def _challenge_action_index(wrapper: Any, action: Any) -> int | None:
    """将原生动作映射为 ChallengeWrapper 所要求的离散 action id。

    CAGE-2 官方 ``ChallengeWrapper`` 在内部叠加了 ``EnumActionWrapper``，
    因而不能直接把 ``Sleep`` / ``Restore`` 对象交给 ``step``。只读取其
    已公开的 ``possible_actions`` 表；找不到完全匹配项就返回 None，让调用方
    将该步标为非法而不是猜一个动作。
    """
    choices = getattr(getattr(wrapper, "env", None), "env", None)
    choices = getattr(choices, "possible_actions", None)
    if not isinstance(choices, list):
        return None
    for index, candidate in enumerate(choices):
        if type(candidate) is not type(action):
            continue
        if getattr(candidate, "hostname", None) != getattr(action, "hostname", None):
            continue
        if getattr(candidate, "agent", None) != getattr(action, "agent", None):
            continue
        return index
    return None


def _enum_actions(wrapper: Any) -> list[Any]:
    """读取 ChallengeWrapper 内 EnumActionWrapper 的真实离散动作表。"""
    current = getattr(wrapper, "env", None)
    while current is not None:
        choices = getattr(current, "possible_actions", None)
        if isinstance(choices, list):
            return choices
        current = getattr(current, "env", None)
    return []


def canonical_safe_blue_actions(wrapper: Any) -> list[dict[str, Any]]:
    """返回本步可直接交给 ``ChallengeWrapper.step`` 的安全 action IDs。"""
    # Refresh the wrapper's current action space before reading the enum map.
    wrapper.get_action_space("Blue")
    rows = []
    for action_id, action in enumerate(_enum_actions(wrapper)):
        action_type = type(action).__name__
        if action_type not in _SAFE_BLUE_ACTION_TYPES:
            continue
        rows.append({
            "action_id": action_id,
            "action_type": action_type,
            "hostname": getattr(action, "hostname", None),
            "agent": getattr(action, "agent", None),
            "display": str(action),
        })
    if not any(row["action_type"] == "Sleep" for row in rows):
        raise RuntimeError("ChallengeWrapper safe action space does not contain Sleep")
    return rows


def _json_safe(value: Any) -> Any:
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        return str(value)


def _selected_action(spec: Any, available: list[dict[str, Any]]) -> tuple[int, dict, bool, str | None]:
    """校验策略请求并返回精确 action id；非法请求显式降级到真实 Sleep。"""
    by_id = {int(row["action_id"]): row for row in available}
    sleep = next(row for row in available if row["action_type"] == "Sleep")
    requested = spec.get("action_id") if isinstance(spec, dict) else None
    try:
        requested_id = int(requested)
    except (TypeError, ValueError):
        return int(sleep["action_id"]), sleep, False, "action_id is required and must be an integer"
    selected = by_id.get(requested_id)
    if selected is None:
        return int(sleep["action_id"]), sleep, False, "action_id is not in current safe action space"
    return requested_id, selected, True, None


async def _invoke_async_policy(policy: Callable[..., Awaitable[dict]], observation: Any,
                               available: list[dict[str, Any]], episode: int,
                               step: int, horizon: int,
                               previous_transition: dict[str, Any] | None) -> Any:
    try:
        params = inspect.signature(policy).parameters
    except (TypeError, ValueError):
        params = {}
    accepts_kwargs = any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())
    kwargs = {
        "episode": episode,
        "step": step,
        "horizon": horizon,
        "available_actions": available,
        "previous_transition": previous_transition,
    }
    selected = kwargs if accepts_kwargs else {key: value for key, value in kwargs.items()
                                               if key in params}
    return await policy(observation, **selected)


def run_cage2(episodes: int = 3, steps: int = 100,
              llm_driven: bool = False,
              policy: "Callable[[Any], dict] | None" = None,
              scenario_path: "str | None" = None,
              red_agent: str = "B_lineAgent",
              seed: int = 153,
              official_wrapper: bool = True) -> dict:
    """运行 CAGE-2 基准，返回逐局与平均奖励。

    Args:
        episodes: 局数。
        steps: 每局最大步数。
        llm_driven: True 时尝试接入我们的蓝队 LLM 决策回路（暂未实现）。

    Returns:
        成功: ``{"episodes": [{"episode": i, "reward": r}, ...],
                 "mean_reward": float, "llm_driven": bool}``
        失败: ``{"error": ..., "install": ...}`` 或
              ``{"error": "not implemented", ...}``。
    """
    try:
        import CybORG  # noqa: F401
    except ImportError:
        return {"error": "CybORG not installed", "install": _INSTALL_HINT}

    if llm_driven and policy is None:
        return {
            "error": "not implemented",
            "message": (
                "llm_driven=True（接入 CyberOrion 蓝队 agent 决策回路）暂未实现："
                "需要把 CAGE-2 observation 映射到蓝队工具语义。请先用 "
                "llm_driven=False 的启发式基线策略。"),
        }

    try:
        from CybORG import CybORG as CybORGEnv
        from CybORG.Agents import B_lineAgent, SleepAgent
        from CybORG.Agents.SimpleAgents.Meander import RedMeanderAgent
        from CybORG.Agents.Wrappers import ChallengeWrapper
    except ImportError as exc:  # 装的是其它版本/不完整安装
        return {"error": f"CybORG import incomplete: {exc}",
                "install": _INSTALL_HINT}

    configured = scenario_path or os.getenv("CYBERORION_CAGE2_SCENARIO")
    if configured:
        scenario = Path(configured)
    else:
        # 与外部 benchmark 资产清单保持一致：没有显式配置时优先使用
        # 仓库内已验证的官方 checkout，而不是历史开发机上的 /opt/cyborg。
        # 不存在时仍返回结构化错误，绝不猜测或创建路径。
        default_root = Path(__file__).resolve().parents[3] / "benchmarks" / "external" / "cage2"
        root = Path(os.getenv("CYBERORION_CAGE2_DIR", str(default_root))).expanduser()
        candidates = list(root.rglob("Scenario2.yaml")) if root.exists() else []
        scenario = candidates[0] if candidates else root / "CybORG" / "CybORG" / "Shared" / "Scenarios" / "Scenario2.yaml"
    if not scenario.is_file():
        return {"error": f"CAGE-2 scenario not found: {scenario}",
                "install": _INSTALL_HINT}
    red_agents = {
        "B_lineAgent": B_lineAgent,
        "RedMeanderAgent": RedMeanderAgent,
        "SleepAgent": SleepAgent,
    }
    if red_agent not in red_agents:
        return {"error": f"unsupported red agent: {red_agent}"}
    random.seed(int(seed))
    try:
        import numpy as np
        np.random.seed(int(seed))
    except ImportError:
        pass
    rewards: list[dict] = []
    for ep in range(int(episodes)):
        env = CybORGEnv(str(scenario), "sim", agents={"Red": red_agents[red_agent]})
        wrapped = ChallengeWrapper(env=env, agent_name="Blue") if official_wrapper else env
        obs = wrapped.reset() if official_wrapper else env.reset().observation
        total = 0.0
        restore_actions = illegal_actions = 0
        actions: list[dict] = []
        for _ in range(int(steps)):
            requested_spec = None
            executed_blue_action = None
            if llm_driven and policy is not None:
                # 新 harness 通过 episode/step 边界让上层把 LLM/工具预算按整局
                # 共享；旧的一参数策略保持兼容。不要用 TypeError 回退，因为
                # 那会吞掉策略内部真实的 TypeError。
                try:
                    params = inspect.signature(policy).parameters
                except (TypeError, ValueError):
                    params = {}
                accepts_kwargs = any(
                    p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())
                available = canonical_safe_blue_actions(wrapped) if official_wrapper else []
                policy_kwargs = {"episode": ep + 1, "step": len(actions) + 1,
                                 "available_actions": available}
                selected_kwargs = (policy_kwargs if accepts_kwargs else
                                   {key: value for key, value in policy_kwargs.items()
                                    if key in params})
                requested_spec = policy(obs, **selected_kwargs)
            else:
                requested_spec = None
            if llm_driven and official_wrapper:
                action_index, executed_blue_action, valid, invalid_reason = _selected_action(
                    requested_spec, available)
                action = _enum_actions(wrapped)[action_index]
            elif llm_driven:
                action, valid, invalid_reason = _mapped_action(requested_spec)
                action_index = None
            else:
                action, valid, invalid_reason = _heuristic_policy(obs), True, None
                action_index = _challenge_action_index(wrapped, action) if official_wrapper else None
            if official_wrapper and not llm_driven:
                if action_index is None:
                    valid = False
                    invalid_reason = invalid_reason or "action not present in ChallengeWrapper action space"
                    available = canonical_safe_blue_actions(wrapped)
                    sleep = next(row for row in available if row["action_type"] == "Sleep")
                    action_index = int(sleep["action_id"])
                    action = _enum_actions(wrapped)[action_index]
                executed_blue_action = next(
                    (row for row in canonical_safe_blue_actions(wrapped)
                     if row["action_id"] == action_index),
                    {"action_id": action_index, "action_type": type(action).__name__,
                     "display": str(action)})
            restore_actions += int(action.__class__.__name__.lower() == "restore")
            illegal_actions += int(not valid)
            if official_wrapper:
                obs, reward, done, info = wrapped.step(action_index)
            else:
                result = env.step(agent="Blue", action=action)
                obs, reward, done, info = (result.observation, result.reward,
                                           result.done, getattr(result, "info", {}))
            total += float(reward or 0.0)
            actions.append({"blue": str(action),
                            "requested_blue_action": _json_safe(requested_spec),
                            "executed_blue_action": _json_safe(executed_blue_action),
                            "red": str(env.get_last_action("Red")),
                            "valid": valid, "invalid_reason": invalid_reason,
                            "reward": float(reward or 0.0)})
            if done:
                break
        rewards.append({
            "episode": ep + 1, "reward": round(total, 3),
            "red_agent": red_agent, "steps": len(actions), "actions": actions,
            "illegal_actions": illegal_actions,
            "restore_actions": restore_actions,
            # CyberOrion 自定义的非原生 Restore 成本代理，不是官方 CAGE
            # availability 组件。
            "restore_cost_proxy": -float(restore_actions),
            # ChallengeWrapper exposes scalar reward only; do not fabricate a
            # compromise count from that aggregate.
            "host_compromise_events": None,
            "host_compromise_metric_status": "not_exposed_by_official_wrapper",
        })

    mean_reward = (sum(r["reward"] for r in rewards) / len(rewards)
                   if rewards else 0.0)
    return {
        "episodes": rewards,
        "mean_reward": round(mean_reward, 3),
        "llm_driven": bool(llm_driven),
        "steps": int(steps),
        "red_agent": red_agent, "seed": int(seed),
        "wrapper": "ChallengeWrapper" if official_wrapper else "raw",
    }


async def run_cage2_async(episodes: int = 3, steps: int = 100,
                          policy: "Callable[..., Awaitable[dict]] | None" = None,
                          scenario_path: "str | None" = None,
                          red_agent: str = "B_lineAgent", seed: int = 153,
                          official_wrapper: bool = True,
                          on_step: "Callable[[dict], Any] | None" = None) -> dict:
    """异步策略版 CAGE-2 loop，避免同步环境线程反向调用事件循环死锁。"""
    if policy is None:
        return {"error": "async policy is required"}
    try:
        from CybORG import CybORG as CybORGEnv
        from CybORG.Agents import B_lineAgent, SleepAgent
        from CybORG.Agents.SimpleAgents.Meander import RedMeanderAgent
        from CybORG.Agents.Wrappers import ChallengeWrapper
    except ImportError as exc:
        return {"error": f"CybORG import incomplete: {exc}", "install": _INSTALL_HINT}
    configured = scenario_path or os.getenv("CYBERORION_CAGE2_SCENARIO")
    if configured:
        scenario = Path(configured)
    else:
        default_root = Path(__file__).resolve().parents[3] / "benchmarks" / "external" / "cage2"
        root = Path(os.getenv("CYBERORION_CAGE2_DIR", str(default_root))).expanduser()
        candidates = list(root.rglob("Scenario2.yaml")) if root.exists() else []
        scenario = candidates[0] if candidates else root / "CybORG" / "CybORG" / "Shared" / "Scenarios" / "Scenario2.yaml"
    if not scenario.is_file():
        return {"error": f"CAGE-2 scenario not found: {scenario}", "install": _INSTALL_HINT}
    red_agents = {"B_lineAgent": B_lineAgent, "RedMeanderAgent": RedMeanderAgent,
                  "SleepAgent": SleepAgent}
    if red_agent not in red_agents:
        return {"error": f"unsupported red agent: {red_agent}"}
    random.seed(int(seed))
    try:
        import numpy as np
        np.random.seed(int(seed))
    except ImportError:
        pass
    rewards = []
    for ep in range(int(episodes)):
        env = CybORGEnv(str(scenario), "sim", agents={"Red": red_agents[red_agent]})
        wrapped = ChallengeWrapper(env=env, agent_name="Blue") if official_wrapper else env
        obs = wrapped.reset() if official_wrapper else env.reset().observation
        total = 0.0
        restore_actions = illegal_actions = 0
        actions = []
        previous_transition = None
        for step_index in range(int(steps)):
            available = canonical_safe_blue_actions(wrapped) if official_wrapper else []
            observation_before = _json_safe(obs)
            spec = await _invoke_async_policy(
                policy, obs, available, ep + 1, step_index + 1, int(steps),
                previous_transition)
            if official_wrapper:
                action_index, executed_blue_action, valid, invalid_reason = _selected_action(
                    spec, available)
                # Execute the exact index selected from the same current enum
                # table.  No action object reconstruction or approximate search.
                action = _enum_actions(wrapped)[action_index]
            else:
                action, valid, invalid_reason = _mapped_action(spec)
                action_index = None
                executed_blue_action = {
                    "action_id": None, "action_type": type(action).__name__,
                    "display": str(action),
                }
            restore_actions += int(action.__class__.__name__.lower() == "restore")
            illegal_actions += int(not valid)
            if official_wrapper:
                obs, reward, done, info = wrapped.step(action_index)
            else:
                result = env.step(agent="Blue", action=action)
                obs, reward, done, info = (result.observation, result.reward,
                                           result.done, getattr(result, "info", {}))
            total += float(reward or 0.0)
            actions.append({"blue": str(action),
                            "requested_blue_action": _json_safe(spec),
                            "executed_blue_action": _json_safe(executed_blue_action),
                            "red": str(env.get_last_action("Red")),
                            "valid": valid, "invalid_reason": invalid_reason,
                            "reward": float(reward or 0.0)})
            controller = (spec.get("_cyberorion")
                          if isinstance(spec, dict)
                          and isinstance(spec.get("_cyberorion"), dict) else {})
            previous_transition = {
                "step": step_index + 1,
                "observation_before": observation_before,
                "requested_blue_action": {
                    "action_id": spec.get("action_id")
                } if isinstance(spec, dict) else None,
                "executed_blue_action": _json_safe(executed_blue_action),
                "controller_status": str(
                    controller.get("status") or ("selected" if valid else "invalid")),
                "fallback_reason": controller.get("fallback_reason"),
                "valid": bool(valid),
                "invalid_reason": invalid_reason,
                "done": bool(done),
            }
            if on_step is not None:
                step_event = {
                    "episode": ep + 1,
                    "step": step_index + 1,
                    "horizon": int(steps),
                    "reward_delta": float(reward or 0.0),
                    "cumulative_episode_reward": round(total, 6),
                    "action": _json_safe(actions[-1]),
                    "transition": _json_safe(previous_transition),
                    "done": bool(done),
                }
                callback_result = on_step(step_event)
                if inspect.isawaitable(callback_result):
                    await callback_result
            if done:
                break
        rewards.append({
            "episode": ep + 1, "reward": round(total, 3), "red_agent": red_agent,
            "steps": len(actions), "actions": actions, "illegal_actions": illegal_actions,
            "restore_actions": restore_actions,
            # CyberOrion 自定义的非原生 Restore 成本代理，不是官方 CAGE
            # availability 组件。
            "restore_cost_proxy": -float(restore_actions),
            "host_compromise_events": None,
            "host_compromise_metric_status": "not_exposed_by_official_wrapper",
        })
    return {"episodes": rewards,
            "mean_reward": round(sum(r["reward"] for r in rewards) / len(rewards), 3)
            if rewards else 0.0,
            "llm_driven": True, "steps": int(steps), "red_agent": red_agent,
            "seed": int(seed), "wrapper": "ChallengeWrapper" if official_wrapper else "raw"}

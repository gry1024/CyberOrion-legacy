"""CAGE-2 分段执行、原子持久化、恢复与确定性归并。"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import os
import shutil
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable

from . import cage2
from .external_common import git_provenance, model_metadata

PROTOCOL_VERSION = "cage2_segmented_v1"
SEGMENT_SCHEMA_VERSION = 1
DEFAULT_WINDOW_STEPS = 25
DEFAULT_MAX_SEGMENT_SEC = 420.0
MAX_SEGMENT_SEC_HARD_CAP = 600.0
ARM_MODES = {
    "single": "single",
    "orchestrator_only": "orchestrator_only",
    "full": "agent",
}


class SegmentError(RuntimeError):
    """分段 manifest、provenance 或覆盖校验失败。"""


class SegmentInterrupted(RuntimeError):
    """测试或外部中断留下了可恢复的部分 run。"""


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), default=str)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    data = json.dumps(value, ensure_ascii=False, indent=2, default=str)
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _segment_ranges(horizon: int, window_steps: int) -> list[tuple[int, int]]:
    return [(start, min(horizon, start + window_steps - 1))
            for start in range(1, horizon + 1, window_steps)]


def _job_id(arm: str, red_agent: str, horizon: int,
            seed: int, episode: int) -> str:
    return (f"cage2/{arm}/{red_agent}/h{horizon}/seed{seed}/"
            f"ep{episode}")


def _segment_id(job_id: str, start: int, end: int) -> str:
    return f"{job_id}/steps{start:03d}-{end:03d}"


def build_manifest(*, run_id: str, arms: Iterable[str],
                   red_agents: Iterable[str], horizons: Iterable[int],
                   seeds: Iterable[int], episodes_per_seed: int,
                   budget_profile: str, step_budget: dict[str, Any],
                   source_provenance: dict[str, Any] | None = None,
                   model_settings: dict[str, Any] | None = None,
                   window_steps: int = DEFAULT_WINDOW_STEPS,
                   max_segment_sec: float = DEFAULT_MAX_SEGMENT_SEC) -> dict[str, Any]:
    """生成顺序稳定的不可变 episode/segment manifest。

    环境对同一 seed 是确定性的：每个 job 都调用 ``run_bench(n=1, seed=job["seed"])``，
    因此 ``episodes_per_seed > 1`` 会用同一环境 seed 重复生成 episode，而非独立
    复现。publication/校准一律要求 ``episodes_per_seed == 1``，独立复现必须通过
    manifest 中显式不同的 seed 表达。重复 seed 同样被拒绝，避免无意的重复环境复现。
    """
    if window_steps <= 0 or episodes_per_seed <= 0:
        raise ValueError("window_steps and episodes_per_seed must be positive")
    if episodes_per_seed != 1:
        raise ValueError(
            "episodes_per_seed must be 1: the environment is deterministic per "
            "seed, so multiple episodes per seed would rerun the same environment "
            "instead of producing independent replicates; express replicates as "
            "distinct seeds in the manifest")
    if not (0 < max_segment_sec <= MAX_SEGMENT_SEC_HARD_CAP):
        raise ValueError(
            f"max_segment_sec must be in (0, {MAX_SEGMENT_SEC_HARD_CAP}]")
    selected_arms = tuple(arms)
    unknown = set(selected_arms) - set(ARM_MODES)
    if not selected_arms or unknown:
        raise ValueError(f"unsupported CAGE arms: {sorted(unknown)}")
    source = dict(source_provenance or git_provenance())
    settings = dict(model_settings or model_metadata())
    selected_red = tuple(red_agents)
    selected_horizons = tuple(map(int, horizons))
    selected_seeds = tuple(map(int, seeds))
    if not selected_red or not selected_horizons or not selected_seeds:
        raise ValueError("red_agents, horizons and seeds must not be empty")
    if len(set(selected_seeds)) != len(selected_seeds):
        raise ValueError(
            "duplicate seeds would rerun identical environments; independent "
            "replicates require distinct seeds in the manifest")
    jobs = []
    # condition/seed 外层、arm 内层，让三个 paired arm 尽早形成可用样本，
    # 同时仍保持严格串行，避免共享 CybORG 状态并发。
    for red_agent in selected_red:
        if red_agent not in cage2.RED_AGENTS:
            raise ValueError(f"unsupported red agent: {red_agent}")
        for horizon in selected_horizons:
            for seed in selected_seeds:
                for episode in range(1, episodes_per_seed + 1):
                    for arm in selected_arms:
                        job_id = _job_id(arm, red_agent, horizon, seed, episode)
                        ranges = _segment_ranges(horizon, window_steps)
                        jobs.append({
                            "job_id": job_id, "arm": arm,
                            "mode": ARM_MODES[arm], "red_agent": red_agent,
                            "horizon": horizon, "seed": seed,
                            "episode": episode,
                            "segment_ids": [
                                _segment_id(job_id, start, end)
                                for start, end in ranges],
                        })
    immutable = {
        "protocol_version": PROTOCOL_VERSION,
        "source_provenance": source,
        "model_settings": settings,
        "budget_profile": budget_profile,
        "step_budget": dict(step_budget),
        "window_steps": int(window_steps),
        "max_segment_sec": float(max_segment_sec),
        "jobs": jobs,
    }
    return {
        "schema_version": SEGMENT_SCHEMA_VERSION,
        "run_id": run_id,
        "created_at": time.time(),
        **immutable,
        "immutable_sha256": _digest(immutable),
    }


def _immutable(manifest: dict[str, Any]) -> dict[str, Any]:
    return {key: manifest.get(key) for key in (
        "protocol_version", "source_provenance", "model_settings",
        "budget_profile", "step_budget", "window_steps", "max_segment_sec",
        "jobs")}


def validate_manifest(manifest: dict[str, Any]) -> None:
    if manifest.get("protocol_version") != PROTOCOL_VERSION:
        raise SegmentError("CAGE segmented protocol version mismatch")
    if manifest.get("immutable_sha256") != _digest(_immutable(manifest)):
        raise SegmentError("CAGE segmented manifest hash mismatch")
    ids = [segment_id for job in manifest.get("jobs") or []
           for segment_id in job.get("segment_ids") or []]
    if len(ids) != len(set(ids)):
        raise SegmentError("duplicate segment IDs in manifest")


def create_run(run_dir: str | Path, manifest: dict[str, Any],
               *, require_clean: bool = True) -> Path:
    directory = Path(run_dir).resolve()
    manifest_path = directory / "manifest.json"
    if manifest_path.exists():
        raise SegmentError(f"run already exists: {directory}")
    validate_manifest(manifest)
    source = manifest.get("source_provenance") or {}
    complete = all(source.get(key) is not None for key in (
        "git_head_sha", "git_tree_sha", "git_dirty"))
    if not complete or (require_clean and source.get("git_dirty") is not False):
        raise SegmentError("segmented run requires complete clean source provenance")
    (directory / "segments").mkdir(parents=True, exist_ok=False)
    (directory / "attempts").mkdir(parents=True, exist_ok=False)
    _atomic_json(manifest_path, manifest)
    _write_live_files(directory, manifest, current_condition=None,
                      status="ready")
    return directory


def load_run(run_dir: str | Path, *, current_source: dict[str, Any] | None = None,
             current_model: dict[str, Any] | None = None,
             budget_profile: str | None = None,
             step_budget: dict[str, Any] | None = None) -> tuple[Path, dict[str, Any]]:
    directory = Path(run_dir).resolve()
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    validate_manifest(manifest)
    checks = {
        "source provenance": (current_source, manifest.get("source_provenance")),
        "model settings": (current_model, manifest.get("model_settings")),
        "budget profile": (budget_profile, manifest.get("budget_profile")),
        "step budget": (step_budget, manifest.get("step_budget")),
    }
    for label, (current, expected) in checks.items():
        if current is not None and current != expected:
            raise SegmentError(f"resume refused: {label} mismatch")
    return directory, manifest


def _segment_path(run_dir: Path, segment_id: str) -> Path:
    return run_dir / "segments" / f"{segment_id}.json"


def _segment_files(run_dir: Path) -> list[Path]:
    return sorted((run_dir / "segments").rglob("*.json"))


def _read_segments(run_dir: Path) -> list[tuple[Path, dict[str, Any]]]:
    rows = []
    for path in _segment_files(run_dir):
        rows.append((path, json.loads(path.read_text(encoding="utf-8"))))
    return rows


def _sum_resources(traces: list[dict[str, Any]]) -> dict[str, Any]:
    totals: Counter[str] = Counter()
    roles: Counter[str] = Counter()
    fallback = exhaustion = 0
    for trace in traces:
        used = trace.get("step_resource_usage") or {}
        for key in ("llm_calls", "tool_calls", "estimated_tokens",
                    "budget_accounted_tokens", "provider_prompt_tokens",
                    "provider_completion_tokens", "provider_total_tokens",
                    "dispatches", "wall_clock_sec"):
            value = used.get(key)
            if isinstance(value, (int, float)):
                totals[key] += value
        fallback += int(trace.get("selected_or_fallback") == "fallback")
        exhaustion += int(trace.get("budget_status") in {
            "exhausted", "violation", "timeout", "episode_safety_ceiling"})
        roles.update(map(str, trace.get("dispatched_roles") or []))
    return {
        **dict(totals), "fallback_count": fallback,
        "exhaustion_count": exhaustion,
        "dispatch_roles": dict(sorted(roles.items())),
    }


def _write_provisional(run_dir: Path, manifest: dict[str, Any], job: dict[str, Any],
                       events: list[dict[str, Any]], window_start: int,
                       window_end: int, segment_started: float,
                       clock: Callable[[], float] = time.perf_counter) -> dict[str, Any]:
    observed_start = int(events[0]["step"])
    observed_end = int(events[-1]["step"])
    # segment_id 由实际落盘步范围推导，保证 wall-time flush 把同一 step-window
    # 拆成多个更细段时 id 仍唯一，不会与其它 segment 冲突。
    segment_id = _segment_id(job["job_id"], observed_start, observed_end)
    traces = [event.get("agent_trace") or {} for event in events]
    payload = {
        "schema_version": SEGMENT_SCHEMA_VERSION,
        "protocol_version": PROTOCOL_VERSION,
        "segment_id": segment_id, "job_id": job["job_id"],
        "arm": job["arm"], "mode": job["mode"],
        "red_agent": job["red_agent"], "horizon": job["horizon"],
        "seed": job["seed"], "episode": job["episode"],
        "planned_step_range": [window_start, window_end],
        "observed_step_range": [observed_start, observed_end],
        "source_provenance": manifest["source_provenance"],
        "model_settings": manifest["model_settings"],
        "budget_profile": manifest["budget_profile"],
        "step_budget": manifest["step_budget"],
        "manifest_sha256": manifest["immutable_sha256"],
        "raw_trace": traces,
        "reward_delta": round(sum(float(event.get("reward_delta") or 0)
                                  for event in events), 6),
        "cumulative_episode_reward": events[-1].get("cumulative_episode_reward"),
        "actions": [event.get("action") for event in events],
        "resource_usage": _sum_resources(traces),
        "segment_wall_clock_sec": round(clock() - segment_started, 4),
        "episode_complete": False,
        "episode_committed": False,
        "provisional_reason": "mid_episode_state_not_serializable",
    }
    _atomic_json(_segment_path(run_dir, segment_id), payload)
    return payload


def _job_segments(run_dir: Path, job_id: str) -> list[tuple[Path, dict[str, Any]]]:
    return [(path, row) for path, row in _read_segments(run_dir)
            if row.get("job_id") == job_id]


def _archive_provisional(run_dir: Path, job_id: str) -> None:
    rows = [(path, row) for path, row in _job_segments(run_dir, job_id)
            if not row.get("episode_committed")]
    if not rows:
        return
    target = run_dir / "attempts" / f"{int(time.time() * 1000)}"
    for path, _ in rows:
        relative = path.relative_to(run_dir / "segments")
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), str(destination))


def _commit_episode(run_dir: Path, manifest: dict[str, Any], job: dict[str, Any],
                    episode_run: dict[str, Any]) -> None:
    rows = sorted(_job_segments(run_dir, job["job_id"]),
                  key=lambda item: item[1]["observed_step_range"][0])
    if not rows:
        raise SegmentError(f"episode emitted no segments: {job['job_id']}")
    expected_start = 1
    for _, row in rows:
        observed = row["observed_step_range"]
        if observed[0] != expected_start:
            raise SegmentError(f"non-contiguous episode segments: {job['job_id']}")
        expected_start = observed[1] + 1
    result_rows = episode_run.get("results") or []
    if len(result_rows) != 1:
        raise SegmentError(f"episode child run returned {len(result_rows)} results")
    for index, (path, row) in enumerate(rows):
        row["episode_committed"] = True
        row["provisional_reason"] = None
        row["episode_complete"] = index == len(rows) - 1
        if row["episode_complete"]:
            row["episode_result"] = result_rows[0]
            resources = episode_run.get("episode_resource_usage") or []
            row["episode_resource_usage"] = resources[0] if resources else None
        _atomic_json(path, row)


def _committed_episode_jobs(run_dir: Path) -> set[str]:
    return {row["job_id"] for _, row in _read_segments(run_dir)
            if row.get("episode_committed") and row.get("episode_complete")}


def reduce_run(run_dir: str | Path, *, require_complete: bool = True) -> dict[str, Any]:
    directory, manifest = load_run(run_dir)
    planned = {segment_id for job in manifest["jobs"]
               for segment_id in job["segment_ids"]}
    known_jobs = {job["job_id"] for job in manifest["jobs"]}
    rows = _read_segments(directory)
    ids = [str(row.get("segment_id")) for _, row in rows]
    duplicates = sorted(segment_id for segment_id, count in Counter(ids).items()
                        if count > 1)
    if duplicates:
        raise SegmentError(f"duplicate segment IDs: {duplicates}")
    foreign_jobs = sorted({row.get("job_id") for _, row in rows
                           if row.get("job_id") not in known_jobs})
    if foreign_jobs:
        raise SegmentError(f"segments from unknown jobs: {foreign_jobs}")
    for _, row in rows:
        if row.get("manifest_sha256") != manifest["immutable_sha256"]:
            raise SegmentError(f"segment provenance mismatch: {row.get('segment_id')}")
    committed = [row for _, row in rows if row.get("episode_committed")]
    completed_jobs = {row["job_id"] for row in committed
                      if row.get("episode_complete")}
    missing_jobs = [job["job_id"] for job in manifest["jobs"]
                    if job["job_id"] not in completed_jobs]
    for job in manifest["jobs"]:
        if job["job_id"] not in completed_jobs:
            continue
        job_rows = sorted((row for row in committed
                           if row["job_id"] == job["job_id"]),
                          key=lambda row: row["observed_step_range"][0])
        if sum(bool(row.get("episode_complete")) for row in job_rows) != 1:
            raise SegmentError(f"invalid episode completion: {job['job_id']}")
        expected_start = 1
        for row in job_rows:
            if row["observed_step_range"][0] != expected_start:
                raise SegmentError(f"missing segment coverage: {job['job_id']}")
            expected_start = row["observed_step_range"][1] + 1
        final_steps = int((job_rows[-1].get("episode_result") or {}).get("steps") or 0)
        if final_steps != expected_start - 1:
            raise SegmentError(f"episode step coverage mismatch: {job['job_id']}")
    complete = not missing_jobs
    if require_complete and not complete:
        raise SegmentError(f"missing completed episodes: {missing_jobs}")
    episode_rows = [row for row in committed if row.get("episode_complete")]
    results = [row["episode_result"] for row in episode_rows]
    rewards = [float(row.get("reward") or 0) for row in results]
    resource_totals: Counter[str] = Counter()
    for row in committed:
        for key, value in (row.get("resource_usage") or {}).items():
            if isinstance(value, (int, float)):
                resource_totals[key] += value
    source = manifest.get("source_provenance") or {}
    provenance_valid = (source.get("git_dirty") is False and all(
        source.get(key) is not None for key in ("git_head_sha", "git_tree_sha")))
    limit_violations = sorted({dimension
        for row in episode_rows
        for dimension in ((row.get("episode_resource_usage") or {}).get(
            "budget_limit_violations") or [])})
    publication_valid = complete and provenance_valid and not limit_violations
    return {
        "schema_version": SEGMENT_SCHEMA_VERSION,
        "protocol_version": PROTOCOL_VERSION,
        "run_id": manifest["run_id"],
        "status": "done" if complete else "partial",
        "publication_valid": publication_valid,
        "publication_invalid_reasons": [name for name, failed in (
            ("missing_episode_coverage", not complete),
            ("source_provenance", not provenance_valid),
            ("resource_limit_violation", bool(limit_violations))) if failed],
        "completed_segments": len(committed),
        "observed_segments": len(rows),
        "expected_segments": len(planned),
        "completed_episodes": len(completed_jobs),
        "total_episodes": len(manifest["jobs"]),
        "missing_jobs": missing_jobs,
        "results": results,
        "mean_reward": (sum(rewards) / len(rewards)) if rewards else None,
        "resource_totals": dict(resource_totals),
        "budget_limit_violations": limit_violations,
        "manifest_sha256": manifest["immutable_sha256"],
    }


def _interim(run_dir: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    reduced = reduce_run(run_dir, require_complete=False)
    episode_rows = [row for _, row in _read_segments(run_dir)
                    if row.get("episode_committed") and row.get("episode_complete")]
    by_key: dict[tuple[Any, ...], dict[str, float]] = defaultdict(dict)
    arm_rewards: dict[str, list[float]] = defaultdict(list)
    for row in episode_rows:
        reward = float((row.get("episode_result") or {}).get("reward") or 0)
        key = (row["red_agent"], row["horizon"], row["seed"], row["episode"])
        by_key[key][row["arm"]] = reward
        arm_rewards[row["arm"]].append(reward)
    pairs = [arms for arms in by_key.values()
             if "full" in arms and "single" in arms]
    return {
        "label": "INTERIM / NOT FINAL" if reduced["status"] != "done" else "FINAL",
        **{key: reduced[key] for key in (
            "completed_segments", "observed_segments", "expected_segments",
            "completed_episodes", "total_episodes", "resource_totals")},
        "interim_mean_rewards": {
            arm: sum(values) / len(values) for arm, values in sorted(arm_rewards.items())},
        "paired_samples_available": len(pairs),
        "interim_full_minus_single": (
            sum(pair["full"] - pair["single"] for pair in pairs) / len(pairs)
            if pairs else None),
        "publication_valid": reduced["publication_valid"],
    }


def _write_live_files(run_dir: Path, manifest: dict[str, Any],
                      *, current_condition: str | None, status: str) -> None:
    interim = _interim(run_dir, manifest)
    progress = {
        "label": interim["label"], "status": status,
        "current_condition": current_condition,
        "completed_segments": interim["completed_segments"],
        "observed_segments": interim["observed_segments"],
        "total_segments": interim["expected_segments"],
        "completed_episodes": interim["completed_episodes"],
        "total_episodes": interim["total_episodes"],
        "updated_at": time.time(),
        "resume_command": f"~/cai_env/bin/python scripts/run_cage_segmented.py --resume {run_dir}",
    }
    _atomic_json(run_dir / "progress.json", progress)
    _atomic_json(run_dir / "interim_summary.json", interim)


EpisodeExecutor = Callable[[dict[str, Any], Callable[[dict[str, Any]], Awaitable[None]]],
                           Awaitable[dict[str, Any]]]


async def _default_executor(job: dict[str, Any], on_step, *, manifest: dict[str, Any]) -> dict:
    return await cage2.run_bench(
        n=1, mode=job["mode"], seed=job["seed"], profile="daily",
        cage_budget_profile=manifest["budget_profile"],
        cage_step_budget=manifest["step_budget"],
        condition_steps=(job["horizon"],), red_agents=(job["red_agent"],),
        on_environment_step=on_step, persist_result=False,
        source_provenance=manifest["source_provenance"])


async def run_segmented(run_dir: str | Path, manifest: dict[str, Any],
                        *, executor: EpisodeExecutor | None = None,
                        clock: Callable[[], float] = time.perf_counter) -> dict[str, Any]:
    """顺序执行 manifest；仅当前未完成 episode 可在恢复时重跑。

    分段边界为「step-window 或 wall-time 阈值」二者取先触发者：每完成一个环境步，
    若当前段已累计超过 ``manifest["max_segment_sec"]`` 秒，则立即以该步为界落盘，
    保证任何已落盘段都不会无限膨胀。step-window 分段仍然保留，作为常规分界。
    """
    directory = Path(run_dir).resolve()
    validate_manifest(manifest)
    window_steps = int(manifest["window_steps"])
    max_segment_sec = float(manifest.get(
        "max_segment_sec", DEFAULT_MAX_SEGMENT_SEC))
    completed = _committed_episode_jobs(directory)

    def _flush(events: list[dict[str, Any]]) -> None:
        """以 events 实际步范围落盘一段。"""
        last = int(events[-1]["step"])
        window_start = ((last - 1) // window_steps) * window_steps + 1
        window_end = min(int(job["horizon"]), window_start + window_steps - 1)
        _write_provisional(directory, manifest, job, events,
                           window_start, window_end, segment_started, clock=clock)

    try:
        for job in manifest["jobs"]:
            if job["job_id"] in completed:
                continue
            _archive_provisional(directory, job["job_id"])
            current = (f"{job['arm']} {job['red_agent']} h{job['horizon']} "
                       f"seed{job['seed']} ep{job['episode']}")
            _write_live_files(directory, manifest, current_condition=current,
                              status="running")
            events: list[dict[str, Any]] = []
            segment_started = clock()

            async def on_step(event: dict[str, Any]) -> None:
                nonlocal events, segment_started
                events.append(event)
                step = int(event["step"])
                boundary = step % window_steps == 0
                time_exceeded = (clock() - segment_started) >= max_segment_sec
                if boundary or time_exceeded or event.get("done"):
                    _flush(events)
                    events = []
                    segment_started = clock()
                    _write_live_files(directory, manifest,
                                      current_condition=current, status="running")

            if executor is None:
                episode_run = await _default_executor(
                    job, on_step, manifest=manifest)
            else:
                episode_run = executor(job, on_step)
                if inspect.isawaitable(episode_run):
                    episode_run = await episode_run
            if events:
                _flush(events)
            _commit_episode(directory, manifest, job, episode_run)
            completed.add(job["job_id"])
            _write_live_files(directory, manifest, current_condition=current,
                              status="running")
    except (asyncio.CancelledError, KeyboardInterrupt, SegmentInterrupted):
        _write_live_files(directory, manifest, current_condition=current,
                          status="interrupted")
        raise
    final = reduce_run(directory, require_complete=True)
    _atomic_json(directory / "final.json", final)
    _write_live_files(directory, manifest, current_condition=None, status="done")
    return final


__all__ = [
    "ARM_MODES", "DEFAULT_WINDOW_STEPS", "DEFAULT_MAX_SEGMENT_SEC",
    "MAX_SEGMENT_SEC_HARD_CAP", "PROTOCOL_VERSION", "SegmentError",
    "SegmentInterrupted", "build_manifest", "create_run", "load_run",
    "reduce_run", "run_segmented",
]

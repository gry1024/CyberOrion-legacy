"""CAGE-2 分段执行、恢复与 reducer 的快速确定性测试。"""

from __future__ import annotations

import asyncio
import json
import shutil
from collections import Counter
from pathlib import Path

import pytest

from cyberorion.bench.cage_segments import (
    MAX_SEGMENT_SEC_HARD_CAP, SegmentError, SegmentInterrupted, build_manifest,
    create_run, load_run, reduce_run, run_segmented,
)


SOURCE = {
    "git_head_sha": "a" * 40,
    "git_tree_sha": "b" * 40,
    "git_dirty": False,
    "git_diff_sha256": None,
}
MODEL = {
    "provider": "openai", "model": "MiniMax-M3",
    "configured_model": "openai/MiniMax-M3", "temperature": 0.0,
    "thinking": "disabled",
}
BUDGET = {
    "max_steps": 7, "max_llm_calls": 5, "max_tool_calls": 4,
    "max_dispatches": 3, "max_role_steps": 4,
    "token_budget": 24576, "wall_clock_sec": 90.0,
}


def _manifest(*, run_id: str = "seg-test", arms=("single",), seeds=(101,),
              window_steps: int = 2, max_segment_sec: float = 420.0,
              horizons=(4,)) -> dict:
    return build_manifest(
        run_id=run_id, arms=arms, red_agents=("B_lineAgent",),
        horizons=horizons, seeds=seeds, episodes_per_seed=1,
        budget_profile="publication_v1", step_budget=BUDGET,
        source_provenance=SOURCE, model_settings=MODEL,
        window_steps=window_steps, max_segment_sec=max_segment_sec)


def _event(step: int, horizon: int) -> dict:
    return {
        "step": step, "horizon": horizon, "reward_delta": -1.0,
        "cumulative_episode_reward": -float(step), "done": step == horizon,
        "action": {"executed_blue_action": {"action_id": step},
                   "reward": -1.0},
        "agent_trace": {
            "step": step, "selected_or_fallback": "selected",
            "budget_status": "ok", "dispatched_roles": [],
            "step_resource_usage": {
                "llm_calls": 1, "tool_calls": 1,
                "budget_accounted_tokens": 100,
                "provider_total_tokens": 100,
                "wall_clock_sec": 0.01, "dispatches": 0,
            },
        },
    }


def _executor(counter: Counter[str], *, interrupt_job: str | None = None,
              interrupt_step: int | None = None):
    async def execute(job, on_step):
        counter[job["job_id"]] += 1
        for step in range(1, job["horizon"] + 1):
            await on_step(_event(step, job["horizon"]))
            if job["job_id"] == interrupt_job and step == interrupt_step:
                raise SegmentInterrupted("synthetic interrupt")
        return {
            "results": [{
                "episode": 1, "reward": -float(job["horizon"]),
                "steps": job["horizon"], "actions": [],
                "illegal_actions": 0, "restore_actions": 0,
                "restore_cost_proxy": 0.0,
            }],
            "episode_resource_usage": [{"budget_limit_violations": []}],
        }
    return execute


def test_interrupt_resume_equals_uninterrupted_and_incomplete_not_counted(
        tmp_path: Path) -> None:
    manifest = _manifest()
    interrupted_dir = create_run(tmp_path / "interrupted", manifest)
    job_id = manifest["jobs"][0]["job_id"]
    calls = Counter()
    with pytest.raises(SegmentInterrupted):
        asyncio.run(run_segmented(
            interrupted_dir, manifest,
            executor=_executor(calls, interrupt_job=job_id, interrupt_step=2)))
    partial = reduce_run(interrupted_dir, require_complete=False)
    assert partial["observed_segments"] == 1
    assert partial["completed_segments"] == 0
    assert partial["completed_episodes"] == 0
    assert partial["publication_valid"] is False

    resumed = asyncio.run(run_segmented(
        interrupted_dir, manifest, executor=_executor(calls)))
    clean_manifest = _manifest(run_id="clean")
    clean_dir = create_run(tmp_path / "clean", clean_manifest)
    uninterrupted = asyncio.run(run_segmented(
        clean_dir, clean_manifest, executor=_executor(Counter())))
    assert resumed["results"] == uninterrupted["results"]
    assert resumed["resource_totals"] == uninterrupted["resource_totals"]
    assert calls[job_id] == 2
    assert list((interrupted_dir / "attempts").rglob("*.json"))


def test_completed_episodes_are_not_rerun(tmp_path: Path) -> None:
    manifest = _manifest(seeds=(101, 102))
    run_dir = create_run(tmp_path / "run", manifest)
    first, second = (job["job_id"] for job in manifest["jobs"])
    calls = Counter()
    with pytest.raises(SegmentInterrupted):
        asyncio.run(run_segmented(
            run_dir, manifest,
            executor=_executor(calls, interrupt_job=second, interrupt_step=2)))
    assert calls[first] == 1
    asyncio.run(run_segmented(run_dir, manifest, executor=_executor(calls)))
    assert calls[first] == 1
    assert calls[second] == 2


def test_duplicate_segment_ids_are_rejected(tmp_path: Path) -> None:
    manifest = _manifest()
    run_dir = create_run(tmp_path / "run", manifest)
    asyncio.run(run_segmented(run_dir, manifest, executor=_executor(Counter())))
    source = next((run_dir / "segments").rglob("*.json"))
    duplicate = run_dir / "segments" / "duplicate.json"
    shutil.copy2(source, duplicate)
    with pytest.raises(SegmentError, match="duplicate segment IDs"):
        reduce_run(run_dir)


def test_resume_refuses_provenance_model_and_budget_mismatch(tmp_path: Path) -> None:
    manifest = _manifest()
    run_dir = create_run(tmp_path / "run", manifest)
    with pytest.raises(SegmentError, match="source provenance mismatch"):
        load_run(run_dir, current_source={**SOURCE, "git_tree_sha": "c" * 40})
    with pytest.raises(SegmentError, match="model settings mismatch"):
        load_run(run_dir, current_model={**MODEL, "model": "different"})
    with pytest.raises(SegmentError, match="budget profile mismatch"):
        load_run(run_dir, budget_profile="publication_v2")
    with pytest.raises(SegmentError, match="step budget mismatch"):
        load_run(run_dir, step_budget={**BUDGET, "token_budget": 1})


def test_final_can_be_rebuilt_from_segments(tmp_path: Path) -> None:
    manifest = _manifest()
    run_dir = create_run(tmp_path / "run", manifest)
    final = asyncio.run(run_segmented(
        run_dir, manifest, executor=_executor(Counter())))
    (run_dir / "final.json").unlink()
    rebuilt = reduce_run(run_dir)
    assert rebuilt == final
    assert rebuilt["publication_valid"] is True
    interim = json.loads((run_dir / "interim_summary.json").read_text())
    progress = json.loads((run_dir / "progress.json").read_text())
    assert interim["label"] == "FINAL"
    assert progress["status"] == "done"


# --- seed / replicate 语义 ---


def test_episodes_per_seed_must_be_one(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="episodes_per_seed must be 1"):
        build_manifest(
            run_id="dup", arms=("single",), red_agents=("B_lineAgent",),
            horizons=(4,), seeds=(101,), episodes_per_seed=2,
            budget_profile="publication_v1", step_budget=BUDGET,
            source_provenance=SOURCE, model_settings=MODEL)


def test_duplicate_seeds_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="duplicate seeds"):
        build_manifest(
            run_id="dup", arms=("single",), red_agents=("B_lineAgent",),
            horizons=(4,), seeds=(101, 101), episodes_per_seed=1,
            budget_profile="publication_v1", step_budget=BUDGET,
            source_provenance=SOURCE, model_settings=MODEL)


def test_max_segment_sec_respects_hard_cap(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="max_segment_sec"):
        build_manifest(
            run_id="cap", arms=("single",), red_agents=("B_lineAgent",),
            horizons=(4,), seeds=(101,), episodes_per_seed=1,
            budget_profile="publication_v1", step_budget=BUDGET,
            source_provenance=SOURCE, model_settings=MODEL,
            max_segment_sec=MAX_SEGMENT_SEC_HARD_CAP + 1.0)


# --- wall-time 分段 flush ---


class _FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _timed_executor(clock: "_FakeClock", step_sec: float,
                    counter: Counter[str] | None = None):
    async def execute(job, on_step):
        if counter is not None:
            counter[job["job_id"]] += 1
        for step in range(1, job["horizon"] + 1):
            clock.advance(step_sec)
            await on_step(_event(step, job["horizon"]))
        return {
            "results": [{
                "episode": 1, "reward": -float(job["horizon"]),
                "steps": job["horizon"], "actions": [],
                "illegal_actions": 0, "restore_actions": 0,
                "restore_cost_proxy": 0.0,
            }],
            "episode_resource_usage": [{"budget_limit_violations": []}],
        }
    return execute


def test_time_triggered_segment_flush_splits_window(tmp_path: Path) -> None:
    # window 大到 step 边界永不触发；只有 wall-time 阈值会触发 flush。
    clock = _FakeClock()
    manifest = _manifest(window_steps=100, max_segment_sec=90.0)
    run_dir = create_run(tmp_path / "run", manifest)
    final = asyncio.run(run_segmented(
        run_dir, manifest, clock=clock,
        executor=_timed_executor(clock, step_sec=30.0)))
    # 4 步，每步 30s：step3 时 elapsed=90 触发 flush([1,3])，step4 done 触发
    # flush([4,4])。step-window=100 永不触发，因此应得 2 段而非 1 段。
    assert final["observed_segments"] == 2
    assert final["completed_episodes"] == 1
    segments = [json.loads(p.read_text()) for p in
                sorted((run_dir / "segments").rglob("*.json"))]
    ranges = sorted(tuple(row["observed_step_range"]) for row in segments)
    assert ranges == [(1, 3), (4, 4)]


def test_no_segment_exceeds_duration_policy(tmp_path: Path) -> None:
    clock = _FakeClock()
    max_segment_sec = 90.0
    step_sec = 30.0
    manifest = _manifest(horizons=(8,), window_steps=100,
                         max_segment_sec=max_segment_sec)
    run_dir = create_run(tmp_path / "run", manifest)
    final = asyncio.run(run_segmented(
        run_dir, manifest, clock=clock,
        executor=_timed_executor(clock, step_sec=step_sec)))
    assert final["completed_episodes"] == 1
    segments = [json.loads(p.read_text()) for p in
                (run_dir / "segments").rglob("*.json")]
    # flush 只在环境步边界检查，因此每段 wall clock 至多超过阈值一个步长。
    for row in segments:
        assert row["segment_wall_clock_sec"] <= max_segment_sec + step_sec
    # 8 步 × 30s = 240s；若只有 step-window 分段将是一段 240s，远超 90s 阈值。
    assert len(segments) >= 3

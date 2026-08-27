#!/usr/bin/env python3
"""运行或恢复可增量归并的 CAGE-2 episode/step-window benchmark。"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent
for candidate in (str(_REPO), str(_REPO.parent)):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)

from cyberorion.bench.cage2 import CAGE_STEP_BUDGETS
from cyberorion.bench.cage_segments import (
    ARM_MODES, DEFAULT_MAX_SEGMENT_SEC, SegmentError, build_manifest,
    create_run, load_run, run_segmented,
)
from cyberorion.bench.external_common import git_provenance, model_metadata


def _csv(raw: str, cast=str) -> tuple:
    return tuple(cast(value.strip()) for value in raw.split(",") if value.strip())


async def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--run-dir", help="新 run 的目录（建议位于 worktree 外）")
    target.add_argument("--resume", help="已有 segmented run 目录")
    parser.add_argument("--arms", default="single,orchestrator_only,full")
    parser.add_argument("--red-agents", default="B_lineAgent,RedMeanderAgent,SleepAgent")
    parser.add_argument("--horizons", default="30,50,100")
    parser.add_argument("--seeds", default="101,102,103")
    parser.add_argument("--episodes-per-seed", type=int, default=1)
    parser.add_argument("--window-steps", type=int, default=25)
    parser.add_argument("--max-segment-sec", type=float,
                        default=DEFAULT_MAX_SEGMENT_SEC)
    parser.add_argument("--budget-profile", default="publication_v1",
                        choices=sorted(CAGE_STEP_BUDGETS))
    args = parser.parse_args()

    current_source = git_provenance()
    # cage2.run_bench 对所有模型臂显式传 temperature=0；provenance 必须记录
    # 实际设置，而不是 CO_BENCH_TEMPERATURE 是否存在。
    current_model = model_metadata(temperature=0.0)
    if args.resume:
        run_dir, manifest = load_run(
            args.resume, current_source=current_source,
            current_model=current_model)
    else:
        run_id = f"{time.strftime('%Y%m%d_%H%M%S')}_cage2_segmented"
        budget = dict(CAGE_STEP_BUDGETS[args.budget_profile])
        manifest = build_manifest(
            run_id=run_id, arms=_csv(args.arms),
            red_agents=_csv(args.red_agents), horizons=_csv(args.horizons, int),
            seeds=_csv(args.seeds, int),
            episodes_per_seed=args.episodes_per_seed,
            budget_profile=args.budget_profile, step_budget=budget,
            source_provenance=current_source, model_settings=current_model,
            window_steps=args.window_steps,
            max_segment_sec=args.max_segment_sec)
        run_dir = create_run(args.run_dir, manifest)
    print(json.dumps({
        "status": "starting", "run_dir": str(run_dir),
        "run_id": manifest["run_id"], "jobs": len(manifest["jobs"]),
        "segments": sum(len(job["segment_ids"]) for job in manifest["jobs"]),
        "model": manifest["model_settings"].get("configured_model"),
        "budget_profile": manifest["budget_profile"],
        "manifest_sha256": manifest["immutable_sha256"],
    }, ensure_ascii=False, indent=2), flush=True)
    try:
        final = await run_segmented(run_dir, manifest)
    except asyncio.CancelledError:
        print(f"已安全中断。恢复命令：~/cai_env/bin/python "
              f"scripts/run_cage_segmented.py --resume {run_dir}", flush=True)
        raise
    print(json.dumps(final, ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(_main()))
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except SegmentError as exc:
        print(json.dumps({"status": "refused", "error": str(exc)},
                         ensure_ascii=False), file=sys.stderr)
        raise SystemExit(2) from None

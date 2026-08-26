#!/usr/bin/env python
"""ExCyTIn 官方 ACESEvals/Inspect/SABER 执行入口（CyberOrion 集成）。

在 ACESEvals 上游仓库自己的 uv 环境中运行（不经过 SQLite adapter，
不重写上游任务/沙箱/遥测/scorer）：

    cd benchmarks/external/excytin
    ./.venv/bin/python <repo>/scripts/run_excytin_official.py \
        --arm cyberorion_single --limit 2 \
        --log-dir /tmp/excytin-official-smoke

可用 arm：react（官方基线）、cyberorion_single、
cyberorion_orchestrator_only、cyberorion_full。
模型与 judge 默认都用 openai/deepseek-v4-flash（可用 --model / --judge-llm
覆盖），credentials 从环境变量读取，绝不打印或提交密钥。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_DEFAULT_UPSTREAM = _REPO / "benchmarks" / "external" / "excytin"
_ARMS = ("react", "cyberorion_single", "cyberorion_orchestrator_only",
         "cyberorion_full")


def _git_sha(directory: Path) -> str | None:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=directory, check=True,
            capture_output=True, text=True, timeout=10,
        ).stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def build_provenance(*, upstream: Path, repo: Path, arm: str, model: str,
                     judge_llm: str, limit: int, task_filter: str | None,
                     extra_task_args: dict[str, str], started: float,
                     finished: float, log_dir: Path) -> dict:
    """官方执行的 provenance 快照；官方执行必须是显式事实，不能推断。"""
    return {
        "runner": "scripts/run_excytin_official.py",
        "official_execution": True,
        "sqlite_projection_involved": False,
        "upstream": "microsoft/ACESEvals",
        "upstream_commit_sha": _git_sha(upstream),
        "cyberorion_commit_sha": _git_sha(repo),
        "arm": arm, "model": model, "judge_llm": judge_llm,
        "limit": limit, "task_filter": task_filter,
        "extra_task_args": extra_task_args,
        "started_at": started, "finished_at": finished,
        "log_dir": str(log_dir),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arm", choices=_ARMS, required=True)
    parser.add_argument("--limit", type=int, default=2)
    parser.add_argument("--model", default="openai/deepseek-v4-flash")
    parser.add_argument("--judge-llm", default=None)
    parser.add_argument("--task-filter", default=None)
    parser.add_argument("--log-dir", required=True)
    parser.add_argument("--acesevals-dir", default=str(_DEFAULT_UPSTREAM))
    parser.add_argument("--task-arg", action="append", default=[],
                        help="额外 -T 参数，格式 key=value")
    args = parser.parse_args()

    upstream = Path(args.acesevals_dir).resolve()
    if not (upstream / "domains" / "excytin" / "excytin.py").is_file():
        print(f"ACESEvals upstream not found at {upstream}", file=sys.stderr)
        return 2
    for path in (str(upstream), str(_REPO)):
        if path not in sys.path:
            sys.path.insert(0, path)

    from cyberorion.bench.excytin_official_agent import register_official_agents
    register_official_agents()
    from domains.excytin.excytin import excytin as excytin_task  # noqa: E402

    judge_llm = args.judge_llm or args.model
    task_kwargs: dict = {"agent": args.arm, "judge_llm": judge_llm}
    if args.task_filter:
        task_kwargs["task_filter"] = args.task_filter
    for item in args.task_arg:
        key, _, value = item.partition("=")
        if not key or not value:
            print(f"--task-arg 必须是 key=value，收到 {item!r}", file=sys.stderr)
            return 2
        task_kwargs[key] = value
    task = excytin_task(**task_kwargs)

    import inspect_ai
    log_dir = Path(args.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    started = time.time()
    inspect_ai.eval(
        task,
        model=args.model,
        limit=args.limit,
        log_dir=str(log_dir),
        display="plain",
    )
    provenance = build_provenance(
        upstream=upstream, repo=_REPO, arm=args.arm, model=args.model,
        judge_llm=judge_llm, limit=args.limit, task_filter=args.task_filter,
        extra_task_args={item.partition("=")[0]: item.partition("=")[2]
                         for item in args.task_arg},
        started=started, finished=time.time(), log_dir=log_dir)
    (log_dir / "cyberorion_official_provenance.json").write_text(
        json.dumps(provenance, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(provenance, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

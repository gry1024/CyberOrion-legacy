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
模型默认使用 openai/MiniMax-M3；judge 默认保持 ExCyTIn 官方配置
openai/azure/gpt-4.1（可用 --model / --judge-llm 覆盖）。credentials 从环境变量
读取，绝不打印或提交密钥。
"""

from __future__ import annotations

import argparse
import hashlib
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


def _git_text(directory: Path, *args: str) -> str | None:
    try:
        return subprocess.run(
            ["git", *args], cwd=directory, check=True, capture_output=True,
            text=True, timeout=20,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None


def _source_snapshot(directory: Path) -> dict:
    status = _git_text(directory, "status", "--porcelain=v1")
    diff = _git_text(directory, "diff", "--binary", "HEAD")
    untracked = _git_text(
        directory, "ls-files", "--others", "--exclude-standard")
    fingerprint = hashlib.sha256()
    fingerprint.update((diff or "").encode())
    untracked_paths = sorted(
        line for line in (untracked or "").splitlines() if line)
    for relative in untracked_paths:
        path = directory / relative
        fingerprint.update(relative.encode())
        fingerprint.update(b"\0")
        if path.is_file():
            fingerprint.update(path.read_bytes())
    return {
        "git_head": _git_sha(directory),
        "git_tree_sha": _git_text(directory, "rev-parse", "HEAD^{tree}"),
        "git_dirty": None if status is None else bool(status),
        "git_diff_sha256": (
            hashlib.sha256((diff or "").encode()).hexdigest()
            if status else None),
        "untracked_paths": untracked_paths,
        "working_tree_fingerprint_sha256": (
            fingerprint.hexdigest() if status else None),
    }


def _load_manifest(path: Path) -> tuple[list[str], str]:
    raw = path.read_bytes()
    data = json.loads(raw)
    task_ids = data.get("task_ids") if isinstance(data, dict) else None
    if not isinstance(task_ids, list) or not task_ids or not all(
            isinstance(item, str) and item for item in task_ids):
        raise ValueError("manifest.task_ids must be a non-empty string list")
    if len(set(task_ids)) != len(task_ids):
        raise ValueError("manifest.task_ids contains duplicates")
    return task_ids, hashlib.sha256(raw).hexdigest()


def build_provenance(*, upstream: Path, repo: Path, arm: str, model: str,
                     judge_llm: str, task_ids: list[str], manifest_sha256: str,
                     extra_task_args: dict[str, str], started: float,
                     finished: float, log_dir: Path, mechanism_only: bool,
                     resource_limits: dict[str, int | None]) -> dict:
    """官方执行的 provenance 快照；官方执行必须是显式事实，不能推断。"""
    return {
        "runner": "scripts/run_excytin_official.py",
        "official_execution": True,
        "sqlite_projection_involved": False,
        "upstream": "microsoft/ACESEvals",
        "upstream_commit_sha": _git_sha(upstream),
        "cyberorion_source": _source_snapshot(repo),
        "arm": arm, "model": model, "judge_llm": judge_llm,
        "decoding_config": {
            "temperature": 0,
            "thinking": "disabled",
            "extra_body": {"thinking": {"type": "disabled"}},
        },
        "judge_config": {"model": judge_llm},
        "mechanism_only": mechanism_only,
        "official_scorer_executed": not mechanism_only,
        "resource_limits": resource_limits,
        "task_ids": task_ids, "task_manifest_sha256": manifest_sha256,
        "extra_task_args": extra_task_args,
        "started_at": started, "finished_at": finished,
        "log_dir": str(log_dir),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arm", choices=_ARMS, required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--model", default="openai/MiniMax-M3")
    parser.add_argument("--judge-llm", default=None)
    parser.add_argument("--log-dir", required=True)
    parser.add_argument("--acesevals-dir", default=str(_DEFAULT_UPSTREAM))
    parser.add_argument("--task-arg", action="append", default=[],
                        help="额外 -T 参数，格式 key=value")
    parser.add_argument(
        "--mechanism-only", action="store_true",
        help="只观察机制/资源，不执行或读取官方 scorer")
    parser.add_argument(
        "--allow-dirty-mechanism-source", action="store_true",
        help="仅机制烟测可用；完整记录工作区指纹，正式运行仍拒绝脏工作区")
    parser.add_argument("--token-limit", type=int, default=1_000_000)
    parser.add_argument("--time-limit", type=int, default=1800)
    parser.add_argument("--max-samples", type=int, default=1,
                        help="并发 sample 上限；官方共享数据库机制烟测默认串行")
    parser.add_argument("--global-tool-call-limit", type=int, default=64)
    parser.add_argument("--global-model-call-limit", type=int, default=64)
    args = parser.parse_args()

    upstream = Path(args.acesevals_dir).resolve()
    if not (upstream / "domains" / "excytin" / "excytin.py").is_file():
        print(f"ACESEvals upstream not found at {upstream}", file=sys.stderr)
        return 2
    for path in (str(upstream), str(_REPO)):
        if path not in sys.path:
            sys.path.insert(0, path)

    from cyberorion.bench.excytin_saber_patch import (
        SaberPatchError, ensure_saber_resource_limit_patch,
    )
    from cyberorion.bench.excytin_saber_startup_patch import (
        SaberStartupPatchError, ensure_saber_startup_health_retry_patch,
    )
    try:
        saber_patch = ensure_saber_resource_limit_patch(_REPO)
    except SaberPatchError as exc:
        print(f"SABER correctness patch verification failed: {exc}",
              file=sys.stderr)
        return 2
    try:
        saber_startup_patch = ensure_saber_startup_health_retry_patch(_REPO)
    except SaberStartupPatchError as exc:
        print(f"SABER startup patch verification failed: {exc}",
              file=sys.stderr)
        return 2

    from cyberorion.bench.excytin_official_agent import register_official_agents
    register_official_agents()
    from domains.excytin.excytin import excytin as excytin_task  # noqa: E402

    manifest_path = Path(args.manifest).resolve()
    try:
        task_ids, manifest_sha256 = _load_manifest(manifest_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"invalid task manifest: {exc}", file=sys.stderr)
        return 2
    source = _source_snapshot(_REPO)
    allow_dirty = args.mechanism_only and args.allow_dirty_mechanism_source
    if (source["git_dirty"] is not False and not allow_dirty) or not all(
            source.get(key) for key in ("git_head", "git_tree_sha")):
        print("publication run requires complete provenance and a clean CyberOrion worktree",
              file=sys.stderr)
        return 2
    if args.allow_dirty_mechanism_source and not args.mechanism_only:
        print("--allow-dirty-mechanism-source requires --mechanism-only",
              file=sys.stderr)
        return 2

    judge_llm = args.judge_llm or "openai/azure/gpt-4.1"
    task_kwargs: dict = {
        "agent": args.arm,
        "judge_llm": judge_llm,
        "global_tool_call_limit": args.global_tool_call_limit,
        "max_model_calls": args.global_model_call_limit,
    }
    task_kwargs["task_filter"] = ",".join(task_ids)
    for item in args.task_arg:
        key, _, value = item.partition("=")
        if not key or not value:
            print(f"--task-arg 必须是 key=value，收到 {item!r}", file=sys.stderr)
            return 2
        task_kwargs[key] = value
    task = excytin_task(**task_kwargs)

    import inspect_ai
    from inspect_ai.model import GenerateConfig, get_model
    log_dir = Path(args.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    started = time.time()
    primary_model = get_model(
        args.model,
        config=GenerateConfig(
            temperature=0,
            extra_body={"thinking": {"type": "disabled"}},
        ),
    )
    inspect_ai.eval(
        task,
        model=primary_model,
        log_dir=str(log_dir),
        display="plain",
        score=not args.mechanism_only,
        token_limit=args.token_limit,
        time_limit=args.time_limit,
        tool_call_limit=args.global_tool_call_limit,
        max_samples=args.max_samples,
    )
    provenance = build_provenance(
        upstream=upstream, repo=_REPO, arm=args.arm, model=args.model,
        judge_llm=judge_llm, task_ids=task_ids,
        manifest_sha256=manifest_sha256,
        extra_task_args={item.partition("=")[0]: item.partition("=")[2]
                         for item in args.task_arg},
        started=started, finished=time.time(), log_dir=log_dir,
        mechanism_only=args.mechanism_only,
        resource_limits={
            "token_limit": args.token_limit,
            "time_limit": args.time_limit,
            "max_samples": args.max_samples,
            "global_model_calls": args.global_model_call_limit,
            "global_tool_call_limit": args.global_tool_call_limit,
            "official_task_max_steps_unchanged": 25,
        })
    provenance["saber_resource_limit_patch"] = saber_patch
    provenance["saber_startup_health_retry_patch"] = saber_startup_patch
    (log_dir / "cyberorion_official_provenance.json").write_text(
        json.dumps(provenance, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(provenance, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

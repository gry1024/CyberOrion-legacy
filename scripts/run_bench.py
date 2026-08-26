#!/usr/bin/env python
"""run_bench：基准 CLI（malware_analysis / attack_kb 套件）。

用法（在 cyberorion/ 目录下）：
    set -a; source ../.env; set +a
    python scripts/run_bench.py --n 100 --mode both      # base + rag 对比
    python scripts/run_bench.py --suite attack_kb --n 30 --mode both --seed 42
    python scripts/run_bench.py --n 100 --mode rag --seed 42
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent
for p in (str(_REPO), str(_REPO.parent)):
    if p not in sys.path:
        sys.path.insert(0, p)

from cyberorion.bench.cybersoceval import SUITES, run_bench  # noqa: E402


def _print_result(run: dict) -> None:
    s = run.get("scores") or {}
    arm = run.get("arm") or "-"
    print(f"\n== 运行 {run['run_id']} ==")
    print(f"  suite={run.get('suite', 'malware_analysis')}  "
          f"arm={arm}（mode={run['mode']}）  "
          f"n={run['n']}  seed={run['seed']}  model={run['model']}  "
          f"thinking={run.get('thinking') or '默认'}  "
          f"耗时={run['elapsed_sec']}s")
    if run.get("suite_desc"):
        print(f"  {run['suite_desc']}")
    if "correct_mc_pct" in s:
        print(f"  correct_mc_pct（全对率）: {s['correct_mc_pct']:.3f}")
        print(f"  avg_score（Jaccard/兼容主分）: {s.get('avg_score', 0):.3f}")
        print(f"  parse_fail（解析失败数）: {s.get('parse_fail', 0)}")
        if s.get("by_difficulty"):
            print("  按难度：")
        for diff, group in s.get("by_difficulty", {}).items():
            print(f"    {diff:<8} n={group['n']:<4} "
                  f"correct={group['correct_mc_pct']:.3f}  "
                  f"avg={group['avg_score']:.3f}")
    else:
        # 外部套件保留各自原生指标，不强行映射为 MC/Jaccard。
        preferred = (
            "accuracy", "attack_recall", "precision", "f1", "pr_auc",
            "native_reward", "official_reward", "mean_reward",
            "restore_cost_proxy", "availability_penalty",
            "illegal_action_rate", "task_success",
        )
        shown = False
        for name in preferred:
            value = s.get(name)
            if isinstance(value, (int, float)):
                print(f"  {name}: {value:.4f}")
                shown = True
        if not shown:
            print("  scores: " + json.dumps(s, ensure_ascii=False, default=str))
    report = run.get("report")
    if report:
        print(f"  报告：{report}")
    elif run.get("path"):
        print(f"  运行产物：{run['path']}")


def _print_compare(base: dict, rag: dict) -> None:
    """框架有效性对比：纯 LLM（base）vs CyberOrion 框架（rag）。

    两臂同 seed、同一批题目、同一模型——唯一差异是框架注入的知识库层
    （KB 检索 + playbook + 作答规则），Δ 即框架增益。
    """
    bs, rs = base["scores"], rag["scores"]
    print("\n===== 框架有效性对比：纯 LLM（base）vs CyberOrion 框架（rag）=====")
    print(f"  同一批题目（seed={base['seed']}）、同一模型（{base['model']}）")
    print(f"{'指标':<24}{'纯 LLM':>10}{'框架':>10}{'Δ（框架-纯LLM）':>16}")
    rows = [
        ("correct_mc_pct 全对率", bs["correct_mc_pct"], rs["correct_mc_pct"]),
        ("avg_score Jaccard", bs["avg_score"], rs["avg_score"]),
        ("parse_fail 解析失败", bs["parse_fail"], rs["parse_fail"]),
    ]
    for name, b, r in rows:
        delta = r - b
        print(f"{name:<24}{b:>10.3f}{r:>10.3f}{delta:>+16.3f}"
              if isinstance(b, float)
              else f"{name:<24}{b:>10}{r:>10}{delta:>+16}")
    delta_pt = (rs["correct_mc_pct"] - bs["correct_mc_pct"]) * 100
    print(f"  全对率 Δ：{delta_pt:+.1f} 个百分点（框架增益）")
    print("  按难度对比（correct_mc_pct 纯LLM -> 框架）：")
    diffs = sorted(set(bs.get("by_difficulty", {})) |
                   set(rs.get("by_difficulty", {})))
    for d in diffs:
        b = bs.get("by_difficulty", {}).get(d, {})
        r = rs.get("by_difficulty", {}).get(d, {})
        print(f"    {d:<8} n={b.get('n', r.get('n', 0)):<4} "
              f"{b.get('correct_mc_pct', 0):.3f} -> "
              f"{r.get('correct_mc_pct', 0):.3f}")
    print("=================================================================")


def _print_questions(run: dict) -> None:
    """打印逐题明细（题干/gold vs pred/判定）。完整选项与模型原始回答
    见随运行落盘的 markdown 报告（run['report']）。"""
    report = (run.get("report") or
              run["path"].replace(".json", ".md"))
    print(f"\n== 逐题明细（{run['run_id']}）==")
    print(f"  完整选项与模型原始回答见报告：{report}")
    for i, r in enumerate(run.get("results") or []):
        mark = "✓" if r.get("exact") else "✗"
        meta = " ".join(x for x in (r.get("difficulty"),
                                     r.get("topic"), r.get("attack")) if x)
        print(f"\n#{i + 1} [{meta}] {mark} exact="
              f"{'True' if r.get('exact') else 'False'} "
              f"gold={r.get('gold')} pred={r.get('pred') or '—'} "
              f"jaccard={r.get('jaccard', 0):.2f}")
        print(f"题目: {r.get('question') or ''}")


async def _main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", default="malware_analysis",
                        choices=list(SUITES),
                        help="attack_kb = ATT&CK 知识库访问能力测试（仅 "
                             "base/rag）")
    parser.add_argument("--n", type=int, default=100, help="任务数；外部大数据可运行代表子集")
    parser.add_argument("--mode", default="both",
                        choices=["base", "rag", "single", "agent", "paired", "both", "compare",
                                 "sc", "sc_base", "rag_fs", "rag_g"],
                        help="rag=默认 v5 配方；rag_fs/sc/sc_base/rag_g "
                             "为 legacy 对比模式（仅 malware_analysis）")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--profile", choices=["daily", "publication"],
                        default="daily", help="daily=固定代表集；publication=全量/官方协议")
    parser.add_argument("--dataset-version", default=None)
    parser.add_argument("--sc-k", type=int, default=3,
                        help="sc/sc_base 模式每题采样次数")
    parser.add_argument("--sc-temp", type=float, default=0.7,
                        help="sc/sc_base 模式采样温度")
    parser.add_argument("--show-questions", action="store_true",
                        help="打印每道题的题干与判定（gold vs pred）；"
                             "完整选项/模型回答见随运行生成的 .md 报告")
    args = parser.parse_args()

    base_run = rag_run = None
    if args.mode == "compare":
        print(f"[run_bench] suite={args.suite} compare 启动：n={args.n} "
              f"profile={args.profile} seed={args.seed}", flush=True)
        run = await run_bench(
            n=args.n, mode="compare", seed=args.seed, suite=args.suite,
            profile=args.profile, dataset_version=args.dataset_version)
        _print_result(run)
        comparison = run.get("comparison") or {}
        for arm in comparison.get("arms") or []:
            print(f"  {arm['mode']}: {arm['scores'].get('avg_score')}")
        print(f"  agent-reference Δ: {comparison.get('agent_minus_reference')}")
        return
    requested_modes = (["base", "rag"] if args.mode == "both" else [args.mode])
    for selected_mode in requested_modes:
        if selected_mode in ("sc", "sc_base", "rag_fs", "rag_g"):
            continue
        print(f"[run_bench] suite={args.suite} {selected_mode} 模式启动：n={args.n} "
              f"profile={args.profile} seed={args.seed}", flush=True)
        run = await run_bench(
            n=args.n, mode=selected_mode, seed=args.seed, suite=args.suite,
            profile=args.profile, dataset_version=args.dataset_version)
        _print_result(run)
        if args.show_questions:
            _print_questions(run)
        if selected_mode == "base":
            base_run = run
        elif selected_mode == "rag":
            rag_run = run
    if args.mode in ("sc", "sc_base", "rag_fs", "rag_g"):
        print(f"[run_bench] suite={args.suite} {args.mode} 模式启动："
              f"n={args.n} seed={args.seed} k={args.sc_k} temp={args.sc_temp}",
              flush=True)
        run = await run_bench(n=args.n, mode=args.mode, seed=args.seed,
                              sc_k=args.sc_k, sc_temperature=args.sc_temp,
                              suite=args.suite, profile=args.profile,
                              dataset_version=args.dataset_version)
        _print_result(run)
        if args.show_questions:
            _print_questions(run)
    if base_run and rag_run:
        _print_compare(base_run, rag_run)


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except Exception as exc:
        # 外部资产缺失和 live harness 未注入都属于可预期的结构化跳过，
        # 绝不能伪造成零分或成功 run。未知异常仍原样抛出，保留诊断栈。
        code = getattr(exc, "code", None)
        if code in {"benchmark_asset_missing", "live_benchmark_unavailable"}:
            skipped = {
                "status": "skipped",
                "code": code,
                "reason": str(exc),
                "suite": getattr(exc, "suite", None),
            }
            asset = getattr(exc, "asset", None)
            if asset is not None:
                skipped["asset"] = asset
            print(json.dumps(skipped, ensure_ascii=False, indent=2))
            raise SystemExit(3) from None
        raise

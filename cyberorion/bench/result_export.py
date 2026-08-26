"""Benchmark 原始 JSON 的只读归一化、可比性审计与发布统计。"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import random
import statistics
import subprocess
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Callable

FAILURE_TAGS = (
    "wrong_verdict", "false_positive", "missed_attack", "parse_failure",
    "insufficient_evidence", "invalid_evidence", "budget_exhausted", "timeout",
    "llm_error", "illegal_action", "unsafe_action",
)
PRIMARY_METRICS = {
    "malware_analysis": "correct_mc_pct",
    "threat_intel": "correct_mc_pct",
    "attack_kb": "correct_mc_pct",
    "secalertbench": "macro_f1",
    "excytin": "official_reward_or_native_reward",
    "cage2": "mean_reward",
    "live_paired": "score",
    "soc_contract": "task_success",
}


def _sha(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"), default=str).encode()
    return hashlib.sha256(raw).hexdigest()


def _git_sha(repo: Path) -> str | None:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, check=True,
            capture_output=True, text=True, timeout=5,
        ).stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def _task_id(row: dict, index: int) -> str:
    for key in ("task_id", "case_id", "question_id", "alert_id", "id"):
        if row.get(key) is not None:
            return str(row[key])
    if row.get("condition") is not None and row.get("episode") is not None:
        return f"{row['condition']}:episode-{row['episode']}"
    if row.get("seed") is not None and row.get("arm") is not None:
        return str(row["seed"])
    return str(row.get("idx", index))


def _dataset_fingerprint(run: dict) -> dict:
    provenance = run.get("benchmark_provenance") or {}
    files = provenance.get("dataset_files") or []
    hashes = sorted(str(item.get("sha256")) for item in files
                    if isinstance(item, dict) and item.get("sha256"))
    return {
        "version": provenance.get("dataset_version"),
        "hash": provenance.get("dataset_sha256") or provenance.get("sha256")
        or (_sha(hashes) if hashes else None),
    }


def _usage(rows: list[dict], elapsed: Any,
           top_level_resources: list[dict] | None = None) -> dict:
    totals = Counter()
    exact = True
    sources = rows
    if top_level_resources and not any(row.get("resource_usage") for row in rows):
        sources = [{"resource_usage": resource} for resource in top_level_resources]
    for row in sources:
        usage = row.get("resource_usage") or {}
        used = usage.get("used") or {}
        for source, target in (
            ("llm_calls", "llm_calls"), ("tool_calls", "tool_calls"),
            ("estimated_tokens", "tokens"), ("tokens", "tokens"),
            ("wall_clock_sec", "wall_clock_sec"),
        ):
            if isinstance(used.get(source), (int, float)):
                totals[target] += used[source]
        if "estimated_tokens" in used:
            exact = False
    n = len(rows)
    wall = totals.get("wall_clock_sec") or (elapsed if isinstance(elapsed, (int, float)) else None)
    return {
        "llm_calls": totals.get("llm_calls") if rows else None,
        "tool_calls": totals.get("tool_calls") if rows else None,
        "tokens": totals.get("tokens") if rows else None,
        "tokens_status": "estimated" if totals.get("tokens") is not None and not exact
        else "provider" if totals.get("tokens") is not None else "unavailable",
        "wall_clock_sec": wall,
        "per_task": {
            "llm_calls": totals.get("llm_calls") / n if n and "llm_calls" in totals else None,
            "tool_calls": totals.get("tool_calls") / n if n and "tool_calls" in totals else None,
            "tokens": totals.get("tokens") / n if n and "tokens" in totals else None,
            "wall_clock_sec": wall / n if n and wall is not None else None,
        },
    }


def _limit_violations(raw: dict) -> list[str]:
    """记录实际使用【超过】声明硬上限的维度。

    达到上限后走文档化 fallback（budget exhausted）不算违规；只有记账的
    实际消耗严格大于上限（如 after-response 的 token 超限）才算。
    """
    methodology = raw.get("methodology") or {}
    limits = (methodology.get("arm_budget")
              or methodology.get("step_budget")
              or (raw.get("resource_usage") or {}).get("limits"))
    if not limits:
        return []
    units: list[dict] = []
    if methodology.get("fairness_scope") == "per_environment_step":
        units.extend({"used": trace.get("step_resource_usage") or {}}
                     for trace in (raw.get("agent_traces") or [])
                     if isinstance(trace, dict))
        violated = set(raw.get("budget_limit_violation_dimensions") or [])
    else:
        violated = set()
    rows = raw.get("results")
    if isinstance(rows, list):
        units.extend(r.get("resource_usage") for r in rows
                     if isinstance(r, dict) and isinstance(r.get("resource_usage"), dict))
    if methodology.get("fairness_scope") != "per_environment_step":
        units.extend(resource for resource in (raw.get("episode_resource_usage") or [])
                     if isinstance(resource, dict))
    for unit in units:
        used = unit.get("used") or {}
        if ("max_llm_calls" in limits
                and int(used.get("llm_calls", 0)) > int(limits["max_llm_calls"])):
            violated.add("llm_calls")
        if ("max_tool_calls" in limits
                and int(used.get("tool_calls", 0)) > int(limits["max_tool_calls"])):
            violated.add("tool_calls")
        if ("token_budget" in limits
                and int(used.get("estimated_tokens", 0)) > int(limits["token_budget"])):
            violated.add("estimated_tokens")
    return sorted(violated)


def _failure_tags(row: dict) -> list[str]:
    tags = {str(tag) for tag in (row.get("failure_tags") or [])}
    pred, gold = row.get("pred"), row.get("gold")
    if row.get("parse_ok") is False or pred == "unknown":
        tags.add("parse_failure")
    if row.get("llm_error"):
        tags.add("llm_error")
    error = str(row.get("error") or "").lower()
    if "timeout" in error:
        tags.add("timeout")
    if "budget" in error or row.get("budget_exhausted"):
        tags.add("budget_exhausted")
    if pred is not None and gold is not None and pred != gold:
        tags.add("wrong_verdict")
        if str(gold).lower() == "benign" and str(pred).lower() == "attack":
            tags.add("false_positive")
        if str(gold).lower() == "attack" and str(pred).lower() != "attack":
            tags.add("missed_attack")
    if int(row.get("illegal_actions") or 0) > 0:
        tags.add("illegal_action")
    if row.get("unsafe_action") or int(row.get("unsafe_actions") or 0) > 0:
        tags.add("unsafe_action")
    aliases = {
        "missing_evidence": "insufficient_evidence",
        "evidence_grounding": "invalid_evidence",
    }
    tags = {aliases.get(tag, tag) for tag in tags}
    return sorted(tag for tag in tags if tag in FAILURE_TAGS)


def normalize_run(raw: dict, source: Path, export_sha: str | None) -> dict:
    rows = raw.get("results") if isinstance(raw.get("results"), list) else []
    provenance = raw.get("benchmark_provenance") or {}
    sample_ids = provenance.get("sample_manifest")
    if not isinstance(sample_ids, list):
        sample_ids = [_task_id(row, i) for i, row in enumerate(rows)]
    model = raw.get("model")
    settings = raw.get("model_settings") or {
        "provider": str(model).split("/", 1)[0] if "/" in str(model) else None,
        "model": str(model).split("/", 1)[-1] if model is not None else None,
        "settings_status": "not_persisted_in_legacy_run",
    }
    methodology = raw.get("methodology") or {}
    budget = (methodology.get("arm_budget") or methodology.get("step_budget")
              or (raw.get("resource_usage") or {}).get("limits"))
    normalized_rows = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        normalized_rows.append({
            "run_id": raw.get("run_id"), "suite": raw.get("suite"),
            "arm": raw.get("mode") or raw.get("arm"),
            "task_id": _task_id(row, index), "failure_tags": _failure_tags(row),
            **row,
        })
    dataset = _dataset_fingerprint(raw)
    errors = {
        "parse_fail": (raw.get("scores") or {}).get("parse_fail"),
        "llm_errors": raw.get("llm_errors", (raw.get("scores") or {}).get("llm_errors")),
        "task_errors": sum(bool(row.get("error")) for row in rows if isinstance(row, dict)),
    }
    git_head = raw.get("git_head_sha")
    git_tree = raw.get("git_tree_sha")
    git_dirty = raw.get("git_dirty")
    missing = [name for name, value in {
        "git_head_sha": git_head,
        "git_tree_sha": git_tree,
        "git_dirty": git_dirty,
        "dataset_version": dataset["version"], "dataset_hash": dataset["hash"],
        "sample_manifest": sample_ids or None, "model_settings": raw.get("model_settings"),
        "fair_arm_budget": budget,
    }.items() if value is None]
    if git_dirty is True and raw.get("git_diff_sha256") is None:
        missing.append("git_diff_sha256_when_dirty")
    exclusion_reasons = []
    if raw.get("status") != "done":
        exclusion_reasons.append("run_status_not_done")
    if str(model).lower() in {"fake-model", "mock", "test-model"}:
        exclusion_reasons.append("fixture_or_fake_model")
    if git_dirty is not False:
        exclusion_reasons.append("git_worktree_not_clean")
    if any(name in missing for name in ("git_head_sha", "git_tree_sha", "git_dirty")):
        exclusion_reasons.append("incomplete_git_provenance")
    provenance_complete = all(value is not None for value in (git_head, git_tree, git_dirty))
    return {
        "run_id": raw.get("run_id") or source.stem,
        "source_file": str(source), "schema_version": raw.get("schema_version"),
        "git_commit_sha": raw.get("git_commit_sha"),
        "git_head_sha": git_head, "git_tree_sha": git_tree,
        "git_dirty": git_dirty, "git_diff_sha256": raw.get("git_diff_sha256"),
        "export_git_commit_sha": export_sha,
        "suite": raw.get("suite") or "malware_analysis", "mode": raw.get("mode"),
        "arm": raw.get("arm") or raw.get("mode"), "status": raw.get("status"),
        "methodology_status": raw.get("methodology_status"),
        "official_upstream_score": raw.get("methodology_status") == "official_compatible",
        "n": raw.get("n", len(rows)), "seed": raw.get("seed"),
        "model": model, "model_settings": settings, "fair_arm_budgets": budget,
        "benchmark_provenance": provenance, "dataset": dataset,
        "sample_ids": sample_ids, "sample_manifest_sha256":
            provenance.get("sample_manifest_sha256") or (_sha(sample_ids) if sample_ids else None),
        "scores": raw.get("scores") or {},
        "resource_usage": _usage(rows, raw.get("elapsed_sec"),
                                 raw.get("episode_resource_usage")),
        "conditions": raw.get("conditions") or [],
        "errors": errors, "started_at": raw.get("started_at"),
        "finished_at": raw.get("finished_at"), "elapsed_sec": raw.get("elapsed_sec"),
        "budget_limit_violations": _limit_violations(raw),
        "provenance_complete": provenance_complete,
        "artifact_class": ("publication_candidate" if provenance_complete and git_dirty is False
                           else "historical_incomplete_provenance"),
        "completeness": {"missing_fields": missing, "complete": not missing},
        "publication_eligible": not exclusion_reasons,
        "publication_exclusion_reasons": exclusion_reasons,
        "per_task": normalized_rows,
    }


def _same(left: Any, right: Any) -> bool:
    return left is not None and right is not None and left == right


def validate_compare_runs(runs: list[dict]) -> dict:
    by_mode = {str(run.get("mode")): run for run in runs}
    single, agent = by_mode.get("single"), by_mode.get("agent")
    modes = ("base", "single", "agent")
    relevant = [by_mode[name] for name in modes if name in by_mode]
    checks = {
        "runs_done": bool(relevant) and all(r.get("status") == "done" for r in relevant),
        "real_model_runs": bool(relevant) and all(r.get("publication_eligible") for r in relevant),
        "required_arms": all(name in by_mode for name in modes),
        "dataset_version_identical": bool(relevant) and len({r["dataset"]["version"] for r in relevant}) == 1
        and relevant[0]["dataset"]["version"] is not None,
        "dataset_hash_identical": bool(relevant) and len({r["dataset"]["hash"] for r in relevant}) == 1
        and relevant[0]["dataset"]["hash"] is not None,
        "sample_ids_identical": bool(relevant) and all(
            r.get("sample_ids") == relevant[0].get("sample_ids") and bool(r.get("sample_ids"))
            for r in relevant),
        "model_identical": bool(relevant) and len({r.get("model") for r in relevant}) == 1
        and relevant[0].get("model") is not None,
        "model_settings_identical": bool(relevant) and all(
            _same(r.get("model_settings"), relevant[0].get("model_settings"))
            for r in relevant),
        "seed_identical": bool(relevant) and len({r.get("seed") for r in relevant}) == 1
        and relevant[0].get("seed") is not None,
        "clean_complete_provenance": bool(relevant) and all(
            r.get("provenance_complete") and r.get("git_dirty") is False
            for r in relevant),
        "single_agent_budgets_identical": bool(single and agent and
            _same(single.get("fair_arm_budgets"), agent.get("fair_arm_budgets"))),
        # 预算【耗尽】并走文档化 fallback 是允许的；实际记账超过声明硬上限
        # 才算违规并使 publication 失效。
        "resource_limits_respected": bool(relevant) and all(
            not r.get("budget_limit_violations") for r in relevant),
    }
    return {"publication_valid": all(checks.values()), "checks": checks,
            "invalid_reasons": [name for name, ok in checks.items() if not ok]}


def validate_persisted_compare_runs(raw_runs: list[dict], seed: int) -> dict:
    """供 compare runner 在写父 run 前审计刚落盘的三个 raw 子 run。"""
    runs = [normalize_run(raw, Path(str(raw.get("path") or raw.get("run_id") or "run.json")),
                          raw.get("git_commit_sha")) for raw in raw_runs]
    audit = validate_compare_runs(runs)
    by_mode = {run["mode"]: run for run in runs}
    if audit["publication_valid"]:
        audit["paired_statistics"] = paired_statistics(
            by_mode["single"], by_mode["agent"], seed + 100)
    else:
        audit["paired_statistics"] = None
    return audit


def _row_value(suite: str, row: dict) -> float | None:
    if suite == "secalertbench":
        return 1.0 if row.get("gold") == row.get("pred") else 0.0
    for key in ("native_reward", "reward", "score"):
        if isinstance(row.get(key), (int, float)):
            return float(row[key])
    metrics = row.get("metrics") or {}
    if isinstance(metrics.get("task_success"), (int, float)):
        return float(metrics["task_success"])
    if "exact" in row:
        return 1.0 if row.get("exact") else 0.0
    return None


def _macro_f1(rows: list[dict]) -> float:
    labels = ("attack", "benign")
    f1s = []
    for label in labels:
        tp = sum(r.get("gold") == label and r.get("pred") == label for r in rows)
        fp = sum(r.get("gold") != label and r.get("pred") == label for r in rows)
        fn = sum(r.get("gold") == label and r.get("pred") != label for r in rows)
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / (tp + fn) if tp + fn else 0.0
        f1s.append(2 * precision * recall / (precision + recall) if precision + recall else 0.0)
    return statistics.fmean(f1s)


def paired_statistics(single: dict, agent: dict, seed: int, rounds: int = 2000) -> dict:
    suite = str(agent.get("suite"))
    left = {row["task_id"]: row for row in single.get("per_task", [])}
    right = {row["task_id"]: row for row in agent.get("per_task", [])}
    ids = list(single.get("sample_ids") or [])
    pairs = [(left[str(item)], right[str(item)]) for item in ids
             if str(item) in left and str(item) in right]
    values = [(_row_value(suite, a), _row_value(suite, b)) for a, b in pairs]
    values = [(a, b) for a, b in values if a is not None and b is not None]
    wins = sum(b > a for a, b in values)
    ties = sum(b == a for a, b in values)
    losses = sum(b < a for a, b in values)
    rng = random.Random(seed)
    if suite == "secalertbench" and pairs:
        point = _macro_f1([b for _, b in pairs]) - _macro_f1([a for a, _ in pairs])
        samples = []
        for _ in range(rounds):
            chosen = rng.choices(pairs, k=len(pairs))
            samples.append(_macro_f1([b for _, b in chosen]) -
                           _macro_f1([a for a, _ in chosen]))
        metric = "macro_f1"
    else:
        deltas = [b - a for a, b in values]
        point = statistics.fmean(deltas) if deltas else None
        samples = [statistics.fmean(rng.choices(deltas, k=len(deltas)))
                   for _ in range(rounds)] if deltas else []
        metric = PRIMARY_METRICS.get(suite, "task_score")
    samples.sort()
    ci = ([round(samples[int(rounds * .025)], 6),
           round(samples[min(rounds - 1, int(rounds * .975))], 6)]
          if samples else None)
    return {"metric": metric, "paired_n": len(values),
            "agent_minus_single": round(point, 6) if point is not None else None,
            "bootstrap_95_ci": ci, "bootstrap_rounds": rounds, "bootstrap_seed": seed,
            "wins": wins, "ties": ties, "losses": losses}


def _group_compare(runs: list[dict]) -> list[dict]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for run in runs:
        rid = run["run_id"]
        for suffix in ("_base", "_single", "_agent"):
            if rid.endswith(suffix):
                groups[rid[:-len(suffix)]].append(run)
                break
    output = []
    for parent_id, arms in sorted(groups.items()):
        audit = validate_compare_runs(arms)
        by_mode = {run["mode"]: run for run in arms}
        stats = (paired_statistics(by_mode["single"], by_mode["agent"],
                                   int(by_mode["agent"].get("seed") or 0) + 100)
                 if audit["publication_valid"] else None)
        output.append({"comparison_id": parent_id, "suite": arms[0]["suite"],
                       "arm_run_ids": {r["mode"]: r["run_id"] for r in arms},
                       **audit, "paired_statistics": stats})
    return output


def _knowledge_pairs(runs: list[dict]) -> list[dict]:
    output = []
    for suite in ("malware_analysis", "threat_intel", "attack_kb"):
        candidates = [r for r in runs if r["suite"] == suite and r["mode"] in ("base", "rag")
                      and r["status"] == "done" and r["publication_eligible"]
                      and r["completeness"]["complete"]]
        compatible = [(base, rag)
                      for base in candidates if base["mode"] == "base"
                      for rag in candidates if rag["mode"] == "rag"
                      and rag["model"] == base["model"] and rag["seed"] == base["seed"]
                      and rag["dataset"] == base["dataset"]
                      and rag["sample_ids"] == base["sample_ids"]]
        if compatible:
            # 每个 suite 只导出规模最大的一个严格同题 pair，避免把历史多次
            # 调参运行交叉配对后重复计入图表。
            base, rag = max(compatible, key=lambda pair: (
                min(int(pair[0].get("n") or 0), int(pair[1].get("n") or 0)),
                str(pair[0]["run_id"]), str(pair[1]["run_id"])))
            exact_b = base["scores"].get("correct_mc_pct")
            exact_r = rag["scores"].get("correct_mc_pct")
            output.append({
                "suite": suite, "base_run_id": base["run_id"], "rag_run_id": rag["run_id"],
                "n": base["n"], "seed": base["seed"], "model": base["model"],
                "methodology_status": rag["methodology_status"],
                "exact_match": {"base": exact_b, "rag": exact_r,
                                "delta": exact_r - exact_b if isinstance(exact_b, (int, float))
                                and isinstance(exact_r, (int, float)) else None},
                "jaccard": {"base": base["scores"].get("avg_score"),
                            "rag": rag["scores"].get("avg_score")},
                "publication_valid": not base["completeness"]["missing_fields"]
                and not rag["completeness"]["missing_fields"],
                "historical_recovery": True,
            })
    return output


def export_results(raw_dir: Path, output_dir: Path, repo: Path) -> dict:
    """只读取已有 raw JSON；不访问 benchmark 资产、网络或 Docker。"""
    export_sha = _git_sha(repo)
    normalized = []
    ignored = []
    for path in sorted(raw_dir.glob("*.json")):
        if path.name.endswith(".sample.json"):
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            ignored.append({"path": str(path), "reason": "invalid_json"})
            continue
        if not isinstance(raw, dict) or not raw.get("run_id") or not raw.get("scores"):
            ignored.append({"path": str(path), "reason": "not_raw_benchmark_run"})
            continue
        normalized.append(normalize_run(raw, path, export_sha))
    output_dir.mkdir(parents=True, exist_ok=True)
    per_task = output_dir / "per_task"
    per_task.mkdir(exist_ok=True)
    for run in normalized:
        target = per_task / f"{run['run_id']}.jsonl"
        target.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n"
                                  for row in run.pop("per_task")), encoding="utf-8")
        run["per_task_path"] = str(target.relative_to(output_dir))
    comparisons = _group_compare([
        {**run, "per_task": [json.loads(line) for line in
          (output_dir / run["per_task_path"]).read_text(encoding="utf-8").splitlines()]}
        for run in normalized])
    summary = {
        "schema_version": 1, "source_policy": "raw_json_only_no_markdown",
        "runs": normalized, "knowledge_layer_pairs": _knowledge_pairs(normalized),
        "agent_architecture_comparisons": comparisons,
        "publication_run_ids": [
            run["run_id"] for run in normalized
            if run["artifact_class"] == "publication_candidate"
            and run["publication_eligible"] and run["completeness"]["complete"]],
        "historical_or_incomplete_run_ids": [
            run["run_id"] for run in normalized
            if run["artifact_class"] != "publication_candidate"
            or not run["publication_eligible"] or not run["completeness"]["complete"]],
    }
    (output_dir / "benchmark_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    columns = ["run_id", "suite", "mode", "arm", "methodology_status", "status",
               "publication_complete", "n", "seed", "model", "primary_metric",
               "primary_score", "parse_fail", "llm_errors", "tokens_per_task",
               "llm_calls_per_task", "tool_calls_per_task", "wall_clock_per_task"]
    with (output_dir / "benchmark_summary.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        for run in normalized:
            metric = PRIMARY_METRICS.get(run["suite"], "avg_score")
            score_key = "native_reward" if metric == "official_reward_or_native_reward" else metric
            writer.writerow({
                "run_id": run["run_id"], "suite": run["suite"], "mode": run["mode"],
                "arm": run["arm"], "methodology_status": run["methodology_status"],
                "status": run["status"], "publication_complete": run["completeness"]["complete"],
                "n": run["n"], "seed": run["seed"], "model": run["model"],
                "primary_metric": metric, "primary_score": run["scores"].get(score_key),
                "parse_fail": run["errors"]["parse_fail"],
                "llm_errors": run["errors"]["llm_errors"],
                "tokens_per_task": run["resource_usage"]["per_task"]["tokens"],
                "llm_calls_per_task": run["resource_usage"]["per_task"]["llm_calls"],
                "tool_calls_per_task": run["resource_usage"]["per_task"]["tool_calls"],
                "wall_clock_per_task": run["resource_usage"]["per_task"]["wall_clock_sec"],
            })
    manifest = {
        "schema_version": 1, "export_git_commit_sha": export_sha,
        "raw_source_directory": str(raw_dir), "raw_run_count": len(normalized),
        "ignored": ignored, "files": {},
        "safety": "read-only raw JSON processing; no download, asset creation, Docker, or LLM calls",
        "publication_run_count": len(summary["publication_run_ids"]),
        "historical_or_incomplete_run_count": len(summary["historical_or_incomplete_run_ids"]),
    }
    for path in sorted(output_dir.rglob("*")):
        if path.is_file() and path.name != "manifest.json":
            manifest["files"][str(path.relative_to(output_dir))] = hashlib.sha256(
                path.read_bytes()).hexdigest()
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"manifest": manifest, "summary": summary}

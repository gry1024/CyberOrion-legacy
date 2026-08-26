"""SecAlertBench 大规模真实 SOC 告警分诊适配器。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import random
import statistics
import time
from collections import Counter
from pathlib import Path
from typing import Any

from .assets import ASSETS, require_asset
from .external_common import (
    DEFAULT_LOG_DIR, LLM_TIMEOUT, _model_name, make_llm, new_run_id,
    FAIR_ARM_BUDGET, MeteredLLM, apply_size_policy, bootstrap_ci, persist_run, provenance,
    read_records, resolve_representative_files, resource_usage, stratified_sample,
)
from .superagent_runtime import RuntimeConfig, run_reference, run_superagent

SUITE = "secalertbench"
MODES = ("base", "single", "agent")
ARM_OF_MODE = {"base": "bare", "single": "single", "agent": "framework"}
METHODOLOGY_STATUS = "external_track"

# SecAlertBench's pinned upstream schema has exactly one evaluation-only
# field (``Label``).  The adapter also accepts common alternate schemas, so
# all aliases used to derive gold are denied recursively before any payload
# reaches a model.  Additional conventional target keys are denied to keep
# nested ``alert``/``event`` wrappers fail-closed.
EVALUATION_ONLY_KEYS = frozenset({
    "label", "groundtruth", "verdict", "class", "gold", "target",
    "expected", "expectedlabel", "truelabel", "y", "isattack",
})
UPSTREAM_MODEL_VISIBLE_FIELDS = frozenset({
    "attack_type", "dip", "dport", "host", "kill_chain_all", "method",
    "parameter", "proto", "req_body", "req_header", "rsp_body",
    "rsp_header", "rsp_status", "rule_name", "sip", "sport", "uri", "xff",
})


def _first(row: dict, names: tuple[str, ...], default: Any = "") -> Any:
    return next((row[name] for name in names if row.get(name) is not None), default)


def _evaluation_key(name: Any) -> bool:
    canonical = re.sub(r"[^a-z0-9]", "", str(name).lower())
    return canonical in EVALUATION_ONLY_KEYS


def model_visible_payload(value: Any) -> Any:
    """递归删除 gold/target 字段；此函数是所有模型可见告警的唯一出口。"""
    if isinstance(value, dict):
        return {str(key): model_visible_payload(item)
                for key, item in value.items() if not _evaluation_key(key)}
    if isinstance(value, list):
        return [model_visible_payload(item) for item in value]
    if isinstance(value, tuple):
        return [model_visible_payload(item) for item in value]
    return value


def _normalise(row: dict, index: int) -> dict | None:
    raw_label = str(_first(row, ("Label", "label", "ground_truth", "verdict", "class"))).lower()
    if raw_label in {"1", "true", "attack", "malicious", "positive"}:
        label = "attack"
    elif raw_label in {"0", "false", "non-attack", "non_attack", "benign", "negative"}:
        label = "benign"
    else:
        return None
    payload = model_visible_payload(
        _first(row, ("alert", "event", "text", "description", "raw"), row))
    stable = hashlib.sha256(json.dumps(row, ensure_ascii=False, sort_keys=True,
                                       default=str).encode("utf-8")).hexdigest()[:20]
    return {
        "id": str(_first(row, ("id", "alert_id", "uuid"), f"sha256:{stable}")),
        "alert": payload, "label": label,
        "alert_type": str(_first(row, ("attack_type", "alert_type", "type", "rule_name"), "unknown")),
        "enterprise": str(_first(row, ("enterprise", "tenant", "organization"), "unknown")),
    }


def load_alerts(paths: list[Path]) -> list[dict]:
    # 上游同时发布主文件与按标签拆分文件；存在主文件时只读主文件，避免三份重复。
    canonical = [p for p in paths if p.name.lower() == "secalertbench.json"]
    source = canonical or [p for p in paths if p.suffix.lower() in {".json", ".jsonl"}]
    rows = [item for i, row in enumerate(read_records(source))
            if (item := _normalise(row, i)) is not None]
    unique: dict[str, dict] = {}
    for row in rows:
        unique.setdefault(row["id"], row)
    return list(unique.values())


def class_balanced_quotas(n: int) -> tuple[int, int]:
    """按 gold 类的确定性平衡配额：attack=floor(n/2)，benign=n-attack。

    奇数 n 时由该规则固定地把多出的 1 条给 benign，不随机决定。
    """
    attack = n // 2
    return attack, n - attack


def select_representative_alerts(rows: list[dict], n: int, seed: int) -> list[dict]:
    """固定种子按类平衡选样；代表集必须同时含 attack 与 benign。

    顶层先按 gold 类配平（attack=floor(n/2)、benign=n-attack），类内再按
    alert_type × enterprise 固定种子轮询分层（复用 stratified_sample），
    最后两类确定性交错输出。任一类容量不足以满足其配额时 fail closed，
    绝不静默改变 Attack/Benign 类比例。
    """
    available = Counter(row["label"] for row in rows)
    if not available.get("attack") or not available.get("benign"):
        from .assets import BenchmarkAssetMissing
        raise BenchmarkAssetMissing(
            SUITE, "SecAlertBench 代表集源数据必须同时包含 attack 与 benign")
    if n < 2:
        raise ValueError("SecAlertBench representative n 必须至少为 2 才能覆盖两类")
    attack_quota, benign_quota = class_balanced_quotas(n)
    attack_rows = [row for row in rows if row["label"] == "attack"]
    benign_rows = [row for row in rows if row["label"] == "benign"]
    if len(attack_rows) < attack_quota or len(benign_rows) < benign_quota:
        from .assets import BenchmarkAssetMissing
        raise BenchmarkAssetMissing(
            SUITE,
            "SecAlertBench 代表集类平衡配额无法满足：需要 attack="
            f"{attack_quota}（可用 {len(attack_rows)}）、benign={benign_quota}"
            f"（可用 {len(benign_rows)}）；fail closed，不静默改变类比例")
    attack_selected = stratified_sample(attack_rows, attack_quota, seed,
                                        ("alert_type", "enterprise"))
    benign_selected = stratified_sample(benign_rows, benign_quota, seed,
                                        ("alert_type", "enterprise"))
    # 确定性交错输出，保证有序 manifest 顶层也是平衡的。
    selected = [row for pair in zip(attack_selected, benign_selected) for row in pair]
    selected.extend(attack_selected[len(benign_selected):])
    selected.extend(benign_selected[len(attack_selected):])
    selected_counts = Counter(row["label"] for row in selected)
    if not selected_counts.get("attack") or not selected_counts.get("benign"):
        raise RuntimeError("deterministic stratification failed to preserve both classes")
    return selected


def _parse_verdict(raw: Any) -> tuple[str, float]:
    try:
        value = json.loads(str(raw)) if not isinstance(raw, dict) else raw
    except (ValueError, TypeError, json.JSONDecodeError):
        value = {}
    verdict = str(value.get("verdict") or value.get("label") or "unknown").lower()
    if verdict == "unknown" and isinstance(raw, str):
        # runtime complete 摘要可为自然语言；只接受显式 verdict/label 字段，
        # 避免从含糊描述中推断分数。
        match = re.search(r"\b(?:verdict|label)\s*[:=]\s*"
                          r"(attack|benign|malicious|non[- ]?attack)\b",
                          raw, flags=re.I)
        if match:
            verdict = match.group(1).lower()
    if verdict in {"malicious", "attack", "positive", "true", "1"}:
        verdict = "attack"
    elif verdict in {"benign", "non-attack", "non_attack", "negative", "false", "0"}:
        verdict = "benign"
    else:
        verdict = "unknown"
    try:
        if value.get("attack_probability") is not None:
            attack_probability = float(value["attack_probability"])
        elif value.get("confidence") is not None:
            # legacy 输出的 confidence 表示“对当前 verdict 的置信度”，
            # Brier/PR-AUC/ECE 需要统一 P(attack)。
            verdict_confidence = float(value["confidence"])
            attack_probability = (1.0 - verdict_confidence
                                  if verdict == "benign" else verdict_confidence)
        else:
            attack_probability = 0.5
        attack_probability = max(0.0, min(1.0, attack_probability))
    except (TypeError, ValueError):
        attack_probability = 0.5
    return verdict, attack_probability


def compute_scores(rows: list[dict]) -> dict:
    tp = sum(r["gold"] == "attack" and r["pred"] == "attack" for r in rows)
    fn = sum(r["gold"] == "attack" and r["pred"] != "attack" for r in rows)
    fp = sum(r["gold"] == "benign" and r["pred"] == "attack" for r in rows)
    # unknown/parse failure 不能伪装成 benign 真阴性。
    tn = sum(r["gold"] == "benign" and r["pred"] == "benign" for r in rows)
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1_attack = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    # unknown 是其 gold 类的 false negative，但不是另一类的 positive。
    # 因此存在 parse failure 时不能使用纯二分类的 fn/fp 互换捷径。
    benign_fp = sum(r["gold"] == "attack" and r["pred"] == "benign" for r in rows)
    benign_fn = sum(r["gold"] == "benign" and r["pred"] != "benign" for r in rows)
    benign_precision = tn / (tn + benign_fp) if tn + benign_fp else 0.0
    benign_recall = tn / (tn + benign_fn) if tn + benign_fn else 0.0
    f1_benign = (2 * benign_precision * benign_recall /
                 (benign_precision + benign_recall)) if benign_precision + benign_recall else 0.0
    ranked = sorted(((float(r.get("confidence", 0.0)), r["gold"] == "attack")
                     for r in rows), key=lambda item: item[0], reverse=True)
    positives = sum(label for _, label in ranked)
    seen_tp = seen = 0
    average_precision = 0.0
    previous_recall = 0.0
    # 同 confidence 必须作为一个阈值组处理；逐项排序会让 tie 的原始顺序
    # 人为抬高/降低 PR-AUC（全 0 confidence 时尤其明显）。
    index = 0
    while index < len(ranked):
        confidence = ranked[index][0]
        group = []
        while index < len(ranked) and ranked[index][0] == confidence:
            group.append(ranked[index]); index += 1
        seen += len(group)
        seen_tp += sum(label for _, label in group)
        recall_at_threshold = seen_tp / positives if positives else 0.0
        precision_at_threshold = seen_tp / seen if seen else 0.0
        average_precision += (recall_at_threshold - previous_recall) * precision_at_threshold
        previous_recall = recall_at_threshold
    pr_auc = average_precision if positives else 0.0
    brier = statistics.fmean(
        (float(r.get("confidence", 0.0)) - (1.0 if r["gold"] == "attack" else 0.0)) ** 2
        for r in rows) if rows else 0.0
    ece = 0.0
    for lower in (i / 10 for i in range(10)):
        bucket = [r for r in rows if lower <= float(r.get("confidence", 0.0)) < lower + .1
                  or lower == .9 and float(r.get("confidence", 0.0)) == 1.0]
        if bucket:
            accuracy = statistics.fmean(1.0 if r["gold"] == "attack" else 0.0
                                        for r in bucket)
            confidence = statistics.fmean(float(r.get("confidence", 0.0)) for r in bucket)
            ece += len(bucket) / len(rows) * abs(accuracy - confidence)
    cost = fp + 5 * fn
    return {
        "n": len(rows), "macro_f1": round((f1_attack + f1_benign) / 2, 4),
        "attack_recall": round(recall, 4),
        "false_positive_rate": round(fp / (fp + tn), 4) if fp + tn else 0.0,
        "tp": tp, "fp": fp, "fn": fn, "tn": tn,
        "pr_auc": round(pr_auc, 4), "brier_score": round(brier, 4),
        "expected_calibration_error_10bin": round(ece, 4),
        "triage_cost": cost, "triage_cost_per_alert": round(cost / len(rows), 4) if rows else 0.0,
        "cost_model": {"false_negative": 5.0, "false_positive": 1.0},
        "correct_mc_pct": round((tp + tn) / len(rows), 4) if rows else 0.0,
        "avg_score": round((f1_attack + f1_benign) / 2, 4),
        "parse_fail": sum(r["pred"] == "unknown" for r in rows),
        "llm_errors": sum(bool(r.get("llm_error")) for r in rows),
        "by_difficulty": {}, "by_topic": {},
    }


async def _run_agent(alert: dict, mode: str, llm: Any) -> tuple[str, dict]:
    async def wrapped(system: str, user: str) -> Any:
        raw = await llm(system, user)
        try:
            parsed = json.loads(str(raw))
        except (ValueError, TypeError, json.JSONDecodeError):
            return raw
        if isinstance(parsed, dict) and ("verdict" in parsed or "label" in parsed):
            return {"action": {"type": "complete", "summary": parsed}}
        return parsed

    tools = {
        "get_alert": lambda: alert["alert"],
        "get_alert_metadata": lambda: {k: alert[k] for k in ("id", "alert_type", "enterprise")},
    }
    config = RuntimeConfig(max_steps=FAIR_ARM_BUDGET["max_steps"],
                           max_llm_calls=FAIR_ARM_BUDGET["max_llm_calls"],
                           max_tool_calls=FAIR_ARM_BUDGET["max_tool_calls"],
                           max_dispatches=3, max_role_steps=3)
    result = await (run_superagent if mode == "agent" else run_reference)(
        task=("Classify this SOC alert as attack or benign. Cite only observable evidence. "
              "Complete with JSON {verdict, attack_probability}."),
        llm=wrapped, tools=tools, config=config,
        role_tools={"watcher": tools.keys(), "analyst": tools.keys(),
                    "hunter": tools.keys(), "responder": tools.keys()},
    )
    return result["output"], result


async def run_bench(n: int | None = None, mode: str = "base", seed: int = 42,
                    profile: str = "daily", dataset_version: str | None = None,
                    log_dir: str | Path = DEFAULT_LOG_DIR, concurrency: int = 8,
                    llm=None, on_progress=None, run_id: str | None = None,
                    source_provenance: dict | None = None, **_: Any) -> dict:
    if mode not in MODES:
        raise ValueError(f"secalertbench mode 必须是 {'/'.join(MODES)}")
    root, files = require_asset(SUITE)
    data_files, representative_decision = resolve_representative_files(SUITE, files)
    all_rows = load_alerts(data_files)
    if not all_rows:
        from .assets import BenchmarkAssetMissing
        raise BenchmarkAssetMissing(SUITE, "未识别出带 label 与 alert/text 的记录")
    count, size_decision = apply_size_policy(
        SUITE, profile, n, len(all_rows), files)
    attack_quota, benign_quota = class_balanced_quotas(count)
    selected = select_representative_alerts(all_rows, count, seed)
    llm = llm or make_llm(timeout=LLM_TIMEOUT)
    sem = asyncio.Semaphore(max(1, concurrency))
    results: list[dict | None] = [None] * len(selected)
    done = errors = 0

    async def evaluate(index: int, alert: dict) -> None:
        nonlocal done, errors
        started = time.perf_counter()
        meter = MeteredLLM(llm)
        trace: dict = {"decision_trace": [], "tool_calls": [], "role_events": []}
        err = None
        try:
            async with sem, asyncio.timeout(FAIR_ARM_BUDGET["wall_clock_sec"]):
                if mode == "base":
                    raw = await meter(
                        "You are a SOC alert triage analyst. Return JSON only.",
                        json.dumps({"alert": alert["alert"],
                                    "output": {"verdict": "attack|benign",
                                               "attack_probability": 0.0}},
                                   ensure_ascii=False))
                else:
                    raw, trace = await _run_agent(alert, mode, meter)
        except Exception as exc:  # noqa: BLE001
            raw, err = "", f"{type(exc).__name__}: {exc}"[:400]
            errors += 1
        pred, confidence = _parse_verdict(raw)
        results[index] = {
            "idx": index, "alert_id": alert["id"], "topic": alert["alert_type"],
            "difficulty": "unknown", "gold": alert["label"], "pred": pred,
            "confidence": confidence, "probability_semantics": "P(attack)",
            "raw": str(raw)[:4000],
            "agent_trace": trace.get("decision_trace", []),
            "tool_calls": trace.get("tool_calls", []),
            "role_events": trace.get("role_events", []),
            "trace_source": "runtime", "llm_error": bool(err), "error": err,
            "latency_ms": round((time.perf_counter() - started) * 1000, 1),
            "resource_usage": resource_usage(
                started=started,
                llm_calls=(trace.get("budget", {}).get("llm_calls", 1)
                           if mode != "base" else 1),
                tool_calls=(trace.get("budget", {}).get("tool_calls", 0)
                            if mode != "base" else 0),
                estimated_tokens=meter.estimated_tokens),
        }
        done += 1
        if on_progress:
            on_progress(done, len(selected), errors)

    started_at = time.time()
    await asyncio.gather(*(evaluate(i, row) for i, row in enumerate(selected)))
    finished_at = time.time()
    rows = [row for row in results if row is not None]
    spec = ASSETS[SUITE]
    scores = compute_scores(rows)
    scores["confidence_intervals"] = {
        "accuracy": bootstrap_ci([1.0 if r["gold"] == r["pred"] else 0.0 for r in rows], seed),
        "attack_recall": bootstrap_ci([
            1.0 if r["pred"] == "attack" else 0.0
            for r in rows if r["gold"] == "attack"], seed + 1),
    }
    run = {
        "schema_version": 3, "run_id": run_id or new_run_id(SUITE, mode, len(rows)),
        "suite": SUITE, "mode": mode, "arm": ARM_OF_MODE[mode], "profile": profile,
        "n": len(rows), "seed": seed, "model": _model_name(),
        "started_at": started_at, "finished_at": finished_at,
        "elapsed_sec": round(finished_at - started_at, 2),
        "scores": scores, "results": rows, "llm_errors": errors,
        "status": "error" if rows and errors == len(rows) else "done",
        "error": next((r["error"] for r in rows if r.get("error")), None),
        "methodology_status": METHODOLOGY_STATUS,
        "methodology": {
            "official_alignment": "RQ1 binary labels and core confusion metrics",
            "differences": [
                "CyberOrion JSON prompt/runner replaces upstream RQ1 prompt and retry code",
                "confidence, PR-AUC, calibration, cost and agent tools are CyberOrion extensions",
                "not directly comparable to the official leaderboard",
            ],
            "arm_budget": dict(FAIR_ARM_BUDGET),
        },
        "benchmark_provenance": provenance(
            suite=SUITE, title=spec.title, upstream_url=spec.upstream_url,
            version=dataset_version or spec.version, files=files,
            selected_ids=[r["id"] for r in selected], total=len(all_rows),
            protocol="binary_soc_alert_triage", comparable=False),
        "asset_root": str(root),
        "size_policy_decision": size_decision,
        "representative_asset_decision": representative_decision,
        "model_visible_schema_audit": {
            "upstream_fields": sorted({"Label", *UPSTREAM_MODEL_VISIBLE_FIELDS}),
            "evaluation_only_upstream_fields": ["Label"],
            "model_visible_upstream_fields": sorted(UPSTREAM_MODEL_VISIBLE_FIELDS),
            "recursive_denied_key_canonical_forms": sorted(EVALUATION_ONLY_KEYS),
        },
        "selection_manifest": {
            "algorithm": "class_balanced_seeded_stratified_v1",
            "sampling_policy": {
                "top_level": "balanced_by_gold_class",
                "within_class": "deterministic_stratified",
                "within_class_fields": ["alert_type", "enterprise"],
                "odd_n_rule": "attack=floor(n/2), benign=n-attack",
                "seed": seed,
                "output_order": "deterministic_class_interleave",
            },
            "requested_class_counts": {
                "attack": attack_quota, "benign": benign_quota,
            },
            "selected_class_counts": dict(
                sorted(Counter(r["label"] for r in selected).items())),
            "class_counts": dict(sorted(Counter(r["label"] for r in selected).items())),
            "selected_ids": [r["id"] for r in selected],
        },
    }
    return persist_run(run, log_dir, source_provenance=source_provenance)

"""公开 SOC benchmark 适配器共享的采样、调用和持久化工具。"""

from __future__ import annotations

import json
import hashlib
import os
import random
import statistics
import subprocess
import time
from pathlib import Path
from typing import Any

from .assets import sha256_file
from .assets import BenchmarkAssetMissing
from .cybersoceval import DEFAULT_LOG_DIR, LLM_TIMEOUT, _model_name, make_llm
from .model_config import max_output_tokens

SIZE_LIMIT_BYTES = 1024 ** 3
TOTAL_CACHE_LIMIT_BYTES = 5 * 1024 ** 3
PROFILE_DEFAULTS = {
    "secalertbench": {"daily": 600, "publication": 8322},
    "excytin": {"daily": 64, "publication": 589},
    "cage2": {"daily": 9, "publication": 100},
}

FAIR_ARM_BUDGET = {
    "max_llm_calls": 18,
    "max_tool_calls": 12,
    "max_steps": 18,
    "wall_clock_sec": 300,
    "token_budget": 32768,
}


class LLMBudgetExceeded(RuntimeError):
    """统一三臂 LLM 调用/token 预算超限。"""


class MeteredLLM:
    """用可复现字符估算在 provider usage 缺失时 fail-closed 限制 token。"""

    def __init__(self, target: Any, budget: dict | None = None) -> None:
        self.target = target
        self.limits = dict(budget or FAIR_ARM_BUDGET)
        self.calls = 0
        self.estimated_tokens = 0
        self.provider_prompt_tokens = 0
        self.provider_completion_tokens = 0
        self.provider_total_tokens = 0
        self.provider_usage_calls = 0

    async def __call__(self, system: str, user: str) -> Any:
        prompt_tokens = max(1, (len(system) + len(user)) // 4)
        if self.calls >= int(self.limits["max_llm_calls"]):
            raise LLMBudgetExceeded("LLM call budget exhausted")
        if self.estimated_tokens + prompt_tokens > int(self.limits["token_budget"]):
            raise LLMBudgetExceeded("estimated token budget exhausted before request")
        self.calls += 1
        self.estimated_tokens += prompt_tokens
        result = await self.target(system, user)
        usage = getattr(result, "usage", None)
        if isinstance(usage, dict):
            self.provider_prompt_tokens += int(usage.get("prompt_tokens", 0))
            self.provider_completion_tokens += int(usage.get("completion_tokens", 0))
            self.provider_total_tokens += int(usage.get("total_tokens", 0))
            self.provider_usage_calls += 1
        self.estimated_tokens += max(1, len(str(result)) // 4)
        if self.estimated_tokens > int(self.limits["token_budget"]):
            raise LLMBudgetExceeded("estimated token budget exhausted after response")
        return result

    def usage(self) -> dict[str, Any]:
        """返回 provider usage（可用时）与始终存在的字符估算。"""
        provider = None
        if self.provider_usage_calls:
            provider = {
                "prompt_tokens": self.provider_prompt_tokens,
                "completion_tokens": self.provider_completion_tokens,
                "total_tokens": self.provider_total_tokens,
                "calls_with_usage": self.provider_usage_calls,
            }
        return {
            "provider": provider,
            "provider_status": ("available" if provider else "unavailable"),
            "estimated_tokens": self.estimated_tokens,
            "llm_calls": self.calls,
        }


def git_commit_sha() -> str | None:
    """返回当前代码提交；无法确认时返回 None，绝不伪造版本。"""
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], check=True, capture_output=True,
            text=True, timeout=5,
        ).stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def git_provenance() -> dict[str, Any]:
    """捕获提交树与工作树状态；任一 Git 查询失败都保持不完整。"""
    def run(*args: str) -> str:
        return subprocess.run(
            ["git", *args], check=True, capture_output=True, text=True,
            timeout=10,
        ).stdout

    try:
        head = run("rev-parse", "HEAD").strip() or None
        tree = run("rev-parse", "HEAD^{tree}").strip() or None
        status = run("status", "--porcelain=v1", "--untracked-files=all")
        dirty = bool(status.strip())
        diff_sha = None
        if dirty:
            # Status includes untracked paths; the binary diff covers staged and
            # unstaged tracked content.  Hash the combined audit material without
            # persisting file contents or secrets in the run JSON.
            diff = run("diff", "--binary", "HEAD")
            diff_sha = hashlib.sha256((status + "\x00" + diff).encode("utf-8")).hexdigest()
        return {
            "git_head_sha": head, "git_tree_sha": tree,
            "git_dirty": dirty, "git_diff_sha256": diff_sha,
        }
    except (OSError, subprocess.SubprocessError):
        return {
            "git_head_sha": None, "git_tree_sha": None,
            "git_dirty": None, "git_diff_sha256": None,
        }


def env_temperature() -> float | None:
    """读取 CO_BENCH_TEMPERATURE；未设置返回 None（provider 默认行为）。"""
    raw = os.getenv("CO_BENCH_TEMPERATURE")
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(
            f"CO_BENCH_TEMPERATURE 必须是数字，收到 {raw!r}") from exc


def model_metadata(model: str | None = None,
                   temperature: float | None = None) -> dict:
    """持久化非敏感模型设置；不记录 API key 或完整私有端点。

    temperature 显式传入时以其为准（如 sc 模式的 0.7）；否则回落到
    CO_BENCH_TEMPERATURE 环境配置；都没有时记录 provider 默认行为
    （temperature_status=provider_default），绝不声称确定性解码。
    """
    configured = os.getenv("CAI_MODEL") or model or _model_name()
    provider, sep, name = str(configured).partition("/")
    effective = temperature if temperature is not None else env_temperature()
    return {
        "provider": provider if sep else None,
        "model": name if sep else str(configured),
        "configured_model": str(configured),
        "thinking": ("enabled" if os.getenv("CO_BENCH_THINKING") == "enabled"
                     else "disabled"),
        "max_output_tokens": max_output_tokens(),
        "temperature": effective,
        "temperature_status": ("explicit" if effective is not None
                               else "provider_default"),
        "usage_accounting": "provider_or_estimated_per_task",
    }


def read_records(paths: list[Path]) -> list[dict[str, Any]]:
    """读取 JSON/JSONL；忽略无法识别的上游元数据文件。"""
    rows: list[dict[str, Any]] = []
    for path in paths:
        if path.suffix.lower() not in {".json", ".jsonl"}:
            continue
        try:
            if path.suffix.lower() == ".jsonl":
                values = [json.loads(line) for line in path.read_text(
                    encoding="utf-8").splitlines() if line.strip()]
            else:
                value = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(value, dict):
                    value = next((value[k] for k in (
                        "records", "alerts", "questions", "examples", "data")
                        if isinstance(value.get(k), list)), [])
                values = value if isinstance(value, list) else []
            rows.extend(row for row in values if isinstance(row, dict))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
    return rows


def stratified_sample(rows: list[dict], n: int, seed: int,
                      strata: tuple[str, ...]) -> list[dict]:
    """固定种子分层代表集；输出顺序稳定，可把 ID 清单写入运行产物。"""
    if n >= len(rows):
        return list(rows)
    groups: dict[tuple[str, ...], list[dict]] = {}
    for row in rows:
        key = tuple(str(row.get(field, "unknown")) for field in strata)
        groups.setdefault(key, []).append(row)
    rng = random.Random(seed)
    selected: list[dict] = []
    ordered = sorted(groups.items(), key=lambda item: item[0])
    # 先保证每个 strata 至少一个，再按轮次均匀补齐。
    pools = []
    for _, group in ordered:
        group = list(group)
        rng.shuffle(group)
        pools.append(group)
    while len(selected) < n and any(pools):
        for pool in pools:
            if pool and len(selected) < n:
                selected.append(pool.pop())
    return selected


def profile_n(suite: str, profile: str, requested: int | None,
              available: int) -> int:
    if profile not in {"daily", "publication"}:
        raise ValueError("profile 必须是 daily/publication")
    default = PROFILE_DEFAULTS[suite][profile]
    return max(1, min(int(requested or default), available))


def apply_size_policy(suite: str, profile: str, requested: int | None,
                      available: int, files: list[Path]) -> tuple[int, dict]:
    """超过 1GiB/文件或 5GiB/总量时强制使用固定代表集。

    强制代表集模式不静默吞掉显式 n：显式 n 小于 daily 默认值时按显式
    执行（smoke 不得被扩大到默认规模），超过默认值时封顶到默认值；
    未指定（None）才回落到 daily 默认值。
    """
    sizes = [p.stat().st_size for p in files if p.is_file()]
    oversized = any(size > SIZE_LIMIT_BYTES for size in sizes)
    over_total = sum(sizes) > TOTAL_CACHE_LIMIT_BYTES
    forced = oversized or over_total
    effective_profile = "daily" if forced else profile
    original_requested = requested
    n_capped_to_daily = False
    if forced and requested is not None:
        daily_default = int(PROFILE_DEFAULTS[suite]["daily"])
        if int(requested) > daily_default:
            requested = daily_default
            n_capped_to_daily = True
    count = profile_n(suite, effective_profile, requested, available)
    return count, {
        "forced_subset": forced,
        "reason": ("single_asset_over_1GiB" if oversized else
                   "cache_over_5GiB" if over_total else None),
        "requested_profile": profile, "effective_profile": effective_profile,
        "observed_bytes": sum(sizes),
        "requested_n": original_requested,
        "n_capped_to_daily_default": n_capped_to_daily,
    }


def resolve_representative_files(suite: str, files: list[Path]) -> tuple[list[Path], dict]:
    """在任何解析前执行容量门禁；超限时只接受显式 representative 资产。"""
    material = [p for p in files if p.is_file()]
    sizes = [p.stat().st_size for p in material]
    oversized = any(size > SIZE_LIMIT_BYTES for size in sizes)
    over_total = sum(sizes) > TOTAL_CACHE_LIMIT_BYTES
    if not (oversized or over_total):
        return files, {"forced_subset": False, "source": "official_assets"}
    representatives = [
        p for p in material
        if "representative" in {part.lower() for part in p.parts}
        and p.stat().st_size <= SIZE_LIMIT_BYTES
    ]
    if not representatives:
        raise BenchmarkAssetMissing(
            suite,
            "资产超过安全阈值；请在资产根的 representative/ 下放置官方任务的"
            "固定种子无损子集（本服务不会自动读取、下载或裁剪超大文件）")
    return representatives, {
        "forced_subset": True,
        "source": "administrator_prepared_representative_directory",
        "reason": "single_asset_over_1GiB" if oversized else "cache_over_5GiB",
        "original_observed_bytes": sum(sizes),
        "representative_files": [str(p) for p in representatives],
    }


def provenance(*, suite: str, title: str, upstream_url: str, version: str,
               files: list[Path], selected_ids: list[str], total: int,
               protocol: str, comparable: bool) -> dict:
    material = [p for p in files if p.is_file()]
    manifest_payload = json.dumps(selected_ids, ensure_ascii=False,
                                  separators=(",", ":")).encode("utf-8")
    return {
        "name": title, "upstream_url": upstream_url, "dataset_version": version,
        "dataset_files": [{
            "name": p.name,
            # 大文件运行时重复全盘扫描会伤害本机；由管理员 manifest 提供
            # 上游 hash。小文件仍现场校验。
            "sha256": sha256_file(p) if p.stat().st_size <= SIZE_LIMIT_BYTES else None,
            "hash_status": "computed" if p.stat().st_size <= SIZE_LIMIT_BYTES
            else "omitted_oversize_require_admin_manifest",
            "bytes": p.stat().st_size,
        } for p in material[:20]],
        "protocol": protocol, "comparable_to_upstream": comparable,
        "sample_scope": "full" if len(selected_ids) == total else "subset",
        "upstream_n": total, "selected_n": len(selected_ids),
        "sample_manifest": selected_ids,
        "sample_manifest_sha256": hashlib.sha256(manifest_payload).hexdigest(),
        "selection": "all" if len(selected_ids) == total else "seeded_stratified",
        "size_policy": {"single_asset_max_bytes": SIZE_LIMIT_BYTES,
                        "total_cache_max_bytes": TOTAL_CACHE_LIMIT_BYTES},
    }


def bootstrap_ci(values: list[float], seed: int, rounds: int = 1000) -> list[float] | None:
    """固定种子的 percentile bootstrap 95% CI。空样本返回 None。"""
    if not values:
        return None
    if len(values) == 1:
        value = round(float(values[0]), 6)
        return [value, value]
    rng = random.Random(seed)
    means = sorted(statistics.fmean(rng.choices(values, k=len(values)))
                   for _ in range(rounds))
    return [round(means[int(rounds * .025)], 6),
            round(means[min(rounds - 1, int(rounds * .975))], 6)]


def resource_usage(*, started: float, llm_calls: int, tool_calls: int,
                   estimated_tokens: int) -> dict:
    """统一记录三臂的预算上限和实际消耗，便于拒绝不公平比较。"""
    return {
        "limits": dict(FAIR_ARM_BUDGET),
        "used": {
            "llm_calls": int(llm_calls), "tool_calls": int(tool_calls),
            "estimated_tokens": int(estimated_tokens),
            "wall_clock_sec": round(time.perf_counter() - started, 4),
        },
    }


def persist_run(run: dict, log_dir: str | Path = DEFAULT_LOG_DIR,
                source_provenance: dict[str, Any] | None = None) -> dict:
    directory = Path(log_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{run['run_id']}.json"
    if source_provenance is not None:
        # compare 模式：整个实验共享一个在产生任何结果文件之前捕获的源码
        # provenance 快照。此处绝不重新捕获——后臂的 git status 会被先臂
        # 刚写出的 untracked 结果文件污染成 dirty，让 benchmark 自己否定
        # 自己。快照直接覆盖，保证三臂完全一致。
        revision = dict(source_provenance)
        run["git_provenance_source"] = "compare_shared_source_snapshot"
    else:
        revision = git_provenance()
        run["git_provenance_source"] = "captured_at_persist"
    for key, value in revision.items():
        run[key] = value
    # Retain the legacy name for old readers, but publication validation uses
    # the complete four-field provenance contract above.
    run["git_commit_sha"] = run.get("git_head_sha")
    run.setdefault("model_settings", model_metadata(run.get("model")))
    run["path"] = str(path)
    provenance_data = run.get("benchmark_provenance") or {}
    sample_ids = provenance_data.get("sample_manifest")
    if isinstance(sample_ids, list):
        manifest_path = directory / f"{run['run_id']}.sample.json"
        manifest_path.write_text(json.dumps({
            "run_id": run["run_id"], "suite": run.get("suite"),
            "seed": run.get("seed"), "selection": provenance_data.get("selection"),
            "sample_ids": sample_ids,
            "sha256": provenance_data.get("sample_manifest_sha256"),
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        run["sample_manifest_path"] = str(manifest_path)
    path.write_text(json.dumps(run, ensure_ascii=False, indent=2), encoding="utf-8")
    return run


def new_run_id(suite: str, mode: str, n: int) -> str:
    return time.strftime(f"%Y%m%d_%H%M%S_{suite}_{mode}_n{n}")


__all__ = [
    "DEFAULT_LOG_DIR", "LLM_TIMEOUT", "_model_name", "make_llm",
    "new_run_id", "persist_run", "profile_n", "provenance", "read_records",
    "stratified_sample", "apply_size_policy", "bootstrap_ci",
    "FAIR_ARM_BUDGET", "resource_usage", "resolve_representative_files",
    "LLMBudgetExceeded", "MeteredLLM",
    "git_commit_sha", "git_provenance", "model_metadata",
]

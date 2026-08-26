"""发布结果层与公平性回归测试；全部使用临时 raw JSON，无外部资产。"""

from __future__ import annotations

import asyncio
import json
import subprocess
from collections import Counter
from pathlib import Path

import pytest

from cyberorion.bench import cage2, secalertbench
from cyberorion.bench.assets import BenchmarkAssetMissing
from cyberorion.bench.external_common import FAIR_ARM_BUDGET
from cyberorion.bench.result_export import (
    export_results, normalize_run, paired_statistics, validate_compare_runs,
)


def _run(mode: str, ids=("a", "b"), budget=None, version="v1", digest="d1") -> dict:
    predictions = {"single": ("benign", "attack"), "agent": ("attack", "benign"),
                   "base": ("benign", "benign")}[mode]
    rows = [{"alert_id": task_id, "gold": gold, "pred": pred, "parse_ok": True}
            for task_id, gold, pred in zip(ids, ("attack", "benign"), predictions)]
    return {
        "schema_version": 4, "run_id": f"parent_{mode}", "suite": "secalertbench",
        "mode": mode, "arm": mode, "status": "done", "methodology_status": "external_track",
        "n": len(rows), "seed": 42, "model": "provider/model", "git_commit_sha": "abc",
        "git_head_sha": "abc", "git_tree_sha": "tree", "git_dirty": False,
        "git_diff_sha256": None,
        "model_settings": {"provider": "provider", "model": "model"},
        "scores": {"macro_f1": .5, "parse_fail": 0, "llm_errors": 0}, "results": rows,
        "methodology": {"arm_budget": budget or dict(FAIR_ARM_BUDGET)},
        "benchmark_provenance": {"dataset_version": version, "dataset_sha256": digest,
                                 "sample_manifest": list(ids)},
    }


def _normal(raw: dict) -> dict:
    return normalize_run(raw, Path(f"{raw['run_id']}.json"), "abc")


def test_identical_paired_manifests_and_budget_are_required() -> None:
    valid = validate_compare_runs([_normal(_run(mode)) for mode in ("base", "single", "agent")])
    assert valid["publication_valid"] is True
    invalid = validate_compare_runs([
        _normal(_run("base")), _normal(_run("single")),
        _normal(_run("agent", ids=("b", "a"))),
    ])
    assert invalid["publication_valid"] is False
    assert "sample_ids_identical" in invalid["invalid_reasons"]


def test_budget_equality_is_value_based() -> None:
    changed = {**FAIR_ARM_BUDGET, "max_tool_calls": FAIR_ARM_BUDGET["max_tool_calls"] + 1}
    audit = validate_compare_runs([
        _normal(_run("base")), _normal(_run("single")),
        _normal(_run("agent", budget=changed)),
    ])
    assert audit["checks"]["single_agent_budgets_identical"] is False
    assert audit["publication_valid"] is False


def test_model_settings_equality_is_required() -> None:
    runs = [_run(mode) for mode in ("base", "single", "agent")]
    runs[-1]["model_settings"] = {
        **runs[-1]["model_settings"], "thinking": "enabled"}
    audit = validate_compare_runs([_normal(run) for run in runs])
    assert audit["checks"]["model_identical"] is True
    assert audit["checks"]["model_settings_identical"] is False
    assert audit["publication_valid"] is False


def test_dirty_or_incomplete_provenance_is_not_publication_valid() -> None:
    dirty = [_run(mode) for mode in ("base", "single", "agent")]
    for run in dirty:
        run["git_dirty"] = True
        run["git_diff_sha256"] = "diff"
    audit = validate_compare_runs([_normal(run) for run in dirty])
    assert audit["checks"]["clean_complete_provenance"] is False
    assert audit["publication_valid"] is False
    legacy = _run("base")
    for key in ("git_head_sha", "git_tree_sha", "git_dirty", "git_diff_sha256"):
        legacy.pop(key, None)
    normalized = _normal(legacy)
    assert normalized["provenance_complete"] is False
    assert normalized["artifact_class"] == "historical_incomplete_provenance"


def test_paired_bootstrap_is_reproducible() -> None:
    single, agent = _normal(_run("single")), _normal(_run("agent"))
    first = paired_statistics(single, agent, seed=142, rounds=300)
    second = paired_statistics(single, agent, seed=142, rounds=300)
    assert first == second
    assert first["wins"] + first["ties"] + first["losses"] == 2


def test_deterministic_secalert_selection_persists_both_classes() -> None:
    rows = [{"id": str(i), "label": "attack" if i % 3 == 0 else "benign",
             "alert_type": f"t{i % 2}", "enterprise": "e"} for i in range(30)]
    one = secalertbench.select_representative_alerts(rows, 12, 42)
    two = secalertbench.select_representative_alerts(rows, 12, 42)
    assert [row["id"] for row in one] == [row["id"] for row in two]
    assert {row["label"] for row in one} == {"attack", "benign"}


def test_secalert_unknown_predictions_are_not_true_negatives() -> None:
    scores = secalertbench.compute_scores([
        {"gold": "attack", "pred": "unknown", "confidence": .5},
        {"gold": "benign", "pred": "unknown", "confidence": .5},
    ])
    assert scores["tn"] == 0
    assert scores["macro_f1"] == 0.0
    assert scores["parse_fail"] == 2
    assert scores["pr_auc"] == .5


def test_secalert_macro_f1_uses_standard_one_vs_rest_with_unknowns() -> None:
    rows = [
        {"gold": "attack", "pred": "attack", "confidence": .9},
        {"gold": "attack", "pred": "unknown", "confidence": .5},
        {"gold": "benign", "pred": "benign", "confidence": .1},
        {"gold": "benign", "pred": "unknown", "confidence": .5},
    ]
    # 两类 precision=1, recall=.5, F1=2/3；unknown 不可成为另一类预测。
    assert secalertbench.compute_scores(rows)["macro_f1"] == .6667


def test_invalid_runs_are_excluded_from_publication_delta(tmp_path: Path) -> None:
    raw = tmp_path / "raw"; raw.mkdir()
    for mode in ("base", "single", "agent"):
        run = _run(mode, ids=("b", "a") if mode == "agent" else ("a", "b"))
        (raw / f"parent_{mode}.json").write_text(json.dumps(run), encoding="utf-8")
    exported = export_results(raw, tmp_path / "results", tmp_path)
    comparison = exported["summary"]["agent_architecture_comparisons"][0]
    assert comparison["publication_valid"] is False
    assert comparison["paired_statistics"] is None


def test_provenance_propagates_and_processing_creates_no_assets(tmp_path: Path) -> None:
    raw = tmp_path / "raw"; raw.mkdir()
    run = _run("base")
    (raw / "run.json").write_text(json.dumps(run), encoding="utf-8")
    before = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))
    exported = export_results(raw, tmp_path / "results", tmp_path)
    normalized = exported["summary"]["runs"][0]
    assert normalized["benchmark_provenance"]["dataset_version"] == "v1"
    assert normalized["dataset"]["hash"] == "d1"
    assert not (tmp_path / "benchmarks").exists()
    assert exported["manifest"]["safety"].startswith("read-only raw JSON")


def test_plotting_survives_optional_metrics_missing(tmp_path: Path) -> None:
    raw = tmp_path / "raw"; raw.mkdir()
    run = _run("base")
    run["scores"] = {"parse_fail": 0, "llm_errors": 0}
    (raw / "run.json").write_text(json.dumps(run), encoding="utf-8")
    export_results(raw, tmp_path / "results", tmp_path)
    from scripts.plot_benchmarks import generate
    outputs = generate(tmp_path / "results" / "benchmark_summary.json",
                       tmp_path / "results" / "figures")
    assert len(outputs) == 6
    assert all(path.is_file() for path in outputs)


def test_cage_episode_budget_is_global_across_environment_steps(
        tmp_path: Path, monkeypatch) -> None:
    asset = tmp_path / "cage"; asset.mkdir()
    (asset / "Scenario2.yaml").write_text("Hosts: {}\n", encoding="utf-8")
    (asset / "evaluation.py").write_text("# fixture\n", encoding="utf-8")
    monkeypatch.setenv("CYBERORION_CAGE2_DIR", str(asset))

    async def fake_run(episodes, steps, policy, scenario, red_agent, seed, wrapper):
        rows = []
        actions = [{"action_id": 0, "action_type": "Sleep", "display": "Sleep"},
                   {"action_id": 2, "action_type": "Analyse", "display": "Analyse Host"}]
        for episode in range(1, episodes + 1):
            for step in range(1, 21):
                await policy({}, episode=episode, step=step, available_actions=actions)
            rows.append({"episode": episode, "reward": 0.0, "illegal_actions": 0,
                         "restore_actions": 0, "restore_cost_proxy": 0.0})
        return {"episodes": rows}

    async def fake_runtime(*, llm, tools, **kwargs):
        await llm("system", "user")
        tools["select_blue_action"].handler(action_id=0)
        return {"decision_trace": [], "tool_calls": [{"tool": "select_blue_action"}],
                "role_events": [], "budget": {"llm_calls": 1, "tool_calls": 1}}

    async def llm(_system, _user): return '{"action":"sleep"}'
    monkeypatch.setattr("cyberorion.eval.benchmarks.run_cage2_async", fake_run)
    monkeypatch.setattr(cage2, "run_reference", fake_runtime)
    run = asyncio.run(cage2.run_bench(
        n=9, mode="single", llm=llm, log_dir=tmp_path / "logs"))
    assert len(run["episode_resource_usage"]) == 9
    assert all(row["used"]["llm_calls"] <= FAIR_ARM_BUDGET["max_llm_calls"]
               for row in run["episode_resource_usage"])
    assert all(row["used"]["tool_calls"] <= FAIR_ARM_BUDGET["max_tool_calls"]
               for row in run["episode_resource_usage"])
    assert all(row["budget_exhausted_steps"] == 8
               for row in run["episode_resource_usage"])


# --------------------------------------------------------------------------- #
# SecAlertBench 代表集类平衡抽样（P0 #2 回归）
# --------------------------------------------------------------------------- #
def _secalert_rows(n_attack: int, n_benign: int) -> list[dict]:
    rows = [{"id": f"a{i}", "label": "attack",
             "alert_type": f"t{i % 5}", "enterprise": f"e{i % 3}"}
            for i in range(n_attack)]
    rows += [{"id": f"b{i}", "label": "benign",
              "alert_type": f"t{i % 7}", "enterprise": f"e{i % 4}"}
             for i in range(n_benign)]
    return rows


def test_secalert_representative_sampling_is_class_balanced_n30() -> None:
    selected = secalertbench.select_representative_alerts(
        _secalert_rows(400, 400), 30, 42)
    assert Counter(row["label"] for row in selected) == {"attack": 15, "benign": 15}


def test_secalert_representative_sampling_is_class_balanced_n600() -> None:
    selected = secalertbench.select_representative_alerts(
        _secalert_rows(400, 400), 600, 42)
    assert Counter(row["label"] for row in selected) == {"attack": 300, "benign": 300}


def test_secalert_representative_sampling_odd_n_uses_documented_rule() -> None:
    selected = secalertbench.select_representative_alerts(
        _secalert_rows(400, 400), 7, 42)
    assert Counter(row["label"] for row in selected) == {"attack": 3, "benign": 4}


def test_secalert_representative_sampling_is_reproducible_in_order() -> None:
    rows = _secalert_rows(400, 400)
    one = secalertbench.select_representative_alerts(rows, 30, 42)
    two = secalertbench.select_representative_alerts(rows, 30, 42)
    three = secalertbench.select_representative_alerts(rows, 30, 42)
    assert [row["id"] for row in one] == [row["id"] for row in two] == \
        [row["id"] for row in three]


def test_secalert_representative_sampling_fails_closed_on_class_capacity() -> None:
    rows = _secalert_rows(5, 100)
    with pytest.raises(BenchmarkAssetMissing, match="类平衡配额"):
        secalertbench.select_representative_alerts(rows, 20, 42)


def test_secalert_representative_manifest_is_identical_across_arms() -> None:
    """三臂以同 seed 同 n 各自选样时必须得到完全相同的样本 manifest。"""
    rows = _secalert_rows(400, 400)
    manifests = [
        [row["id"] for row in secalertbench.select_representative_alerts(
            rows, 30, seed)]
        for seed in (42, 42, 42)]
    assert manifests[0] == manifests[1] == manifests[2]


# --------------------------------------------------------------------------- #
# compare 共享源码 provenance 快照（P0 #1 回归）
# --------------------------------------------------------------------------- #
def _init_git_repo(root: Path) -> None:
    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=root, check=True,
                       capture_output=True, text=True)
    git("init", "-q")
    git("config", "user.email", "bench@example.com")
    git("config", "user.name", "bench")
    (root / "src.py").write_text("print('x')\n", encoding="utf-8")
    git("add", "src.py")
    git("commit", "-q", "-m", "init")


def _arm_run(mode: str) -> dict:
    return {
        "run_id": f"parent_{mode}", "suite": "secalertbench", "mode": mode,
        "arm": mode, "status": "done", "n": 2, "seed": 42,
        "scores": {"macro_f1": 0.5}, "results": [],
        "benchmark_provenance": {"sample_manifest": ["a", "b"]},
    }


def test_compare_arms_share_one_source_provenance_snapshot(
        tmp_path: Path, monkeypatch) -> None:
    """compare 从一次共享干净快照开始；base 落盘后，single/agent 仍记录
    同一份干净 provenance（旧逐臂重捕获行为会因 base 的 untracked 产物
    把后臂污染成 dirty）。"""
    from cyberorion.bench.external_common import git_provenance, persist_run
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    monkeypatch.chdir(repo)
    log_dir = repo / "logs" / "bench"
    snapshot = git_provenance()
    assert snapshot["git_dirty"] is False and snapshot["git_head_sha"]
    arms = [persist_run(_arm_run(mode), log_dir, source_provenance=snapshot)
            for mode in ("base", "single", "agent")]
    # base 的输出此刻已落盘：直接重捕获必然看到 untracked 结果文件。
    assert git_provenance()["git_dirty"] is True
    for field in ("git_head_sha", "git_tree_sha", "git_dirty", "git_diff_sha256"):
        assert {arm[field] for arm in arms} == {snapshot[field]}
    assert all(arm["git_dirty"] is False for arm in arms)
    assert all(arm["git_provenance_source"] == "compare_shared_source_snapshot"
               for arm in arms)


def test_standalone_persist_captures_own_provenance_normally(
        tmp_path: Path, monkeypatch) -> None:
    from cyberorion.bench.external_common import persist_run
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    monkeypatch.chdir(repo)
    run = persist_run(_arm_run("single"), repo / "logs" / "bench")
    assert run["git_provenance_source"] == "captured_at_persist"
    assert run["git_dirty"] is False


def test_compare_from_dirty_source_tree_stays_publication_invalid(
        tmp_path: Path, monkeypatch) -> None:
    """compare 开始时树就已脏：共享快照应如实记录 dirty=true，
    且三臂全部保持 publication-invalid。"""
    from cyberorion.bench.external_common import git_provenance, persist_run
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    (repo / "untracked.bin").write_bytes(b"x")
    monkeypatch.chdir(repo)
    snapshot = git_provenance()
    assert snapshot["git_dirty"] is True and snapshot["git_diff_sha256"]
    arms = [persist_run(_arm_run(mode), repo / "logs" / "bench",
                        source_provenance=snapshot)
            for mode in ("base", "single", "agent")]
    normalized = [normalize_run(arm, Path(arm["path"]), arm["git_head_sha"])
                  for arm in arms]
    assert all(arm["git_dirty"] is True for arm in arms)
    assert all("git_worktree_not_clean" in arm["publication_exclusion_reasons"]
               for arm in normalized)
    audit = validate_compare_runs(normalized)
    assert audit["checks"]["clean_complete_provenance"] is False
    assert audit["publication_valid"] is False

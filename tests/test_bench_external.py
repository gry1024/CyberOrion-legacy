"""外部蓝队 benchmark 适配器测试：只用 tmp_path fixture，无网络。"""

from __future__ import annotations

import asyncio
import io
import json
import sqlite3
import tarfile
from pathlib import Path

import pytest

from cyberorion.bench import (
    cage2, cybersoceval, excytin, live_paired, secalertbench, soc_contract,
    threat_intel,
)
from cyberorion.bench.cybergym_lite import _safe_extract
from cyberorion.bench.external_common import apply_size_policy, stratified_sample


def test_stratified_sample_is_deterministic_and_auditable() -> None:
    rows = [{"id": str(i), "label": "attack" if i % 2 else "benign",
             "type": f"t{i % 3}"} for i in range(30)]
    a = stratified_sample(rows, 12, 42, ("label", "type"))
    b = stratified_sample(rows, 12, 42, ("label", "type"))
    assert [r["id"] for r in a] == [r["id"] for r in b]
    assert {r["label"] for r in a} == {"attack", "benign"}


def test_threat_intel_keeps_complete_answer_set(tmp_path: Path) -> None:
    path = tmp_path / "questions.json"
    path.write_text(json.dumps([{
        "question_text": "q", "options": ["A. one", "B. two", "C. three"],
        "correct_answer": ["A", "C"], "source": "report",
    }]), encoding="utf-8")
    assert threat_intel.load_questions(path)[0]["correct_options"] == ["A", "C"]


def test_threat_intel_base_does_not_require_local_kb(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(threat_intel, "load_questions", lambda: [{
        "idx": 0, "question": "q", "options": ["A. x"],
        "correct_options": ["A"], "topic": "t", "difficulty": "easy",
    }])

    async def llm(system: str, user: str) -> str:
        return 'ANSWER: ["A"]'

    run = asyncio.run(threat_intel.run_bench(
        n=1, mode="base", llm=llm, log_dir=tmp_path))
    assert run["scores"]["correct_mc_pct"] == 1.0


def test_oversize_asset_forces_daily_representative_set(tmp_path: Path) -> None:
    huge = tmp_path / "huge.jsonl"
    with huge.open("wb") as stream:
        stream.truncate(1024 ** 3 + 1)  # sparse，不实际占用 1GiB 磁盘
    count, decision = apply_size_policy(
        "secalertbench", "publication", None, 8322, [huge])
    assert count == 600
    assert decision["forced_subset"] is True
    assert decision["reason"] == "single_asset_over_1GiB"


def test_forced_size_policy_honors_explicit_smoke_n(tmp_path: Path) -> None:
    """强制代表集模式不得把显式 smoke n 静默扩大到 daily 默认值。"""
    huge = tmp_path / "huge.jsonl"
    with huge.open("wb") as stream:
        stream.truncate(1024 ** 3 + 1)
    expectations = [(30, 30), (600, 600), (1000, 600), (None, 600)]
    for requested, expected in expectations:
        count, decision = apply_size_policy(
            "secalertbench", "publication", requested, 8322, [huge])
        assert count == expected, f"requested={requested}"
        assert decision["forced_subset"] is True
        assert decision["requested_n"] == requested
        assert decision["n_capped_to_daily_default"] is (requested is not None
                                                         and requested > 600)


def test_size_policy_not_forced_preserves_requested(tmp_path: Path) -> None:
    small = tmp_path / "small.jsonl"
    small.write_text("[]\n", encoding="utf-8")
    count, decision = apply_size_policy(
        "secalertbench", "publication", 30, 8322, [small])
    assert count == 30
    assert decision["forced_subset"] is False


def test_secalertbench_fixture_run(tmp_path: Path, monkeypatch) -> None:
    data_dir = tmp_path / "alerts"
    data_dir.mkdir()
    rows = [
        {"id": "a1", "alert": "malware execution", "label": "Attack", "alert_type": "edr"},
        {"id": "a2", "alert": "approved backup", "label": "Non-Attack", "alert_type": "backup"},
    ]
    (data_dir / "alerts.json").write_text(json.dumps(rows), encoding="utf-8")
    monkeypatch.setenv("CYBERORION_SECALERTBENCH_DIR", str(data_dir))

    async def llm(_system: str, user: str) -> str:
        verdict = "attack" if "malware" in user else "benign"
        return json.dumps({"verdict": verdict, "confidence": 0.9})

    run = asyncio.run(secalertbench.run_bench(
        n=2, mode="base", log_dir=tmp_path / "logs", llm=llm))
    assert run["scores"]["macro_f1"] == 1.0
    assert run["benchmark_provenance"]["sample_manifest"] == ["a1", "a2"]
    assert run["methodology_status"] == "external_track"
    assert run["scores"]["pr_auc"] == 1.0
    assert "brier_score" in run["scores"]
    assert Path(run["sample_manifest_path"]).is_file()


def _imbalanced_alerts_fixture(tmp_path: Path, monkeypatch,
                               n_attack: int = 6, n_benign: int = 2) -> None:
    data_dir = tmp_path / "alerts_imbalanced"
    data_dir.mkdir()
    rows = [{"id": f"a{i}", "alert": f"malware {i}", "label": "Attack",
             "alert_type": "edr"} for i in range(n_attack)]
    rows += [{"id": f"b{i}", "alert": f"backup {i}", "label": "Non-Attack",
              "alert_type": "backup"} for i in range(n_benign)]
    (data_dir / "secalertbench.json").write_text(json.dumps(rows), encoding="utf-8")
    monkeypatch.setenv("CYBERORION_SECALERTBENCH_DIR", str(data_dir))


def test_secalert_full_upstream_preserves_natural_prevalence(
        tmp_path: Path, monkeypatch) -> None:
    """真正的全量上游评估（count 覆盖全部可用行）选择所有行、不做类
    重采样，天然不平衡的上游数据不会因 50/50 配额失败。"""
    _imbalanced_alerts_fixture(tmp_path, monkeypatch)

    async def llm(_system: str, _user: str) -> str:
        return json.dumps({"verdict": "attack", "confidence": 0.5})

    run = asyncio.run(secalertbench.run_bench(
        n=8, mode="base", log_dir=tmp_path / "logs", llm=llm))
    manifest = run["selection_manifest"]
    assert manifest["selection_policy"] == "full_upstream_no_resampling"
    assert manifest["algorithm"] == "full_upstream_no_resampling"
    assert manifest["resampling"] == "none"
    assert manifest["requested_class_counts"] is None
    assert manifest["selected_class_counts"] == {"attack": 6, "benign": 2}
    assert len(manifest["selected_ids"]) == 8
    assert run["n"] == 8


def test_secalert_imbalanced_dataset_subset_is_class_balanced(
        tmp_path: Path, monkeypatch) -> None:
    """同一不平衡数据集取子集时仍执行类平衡抽样。"""
    _imbalanced_alerts_fixture(tmp_path, monkeypatch)

    async def llm(_system: str, _user: str) -> str:
        return json.dumps({"verdict": "benign", "confidence": 0.5})

    run = asyncio.run(secalertbench.run_bench(
        n=4, mode="base", log_dir=tmp_path / "logs", llm=llm))
    manifest = run["selection_manifest"]
    assert manifest["selection_policy"] == "class_balanced_representative_subset"
    assert manifest["requested_class_counts"] == {"attack": 2, "benign": 2}
    assert manifest["selected_class_counts"] == {"attack": 2, "benign": 2}


def test_forced_representative_directory_stays_subset_mode(
        tmp_path: Path, monkeypatch) -> None:
    """强制代表目录即使 count 等于其行数也仍是代表子集，不误判为全量
    上游评估。"""
    root = tmp_path / "asset_root"
    rep = root / "representative"
    rep.mkdir(parents=True)
    huge = root / "secalertbench.json"
    with huge.open("wb") as stream:
        stream.truncate(1024 ** 3 + 1)  # sparse，强制代表集模式
    rows = [{"id": f"a{i}", "alert": f"malware {i}", "label": "Attack",
             "alert_type": "edr"} for i in range(5)]
    rows += [{"id": f"b{i}", "alert": f"backup {i}", "label": "Non-Attack",
              "alert_type": "backup"} for i in range(5)]
    (rep / "rep.json").write_text(json.dumps(rows), encoding="utf-8")
    monkeypatch.setenv("CYBERORION_SECALERTBENCH_DIR", str(root))

    async def llm(_system: str, _user: str) -> str:
        return json.dumps({"verdict": "benign", "confidence": 0.5})

    run = asyncio.run(secalertbench.run_bench(
        n=10, mode="base", log_dir=tmp_path / "logs", llm=llm))
    assert run["representative_asset_decision"]["forced_subset"] is True
    manifest = run["selection_manifest"]
    assert manifest["selection_policy"] == "class_balanced_representative_subset"
    assert manifest["selected_class_counts"] == {"attack": 5, "benign": 5}
    assert run["n"] == 10


def test_secalertbench_official_label_schema_and_split_dedup(tmp_path: Path) -> None:
    canonical = tmp_path / "secalertbench.json"
    split = tmp_path / "secalertbench_attack.json"
    row = {"Label": "Attack", "attack_type": "代码执行", "rule_name": "r"}
    canonical.write_text(json.dumps([row]), encoding="utf-8")
    split.write_text(json.dumps([row]), encoding="utf-8")
    loaded = secalertbench.load_alerts([canonical, split])
    assert len(loaded) == 1
    assert loaded[0]["label"] == "attack"
    assert loaded[0]["alert_type"] == "代码执行"
    assert "Label" not in loaded[0]["alert"]


def test_secalert_model_visible_payload_recursively_removes_evaluation_fields() -> None:
    row = {
        "Label": "Attack", "attack_type": "代码执行", "uri": "/safe-feature",
        "nested": {"ground_truth": "Attack", "verdict": "malicious",
                   "class": 1, "rule_name": "observable"},
    }
    alert = secalertbench._normalise(row, 0)
    visible = json.dumps(alert["alert"], ensure_ascii=False)
    for key in ("Label", "ground_truth", "verdict", "class"):
        assert key not in visible
    assert "observable" in visible


def test_secalert_base_prompt_and_get_alert_never_expose_gold(tmp_path: Path,
                                                              monkeypatch) -> None:
    data_dir = tmp_path / "alerts_no_leak"; data_dir.mkdir()
    records = [
        {"id": "a", "Label": "Attack", "alert": {"message": "suspicious",
         "label": "Attack", "evaluation": {"ground_truth": "Attack"}}},
        {"id": "b", "Label": "Non-Attack", "alert": {"message": "routine",
         "verdict": "Non-Attack", "class": "benign"}},
    ]
    (data_dir / "secalertbench.json").write_text(json.dumps(records), encoding="utf-8")
    monkeypatch.setenv("CYBERORION_SECALERTBENCH_DIR", str(data_dir))
    seen: list[str] = []

    async def base_llm(_system: str, user: str) -> str:
        seen.append(user)
        return '{"verdict":"attack","attack_probability":0.5}'

    asyncio.run(secalertbench.run_bench(
        n=2, mode="base", log_dir=tmp_path / "base", llm=base_llm))
    assert all('"Label"' not in prompt and '"label"' not in prompt
               and "ground_truth" not in prompt and '"class"' not in prompt
               for prompt in seen)

    calls = 0
    async def agent_llm(_system: str, _user: str) -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            return '{"action":{"type":"tool","tool":"get_alert","arguments":{}}}'
        return ('{"action":{"type":"tool","tool":"task_complete",'
                '"arguments":{"verdict":"attack","attack_probability":0.5}}}')

    _, trace = asyncio.run(secalertbench._run_agent(
        secalertbench.load_alerts([data_dir / "secalertbench.json"])[0],
        "single", agent_llm))
    output = trace["tool_calls"][0]["output"]
    assert '"Label"' not in output and '"label"' not in output
    assert "ground_truth" not in output and '"class"' not in output


def test_secalertbench_accepts_explicit_runtime_text_verdict() -> None:
    verdict, confidence = secalertbench._parse_verdict(
        "Investigation complete. verdict: attack; confidence high.")
    assert verdict == "attack"
    assert confidence == 0.5  # 未给可解析概率时保持中性，不伪造确定性。


def test_secalert_verdict_prefix_canonicalization_real_smoke_strings() -> None:
    """真实冒烟中出现的显式 verdict 变体必须按前缀归一化，而不是 unknown。"""
    cases = [
        (json.dumps({"verdict": "attack (attempted but failed, no successful compromise)",
                     "attack_probability": 0.5}), "attack"),
        (json.dumps({"verdict": "attack_attempt_unsuccessful"}), "attack"),
        (json.dumps({"verdict": "malicious (confirmed by signature)"}), "attack"),
        (json.dumps({"verdict": "non-attack"}), "benign"),
        (json.dumps({"verdict": "non_attack"}), "benign"),
        (json.dumps({"verdict": "benign (routine backup activity)"}), "benign"),
        ("INVALID JSON {", "unknown"),
        ("", "unknown"),
    ]
    for raw, expected in cases:
        verdict, _ = secalertbench._parse_verdict(raw)
        assert verdict == expected, raw


def test_secalert_verdict_never_inferred_from_prose() -> None:
    """无显式 verdict/label 字段的说明文字绝不推断 verdict。"""
    verdict, _ = secalertbench._parse_verdict(
        "The alert shows attack behavior clearly.")
    assert verdict == "unknown"
    verdict, _ = secalertbench._parse_verdict(
        '{"assessment": "attack-like activity was observed"}')
    assert verdict == "unknown"


def _fake_openai(monkeypatch, captured: dict) -> None:
    import openai

    class FakeCompletions:
        async def create(self, **kwargs):
            captured.update(kwargs)

            class _Message:
                content = "{}"

            class _Choice:
                message = _Message()

            class _Response:
                choices = [_Choice()]

            return _Response()

    class FakeChat:
        completions = FakeCompletions()

    class FakeClient:
        def __init__(self, **kwargs):
            captured["init_kwargs"] = kwargs

        chat = FakeChat()

    monkeypatch.setattr(openai, "AsyncOpenAI", FakeClient)


def test_explicit_temperature_zero_is_passed_to_llm_client(
        monkeypatch) -> None:
    captured: dict = {}
    _fake_openai(monkeypatch, captured)
    monkeypatch.setenv("CO_BENCH_TEMPERATURE", "0")
    llm = cybersoceval.make_llm()
    asyncio.run(llm("system", "user"))
    assert captured["temperature"] == 0.0


def test_model_metadata_persists_explicit_temperature(monkeypatch) -> None:
    from cyberorion.bench.external_common import model_metadata
    monkeypatch.setenv("CO_BENCH_TEMPERATURE", "0")
    meta = model_metadata("openai/x")
    assert meta["temperature"] == 0.0
    assert meta["temperature_status"] == "explicit"
    monkeypatch.delenv("CO_BENCH_TEMPERATURE", raising=False)
    meta = model_metadata("openai/x")
    assert meta["temperature"] is None
    assert meta["temperature_status"] == "provider_default"


def test_absent_temperature_preserves_provider_default(monkeypatch) -> None:
    captured: dict = {}
    _fake_openai(monkeypatch, captured)
    monkeypatch.delenv("CO_BENCH_TEMPERATURE", raising=False)
    llm = cybersoceval.make_llm()
    asyncio.run(llm("system", "user"))
    assert "temperature" not in captured


def test_default_max_output_tokens_matches_request_and_metadata(monkeypatch) -> None:
    from cyberorion.bench.external_common import model_metadata

    captured: dict = {}
    _fake_openai(monkeypatch, captured)
    monkeypatch.delenv("CO_BENCH_MAX_TOKENS", raising=False)
    llm = cybersoceval.make_llm()
    asyncio.run(llm("system", "user"))
    assert captured["max_tokens"] == 8192
    assert model_metadata("openai/x")["max_output_tokens"] == 8192


def test_explicit_max_output_tokens_controls_request_and_metadata(monkeypatch) -> None:
    from cyberorion.bench.external_common import model_metadata

    captured: dict = {}
    _fake_openai(monkeypatch, captured)
    monkeypatch.setenv("CO_BENCH_MAX_TOKENS", "6144")
    llm = cybersoceval.make_llm()
    asyncio.run(llm("system", "user"))
    assert captured["max_tokens"] == 6144
    assert model_metadata("openai/x")["max_output_tokens"] == 6144


@pytest.mark.parametrize("raw", ["0", "-1", "not-an-int", "1.5"])
def test_invalid_max_output_tokens_fails_clearly(monkeypatch, raw: str) -> None:
    from cyberorion.bench.external_common import model_metadata

    captured: dict = {}
    _fake_openai(monkeypatch, captured)
    monkeypatch.setenv("CO_BENCH_MAX_TOKENS", raw)
    with pytest.raises(ValueError, match="CO_BENCH_MAX_TOKENS 必须是正整数"):
        cybersoceval.make_llm()
    with pytest.raises(ValueError, match="CO_BENCH_MAX_TOKENS 必须是正整数"):
        model_metadata("openai/x")


def test_secalert_persists_effective_max_output_tokens(
        tmp_path: Path, monkeypatch) -> None:
    data_dir = tmp_path / "alerts_max_tokens"
    data_dir.mkdir()
    (data_dir / "alerts.json").write_text(json.dumps([
        {"id": "a1", "alert": "malware", "label": "Attack"},
        {"id": "a2", "alert": "backup", "label": "Non-Attack"},
    ]), encoding="utf-8")
    monkeypatch.setenv("CYBERORION_SECALERTBENCH_DIR", str(data_dir))
    monkeypatch.setenv("CO_BENCH_MAX_TOKENS", "6144")

    async def llm(_system: str, _user: str) -> str:
        return '{"verdict":"attack","attack_probability":0.5}'

    run = asyncio.run(secalertbench.run_bench(
        n=2, mode="base", log_dir=tmp_path / "logs", llm=llm))
    persisted = json.loads(Path(run["path"]).read_text(encoding="utf-8"))
    assert run["model_settings"]["max_output_tokens"] == 6144
    assert persisted["model_settings"]["max_output_tokens"] == 6144


def test_compare_parent_keeps_three_arms_under_one_run(tmp_path: Path,
                                                       monkeypatch) -> None:
    data_dir = tmp_path / "alerts_compare"
    data_dir.mkdir()
    (data_dir / "alerts.json").write_text(json.dumps([
        {"id": "a1", "alert": "malware", "label": "Attack", "alert_type": "edr"},
        {"id": "a2", "alert": "backup", "label": "Non-Attack", "alert_type": "backup"},
    ]), encoding="utf-8")
    monkeypatch.setenv("CYBERORION_SECALERTBENCH_DIR", str(data_dir))

    async def llm(_system: str, _user: str) -> str:
        return json.dumps({"verdict": "attack", "confidence": 1.0})

    run = asyncio.run(cybersoceval.run_bench(
        n=2, mode="compare", suite="secalertbench", llm=llm,
        log_dir=tmp_path / "logs", run_id="parent"))
    assert [a["mode"] for a in run["comparison"]["arms"]] == [
        "base", "single", "agent"]
    assert run["comparison"]["shared"]["seed"] == 42
    assert Path(run["path"]).is_file()


def _secalert_compare_fixture(tmp_path: Path, monkeypatch) -> Path:
    data_dir = tmp_path / "alerts_compare_prov"
    data_dir.mkdir()
    (data_dir / "alerts.json").write_text(json.dumps([
        {"id": "a1", "alert": "malware", "label": "Attack", "alert_type": "edr"},
        {"id": "a2", "alert": "backup", "label": "Non-Attack", "alert_type": "backup"},
    ]), encoding="utf-8")
    monkeypatch.setenv("CYBERORION_SECALERTBENCH_DIR", str(data_dir))
    return data_dir


def test_compare_captures_shared_provenance_before_any_output(
        tmp_path: Path, monkeypatch) -> None:
    """compare 只捕获一次共享源码快照；三臂持久化同一份干净 provenance，
    基准自身产物不污染后臂。旧行为会在每臂 persist 时重捕获 → dirty。"""
    import cyberorion.bench.external_common as external_common
    _secalert_compare_fixture(tmp_path, monkeypatch)

    async def llm(_system: str, _user: str) -> str:
        return json.dumps({"verdict": "attack", "confidence": 1.0})

    calls = {"count": 0}

    def fake_git_provenance():
        calls["count"] += 1
        # 除 compare 开始时的共享快照外，任何重捕获都视为被本 run 自己
        # 刚写出的结果文件污染 → dirty。
        dirty = calls["count"] > 1
        return {"git_head_sha": "h1", "git_tree_sha": "t1",
                "git_dirty": dirty, "git_diff_sha256": "d" if dirty else None}

    monkeypatch.setattr(external_common, "git_provenance", fake_git_provenance)
    run = asyncio.run(cybersoceval.run_bench(
        n=2, mode="compare", suite="secalertbench", llm=llm,
        log_dir=tmp_path / "logs", run_id="parent"))
    assert calls["count"] == 1
    arm_jsons = {
        mode: json.loads((tmp_path / "logs" / f"parent_{mode}.json")
                         .read_text(encoding="utf-8"))
        for mode in ("base", "single", "agent")}
    for arm in arm_jsons.values():
        assert arm["git_dirty"] is False
        assert arm["git_head_sha"] == "h1" and arm["git_tree_sha"] == "t1"
        assert arm["git_provenance_source"] == "compare_shared_source_snapshot"
    assert run["git_dirty"] is False and run["git_head_sha"] == "h1"
    assert run["git_provenance_source"] == "compare_shared_source_snapshot"
    assert run["comparison"]["publication_valid"] is True


def test_compare_starting_from_dirty_tree_is_not_publication_valid(
        tmp_path: Path, monkeypatch) -> None:
    import cyberorion.bench.external_common as external_common
    _secalert_compare_fixture(tmp_path, monkeypatch)

    async def llm(_system: str, _user: str) -> str:
        return json.dumps({"verdict": "attack", "confidence": 1.0})

    def fake_git_provenance():
        return {"git_head_sha": "h1", "git_tree_sha": "t1",
                "git_dirty": True, "git_diff_sha256": "dirty-diff"}

    monkeypatch.setattr(external_common, "git_provenance", fake_git_provenance)
    run = asyncio.run(cybersoceval.run_bench(
        n=2, mode="compare", suite="secalertbench", llm=llm,
        log_dir=tmp_path / "logs", run_id="parent_dirty"))
    assert run["git_dirty"] is True
    assert run["comparison"]["publication_valid"] is False
    assert "clean_complete_provenance" in run["comparison"]["invalid_reasons"]


def test_excytin_fixture_run_with_read_only_database(tmp_path: Path, monkeypatch) -> None:
    data_dir = tmp_path / "excytin"
    data_dir.mkdir()
    (data_dir / "questions.json").write_text(json.dumps([
        {"id": "q1", "question": "Which account was compromised?",
         "answer": "svc_backup", "incident": "i1", "hop_length": 2},
    ]), encoding="utf-8")
    db = data_dir / "telemetry.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE auth(account TEXT, status TEXT)")
        conn.execute("INSERT INTO auth VALUES ('svc_backup', 'compromised')")
    monkeypatch.setenv("CYBERORION_EXCYTIN_DIR", str(data_dir))

    async def llm(_system: str, _user: str) -> str:
        return json.dumps({"answer": "svc_backup"})

    run = asyncio.run(excytin.run_bench(
        n=1, mode="base", log_dir=tmp_path / "logs", llm=llm))
    assert run["scores"]["answer_accuracy"] == 1.0
    tools = excytin.ReadOnlySQLTools(db)
    assert tools.run_query("SELECT * FROM auth")["rows"][0]["account"] == "svc_backup"
    assert "error" in tools.run_query("DELETE FROM auth")
    assert run["methodology_status"] == "external_track"
    assert run["scores"]["official_reward"] is None
    assert run["scores"]["native_reward"] == 1.0
    validation = run["telemetry_database_validation"]
    assert validation["header_verified"] is True
    assert validation["sqlite_version"]


def test_excytin_rejects_dockerfile_dot_db_before_llm(tmp_path: Path,
                                                       monkeypatch) -> None:
    data_dir = tmp_path / "bad_excytin"; data_dir.mkdir()
    (data_dir / "questions.json").write_text(json.dumps([
        {"id": "q", "question": "q?", "answer": "a"}]), encoding="utf-8")
    (data_dir / "Dockerfile.db").write_text("# Custom MySQL image\nFROM mysql:8\n",
                                             encoding="utf-8")
    monkeypatch.setenv("CYBERORION_EXCYTIN_DIR", str(data_dir))
    calls = 0
    async def llm(_system: str, _user: str) -> str:
        nonlocal calls
        calls += 1
        return '{"answer":"a"}'
    with pytest.raises(Exception, match="资产未配置|SQLite"):
        asyncio.run(excytin.run_bench(
            n=1, mode="base", log_dir=tmp_path / "logs", llm=llm))
    assert calls == 0


def test_excytin_official_mode_does_not_require_sqlite(
        tmp_path: Path, monkeypatch) -> None:
    """official 模式绝不能走 SQLite 选择路径（即使资产缺失）。"""
    import cyberorion.bench.excytin as module
    data_dir = tmp_path / "excytin_official"
    data_dir.mkdir()
    (data_dir / "questions.json").write_text("[]", encoding="utf-8")
    monkeypatch.setenv("CYBERORION_EXCYTIN_DIR", str(data_dir))
    selected: list[bool] = []
    monkeypatch.setattr(module, "select_telemetry_database",
                        lambda files: (selected.append(True), None)[1]
                        or (_ for _ in ()).throw(AssertionError("must not run")))
    with pytest.raises(module.BenchmarkAssetMissing, match="Inspect/SABER"):
        asyncio.run(module.run_bench(
            n=1, mode="base", log_dir=tmp_path / "logs",
            execution_mode="official"))
    assert selected == []


def test_excytin_invalid_execution_mode_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="execution_mode"):
        asyncio.run(excytin.run_bench(
            n=1, mode="base", log_dir=tmp_path / "logs",
            execution_mode="nonsense"))


def test_excytin_official_agent_bridge_factory_shape() -> None:
    """桥接工厂必须符合 SABER 两级工厂契约：create_agent() ->
    create_with_prompts(instruction_prompt, assistant_prompt, tools,
    max_steps) -> async solve(state, generate)。"""
    from cyberorion.bench.excytin_official_agent import create_agent
    for arm in ("single", "orchestrator_only", "full"):
        # create_agent(...) 返回中间层 create_with_prompts；
        # create_with_prompts(...) 返回 async solve(state, generate)。
        create_with_prompts = create_agent(arm=arm)
        solver = create_with_prompts(
            instruction_prompt="instr", assistant_prompt="assist",
            tools=[], max_steps=5)
        assert callable(solver)
        import inspect
        assert inspect.iscoroutinefunction(solver)


def test_excytin_official_agent_tool_spec_prefers_attributes() -> None:
    from cyberorion.bench.excytin_official_agent import _tool_spec

    async def official_call(x: str) -> str:
        """query telemetry"""
        return x

    official_call.name = "run_query"  # type: ignore[attr-defined]
    official_call.description = "Run a read-only SQL query"  # type: ignore[attr-defined]
    official_call.input = {"type": "object",  # type: ignore[attr-defined]
                           "properties": {"x": {"type": "string"}},
                           "required": ["x"]}
    spec = _tool_spec(official_call)
    assert spec.name == "run_query"
    assert spec.description == "Run a read-only SQL query"
    assert spec.input_schema["required"] == ["x"]
    # 无属性回退到签名/docstring 自省
    async def plain(y: str) -> str:
        """plain tool"""
        return y

    spec2 = _tool_spec(plain)
    assert spec2.name == "plain"
    assert spec2.input_schema["required"] == ["y"]


def test_official_runner_provenance_is_explicit(tmp_path: Path) -> None:
    from scripts.run_excytin_official import build_provenance
    provenance = build_provenance(
        upstream=tmp_path, repo=tmp_path, arm="cyberorion_single",
        model="openai/m", judge_llm="openai/j", task_ids=["task-1", "task-2"],
        manifest_sha256="a" * 64,
        extra_task_args={}, started=1.0, finished=2.0, log_dir=tmp_path / "logs")
    assert provenance["official_execution"] is True
    assert provenance["sqlite_projection_involved"] is False
    assert provenance["upstream"] == "microsoft/ACESEvals"
    assert provenance["arm"] == "cyberorion_single"
    assert provenance["task_ids"] == ["task-1", "task-2"]
    assert provenance["task_manifest_sha256"] == "a" * 64
    assert provenance["decoding_config"] == {"temperature": 0}
    assert provenance["judge_config"] == {"model": "openai/j"}
    assert set(provenance["cyberorion_source"]) == {
        "git_head", "git_tree_sha", "git_dirty", "git_diff_sha256"}


def test_excytin_sql_tools_have_explicit_required_schemas(tmp_path: Path) -> None:
    db = tmp_path / "telemetry.sqlite"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE events(id INTEGER)")
    specs = excytin.sql_tool_specs(excytin.ReadOnlySQLTools(db))
    assert specs["list_tables"].input_schema["properties"] == {}
    assert specs["describe_table"].input_schema["required"] == ["table"]
    assert specs["run_query"].input_schema["required"] == ["sql"]
    assert specs["run_query"].input_schema["properties"]["sql"]["type"] == "string"


def test_soc_contract_has_12_cases_and_real_runtime_trace(tmp_path: Path) -> None:
    calls = 0

    async def llm(_system: str, _user: str) -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            return json.dumps({"hypothesis": "inspect", "evidence_ids": [],
                               "action": {"type": "tool", "tool": "query_telemetry",
                                          "arguments": {}}})
        return json.dumps({
            "verdict": "malicious", "incident_labels": ["credential_access", "lateral_movement"],
            "attack_techniques": ["T1110", "T1021.001"],
            "evidence_ids": ["E1", "E2", "E3"],
            "response_actions": ["isolate_host", "disable_account", "preserve_evidence"],
            "claims": [{"text": "chain", "evidence_ids": ["E1"]}],
            "confidence": .9,
        })

    assert len(soc_contract.load_cases()) == 12
    run = asyncio.run(soc_contract.run_bench(
        n=1, mode="single", seed=1, log_dir=tmp_path, llm=llm))
    assert run["methodology_status"] == "engineering_only"
    assert run["results"][0]["tool_calls"][0]["tool"] == "query_telemetry"
    assert run["results"][0]["trace_source"] == "runtime"


def test_cage2_uses_official_3x3_matrix_but_is_not_leaderboard_comparable(
        tmp_path: Path, monkeypatch) -> None:
    asset = tmp_path / "cage"
    asset.mkdir()
    (asset / "Scenario2.yaml").write_text("Hosts: {}\n", encoding="utf-8")
    (asset / "evaluation.py").write_text("# fixture\n", encoding="utf-8")
    monkeypatch.setenv("CYBERORION_CAGE2_DIR", str(asset))

    def fake_run(episodes, steps, llm_driven, policy, scenario, red_agent, seed, wrapper):
        return {"episodes": [{"episode": i + 1, "reward": -float(steps),
                              "illegal_actions": 0, "restore_actions": 0,
                              "restore_cost_proxy": 0.0}
                             for i in range(episodes)]}

    monkeypatch.setattr("cyberorion.eval.benchmarks.run_cage2", fake_run)
    run = asyncio.run(cage2.run_bench(n=9, mode="base", log_dir=tmp_path / "logs"))
    assert len(run["conditions"]) == 9
    assert run["methodology_status"] == "external_track"
    assert run["benchmark_provenance"]["comparable_to_upstream"] is False
    assert "availability_penalty" not in run["scores"]
    assert run["scores"]["restore_cost_proxy_status"] == "non_native_proxy"


def test_cage_challenge_wrapper_executes_exact_non_sleep_action_id() -> None:
    from cyberorion.eval.benchmarks.cyborg_adapter import run_cage2_async

    async def choose_restore(_observation, *, available_actions, **_kwargs):
        restore = next(row for row in available_actions
                       if row["action_type"] == "Restore")
        return {"action_id": restore["action_id"]}

    async def choose_sleep(_observation, *, available_actions, **_kwargs):
        sleep = next(row for row in available_actions
                     if row["action_type"] == "Sleep")
        return {"action_id": sleep["action_id"]}

    restored = asyncio.run(run_cage2_async(
        episodes=1, steps=1, policy=choose_restore,
        red_agent="SleepAgent", seed=42, official_wrapper=True))
    slept = asyncio.run(run_cage2_async(
        episodes=1, steps=1, policy=choose_sleep,
        red_agent="SleepAgent", seed=42, official_wrapper=True))
    assert "error" not in restored and "error" not in slept
    action = restored["episodes"][0]["actions"][0]
    assert action["valid"] is True
    assert action["executed_blue_action"]["action_type"] == "Restore"
    assert action["executed_blue_action"]["action_id"] == action["requested_blue_action"]["action_id"]
    assert restored["episodes"][0]["reward"] != slept["episodes"][0]["reward"]


def test_cage_invalid_action_trace_shows_executed_sleep() -> None:
    from cyberorion.eval.benchmarks.cyborg_adapter import run_cage2_async

    async def invalid(_observation, **_kwargs):
        return {"action_id": 999999}

    run = asyncio.run(run_cage2_async(
        episodes=1, steps=1, policy=invalid,
        red_agent="SleepAgent", seed=42, official_wrapper=True))
    action = run["episodes"][0]["actions"][0]
    assert action["valid"] is False
    assert action["executed_blue_action"]["action_type"] == "Sleep"
    assert action["blue"] == "Sleep"


def test_live_paired_requires_verified_same_plan_and_snapshot(tmp_path: Path) -> None:
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps({"actions": [{"tool": "http", "args": {}}]}),
                         encoding="utf-8")
    plan_hash = __import__("hashlib").sha256(plan_path.read_bytes()).hexdigest()

    class Harness:
        def validate_environment(self): return {"ok": True, "isolated": True}
        def capture_initial_snapshot(self, seed): return {"sha256": f"snap-{seed}"}
        def reset_to_snapshot(self, snapshot): return {"ok": True, "sha256": snapshot["sha256"]}
        def run_trial(self, *, arm, attack_plan, seed, snapshot, budget):
            return {"status": "done", "score": {"base": .2, "single": .4, "agent": .7}[arm],
                    "attack_sequence_sha256": plan_hash,
                    "initial_snapshot_sha256": snapshot["sha256"], "budget": budget,
                    "metrics": {
                        "detection": 1.0, "attribution_correctness": 1.0,
                        "containment_success": 1.0, "mttd_sec": 1.0,
                        "time_to_containment_sec": 2.0, "compromise_count": 0,
                        "blast_radius": 0, "false_positives": 0,
                        "unsafe_actions": 0, "availability_penalty": 0.0,
                        "llm_calls": 1, "tool_calls": 1, "tokens": 10,
                        "wall_clock_sec": 1.0,
                    }}

    run = asyncio.run(live_paired.run_bench(
        n=2, harness=Harness(), attack_plan_path=plan_path, log_dir=tmp_path / "logs"))
    assert len(run["results"]) == 6
    assert run["scores"]["agent_minus_single"] == pytest.approx(.3)
    assert run["methodology"]["paired"] is True


def test_safe_extract_rejects_repository_and_path_traversal(tmp_path: Path,
                                                            monkeypatch) -> None:
    import cyberorion.bench.cybergym_lite as module
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setattr(module, "CACHE_DIR", cache)
    archive_path = tmp_path / "bad.tar.gz"
    with tarfile.open(archive_path, "w:gz") as archive:
        info = tarfile.TarInfo("../escape.txt")
        payload = b"bad"
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))
    with tarfile.open(archive_path, "r:gz") as archive:
        with pytest.raises(ValueError, match="路径穿越"):
            _safe_extract(archive, cache / "task")
    with tarfile.open(archive_path, "r:gz") as archive:
        with pytest.raises(ValueError, match="危险目录|cache"):
            _safe_extract(archive, Path.cwd())


def test_bench_suites_api_function_exposes_assets_without_testclient() -> None:
    """绕过当前环境会挂住的 TestClient，直接守护 API 数据组装。"""
    import server

    payload = asyncio.run(server.bench_suites())
    rows = {row["suite"]: row for row in payload["suites"]}
    assert rows["malware_analysis"]["asset"]["available"] is True
    assert rows["secalertbench"]["asset"]["suite"] == "secalertbench"
    assert "live_paired" in rows


def test_excytin_official_context_preserves_all_saber_prompts_without_gold() -> None:
    from cyberorion.bench.excytin_official_agent import build_official_context

    serialized, audit = build_official_context(
        instruction_prompt="official instruction sentinel",
        assistant_prompt="official assistant sentinel",
        task_input="official task sentinel",
    )
    assert json.loads(serialized) == {
        "instruction_prompt": "official instruction sentinel",
        "assistant_prompt": "official assistant sentinel",
        "task_input": "official task sentinel",
    }
    assert all(audit[f"{name}_present"] for name in (
        "instruction_prompt", "assistant_prompt", "task_input"))
    assert audit["gold_or_scorer_context_added"] is False
    assert len(audit["effective_context_sha256"]) == 64

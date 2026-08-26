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

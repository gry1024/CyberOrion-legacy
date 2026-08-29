"""Official ExCyTIn bridge architecture-validity regression tests."""
from __future__ import annotations

import ast
import hashlib
import inspect
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from cyberorion.bench import excytin_official_agent as bridge


REPO = Path(__file__).resolve().parents[1]
UPSTREAM = REPO / "benchmarks" / "external" / "excytin"
UPSTREAM_PYTHON = UPSTREAM / ".venv" / "bin" / "python"


def test_official_bridge_does_not_import_parallel_json_runtime() -> None:
    source_path = REPO / "cyberorion" / "bench" / "excytin_official_agent.py"
    source = source_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert "superagent_runtime" not in source
    assert not any(name.endswith("superagent_runtime") for name in imports)
    assert "_tool_spec" not in source
    assert "custom_json_action_protocol\": False" in source


def test_official_upstream_and_lock_pins_are_unchanged() -> None:
    if not (UPSTREAM / ".git").exists():
        pytest.skip("pinned ACESEvals checkout is unavailable")
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=UPSTREAM, check=True,
        capture_output=True, text=True).stdout.strip()
    assert head == "17135140d0fdf52c2264a1fc248cf01e16b23a79"
    subprocess.run(
        ["git", "diff", "--quiet", "HEAD", "--", "domains/excytin",
         "uv.lock", "pyproject.toml"],
        cwd=UPSTREAM, check=True)
    lock = (UPSTREAM / "uv.lock").read_text(encoding="utf-8")
    assert "#a9bdce1343fd1c331aafda3119cbf0d48f215382" in lock
    assert "#f94a85b6b3a246d2e4417b49bdda96fd8f04b93a" in lock


def test_three_arms_have_identical_official_environment_tool_union() -> None:
    tools = [SimpleNamespace(name="bash"), SimpleNamespace(name="python")]
    contracts = {
        arm: bridge.arm_tool_contract(arm, tools)
        for arm in ("single", "orchestrator_only", "full")
    }
    unions = {tuple(row["official_tool_union"]) for row in contracts.values()}
    assert unions == {("bash", "python")}
    assert contracts["single"]["delegation_tools"] == []
    assert contracts["orchestrator_only"]["delegation_tools"] == []
    assert set(contracts["full"]["delegation_tools"]) == {
        "dispatch_triage", "dispatch_threat_hunter",
        "dispatch_lateral_analyst", "dispatch_escalation",
    }
    assert contracts["full"]["commander_environment_tools"] == []
    assert set(contracts["full"]["worker_environment_tools"]) == set(
        bridge.ROLES)
    assert all(
        tools == ["bash", "python"]
        for tools in contracts["full"]["worker_environment_tools"].values()
    )
    assert contracts["single"]["investigation_tool_union"] == [
        "bash", "python"]


def test_prompts_define_strong_arms_and_all_four_excytin_roles() -> None:
    assert "strong monolithic" in bridge.SINGLE_PROMPT
    assert "schema discovery" in bridge.SINGLE_PROMPT
    assert "multi-table correlation" in bridge.SINGLE_PROMPT
    assert "LIMIT 5" in bridge.SINGLE_PROMPT
    assert "row LIMIT does not make a wide projection safe" in bridge.SINGLE_PROMPT
    assert "LEFT(column, 500)" in bridge.SINGLE_PROMPT
    assert "about 20 official tool calls" in bridge.SINGLE_PROMPT
    assert "AdditionalFields" in bridge.SINGLE_PROMPT
    assert "Dispatch is disabled" in bridge.ORCHESTRATOR_ONLY_PROMPT
    assert "do not directly execute" in bridge.COMMANDER_PROMPT
    assert "automatically receives the official task context" in bridge.COMMANDER_PROMPT
    assert "run concurrently" in bridge.COMMANDER_PROMPT
    assert all(
        f"dispatch_{role}(mission)" in bridge.COMMANDER_PROMPT
        for role in bridge.ROLES
    )
    assert set(bridge.ROLE_DESCRIPTIONS) == set(bridge.ROLES)
    assert all(
        role in bridge.COMMANDER_PROMPT
        and description in bridge.COMMANDER_PROMPT
        for role, description in bridge.ROLE_DESCRIPTIONS.items()
    )
    prompts = {role: bridge.specialist_prompt(role) for role in bridge.ROLES}
    assert "schema" in prompts["triage"]
    assert "incident chain" in prompts["threat_hunter"]
    assert "cross-host" in prompts["lateral_analyst"]
    assert "Never invent environment actions" in prompts["escalation"]
    irrelevant = ("iptables", "docker exec", "remove_file", "kill_process")
    assert not any(term in "\n".join(prompts.values()) for term in irrelevant)


def test_specialist_reports_are_structured_and_fail_closed() -> None:
    valid = {
        "findings": ["finding"],
        "evidence": [{"claim": "c", "source": "bash", "snippet": "row"}],
        "commands_or_queries": ["mysql -e SELECT"],
        "confidence": "high",
        "uncertainties": [],
        "recommended_next_investigation": [],
        "candidate_answer_implications": ["answer"],
    }
    status, report, error = bridge.parse_specialist_report(valid, "threat_hunter")
    assert (status, error) == ("successful_report", None)
    assert report and report["role"] == "threat_hunter"
    assert bridge.parse_specialist_report("not-json", "triage")[0] == "parse_failure"
    empty = {key: [] for key in (
        "findings", "evidence", "commands_or_queries", "uncertainties",
        "recommended_next_investigation", "candidate_answer_implications")}
    empty["confidence"] = "low"
    assert bridge.parse_specialist_report(empty, "triage")[0] == "empty_report"


def test_dispatch_propagates_global_inspect_limits_before_generic_errors() -> None:
    source = inspect.getsource(bridge._DispatchController.dispatch)
    hard_limit = source.index("except LimitExceededError:")
    propagation = source.index("raise", hard_limit)
    generic_error = source.index("except Exception", propagation)
    assert hard_limit < propagation < generic_error
    assert "status = \"role_budget_exhaustion\"" not in source


def test_native_agents_use_pinned_inspect_context_compaction() -> None:
    source_path = REPO / "cyberorion" / "bench" / "excytin_official_agent.py"
    source = source_path.read_text(encoding="utf-8")
    assert "CompactionTrim" in source
    assert "_NATIVE_CONTEXT_COMPACTION_THRESHOLD_TOKENS = 48_000" in source
    assert "preserve=_NATIVE_CONTEXT_COMPACTION_PRESERVE" in source
    assert source.count('compaction=_native_context_compaction()') == 2
    assert source.count('truncation="auto"') == 2
    assert "raw_inspect_transcript_retained" in source
    assert "class _BudgetedModel(Model)" in source
    assert "custom agent and the agent needs to handle compaction directly" not in source


def test_mechanism_reducer_records_native_context_compaction() -> None:
    source = (REPO / "scripts" /
              "analyze_excytin_mechanism.py").read_text(encoding="utf-8")
    assert "CompactionEvent" in source
    assert '"native_context_compaction_events"' in source


def test_shared_state_compacts_visible_native_messages_with_raw_identity() -> None:
    long_output = "e" * 12_000
    call = SimpleNamespace(id="tc-1", function="bash",
                           arguments={"cmd": "mysql -e SHOW TABLES"})
    assistant = SimpleNamespace(
        id="m-1", role="assistant", tool_calls=[call], content="")
    tool_message = SimpleNamespace(
        id="m-2", role="tool", tool_call_id="tc-1", function="bash",
        content=long_output, error=None)
    state = bridge.InvestigationState(hashlib.sha256(b"task").hexdigest())
    state.ingest_messages(
        [assistant, tool_message], role="triage", official_names={"bash"})
    assert state.model_calls == 1
    assert state.official_tool_calls == 1
    assert "SHOW TABLES" in state.executed_commands[0]["arguments"]["text"]
    assert len(state.evidence[0]["snippet"]) < len(long_output)
    assert state.evidence[0]["raw_chars"] == len(long_output)
    assert state.evidence[0]["raw_sha256"] == hashlib.sha256(
        long_output.encode()).hexdigest()
    assert state.evidence[0]["shared_state_truncated"] is True
    assert "output" not in state.executed_commands[0]
    snapshot = state.public_snapshot()
    assert "gold" not in snapshot
    assert "scorer" not in snapshot
    assert "target" not in snapshot


def test_public_workspace_summarizes_prior_reports_but_audit_keeps_bounded_report() -> None:
    state = bridge.InvestigationState(hashlib.sha256(b"task").hexdigest())
    report = {
        "role": "triage",
        "findings": ["f" * 900 for _ in range(8)],
        "evidence": [
            {"claim": "c" * 900, "source": "bash", "snippet": "s" * 900}
            for _ in range(8)
        ],
        "commands_or_queries": ["q" * 900 for _ in range(8)],
        "confidence": "high",
        "uncertainties": ["u" * 900 for _ in range(4)],
        "recommended_next_investigation": ["n" * 900 for _ in range(4)],
        "candidate_answer_implications": ["i" * 900 for _ in range(4)],
    }
    state.record_report(
        status="successful_report", role="triage", mission="mission",
        report=report, error=None,
    )
    public_report = state.public_snapshot()["specialist_reports"][0]["report"]
    audit_report = state.audit_snapshot()["specialist_reports"][0]["report"]
    assert len(public_report["findings"]) == 3
    assert len(public_report["evidence"]) == 3
    assert len(public_report["commands_or_queries"]) == 3
    assert len(audit_report["findings"]) == 8
    assert len(audit_report["evidence"]) == 8
    assert len(json.dumps(state.public_snapshot())) < 12_000


def test_public_workspace_has_a_hard_model_visible_size_bound() -> None:
    state = bridge.InvestigationState(hashlib.sha256(b"task").hexdigest())
    state.discovered_schema.extend(
        {"table": "t" * 3, "columns": "c" * 3_000}
        for _ in range(80)
    )
    for index in range(80):
        state.executed_commands.append({
            "id": f"C-{index}", "sequence": index, "role": "triage",
            "tool": "bash", "arguments": {
                "text": "q" * 700,
                "raw_chars": 700,
                "raw_sha256": "a" * 64,
            }, "tool_call_id": f"tc-{index}", "error": None,
        })
        state.evidence.append({
            "id": f"E-{index}", "sequence": index, "role": "triage",
            "source": "bash", "tool_call_id": f"tc-{index}",
            "query_or_command": {"text": "q" * 700},
            "snippet": "e" * 1200, "raw_chars": 1200,
            "raw_sha256": "b" * 64, "shared_state_truncated": True,
        })
    snapshot = state.public_snapshot()
    assert len(json.dumps(snapshot, ensure_ascii=False)) <= (
        bridge._MAX_MODEL_VISIBLE_SNAPSHOT_CHARS)
    assert snapshot["workspace_omissions"].get(
        "model_visible_snapshot_bound_applied") is True


def test_context_audit_hashes_only_official_model_visible_fields() -> None:
    serialized, audit = bridge.build_official_context(
        instruction_prompt="instruction", assistant_prompt="assistant",
        task_input="task")
    assert set(json.loads(serialized)) == {
        "instruction_prompt", "assistant_prompt", "task_input"}
    assert audit["gold_or_scorer_context_added"] is False
    assert audit["native_inspect_tool_execution"] is True
    assert audit["custom_json_action_protocol"] is False


def test_bridge_never_reads_hidden_task_or_scorer_state() -> None:
    source = (REPO / "cyberorion" / "bench" /
              "excytin_official_agent.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported_modules = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    accessed_attributes = {
        node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    }
    assert not any("scor" in module.lower() for module in imported_modules)
    assert {"metadata", "target", "sandbox"}.isdisjoint(accessed_attributes)


def test_mechanism_reducer_never_reads_score_target_or_answer_output() -> None:
    source = (REPO / "scripts" /
              "analyze_excytin_mechanism.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    accessed_attributes = {
        node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    }
    assert {"scores", "target", "output"}.isdisjoint(accessed_attributes)


def test_mechanism_reducer_separates_model_parsing_and_limit_spans() -> None:
    source = (REPO / "scripts" /
              "analyze_excytin_mechanism.py").read_text(encoding="utf-8")
    assert 'getattr(event.error, "type", None) != "parsing"' in source
    assert '"post_limit_model_events_same_span"' in source
    assert '"post_limit_model_events_any_span"' in source


def test_native_inspect_multi_agent_mechanism_probe(tmp_path: Path) -> None:
    if not UPSTREAM_PYTHON.is_file():
        pytest.skip("pinned Inspect/SABER environment is unavailable")
    # The safety policy only permits benchmark artifacts in the explicit cage
    # artifact root, not pytest's generic /tmp directory.
    artifact_root = Path("/tmp/cyberorion_cage_runs")
    artifact_root.mkdir(parents=True, exist_ok=True)
    log_root = artifact_root / (
        f"excytin_pytest_probe_{tmp_path.parent.name}_{tmp_path.name}")
    state_root = log_root / "runtime_state"
    state_root.mkdir(parents=True, exist_ok=True)
    result_path = log_root / "probe_result.json"
    with (log_root / "probe_stdout.log").open("w", encoding="utf-8") as stdout, \
            (log_root / "probe_stderr.log").open("w", encoding="utf-8") as stderr:
        subprocess.run(
            [str(UPSTREAM_PYTHON), str(REPO / "scripts" /
                                      "excytin_architecture_probe.py"),
             "--log-root", str(log_root),
             "--result-json", str(result_path)],
            cwd=REPO, check=True, stdout=stdout, stderr=stderr, text=True,
            env={
                "PYTHONPATH": str(REPO),
                "CAI_DISABLE_USAGE_TRACKING": "true",
                "HOME": str(state_root),
                "XDG_DATA_HOME": str(state_root / "xdg_data"),
            }, timeout=90)
    probe = json.loads(result_path.read_text(encoding="utf-8"))
    assert probe["full_status"] == probe["single_status"] == "success"
    assert probe["full_error"] is probe["single_error"] is None
    assert probe["single_official_tool_visible"] is True
    assert probe["full_official_tool_union"] == probe["single_official_tool_union"]
    assert probe["full_commander_has_no_environment_tools"] is True
    assert {"dispatch_triage", "dispatch_threat_hunter",
            "dispatch_lateral_analyst", "dispatch_escalation",
            "get_investigation_summary", "submit"}.issubset(
                probe["full_commander_coordination_tools"])
    assert probe["specialist_real_tools"] is True
    assert all(all(flags.values()) for flags in probe["specialist_context"].values())
    assert probe["shared_evidence_propagated_to_lateral_analyst"] is True
    assert probe["parallel_dispatch_overlap"] is True
    assert probe["commander_reports_intact"] is True
    assert probe["full_dispatches"] == 3
    assert probe["full_successful_reports"] == 3
    assert not any(probe["full_report_failures"].values())
    assert probe["global_model_call_budget"]["calls"] == probe["full_model_calls"]
    assert probe["global_model_call_budget"]["remaining"] > 0
    assert probe["global_model_call_budget"]["by_role"] == {
        "lateral_analyst": 2, "orchestrator": 3,
        "threat_hunter": 2, "triage": 2,
    }
    assert probe["global_child_usage_counted"] is True
    assert probe["global_child_tokens_counted"] is True
    assert probe["representative_output_compacted"] is True
    assert probe["model_visible_snapshot_chars"] < 16_384
    assert probe["bridge_truncation"] is False
    assert probe["custom_json_action_protocol"] is False
    assert probe["native_inspect_tool_execution"] is True

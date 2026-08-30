#!/usr/bin/env python
"""Offline native-Inspect mechanism probe for the ExCyTIn bridge.

Run with the pinned ACESEvals virtual environment. The probe uses Inspect's
mock model and the real official ``bash``/``python`` tool implementations in
an explicit local sandbox; it does not start Docker, contact an LLM provider,
or execute the ExCyTIn scorer.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))


def _output(content: str = "", tool_calls=None):
    from inspect_ai.model import ChatMessageAssistant, ModelOutput, ModelUsage
    output = ModelOutput.from_message(
        ChatMessageAssistant(content=content, tool_calls=tool_calls),
        stop_reason="tool_calls" if tool_calls else "stop",
    )
    output.usage = ModelUsage(
        input_tokens=100, output_tokens=50, total_tokens=150)
    return output


def run_probe(log_root: Path) -> dict[str, Any]:
    from inspect_ai import Task, eval
    from inspect_ai.dataset import Sample
    from inspect_ai.model import get_model
    from inspect_ai.solver import solver as solver_decorator
    from inspect_ai.tool import ToolCall, bash, python

    from cyberorion.bench.excytin_official_agent import create_agent

    observed: dict[str, Any] = {
        "specialist_intervals": {},
        "specialist_inputs": {},
        "specialist_tool_sets": {},
        "commander_tool_sets": [],
        "single_tool_sets": [],
        "orchestrator_only_tool_sets": [],
        "resource_balances": {},
        "dispatch_tool_parameters": {},
    }

    def capture_balances(actor: str, messages) -> None:
        marker = "CYBERORION RUNTIME RESOURCE BALANCE"
        for message in messages:
            text = getattr(message, "text", "")
            if marker not in text:
                continue
            payload = text.split(marker, 1)[1].strip()
            observed["resource_balances"].setdefault(actor, []).append(
                json.loads(payload))

    def full_callback(messages, tools, _tool_choice, _config):
        names = [tool.name for tool in tools]
        system = "\n".join(
            getattr(message, "text", "")
            for message in messages
            if getattr(message, "role", "") == "system"
        )
        specialist = next(
            (role for role in ("triage", "threat_hunter", "lateral_analyst")
             if f"{role} specialist" in system),
            None,
        )
        if specialist:
            capture_balances(specialist, messages)
            observed["specialist_inputs"].setdefault(
                specialist,
                "\n".join(getattr(message, "text", "") for message in messages),
            )
            observed["specialist_tool_sets"][specialist] = names
            used_bash = any(
                getattr(message, "role", "") == "tool"
                and getattr(message, "function", "") == "bash"
                for message in messages
            )
            if not used_bash:
                observed["specialist_intervals"][specialist] = [
                    time.perf_counter(), None]
                return _output("investigate", [ToolCall(
                    id=f"{specialist}-query",
                    function="bash",
                    arguments={
                        "command": (
                            "sleep 0.08; python3 -c \"print('E' * 12000)\""
                        ),
                    },
                )])
            observed["specialist_intervals"][specialist][1] = time.perf_counter()
            return _output("report", [ToolCall(
                id=f"{specialist}-report",
                function=f"submit_{specialist}_report",
                arguments={
                    "findings": [f"{specialist} verified evidence"],
                    "evidence": [{
                        "claim": "verified", "source": "bash",
                        "snippet": specialist,
                    }],
                    "commands_or_queries": [
                        "sleep 0.08; python3 -c print representative evidence"],
                    "confidence": "high",
                    "uncertainties": ["none"],
                    "recommended_next_investigation": ["continue correlation"],
                    "candidate_answer_implications": [specialist],
                },
            )])

        capture_balances("full_commander", messages)
        for tool in tools:
            if tool.name.startswith("dispatch_"):
                observed["dispatch_tool_parameters"].setdefault(
                    tool.name, tool.parameters)
        observed["commander_tool_sets"].append(names)
        delegated = {
            str(getattr(message, "function", ""))
            for message in messages
            if getattr(message, "role", "") == "tool"
        }
        if "dispatch_triage" not in delegated:
            return _output("parallel independent survey", [
                ToolCall(id="dispatch-triage", function="dispatch_triage",
                         arguments={
                             "mission": "survey schema and evidence",
                             "token_limit": 20_000,
                             "tool_call_limit": 4,
                             "model_call_limit": 4,
                             "wall_time_sec": 10,
                         }),
                ToolCall(id="dispatch-hunter", function="dispatch_threat_hunter",
                         arguments={
                             "mission": "independently correlate evidence",
                             "token_limit": 20_000,
                             "tool_call_limit": 4,
                             "model_call_limit": 4,
                             "wall_time_sec": 10,
                         }),
            ])
        if "dispatch_lateral_analyst" not in delegated:
            return _output("follow shared evidence", [ToolCall(
                id="dispatch-lateral", function="dispatch_lateral_analyst",
                arguments={
                    "mission": "hunt residual evidence from shared state",
                    "token_limit": 20_000,
                    "tool_call_limit": 4,
                    "model_call_limit": 4,
                    "wall_time_sec": 10,
                },
            )])
        return _output("final", [ToolCall(
            id="full-submit", function="submit",
            arguments={"answer": "verified answer"},
        )])

    def single_callback(messages, tools, _tool_choice, _config):
        capture_balances("single", messages)
        observed["single_tool_sets"].append([tool.name for tool in tools])
        if not any(
            getattr(message, "role", "") == "tool"
            and getattr(message, "function", "") == "bash"
            for message in messages
        ):
            return _output("investigate", [ToolCall(
                id="single-query", function="bash",
                arguments={"command": "printf single-native-tool"},
            )])
        return _output("final", [ToolCall(
            id="single-submit", function="submit",
            arguments={"answer": "verified answer"},
        )])

    def orchestrator_only_callback(messages, tools, tool_choice, config):
        capture_balances("orchestrator_only", messages)
        observed["orchestrator_only_tool_sets"].append(
            [tool.name for tool in tools])
        if not any(
            getattr(message, "role", "") == "tool"
            and getattr(message, "function", "") == "bash"
            for message in messages
        ):
            return _output("investigate", [ToolCall(
                id="orchestrator-only-query", function="bash",
                arguments={"command": "printf orchestrator-native-tool"},
            )])
        return _output("final", [ToolCall(
            id="orchestrator-only-submit", function="submit",
            arguments={"answer": "verified answer"},
        )])

    exhaustion_observed = {
        "worker_model_callbacks": 0,
        "worker_report_calls": 0,
        "commander_received_exhaustion": False,
    }

    allocation_rejection_observed = {"worker_callbacks": 0}

    def allocation_rejection_callback(messages, tools, _tool_choice, _config):
        system = "\n".join(
            getattr(message, "text", "")
            for message in messages
            if getattr(message, "role", "") == "system"
        )
        if "triage specialist" in system:
            allocation_rejection_observed["worker_callbacks"] += 1
            return _output("unexpected worker execution")
        dispatch_messages = [
            message for message in messages
            if getattr(message, "role", "") == "tool"
            and getattr(message, "function", "") == "dispatch_triage"
        ]
        if not dispatch_messages:
            return _output("reject unsafe allocation", [ToolCall(
                id="oversized-dispatch", function="dispatch_triage",
                arguments={
                    "mission": "oversized allocation must not start",
                    "token_limit": 2_000_000,
                    "tool_call_limit": 20,
                    "model_call_limit": 20,
                    "wall_time_sec": 50,
                },
            )])
        return _output("continue after allocation rejection", [ToolCall(
            id="rejection-submit", function="submit",
            arguments={"answer": "unsafe allocation rejected"},
        )])

    def local_exhaustion_callback(messages, tools, _tool_choice, _config):
        system = "\n".join(
            getattr(message, "text", "")
            for message in messages
            if getattr(message, "role", "") == "system"
        )
        if "triage specialist" in system:
            exhaustion_observed["worker_model_callbacks"] += 1
            exhaustion_observed["worker_report_calls"] += sum(
                1 for message in messages
                if getattr(message, "role", "") == "tool"
                and getattr(message, "function", "")
                == "submit_triage_report")
            return _output("use local tool", [ToolCall(
                id="exhaustion-worker-bash", function="bash",
                arguments={"command": "printf bounded-worker-evidence"},
            )])

        dispatch_messages = [
            getattr(message, "text", "")
            for message in messages
            if getattr(message, "role", "") == "tool"
            and getattr(message, "function", "") == "dispatch_triage"
        ]
        if not dispatch_messages:
            return _output("bounded dispatch", [ToolCall(
                id="exhaustion-dispatch", function="dispatch_triage",
                arguments={
                    "mission": "map one bounded source then report",
                    "token_limit": 10_000,
                    "tool_call_limit": 4,
                    "model_call_limit": 1,
                    "wall_time_sec": 10,
                },
            )])
        exhaustion_observed["commander_received_exhaustion"] = any(
            '"status": "role_budget_exhaustion"' in text
            for text in dispatch_messages)
        return _output("final after bounded worker stopped", [ToolCall(
            id="exhaustion-submit", function="submit",
            arguments={"answer": "bounded worker stopped without report"},
        )])

    nested_limit_observed = {
        "token": {"worker_callbacks": 0},
        "tool_call": {"worker_callbacks": 0},
    }

    def nested_limit_callback(limit_type: str):
        def callback(messages, tools, _tool_choice, _config):
            system = "\n".join(
                getattr(message, "text", "")
                for message in messages
                if getattr(message, "role", "") == "system"
            )
            if "triage specialist" in system:
                nested_limit_observed[limit_type]["worker_callbacks"] += 1
                if limit_type == "token":
                    # The mock usage is 150 tokens, so a 100-token child scope
                    # must stop after this completed provider call.
                    return _output("token bounded worker output")
                used_bash = any(
                    getattr(message, "role", "") == "tool"
                    and getattr(message, "function", "") == "bash"
                    for message in messages)
                if not used_bash:
                    return _output("one allowed tool", [ToolCall(
                        id="tool-limit-bash", function="bash",
                        arguments={"command": "printf one-tool"},
                    )])
                # The report is the second tool attempt and must be rejected by
                # the worker's one-call nested tool limit.
                return _output("report attempt", [ToolCall(
                    id="tool-limit-report",
                    function="submit_triage_report",
                    arguments={
                        "findings": ["one finding"],
                        "evidence": [{"source": "bash", "snippet": "one-tool"}],
                        "commands_or_queries": ["printf one-tool"],
                        "confidence": "medium",
                        "uncertainties": [],
                        "recommended_next_investigation": [],
                        "candidate_answer_implications": [],
                    },
                )])

            dispatch_messages = [
                getattr(message, "text", "")
                for message in messages
                if getattr(message, "role", "") == "tool"
                and getattr(message, "function", "") == "dispatch_triage"
            ]
            if not dispatch_messages:
                allocation = {
                    "token_limit": 100 if limit_type == "token" else 10_000,
                    "tool_call_limit": 4 if limit_type == "token" else 1,
                    "model_call_limit": 4,
                    "wall_time_sec": 10,
                }
                return _output(f"{limit_type} bounded dispatch", [ToolCall(
                    id=f"{limit_type}-bounded-dispatch",
                    function="dispatch_triage",
                    arguments={
                        "mission": f"exercise nested {limit_type} limit",
                        **allocation,
                    },
                )])
            return _output("final after nested limit", [ToolCall(
                id=f"{limit_type}-bounded-submit", function="submit",
                arguments={"answer": f"{limit_type} limit observed"},
            )])
        return callback

    cumulative_race_observed = {
        "worker_roles_started": [],
        "allocation_errors": 0,
    }

    def cumulative_allocation_race_callback(
        messages, tools, _tool_choice, _config,
    ):
        system = "\n".join(
            getattr(message, "text", "")
            for message in messages
            if getattr(message, "role", "") == "system"
        )
        worker_role = next(
            (role for role in ("triage", "threat_hunter")
             if f"{role} specialist" in system), None)
        if worker_role:
            cumulative_race_observed["worker_roles_started"].append(worker_role)
            return _output("bounded direct report", [ToolCall(
                id=f"race-{worker_role}-report",
                function=f"submit_{worker_role}_report",
                arguments={
                    "findings": [f"{worker_role} retained the reservation"],
                    "evidence": [],
                    "commands_or_queries": [],
                    "confidence": "medium",
                    "uncertainties": [],
                    "recommended_next_investigation": [],
                    "candidate_answer_implications": [],
                },
            )])

        dispatch_messages = [
            message for message in messages
            if getattr(message, "role", "") == "tool"
            and str(getattr(message, "function", "")).startswith("dispatch_")
        ]
        if not dispatch_messages:
            allocation = {
                # Either allocation fits the current ~1M-token root balance,
                # but both cannot coexist after the commander reserve.
                "token_limit": 600_000,
                "tool_call_limit": 4,
                "model_call_limit": 4,
                "wall_time_sec": 10,
            }
            return _output("parallel cumulative reservation race", [
                ToolCall(
                    id="race-triage", function="dispatch_triage",
                    arguments={
                        "mission": "independent race participant A",
                        **allocation,
                    }),
                ToolCall(
                    id="race-hunter", function="dispatch_threat_hunter",
                    arguments={
                        "mission": "independent race participant B",
                        **allocation,
                    }),
            ])
        cumulative_race_observed["allocation_errors"] = sum(
            bool(getattr(message, "error", None))
            or "exceeds safely allocatable" in getattr(message, "text", "")
            for message in dispatch_messages)
        return _output("final after cumulative race", [ToolCall(
            id="race-submit", function="submit",
            arguments={"answer": "one cumulative reservation retained"},
        )])

    def run_arm(arm: str, callback, directory: Path):
        model = get_model(
            "mockllm/model", custom_outputs=callback, memoize=False)
        raw_solver = create_agent(
            arm=arm, max_dispatches=6, max_parallel_dispatches=2,
            max_model_calls=64, global_tool_call_limit=25,
            global_token_limit=1_000_000, global_time_limit=60)(
                instruction_prompt="OFFICIAL INSTRUCTION SENTINEL",
                assistant_prompt="OFFICIAL ASSISTANT SENTINEL",
                tools=[bash(timeout=30), python(timeout=30)], max_steps=25)

        @solver_decorator(name=f"excytin_native_probe_{arm}")
        def bridge_solver():
            return raw_solver

        task = Task(
            dataset=[Sample(input="OFFICIAL TASK CONTEXT SENTINEL")],
            solver=bridge_solver(),
            sandbox="local",
            tool_call_limit=25,
            token_limit=1_000_000,
            time_limit=60,
        )
        return eval(
            task, model=model, log_dir=str(directory), log_format="json",
            display="none")[0]

    full_log = run_arm("full", full_callback, log_root / "full")
    single_log = run_arm("single", single_callback, log_root / "single")
    orchestrator_only_log = run_arm(
        "orchestrator_only", orchestrator_only_callback,
        log_root / "orchestrator_only")
    local_exhaustion_log = run_arm(
        "full", local_exhaustion_callback, log_root / "local_exhaustion")
    allocation_rejection_log = run_arm(
        "full", allocation_rejection_callback,
        log_root / "allocation_rejection")
    token_limit_log = run_arm(
        "full", nested_limit_callback("token"),
        log_root / "nested_token_limit")
    tool_limit_log = run_arm(
        "full", nested_limit_callback("tool_call"),
        log_root / "nested_tool_limit")
    cumulative_race_log = run_arm(
        "full", cumulative_allocation_race_callback,
        log_root / "cumulative_allocation_race")
    full_sample = full_log.samples[0]
    single_sample = single_log.samples[0]
    orchestrator_only_sample = orchestrator_only_log.samples[0]
    local_exhaustion_sample = local_exhaustion_log.samples[0]
    allocation_rejection_sample = allocation_rejection_log.samples[0]
    token_limit_sample = token_limit_log.samples[0]
    tool_limit_sample = tool_limit_log.samples[0]
    cumulative_race_sample = cumulative_race_log.samples[0]
    full_trace = (full_sample.store or {})["cyberorion_runtime_trace"]
    single_trace = (single_sample.store or {})["cyberorion_runtime_trace"]
    exhaustion_trace = (local_exhaustion_sample.store or {})[
        "cyberorion_runtime_trace"]
    rejection_trace = (allocation_rejection_sample.store or {})[
        "cyberorion_runtime_trace"]
    nested_limit_samples = {
        "token": (token_limit_log, token_limit_sample),
        "tool_call": (tool_limit_log, tool_limit_sample),
    }
    cumulative_race_trace = (cumulative_race_sample.store or {})[
        "cyberorion_runtime_trace"]

    triage = observed["specialist_intervals"]["triage"]
    hunter = observed["specialist_intervals"]["threat_hunter"]
    parallel_overlap = min(triage[1], hunter[1]) > max(triage[0], hunter[0])
    commander_reports = [
        message.text
        for message in full_sample.messages
        if getattr(message, "role", "") == "tool"
        and str(getattr(message, "function", "")).startswith("dispatch_")
    ]
    full_state = full_trace["shared_investigation_state"]
    root_limits = full_trace["inspect_usage"].get("sample_root_limits", {})
    tool_limit_usage = root_limits.get("tool_call", {}).get("usage")
    token_limit_usage = root_limits.get("token", {}).get("usage")
    evidence_lengths = [len(row["snippet"]) for row in full_state["evidence"]]
    lateral_input = observed["specialist_inputs"].get("lateral_analyst", "")
    specialist_real_tools = all(
        all(name in observed["specialist_tool_sets"].get(role, [])
            for name in ("bash", "python"))
        for role in ("triage", "threat_hunter", "lateral_analyst")
    )
    report_tools_are_only_coordination = all(
        f"submit_{role}_report" in observed["specialist_tool_sets"].get(role, [])
        for role in ("triage", "threat_hunter", "lateral_analyst")
    )

    return {
        "probe_schema": "excytin_native_mechanism_probe_v2_resource_delegation",
        "full_status": full_log.status,
        "single_status": single_log.status,
        "orchestrator_only_status": orchestrator_only_log.status,
        "full_error": None if full_sample.error is None else str(full_sample.error),
        "single_error": None if single_sample.error is None else str(single_sample.error),
        "orchestrator_only_error": (
            None if orchestrator_only_sample.error is None
            else str(orchestrator_only_sample.error)),
        "single_official_tool_visible": all(
            all(tool in names for tool in ("bash", "python"))
            for names in observed["single_tool_sets"]),
        "full_official_tool_union": full_trace["official_tool_contract"][
            "official_tool_union"],
        "single_official_tool_union": single_trace["official_tool_contract"][
            "official_tool_union"],
        "full_commander_has_no_environment_tools": all(
            "bash" not in names and "python" not in names
            for names in observed["commander_tool_sets"]
        ),
        "full_commander_coordination_tools": sorted(set.intersection(*(
            set(names) for names in observed["commander_tool_sets"]
        )) if observed["commander_tool_sets"] else set()),
        "specialist_real_tools": specialist_real_tools,
        "report_tools_are_only_coordination": report_tools_are_only_coordination,
        "specialist_context": {
            role: {
                "official_task": "OFFICIAL TASK CONTEXT SENTINEL" in text,
                "mission": "SPECIALIST MISSION" in text,
                "shared_state": "CURRENT SHARED INVESTIGATION STATE" in text,
                "official_instruction": "OFFICIAL INSTRUCTION SENTINEL" in text,
            }
            for role, text in observed["specialist_inputs"].items()
        },
        "shared_evidence_propagated_to_lateral_analyst": (
            "triage verified evidence" in lateral_input
            and "threat_hunter verified evidence" in lateral_input
        ),
        "parallel_dispatch_overlap": parallel_overlap,
        "resource_balance_scopes": {
            actor: sorted({row["scope"] for row in rows})
            for actor, rows in observed["resource_balances"].items()
        },
        "root_balance_resource_keys": {
            actor: sorted(rows[0]["resources"])
            for actor, rows in observed["resource_balances"].items()
            if actor in ("single", "orchestrator_only", "full_commander")
        },
        "worker_balance_resource_keys": {
            role: sorted(observed["resource_balances"][role][0]["resources"])
            for role in ("triage", "threat_hunter", "lateral_analyst")
        },
        "worker_local_balance_limits": {
            role: observed["resource_balances"][role][0]["resources"]
            for role in ("triage", "threat_hunter", "lateral_analyst")
        },
        "root_balance_updates_each_call": all(
            len(observed["resource_balances"].get(actor, [])) >= 2
            for actor in ("single", "orchestrator_only", "full_commander")
        ),
        "resource_balance_message_counts": {
            actor: len(rows)
            for actor, rows in observed["resource_balances"].items()
        },
        "local_exhaustion": {
            "run_status": local_exhaustion_log.status,
            "sample_error": (
                None if local_exhaustion_sample.error is None
                else str(local_exhaustion_sample.error)),
            "worker_model_callbacks": exhaustion_observed[
                "worker_model_callbacks"],
            "worker_report_calls": exhaustion_observed["worker_report_calls"],
            "commander_received_exhaustion": exhaustion_observed[
                "commander_received_exhaustion"],
            "report_counts": exhaustion_trace[
                "shared_investigation_state"]["report_counts"],
            "global_model_calls": exhaustion_trace[
                "global_model_call_budget"],
            "final_output": local_exhaustion_sample.output.completion,
        },
        "allocation_rejection": {
            "run_status": allocation_rejection_log.status,
            "sample_error": (
                None if allocation_rejection_sample.error is None
                else str(allocation_rejection_sample.error)),
            "worker_callbacks": allocation_rejection_observed[
                "worker_callbacks"],
            "dispatches_started": rejection_trace[
                "shared_investigation_state"]["dispatches"],
            "final_output": allocation_rejection_sample.output.completion,
        },
        "nested_limit_matrix": {
            limit_type: {
                "run_status": log.status,
                "sample_error": (
                    None if sample.error is None else str(sample.error)),
                "worker_callbacks": nested_limit_observed[
                    limit_type]["worker_callbacks"],
                "report_counts": (sample.store or {})[
                    "cyberorion_runtime_trace"][
                        "shared_investigation_state"]["report_counts"],
                "limit_error": (sample.store or {})[
                    "cyberorion_runtime_trace"][
                        "shared_investigation_state"][
                            "specialist_reports"][0]["error"],
            }
            for limit_type, (log, sample) in nested_limit_samples.items()
        },
        "cumulative_parallel_allocation_race": {
            "run_status": cumulative_race_log.status,
            "sample_error": (
                None if cumulative_race_sample.error is None
                else str(cumulative_race_sample.error)),
            "worker_roles_started": cumulative_race_observed[
                "worker_roles_started"],
            "allocation_errors": cumulative_race_observed[
                "allocation_errors"],
            "dispatches_started": cumulative_race_trace[
                "shared_investigation_state"]["dispatches"],
            "successful_reports": cumulative_race_trace[
                "shared_investigation_state"]["report_counts"][
                    "successful_report"],
            "final_output": cumulative_race_sample.output.completion,
        },
        "dispatch_tool_required_parameters": {
            name: sorted((
                schema.model_dump() if hasattr(schema, "model_dump")
                else schema).get("required", []))
            for name, schema in observed["dispatch_tool_parameters"].items()
        },
        "commander_report_count": len(commander_reports),
        "commander_reports_intact": all(
            '"status": "successful_report"' in report
            and '"report": {' in report
            for report in commander_reports
        ),
        "full_dispatches": full_state["dispatches"],
        "full_successful_reports": full_state["report_counts"][
            "successful_report"],
        "full_report_failures": {
            key: value for key, value in full_state["report_counts"].items()
            if key != "successful_report"
        },
        "full_official_tool_calls": full_state["official_tool_calls"],
        "full_model_calls": full_state["model_calls"],
        "global_model_call_budget": full_trace["global_model_call_budget"],
        "inspect_root_tool_usage": tool_limit_usage,
        "inspect_root_token_usage": token_limit_usage,
        "global_child_usage_counted": (
            isinstance(tool_limit_usage, (int, float))
            and tool_limit_usage >= (
                full_state["official_tool_calls"]
                + full_state["dispatches"]
                + full_state["report_counts"]["successful_report"]
                + 1
            )
        ),
        "global_child_tokens_counted": (
            isinstance(token_limit_usage, (int, float))
            and token_limit_usage > 0
        ),
        "max_notebook_evidence_chars": max(evidence_lengths, default=0),
        "model_visible_snapshot_chars": full_state["bounds"][
            "model_visible_snapshot_chars"],
        "representative_output_compacted": (
            max(evidence_lengths, default=0) <= 1300
            and full_trace["bounded_workspace"]["max_raw_tool_output_chars"]
            > 12_000
        ),
        "bridge_truncation": full_trace[
            "output_or_context_truncation_by_bridge"],
        "custom_json_action_protocol": full_trace[
            "custom_json_action_protocol"],
        "native_inspect_tool_execution": full_trace[
            "native_inspect_tool_execution"],
        "context_audit": full_trace["context_audit"],
        "full_output": full_sample.output.completion,
        "single_output": single_sample.output.completion,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log-root", required=True)
    parser.add_argument("--result-json")
    args = parser.parse_args()
    result = run_probe(Path(args.log_root).resolve())
    if args.result_json:
        result_path = Path(args.result_json).resolve()
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
    print("EXCYTIN_PROBE_JSON=" + json.dumps(
        result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

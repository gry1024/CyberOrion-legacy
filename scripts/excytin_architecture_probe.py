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
    }
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
                    "findings": f"{specialist} verified evidence",
                    "evidence": [{
                        "claim": "verified", "source": "bash",
                        "snippet": specialist,
                    }],
                    "commands_or_queries": (
                        "sleep 0.08; python3 -c print representative evidence"),
                    "confidence": "high",
                    "uncertainties": "none",
                    "recommended_next_investigation": "continue correlation",
                    "candidate_answer_implications": specialist,
                },
            )])

        observed["commander_tool_sets"].append(names)
        delegated = {
            str(getattr(message, "function", ""))
            for message in messages
            if getattr(message, "role", "") == "tool"
        }
        if "dispatch_triage" not in delegated:
            return _output("parallel independent survey", [
                ToolCall(id="dispatch-triage", function="dispatch_triage",
                         arguments={"mission": "survey schema and evidence"}),
                ToolCall(id="dispatch-hunter", function="dispatch_threat_hunter",
                         arguments={"mission": "independently correlate evidence"}),
            ])
        if "dispatch_lateral_analyst" not in delegated:
            return _output("follow shared evidence", [ToolCall(
                id="dispatch-lateral", function="dispatch_lateral_analyst",
                arguments={"mission": "hunt residual evidence from shared state"},
            )])
        return _output("final", [ToolCall(
            id="full-submit", function="submit",
            arguments={"answer": "verified answer"},
        )])

    def single_callback(messages, tools, _tool_choice, _config):
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

    def run_arm(arm: str, callback, directory: Path):
        model = get_model(
            "mockllm/model", custom_outputs=callback, memoize=False)
        raw_solver = create_agent(
            arm=arm, max_dispatches=6, max_parallel_dispatches=2)(
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
    full_sample = full_log.samples[0]
    single_sample = single_log.samples[0]
    full_trace = (full_sample.store or {})["cyberorion_runtime_trace"]
    single_trace = (single_sample.store or {})["cyberorion_runtime_trace"]

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
        "probe_schema": "excytin_native_mechanism_probe_v1",
        "full_status": full_log.status,
        "single_status": single_log.status,
        "full_error": None if full_sample.error is None else str(full_sample.error),
        "single_error": None if single_sample.error is None else str(single_sample.error),
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

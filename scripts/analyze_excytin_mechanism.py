#!/usr/bin/env python
"""Reduce ExCyTIn mechanism/resource traces without reading score or answers."""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

from inspect_ai.event import CompactionEvent, ModelEvent, SampleLimitEvent, ToolEvent
from inspect_ai.log import read_eval_log


def _tool_seen_in_model_input(event: ModelEvent, function: str) -> bool:
    for message in event.input or ():
        if (str(getattr(message, "role", "")) == "tool"
                and str(getattr(message, "function", "")) == function):
            return True
    return False


def analyze(path: Path) -> dict[str, Any]:
    log = read_eval_log(str(path))
    samples = []
    for sample in log.samples or ():
        events = sample.events or ()
        attachments = sample.attachments or {}
        trace = (sample.store or {}).get("cyberorion_runtime_trace") or {}
        state = trace.get("shared_investigation_state") or {}
        tools = [event for event in events if isinstance(event, ToolEvent)]
        models = [event for event in events if isinstance(event, ModelEvent)]
        compactions = [
            event for event in events if isinstance(event, CompactionEvent)
        ]
        dispatch_rows = [
            (index, event) for index, event in enumerate(events)
            if isinstance(event, ToolEvent)
            and event.function.startswith("dispatch_")
        ]
        consumed = 0
        for index, dispatch in dispatch_rows:
            if any(
                isinstance(later, ModelEvent)
                and _tool_seen_in_model_input(later, dispatch.function)
                for later in events[index + 1:]
            ):
                consumed += 1
        parsing_errors = [
            event for event in tools
            if event.error is not None
            and "type='parsing'" in str(event.error)
        ]
        contract_errors = [
            event for event in tools
            if event.error is not None
            and getattr(event.error, "type", None) != "parsing"
            and any(
                marker in str(event.error).lower()
                for marker in (
                    "unknown tool", "tool not found", "not available",
                    "not allowed", "invalid role",
                )
            )
        ]
        limit_rows = []
        for index, event in enumerate(events):
            if isinstance(event, SampleLimitEvent):
                span_id = getattr(event, "span_id", None)
                limit_rows.append({
                    "type": event.type,
                    "limit": event.limit,
                    "span_id": span_id,
                    "event_index": index,
                    "post_limit_model_events_same_span": sum(
                        isinstance(later, ModelEvent)
                        and getattr(later, "span_id", None) == span_id
                        for later in events[index + 1:]
                    ),
                    "post_limit_model_events_any_span": sum(
                        isinstance(later, ModelEvent)
                        for later in events[index + 1:]
                    ),
                })

        def resolve_attachments(value: Any) -> Any:
            if isinstance(value, str) and value.startswith("attachment://"):
                return attachments.get(value.removeprefix("attachment://"), value)
            if isinstance(value, dict):
                return {
                    key: resolve_attachments(item)
                    for key, item in value.items()
                }
            if isinstance(value, list):
                return [resolve_attachments(item) for item in value]
            return value

        def bounded_arguments(event: ToolEvent) -> str:
            serialized = json.dumps(
                resolve_attachments(event.arguments or {}),
                ensure_ascii=False, sort_keys=True,
                default=str,
            )
            return serialized if len(serialized) <= 1200 else (
                serialized[:1200] + "...[diagnostic argument clipping]")
        reports = state.get("specialist_reports") or []
        valid_reports = [
            row for row in reports
            if row.get("status") == "successful_report"
        ]
        evidence_reports = [
            row for row in valid_reports
            if (row.get("report") or {}).get("findings")
            and (row.get("report") or {}).get("evidence")
        ]
        root = (trace.get("inspect_usage") or {}).get(
            "sample_root_limits") or {}
        model_budget = trace.get("global_model_call_budget") or {}
        sample_row = {
            "sample_id": str(sample.id),
            "attachment_inventory": {
                "count": len(attachments),
                "max_value_chars": max(
                    (len(str(value)) for value in attachments.values()),
                    default=0,
                ),
            },
            "dispatch_count": len(dispatch_rows),
            "dispatch_roles": [
                event.function.removeprefix("dispatch_")
                for _, event in dispatch_rows
            ],
            "dispatch_reports_consumed_by_later_model_call": consumed,
            "official_tool_calls": int(state.get("official_tool_calls", 0)),
            "worker_official_tool_calls_by_role": dict(Counter(
                row.get("role") for row in state.get("executed_commands", [])
                if row.get("role") != "orchestrator"
            )),
            "valid_reports": len(valid_reports),
            "evidence_bearing_reports": len(evidence_reports),
            "report_counts": state.get("report_counts") or {},
            "model_invalid_tool_argument_errors": len(parsing_errors),
            "model_invalid_tool_argument_functions": [
                event.function for event in parsing_errors
            ],
            "contract_errors": len(contract_errors),
            "all_tool_errors": sum(event.error is not None for event in tools),
            "inspect_16k_truncation_events": sum(
                bool(event.truncated) for event in tools
            ),
            "native_context_compaction_events": len(compactions),
            "native_context_compaction_tokens_before": [
                getattr(event, "tokens_before", None) for event in compactions
            ],
            "native_context_compaction_tokens_after": [
                getattr(event, "tokens_after", None) for event in compactions
            ],
            "truncated_tools": [
                {"function": event.function, "truncated": event.truncated}
                for event in tools if event.truncated
            ],
            "llm_calls": len(models),
            "global_model_budget": model_budget,
            "tool_events": len(tools),
            "provider_tokens": (root.get("token") or {}).get("usage"),
            "token_limit": (root.get("token") or {}).get("limit"),
            "root_tool_usage": (root.get("tool_call") or {}).get("usage"),
            "root_tool_limit": (root.get("tool_call") or {}).get("limit"),
            "wall_clock_sec": trace.get("wall_clock_sec"),
            "time_limit_sec": (root.get("time") or {}).get("limit"),
            "sample_limit_events": limit_rows,
            "official_tool_call_trace": [
                {
                    "function": event.function,
                    "arguments": bounded_arguments(event),
                    "truncated": event.truncated,
                    "error": None if event.error is None else str(event.error),
                }
                for event in tools if event.function in {"bash", "python"}
            ],
            "model_visible_snapshot_chars": (
                (state.get("bounds") or {}).get(
                    "model_visible_snapshot_chars")),
            "shared_workspace_compactions": (
                (trace.get("bounded_workspace") or {}).get(
                    "compacted_evidence_count")),
        }
        samples.append(sample_row)
    totals = {
        "samples": len(samples),
        "samples_with_delegation": sum(row["dispatch_count"] > 0 for row in samples),
        "dispatches": sum(row["dispatch_count"] for row in samples),
        "roles_used": sorted({
            role for row in samples for role in row["dispatch_roles"]
        }),
        "official_tool_calls": sum(row["official_tool_calls"] for row in samples),
        "valid_reports": sum(row["valid_reports"] for row in samples),
        "evidence_bearing_reports": sum(
            row["evidence_bearing_reports"] for row in samples),
        "dispatch_reports_consumed": sum(
            row["dispatch_reports_consumed_by_later_model_call"]
            for row in samples),
        "model_invalid_tool_argument_errors": sum(
            row["model_invalid_tool_argument_errors"] for row in samples),
        "contract_errors": sum(row["contract_errors"] for row in samples),
        "inspect_16k_truncation_events": sum(
            row["inspect_16k_truncation_events"] for row in samples),
        "native_context_compaction_events": sum(
            row["native_context_compaction_events"] for row in samples),
        "llm_calls": sum(row["llm_calls"] for row in samples),
        "provider_tokens": sum(int(row["provider_tokens"] or 0) for row in samples),
        "tool_events": sum(row["tool_events"] for row in samples),
        "wall_clock_sec": sum(float(row["wall_clock_sec"] or 0) for row in samples),
        "sample_limit_events": sum(len(row["sample_limit_events"]) for row in samples),
    }
    return {
        "schema": "excytin_mechanism_summary_v1",
        "score_or_target_fields_read": False,
        "log_status": log.status,
        "samples": samples,
        "totals": totals,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("eval_log")
    args = parser.parse_args()
    print(json.dumps(analyze(Path(args.eval_log)), ensure_ascii=False,
                     indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""CyberOrion 内部 SOC runtime-loop 契约集（不属于公共 benchmark）。"""

from __future__ import annotations

import asyncio
import json
import random
import time
from pathlib import Path
from typing import Any

from .cybersoceval import DEFAULT_LOG_DIR, LLM_TIMEOUT, _model_name, make_llm
from .external_common import FAIR_ARM_BUDGET, MeteredLLM, persist_run, resource_usage
from .soc_evidence import (
    aggregate_scores, load_cases, parse_prediction, score_prediction, write_report,
)
from .superagent_runtime import RuntimeConfig, ToolSpec, run_reference, run_superagent

SUITE = "soc_contract"
SUITE_DESC = "12-case internal SOC contract set with real auditable tool loops"
MODES = ("base", "single", "agent")
ARM_OF_MODE = {"base": "bare", "single": "single", "agent": "framework"}
METHODOLOGY_STATUS = "engineering_only"


def sample_cases(n: int, seed: int) -> list[dict]:
    cases = load_cases()
    return random.Random(seed).sample(cases, min(max(1, n), len(cases)))


def _tools(case: dict) -> dict[str, ToolSpec]:
    telemetry = list(case["telemetry"])
    context = list(case.get("knowledge_context") or [])
    evidence_ids = {str(row.get("id")) for row in telemetry}

    def query_telemetry(source: str = "") -> list[dict]:
        return [row for row in telemetry if not source or row.get("source") == source]

    def lookup_context(query: str = "") -> list[dict]:
        token = query.lower().strip()
        return [row for row in context if not token or token in json.dumps(
            row, ensure_ascii=False).lower()]

    def verify_evidence(ids: list[str] | None = None) -> dict:
        requested = [str(value) for value in (ids or [])]
        return {"valid": [value for value in requested if value in evidence_ids],
                "unknown": [value for value in requested if value not in evidence_ids]}

    def validate_response(actions: list[str] | None = None) -> dict:
        values = [str(value) for value in (actions or [])]
        unsafe = [value for value in values if any(
            word in value.lower() for word in ("wipe", "disable_edr", "block_public_dns"))]
        return {"actions": values, "unsafe": unsafe, "executed": False}

    array = {"type": "array", "items": {"type": "string"}}
    return {
        "query_telemetry": ToolSpec(
            "query_telemetry", query_telemetry, "Filter case telemetry by source.",
            {"type": "object", "properties": {"source": {"type": "string"}}}),
        "lookup_context": ToolSpec(
            "lookup_context", lookup_context, "Search the case-local ATT&CK/playbook context.",
            {"type": "object", "properties": {"query": {"type": "string"}}}),
        "verify_evidence": ToolSpec(
            "verify_evidence", verify_evidence, "Check cited IDs exist in observable telemetry.",
            {"type": "object", "properties": {"ids": array}}),
        "validate_response": ToolSpec(
            "validate_response", validate_response,
            "Validate proposed actions without executing any action.",
            {"type": "object", "properties": {"actions": array}}),
    }


async def _runtime(case: dict, mode: str, llm: Any) -> tuple[str, dict]:
    async def wrapped(system: str, user: str) -> Any:
        raw = await llm(system, user)
        try:
            value = json.loads(str(raw)) if not isinstance(raw, dict) else raw
        except (TypeError, ValueError, json.JSONDecodeError):
            return raw
        if isinstance(value, dict) and "verdict" in value and "action" not in value:
            return {"action": {"type": "complete", "summary": value}}
        return value

    tools = _tools(case)
    task = (
        f"{case['prompt']} Case={case['case_id']}. Use observable tools, cite evidence IDs, "
        "validate reversible response actions, then complete with the documented SOC JSON schema."
    )
    config = RuntimeConfig(
        max_steps=FAIR_ARM_BUDGET["max_steps"],
        max_llm_calls=FAIR_ARM_BUDGET["max_llm_calls"],
        max_tool_calls=FAIR_ARM_BUDGET["max_tool_calls"],
        max_dispatches=6, max_role_steps=6, max_trace_text_chars=12000,
    )
    runtime = await (run_superagent if mode == "agent" else run_reference)(
        task=task, llm=wrapped, tools=tools, config=config,
        role_tools={
            "watcher": ("query_telemetry", "verify_evidence"),
            "analyst": ("query_telemetry", "lookup_context", "verify_evidence"),
            "hunter": ("query_telemetry", "lookup_context", "verify_evidence"),
            "responder": ("verify_evidence", "validate_response"),
        },
    )
    return str(runtime["output"]), runtime


async def run_bench(n: int = 12, mode: str = "agent", seed: int = 42,
                    profile: str = "daily", dataset_version: str | None = None,
                    log_dir: str | Path = DEFAULT_LOG_DIR, concurrency: int = 4,
                    llm=None, on_progress=None, run_id: str | None = None,
                    source_provenance: dict | None = None, **_: Any) -> dict:
    if mode not in MODES:
        raise ValueError(f"soc_contract mode 必须是 {'/'.join(MODES)}")
    llm = llm or make_llm(timeout=LLM_TIMEOUT)
    cases = sample_cases(n, seed)
    sem = asyncio.Semaphore(max(1, concurrency))
    results: list[dict | None] = [None] * len(cases)
    done = errors = 0

    async def evaluate(index: int, case: dict) -> None:
        nonlocal done, errors
        started = time.perf_counter()
        meter = MeteredLLM(llm)
        runtime: dict = {"decision_trace": [], "tool_calls": [], "role_events": [],
                         "budget": {}}
        error = None
        try:
            async with sem, asyncio.timeout(FAIR_ARM_BUDGET["wall_clock_sec"]):
                if mode == "base":
                    raw = await meter(
                        "You are a SOC analyst. Return one grounded JSON object only.",
                        json.dumps({"task": case["prompt"], "telemetry": case["telemetry"]},
                                   ensure_ascii=False))
                else:
                    raw, runtime = await _runtime(case, mode, meter)
        except Exception as exc:  # noqa: BLE001
            raw, error = "", f"{type(exc).__name__}: {exc}"[:400]
            errors += 1
        parsed = parse_prediction(raw)
        prediction = parsed["prediction"]
        actual_calls = [{
            "agent": call.get("role"), "tool": call.get("tool"),
            "status": call.get("status"),
            "useful": call.get("status") == "ok" and not str(call.get("output", "")).startswith("ERROR"),
        } for call in runtime.get("tool_calls", [])]
        prediction["tool_trace"] = actual_calls
        scored = score_prediction(case, prediction, parse_ok=parsed["parse_ok"],
                                  tool_expected=mode != "base")
        results[index] = {
            "idx": index, "case_id": case["case_id"], "title": case["title"],
            "task_type": case["task_type"], "difficulty": case["difficulty"],
            "prompt": case["prompt"], "telemetry": case["telemetry"],
            "gold": case["gold"], "evidence_map": case["evidence_map"],
            "prediction": prediction, "raw": str(raw)[:12000],
            "parse_ok": parsed["parse_ok"], "parse_error": parsed["error"],
            "llm_error": bool(error), "error": error,
            "agent_trace": runtime.get("decision_trace", []),
            "tool_calls": runtime.get("tool_calls", []),
            "role_events": runtime.get("role_events", []),
            "trace_source": "runtime", "metrics": scored["metrics"],
            "failure_tags": scored["failure_tags"],
            "latency_ms": round((time.perf_counter() - started) * 1000, 1),
            "resource_usage": resource_usage(
                started=started,
                llm_calls=runtime.get("budget", {}).get("llm_calls", 1),
                tool_calls=runtime.get("budget", {}).get("tool_calls", 0),
                estimated_tokens=meter.estimated_tokens),
        }
        done += 1
        if on_progress:
            on_progress(done, len(cases), errors)

    started_at = time.time()
    await asyncio.gather(*(evaluate(i, case) for i, case in enumerate(cases)))
    finished_at = time.time()
    rows = [row for row in results if row is not None]
    rid = run_id or time.strftime(f"%Y%m%d_%H%M%S_{SUITE}_{mode}_n{len(rows)}")
    run = {
        "schema_version": 4, "run_id": rid, "suite": SUITE, "mode": mode,
        "arm": ARM_OF_MODE[mode], "profile": profile, "n": len(rows), "seed": seed,
        "model": _model_name(), "started_at": started_at, "finished_at": finished_at,
        "elapsed_sec": round(finished_at - started_at, 2),
        "scores": aggregate_scores(rows, seed), "results": rows,
        "llm_errors": errors, "status": "error" if rows and errors == len(rows) else "done",
        "error": next((row["error"] for row in rows if row.get("error")), None),
        "methodology_status": METHODOLOGY_STATUS, "trace_source": "runtime",
        "benchmark_provenance": {
            "name": "CyberOrion SOC contract set v2", "origin": "internal",
            "dataset_version": dataset_version or "v2-12-independent-cases",
            "sample_scope": "full" if len(rows) == 12 else "subset",
            "sample_manifest": [row["case_id"] for row in rows],
            "comparable_to_upstream": False,
        },
        "methodology": {
            "task_family": "open_response_soc_runtime_loop",
            "case_count": 12, "public_recognition": False,
            "leaderboard_comparable": False, "arm_budget": dict(FAIR_ARM_BUDGET),
        },
    }
    run = persist_run(run, log_dir, source_provenance=source_provenance)
    run["report"] = write_report(run, Path(log_dir) / f"{rid}.md")
    return run

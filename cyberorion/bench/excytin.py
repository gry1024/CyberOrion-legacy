"""ExCyTIn/ACESEvals 多源事件调查适配器。

适配器读取上游题目 JSON/JSONL 和 SQLite 遥测库，向单代理及 SUPER-AGENT
暴露只读 SQL 工具。若 ACESEvals 官方 scorer 随资产提供，优先使用其预计算
``score``；否则明确标记为 adapter exact-match，不冒充论文可比成绩。
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sqlite3
import statistics
import time
from pathlib import Path
from typing import Any

from .assets import ASSETS, BenchmarkAssetMissing, require_asset, sha256_file
from .external_common import (
    DEFAULT_LOG_DIR, LLM_TIMEOUT, _model_name, make_llm, new_run_id,
    FAIR_ARM_BUDGET, MeteredLLM, apply_size_policy, bootstrap_ci, persist_run, provenance,
    read_records, resolve_representative_files, resource_usage, stratified_sample,
)
from .superagent_runtime import RuntimeConfig, ToolSpec, run_reference, run_superagent

SUITE = "excytin"
MODES = ("base", "single", "agent")
ARM_OF_MODE = {"base": "bare", "single": "single", "agent": "framework"}
METHODOLOGY_STATUS = "external_track"
_READ_ONLY = re.compile(r"^\s*(select|with|pragma\s+table_info)\b", re.I)
_SQLITE_HEADER = b"SQLite format 3\x00"
_SQLITE_ENV = "CYBERORION_EXCYTIN_SQLITE_PATH"


def official_harness_status(root: Path, *, selected: bool = False) -> dict:
    """只读探测官方 Inspect/Docker/scorer 组件，不尝试安装或替代。"""
    task_module = root / "domains" / "excytin" / "excytin.py"
    scorer_files = [p for p in root.rglob("*.py")
                    if "scor" in p.name.lower() or "scoring" in p.parts]
    inspect_available = importlib.util.find_spec("inspect_ai") is not None
    docker_cli = shutil.which("docker") is not None
    try:
        docker_daemon = bool(docker_cli and subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True, text=True, timeout=10, check=True).stdout.strip())
    except (OSError, subprocess.SubprocessError):
        docker_daemon = False
    components_present = task_module.is_file() and bool(scorer_files)
    # CyberOrion 的三臂 runner 尚未通过 Inspect Task 执行，所以即使依赖均
    # 安装也不能把 adapter exact-match 标成 official score。
    return {
        "inspect_ai_installed": inspect_available,
        "docker_cli_available": docker_cli,
        "docker_daemon_verified": docker_daemon,
        "official_task_module_present": task_module.is_file(),
        "official_scorer_files_present": bool(scorer_files),
        "components_present": components_present,
        "official_execution_selected": bool(selected and components_present and docker_daemon),
        "official_telemetry_backend": "MySQL containers via Inspect/SABER Docker harness",
        "sqlite_projection_is_official": False,
        "status": "official_selected" if selected and components_present and docker_daemon
        else "adapter_selected_non_official",
    }


def validate_sqlite_asset(path: Path) -> dict:
    """在任何 LLM 调用前验证显式 SQLite telemetry 投影。"""
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise BenchmarkAssetMissing(SUITE, f"SQLite telemetry 不存在: {resolved}")
    try:
        header = resolved.read_bytes()[:16]
    except OSError as exc:
        raise BenchmarkAssetMissing(
            SUITE, f"无法读取 SQLite telemetry {resolved}: {exc}") from exc
    if header != _SQLITE_HEADER:
        raise BenchmarkAssetMissing(
            SUITE,
            f"拒绝非 SQLite telemetry: {resolved} (header={header.hex() or 'empty'})")
    try:
        with sqlite3.connect(f"file:{resolved}?mode=ro", uri=True) as conn:
            sqlite_version = str(conn.execute("SELECT sqlite_version()").fetchone()[0])
            tables = [str(row[0]) for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    except sqlite3.Error as exc:
        raise BenchmarkAssetMissing(
            SUITE, f"SQLite telemetry 查询验证失败 {resolved}: {exc}") from exc
    if not tables:
        raise BenchmarkAssetMissing(SUITE, f"SQLite telemetry 没有表: {resolved}")
    digest = sha256_file(resolved)
    return {
        "path": str(resolved), "format": "sqlite3", "header_verified": True,
        "sqlite_version": sqlite_version, "table_count": len(tables),
        "sha256": digest, "selection": "explicit_env" if os.getenv(_SQLITE_ENV)
        else "single_validated_candidate",
        "methodology": "CyberOrion SQLite projection (non-official)",
    }


def select_telemetry_database(files: list[Path]) -> tuple[Path, dict]:
    """只接受显式路径或唯一且通过格式验证的 SQLite，绝不取列表首项。"""
    configured = os.getenv(_SQLITE_ENV)
    if configured:
        path = Path(configured)
        return path.expanduser().resolve(), validate_sqlite_asset(path)
    candidates = sorted({p.resolve() for p in files if p.is_file()
                         and p.suffix.lower() in {".db", ".sqlite", ".sqlite3"}})
    valid: list[tuple[Path, dict]] = []
    rejected: list[str] = []
    for candidate in candidates:
        try:
            valid.append((candidate, validate_sqlite_asset(candidate)))
        except BenchmarkAssetMissing:
            rejected.append(str(candidate))
    if not valid:
        detail = ("; rejected non-SQLite candidates: " + ", ".join(rejected)) if rejected else ""
        raise BenchmarkAssetMissing(
            SUITE,
            "官方 ACESEvals telemetry 是 Inspect/SABER 管理的 MySQL Docker 环境；"
            f"当前 adapter 需要通过 {_SQLITE_ENV} 指定经过验证的非官方 SQLite 投影"
            + detail)
    if len(valid) != 1:
        raise BenchmarkAssetMissing(
            SUITE, f"发现 {len(valid)} 个有效 SQLite；必须用 {_SQLITE_ENV} 明确选择")
    return valid[0]


def _normalise(row: dict, index: int) -> dict | None:
    question = (row.get("question") or row.get("prompt") or row.get("input")
                or row.get("task") or row.get("instructions")
                or (row.get("initial_context") or {}).get("question")
                or row.get("description"))
    scoring = row.get("scoring") if isinstance(row.get("scoring"), dict) else {}
    judge = scoring.get("llm_judge") if isinstance(scoring.get("llm_judge"), dict) else {}
    submission = judge.get("submission") if isinstance(judge.get("submission"), dict) else {}
    answer = (row.get("answer") if "answer" in row else row.get("target")
              if "target" in row else scoring.get("target")
              or submission.get("description"))
    if not question or answer is None:
        return None
    return {
        "id": str(row.get("id") or row.get("question_id") or f"q-{index}"),
        "question": str(question), "answer": answer,
        "incident": str(row.get("incident") or row.get("scenario") or "unknown"),
        "hop_length": str(row.get("hop_length") or row.get("difficulty") or "unknown"),
        "scoring": scoring,
    }


def load_questions(paths: list[Path]) -> list[dict]:
    rows = read_records(paths)
    yaml_paths = [p for p in paths if p.suffix.lower() in {".yaml", ".yml"}
                  and p.name.lower() != "global.yaml"]
    if yaml_paths:
        try:
            import yaml
            for path in yaml_paths:
                value = yaml.safe_load(path.read_text(encoding="utf-8"))
                if isinstance(value, dict):
                    tasks = value.get("tasks")
                    candidates = tasks if isinstance(tasks, list) else [value]
                    for task in candidates:
                        if not isinstance(task, dict):
                            continue
                        task = dict(task)
                        task.setdefault("id", task.get("task_id") or path.stem)
                        task.setdefault("incident", path.parent.name)
                        rows.append(task)
        except (ImportError, OSError, ValueError):
            pass
    return [item for i, row in enumerate(rows)
            if (item := _normalise(row, i)) is not None]


class ReadOnlySQLTools:
    def __init__(self, database: Path) -> None:
        self.database = database

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(f"file:{self.database}?mode=ro", uri=True)

    def list_tables(self) -> list[str]:
        with self._connect() as conn:
            return [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]

    def describe_table(self, table: str) -> list[dict]:
        if table not in self.list_tables():
            return [{"error": "unknown table"}]
        with self._connect() as conn:
            return [{"name": r[1], "type": r[2]} for r in conn.execute(
                f'PRAGMA table_info("{table}")')]

    def run_query(self, sql: str) -> dict:
        if not _READ_ONLY.match(sql or "") or ";" in (sql or "").rstrip(";"):
            return {"error": "only one read-only SELECT/WITH query is allowed"}
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(sql)
            rows = [dict(row) for row in cursor.fetchmany(201)]
        truncated = len(rows) > 200
        return {"rows": rows[:200], "truncated": truncated}


def _parse_answer(raw: Any) -> Any:
    if isinstance(raw, dict):
        value = raw
    else:
        text = str(raw or "").strip()
        try:
            value = json.loads(text)
        except (ValueError, TypeError, json.JSONDecodeError):
            return text
    return value.get("answer", value.get("summary", value)) if isinstance(value, dict) else value


def _canon(value: Any) -> str:
    if isinstance(value, (list, tuple, set)):
        return json.dumps(sorted(_canon(v) for v in value), ensure_ascii=False)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return " ".join(str(value).strip().lower().split())


def compute_scores(rows: list[dict]) -> dict:
    exact = sum(bool(row["exact"]) for row in rows)
    score = exact / len(rows) if rows else 0.0
    query_calls = [sum(1 for call in row.get("tool_calls", [])
                       if call.get("tool") == "run_query") for row in rows]
    evidence_counts = [len(row.get("evidence_ids", [])) for row in rows]
    return {
        "n": len(rows), "official_reward": None,
        "native_reward": round(score, 4),
        "answer_accuracy": round(exact / len(rows), 4) if rows else 0.0,
        "correct_mc_pct": round(exact / len(rows), 4) if rows else 0.0,
        "avg_score": round(score, 4), "parse_fail": sum(not r["parse_ok"] for r in rows),
        "llm_errors": sum(bool(r.get("llm_error")) for r in rows),
        "avg_tool_calls": round(sum(len(r.get("tool_calls", [])) for r in rows) /
                                len(rows), 2) if rows else 0.0,
        "avg_sql_queries": round(statistics.fmean(query_calls), 3) if rows else 0.0,
        "avg_evidence_items": round(statistics.fmean(evidence_counts), 3) if rows else 0.0,
        "query_cost": sum(query_calls), "evidence_cost": sum(evidence_counts),
        "by_difficulty": {}, "by_topic": {},
    }


async def _tool_arm(question: dict, mode: str, llm: Any,
                    sql_tools: ReadOnlySQLTools) -> tuple[Any, dict]:
    async def wrapped(system: str, user: str) -> Any:
        raw = await llm(system, user)
        try:
            parsed = json.loads(str(raw))
        except (ValueError, TypeError, json.JSONDecodeError):
            return raw
        if isinstance(parsed, dict) and "answer" in parsed and "action" not in parsed:
            return {"action": {"type": "complete", "summary": parsed}}
        return parsed

    tools = sql_tool_specs(sql_tools)
    config = RuntimeConfig(max_steps=18, max_llm_calls=18, max_tool_calls=12,
                           max_dispatches=5, max_role_steps=5)
    result = await (run_superagent if mode == "agent" else run_reference)(
        task=(question["question"] +
              "\nInvestigate the telemetry with read-only SQL. Complete with JSON {answer, evidence_ids}."),
        llm=wrapped, tools=tools, config=config,
        role_tools={role: tools.keys() for role in ("watcher", "analyst", "hunter", "responder")},
    )
    return result["output"], result


def sql_tool_specs(sql_tools: ReadOnlySQLTools) -> dict[str, ToolSpec]:
    """返回显式、可审计的 SQLite 工具 schema。"""
    return {
        "list_tables": ToolSpec(
            "list_tables", sql_tools.list_tables, "List available telemetry tables.",
            {"type": "object", "properties": {}, "additionalProperties": False}),
        "describe_table": ToolSpec(
            "describe_table", sql_tools.describe_table, "Describe one telemetry table.",
            {"type": "object", "properties": {"table": {"type": "string"}},
             "required": ["table"], "additionalProperties": False}),
        "run_query": ToolSpec(
            "run_query", sql_tools.run_query, "Run one read-only SQL SELECT/WITH query.",
            {"type": "object", "properties": {"sql": {"type": "string"}},
             "required": ["sql"], "additionalProperties": False}),
    }


async def run_bench(n: int | None = None, mode: str = "base", seed: int = 42,
                    profile: str = "daily", dataset_version: str | None = None,
                    log_dir: str | Path = DEFAULT_LOG_DIR, concurrency: int = 4,
                    llm=None, on_progress=None, run_id: str | None = None,
                    source_provenance: dict | None = None,
                    execution_mode: str = "sqlite_adapter", **_: Any) -> dict:
    """Run the legacy SQLite adapter; official execution is Inspect CLI based.

    ``execution_mode='official'`` is intentionally fail-closed here: callers
    must invoke the pinned ACESEvals task with ``excytin_official_agent`` and
    persist its native log, rather than silently falling back to SQLite.
    """
    if execution_mode not in {"sqlite_adapter", "official"}:
        raise ValueError("execution_mode must be sqlite_adapter or official")
    if execution_mode == "official":
        raise BenchmarkAssetMissing(
            SUITE,
            "official mode requires the pinned ACESEvals Inspect/SABER runner; "
            "use inspect eval with cyberorion_single/full, never SQLite adapter")
    if mode not in MODES:
        raise ValueError(f"excytin mode 必须是 {'/'.join(MODES)}")
    root, files = require_asset(SUITE)
    harness_status = official_harness_status(root, selected=False)
    data_files, representative_decision = resolve_representative_files(SUITE, files)
    # Validate before question sampling, LLM construction, or any model call.
    database, database_validation = select_telemetry_database(data_files)
    questions = load_questions(data_files)
    if not questions:
        raise BenchmarkAssetMissing(SUITE, "未识别 ACESEvals YAML/JSON 题目与可评分目标")
    count, size_decision = apply_size_policy(
        SUITE, profile, n, len(questions), files)
    selected = stratified_sample(questions, count, seed, ("incident", "hop_length"))
    sql_tools = ReadOnlySQLTools(database)
    llm = llm or make_llm(timeout=LLM_TIMEOUT)
    sem = asyncio.Semaphore(max(1, concurrency))
    output: list[dict | None] = [None] * len(selected)
    done = errors = 0

    async def evaluate(index: int, question: dict) -> None:
        nonlocal done, errors
        trace: dict = {"decision_trace": [], "tool_calls": [], "role_events": []}
        err = None
        started = time.perf_counter()
        meter = MeteredLLM(llm)
        try:
            async with sem, asyncio.timeout(FAIR_ARM_BUDGET["wall_clock_sec"]):
                if mode == "base":
                    raw = await meter("You answer security investigation questions. Return JSON {answer}.",
                                      question["question"])
                else:
                    raw, trace = await _tool_arm(question, mode, meter, sql_tools)
        except Exception as exc:  # noqa: BLE001
            raw, err = "", f"{type(exc).__name__}: {exc}"[:400]
            errors += 1
        pred = _parse_answer(raw)
        parsed_payload = {}
        try:
            parsed_payload = json.loads(str(raw)) if not isinstance(raw, dict) else raw
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
        evidence_ids = (parsed_payload.get("evidence_ids", [])
                        if isinstance(parsed_payload, dict) else [])
        output[index] = {
            "idx": index, "question_id": question["id"], "question": question["question"],
            "topic": question["incident"], "difficulty": question["hop_length"],
            "gold": question["answer"], "pred": pred,
            "exact": _canon(pred) == _canon(question["answer"]),
            "official_score": None, "native_reward": 1.0 if _canon(pred) == _canon(question["answer"]) else 0.0,
            "evidence_ids": evidence_ids if isinstance(evidence_ids, list) else [],
            "scoring_config": question.get("scoring", {}),
            "parse_ok": pred not in ("", None),
            "raw": str(raw)[:8000], "agent_trace": trace.get("decision_trace", []),
            "tool_calls": trace.get("tool_calls", []), "role_events": trace.get("role_events", []),
            "trace_source": "runtime", "llm_error": bool(err), "error": err,
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
    await asyncio.gather(*(evaluate(i, q) for i, q in enumerate(selected)))
    finished_at = time.time()
    rows = [row for row in output if row is not None]
    spec = ASSETS[SUITE]
    comparable = False
    scores = compute_scores(rows)
    scores["confidence_intervals"] = {
        "native_reward": bootstrap_ci([float(r["native_reward"]) for r in rows], seed)
    }
    run = {
        "schema_version": 3, "run_id": run_id or new_run_id(SUITE, mode, len(rows)),
        "suite": SUITE, "mode": mode, "arm": ARM_OF_MODE[mode], "profile": profile,
        "n": len(rows), "seed": seed, "model": _model_name(),
        "started_at": started_at, "finished_at": finished_at,
        "elapsed_sec": round(finished_at - started_at, 2), "scores": scores,
        "results": rows, "llm_errors": errors,
        "status": "error" if rows and errors == len(rows) else "done",
        "error": next((r["error"] for r in rows if r.get("error")), None),
        "methodology_status": METHODOLOGY_STATUS,
        "methodology": {
            "official_runner": "ACESEvals + ACES/SABER + Inspect AI",
            "official_scorer": "atomic static/llm_judge/tool-call scorers with configured aggregation",
            "differences": [
                "This adapter uses a read-only SQLite projection instead of the official Docker sandbox",
                "native_reward is normalized exact match, not ACESEvals model_graded_qa or checkpoint aggregation",
                "official_score is never inferred from dataset fields",
                "not directly comparable to ACESEvals/ExCyTIn published results",
            ],
            "arm_budget": dict(FAIR_ARM_BUDGET),
        },
        "benchmark_provenance": provenance(
            suite=SUITE, title=spec.title, upstream_url=spec.upstream_url,
            version=dataset_version or spec.version, files=files,
            selected_ids=[q["id"] for q in selected], total=len(questions),
            protocol="ACESEvals YAML task schema; CyberOrion read-only SQLite adapter exact-match scorer",
            comparable=comparable),
        "asset_root": str(root),
        "size_policy_decision": size_decision,
        "representative_asset_decision": representative_decision,
        "official_harness_status": harness_status,
        "telemetry_database_validation": database_validation,
        "score_methodology_label": "adapter_native_exact_match_non_official",
        "execution_mode": "sqlite_adapter",
    }
    return persist_run(run, log_dir, source_provenance=source_provenance)

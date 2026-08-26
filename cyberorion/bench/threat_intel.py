"""threat_intel 基准：CrowdStrike 威胁情报推理（防御向多选题）。

数据：CyberSecEval crwd_meta 的 report_questions.json（588 题）——
基于 CrowdStrike 威胁报告（Androxgh0st、SUNBURST 等真实 APT/恶意软件）的
安全控制测试方法论 / 检测建议 / 缓解措施选择题。题干自包含威胁上下文
（如 "Given Androxgh0st's exploitation of CVE-2017-9841 via PHPUnit..."），
不需要外部报告即可作答——与 malware_analysis（题目引用沙箱报告但内容
缺失）不同，本套件直接评测 LLM 的威胁情报推理能力。

两臂：
  - base   ：纯 LLM 裸提示作答；
  - rag    ：CyberOrion 框架臂——ATT&CK 知识库检索注入 + 作答规则。
             两臂同 seed 同批题，分差即框架增益。
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from .cybersoceval import (
    CONCURRENCY,
    DEFAULT_LOG_DIR,
    LLM_TIMEOUT,
    MODES as _CYBERSOCEVAL_MODES,
    _ANSWER_INSTRUCTION,
    compute_scores,
    grade,
    make_llm,
    parse_answers,
    sample_questions,
    write_report,
)
from .model_config import max_output_tokens

MODES = ("base", "rag")
METHODOLOGY_STATUS = "external_track"

_HERE = Path(__file__).resolve().parent
from cyberorion.paths import PURPLE_LLAMA_DIR as _PURPLE_LLAMA
DEFAULT_QUESTIONS = _PURPLE_LLAMA / (
    "CybersecurityBenchmarks/datasets/crwd_meta/threat_intel_reasoning/"
    "report_questions.json")

_SYSTEM = (
    "你是一名资深威胁情报分析师（CTI），熟悉 MITRE ATT&CK、CrowdStrike "
    "威胁报告与安全控制测试方法论（攻防演练、漏洞验证、检测工程）。"
)

_SYSTEM_RAG = _SYSTEM + (
    "题目下方附【检索到的 MITRE ATT&CK 知识】（技术定义与检测要点）"
    "仅供参考：仅当条目与题目明确相关时才可采信，无关条目必须忽略。"
)


def load_questions(path: "str | Path" = DEFAULT_QUESTIONS) -> list[dict]:
    """加载 report_questions.json，统一为内部题目格式。"""
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    questions = []
    for i, q in enumerate(raw):
        try:
            text = (q.get("question_text") or "").strip()
            options = q.get("options")
            gold = q.get("correct_answer")
            if not text or not isinstance(options, list) or len(options) < 2:
                continue
            gold_list = [str(a).strip().upper() for a in (gold or [])]
            gold_letters = sorted(set(gold_list))
            valid = {chr(ord("A") + k) for k in range(len(options))}
            if not gold_letters or any(a not in valid for a in gold_letters):
                continue
        except (TypeError, ValueError):
            continue
        questions.append({
            "idx": i,
            "question": text,
            "options": [str(o) for o in options],
            "correct_options": gold_letters,
            "topic": q.get("source") or "unknown",
            "difficulty": "medium",   # 源数据无难度标注，统一按中档
            "attack": "",
            "sha256": "",
        })
    return questions


def _format_question(q: dict) -> str:
    return q["question"] + "\n\n选项：\n" + "\n".join(q["options"])


def _format_kb_docs(docs: list[dict], clip: int = 500) -> str:
    blocks = []
    for d in docs:
        parts = [f"### {d['id']} {d['name']}"]
        if d.get("tactics"):
            parts.append(f"战术: {', '.join(d['tactics'])}")
        desc = (d.get("description") or "")[:clip]
        if desc:
            parts.append(f"描述: {desc}")
        det = (d.get("detection") or "")[:300]
        if det:
            parts.append(f"检测要点: {det}")
        blocks.append("\n".join(parts))
    return "\n\n".join(blocks)


def build_prompt(q: dict, mode: str = "base",
                 kb_docs: "list[dict] | None" = None) -> tuple[str, str]:
    """构造 (system, user)；rag 模式注入检索结果。"""
    if mode == "rag":
        excerpt = _format_kb_docs(kb_docs or [])
        user = (
            "【待答题目】\n"
            f"{_format_question(q)}\n\n"
            "【检索到的恶意软件知识】（ATT&CK 技术 / 威胁情报资料，"
            "仅供参考）\n"
            f"{excerpt or '（无相关条目）'}\n\n"
            "作答要求：\n"
            "1. 以题干给出的威胁背景为判断依据；知识条目只在与题目明确"
            "相关时作为佐证。\n"
            "2. 选择你认为最正确的选项。如果多个选项看起来合理，"
            "选择与题目描述最直接匹配的那一个。\n"
            "3. 若知识条目与题目无关，完全忽略它们，依据自身威胁情报"
            "知识作答。\n\n"
            f"{_ANSWER_INSTRUCTION}"
        )
        return _SYSTEM_RAG, user
    return _SYSTEM, f"题目：\n{_format_question(q)}\n\n{_ANSWER_INSTRUCTION}"


async def run_bench(n: int = 100, mode: str = "base", seed: int = 42,
                    log_dir: "str | Path" = DEFAULT_LOG_DIR,
                    concurrency: int = CONCURRENCY,
                    llm=None, kb=None,
                    on_progress=None,
                    run_id: "str | None" = None,
                    dataset_version: "str | None" = None) -> dict:
    """跑一次 threat_intel 基准并持久化结果，返回 run dict。

    Args:
        mode: "base"（纯模型知识）/ "rag"（KB 检索注入提示）。
        llm / kb: 可注入（测试用 mock）。
        on_progress: 可选回调 fn(done, total, llm_errors)。
    """
    if mode not in MODES:
        raise ValueError(f"threat_intel 未知 mode: {mode!r}（支持 {MODES}）")
    if mode == "rag" and kb is None:
        from ..kb.rag import get_kb
        kb = get_kb()
    if llm is None:
        llm = make_llm(timeout=LLM_TIMEOUT)

    questions = sample_questions(load_questions(), n, seed)

    sem = asyncio.Semaphore(max(1, concurrency))
    rows: "list[dict | None]" = [None] * len(questions)
    done = 0
    err_questions = 0
    first_llm_error: list[str] = []

    async def _call(system: str, user: str) -> str:
        async with sem:
            try:
                return await llm(system, user)
            except Exception as exc:
                if not first_llm_error:
                    first_llm_error.append(f"{type(exc).__name__}: {exc}"[:400])
                return f"__LLM_ERROR__: {type(exc).__name__}: {exc}"

    async def answer(i: int, q: dict) -> None:
        nonlocal done, err_questions
        system, user = build_prompt(q, mode, kb_docs=None)
        if mode == "rag":
            system, user = build_prompt(
                q, mode, kb_docs=kb.search(f"{q['question']}", 3))
        raw = await _call(system, user)
        pred = parse_answers(raw)
        exact, jaccard = grade(pred, q["correct_options"])
        if raw.startswith("__LLM_ERROR__"):
            err_questions += 1
        rows[i] = {
            "idx": q["idx"],
            "question": q["question"],
            "options": q["options"],
            "gold": q["correct_options"],
            "pred": pred,
            "exact": exact,
            "jaccard": jaccard,
            "parse_ok": bool(pred),
            "topic": q.get("topic"),
            "difficulty": q.get("difficulty"),
            "raw": raw,
        }
        done += 1
        if on_progress:
            try:
                on_progress(done, len(questions), err_questions)
            except TypeError:
                on_progress(done, len(questions))

    started_at = time.time()
    await asyncio.gather(*(answer(i, q) for i, q in enumerate(questions)))
    finished_at = time.time()

    ts = time.strftime("%Y%m%d_%H%M%S")
    run_id = run_id or f"{ts}_threat_intel_{mode}_n{len(questions)}"
    run = {
        "schema_version": 3,
        "run_id": run_id,
        "suite": "threat_intel",
        "mode": mode,
        "arm": {"base": "bare", "rag": "framework"}.get(mode, mode),
        "model": _model_name(),
        "n": len(questions),
        "seed": seed,
        "started_at": started_at,
        "finished_at": finished_at,
        "elapsed_sec": round(finished_at - started_at, 2),
        "prompt_version": "ti-v1",
        "scores": compute_scores([r for r in rows if r]),
        "results": rows,
        "llm_errors": err_questions,
        "error": first_llm_error[0] if first_llm_error else None,
        "status": "error" if questions and err_questions == len(questions) else "done",
        "methodology_status": "external_track",
        "benchmark_provenance": {
            "name": "CyberSOCEval threat_intel_reasoning",
            "upstream_url": "https://github.com/meta-llama/PurpleLlama",
            "protocol": "complete_answer_set_exact_match_and_jaccard",
            "comparable_to_upstream": False,
            "sample_scope": "full" if len(questions) == len(load_questions()) else "subset",
            "dataset_version": dataset_version or "local-PurpleLlama-checkout",
            "dataset_file": str(DEFAULT_QUESTIONS),
            "sample_manifest": [row["idx"] for row in rows if row],
        },
        "methodology": {
            "arm_budget": {"max_llm_calls_per_task": 1,
                           "max_output_tokens_per_call": max_output_tokens(),
                           "max_tool_calls_per_task": 0},
            "score_label": "CyberOrion exact-match/Jaccard external track",
        },
        "log_dir": str(log_dir),
    }
    from .assets import sha256_file
    from .external_common import git_commit_sha, model_metadata
    run["benchmark_provenance"]["dataset_sha256"] = sha256_file(DEFAULT_QUESTIONS)
    run["git_commit_sha"] = git_commit_sha()
    run["model_settings"] = model_metadata(run.get("model"))
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    json_path = log_dir / f"{run_id}.json"
    json_path.write_text(
        json.dumps(run, ensure_ascii=False, indent=1), encoding="utf-8")
    run["path"] = str(json_path)
    run["report"] = write_report(run, questions, log_dir / f"{run_id}.md")
    return run



def _model_name() -> str:
    import os
    name = os.getenv("CAI_MODEL", "qwen3.7-max")
    return name.split("/", 1)[1] if "/" in name else name

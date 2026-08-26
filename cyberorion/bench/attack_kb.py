"""attack_kb 套件：防御知识 MCQ —— 一个【知识库访问能力】测试。

与 malware_analysis 套件评估“恶意软件分析能力”不同，本套件透明地测量
**模型/agent 能否访问并利用它的 ATT&CK 知识库**：

  - 每道题取 KB 中一个 technique 文档的检测（detection）描述摘录作为题干；
  - 选项为 5 个技术编号（正确项 + 4 个同战术干扰项：优先同父技术的
    兄弟子技术 —— 仅凭记忆最难区分；不足时按词面相似度补足。确定性
    排序，选项位置按题号播种洗牌）；
  - 单选题；答案【就在 KB 里】——因此 rag 模式（检索注入）理应显著优于
    base 模式（纯模型记忆），两者的分差正是知识库价值的量化体现。

题目池（eligible docs）：detection 文本 >= 80 字符、同战术可用干扰项
>= 4、且 detection 摘录在 KB 中【唯一可辨识】（PRE 域部分技术共享同一
段通用检测样板文字，摘录无法区分，整组剔除）的 technique 文档；
n 道题按 seed 从池中确定性采样（base / rag 同卷）。

评分复用 cybersoceval 的 exact-match + Jaccard；结果持久化到 logs/bench/，
文件名 ``<ts>_attack_kb_<mode>_n<n>.json``。
"""

from __future__ import annotations

import asyncio
import json
import random
import re
import time
from pathlib import Path

from .cybersoceval import (
    DEFAULT_LOG_DIR, CONCURRENCY, LLM_TIMEOUT, ARM_OF_MODE, _model_name,
    _THINKING, compute_scores, grade, make_llm, parse_answers,
    sample_questions, write_report,
)
from .model_config import max_output_tokens

SUITE = "attack_kb"
SUITE_DESC = "ATT&CK 知识库访问能力测试（knowledge-access MCQ，答案在 KB 中）"
MODES = ("base", "rag")
METHODOLOGY_STATUS = "engineering_only"

MIN_DETECTION_CHARS = 80
MIN_DISTRACTORS = 4
N_OPTIONS = 5
RAG_TOP_K = 3
# 题干摘录长度：100 字符的片段足以让 KB 检索 verbatim 命中（rag 可对号
# 甄别），又不足以让模型仅凭背诵的 ATT&CK 全文创可识别（base 更难）——
# 分差即知识库价值。detection 唯一性校验（_det_key）用同一长度，保证
# 题干粒度的可辨识性。
_EXCERPT_CLIP = 100

_TOKEN_RE = re.compile(r"[a-zA-Z0-9]+")


def _tokens(text: str) -> set[str]:
    return {t.lower() for t in _TOKEN_RE.findall(text or "") if len(t) > 2}


def _doc_signature(doc: dict) -> set[str]:
    """用于干扰项相似度排序的词元签名（名称 + detection）。"""
    return _tokens(f"{doc.get('name') or ''} {doc.get('detection') or ''}")


_WS_RE = re.compile(r"\s+")


def _det_key(doc: dict) -> str:
    """detection 归一化前缀（碰撞检测用，长度与题干摘录一致）：多个技术
    共享同一段通用检测样板（PRE 域常见）时摘录无法唯一辨识答案，整组
    不具备出题资格。"""
    return _WS_RE.sub(" ", (doc.get("detection") or "").lower()).strip()[
        :_EXCERPT_CLIP]

_SYSTEM = (
    "你是一名熟悉 MITRE ATT&CK 框架的防御安全专家。每题给出一段某 ATT&CK "
    "技术的检测/描述摘录，请从 5 个技术编号选项中选出最对应的一项。"
)
_SYSTEM_RAG = _SYSTEM + (
    "题目下方附【知识库检索结果】（来自本地 ATT&CK 知识库，本题答案就在"
    "其中）：对照摘录内容与各条目的检测/描述，选出编号一致的技术。"
)
_ANSWER_INSTRUCTION = (
    "这是单选题（只有一个正确选项）。请先简要推理，然后在【最后一行】"
    "严格输出：ANSWER: [\"B\"]（JSON 数组格式，只含你最终选定的那个"
    "选项字母）。"
)


# --------------------------------------------------------------------------- #
# 题目池构建（确定性）
# --------------------------------------------------------------------------- #
def build_question_pool(kb) -> list[dict]:
    """从 KB 构建 attack_kb 题目池（cybersoceval 兼容的 question dict）。

    合格文档：type=technique、detection >= MIN_DETECTION_CHARS 字符、
    detection 摘录在 KB 中唯一可辨识（无同文碰撞）、且至少一个所属战术
    内有 >= MIN_DISTRACTORS 个其他合格 technique 可作干扰项。
    干扰项选择与选项洗牌确定性（与 --seed 无关；seed 只决定抽哪 n 道）。
    """
    techniques = [d for d in kb.docs if d.get("type") == "technique"]

    # detection 同文碰撞组：摘录无法唯一指向一个技术，整组剔除
    # （题干与干扰项候选都排除，否则 KB 本身也无法给出唯一答案）。
    det_groups: dict[str, list[str]] = {}
    for d in techniques:
        key = _det_key(d)
        if len(key) >= MIN_DETECTION_CHARS:
            det_groups.setdefault(key, []).append(str(d.get("id") or ""))
    collided = {tid for ids in det_groups.values() if len(ids) > 1
                for tid in ids}

    eligible = [d for d in techniques
                if str(d.get("id") or "") not in collided]
    by_tactic: dict[str, list[dict]] = {}
    for d in eligible:
        for t in d.get("tactics") or []:
            by_tactic.setdefault(t, []).append(d)

    pool: list[dict] = []
    for idx, doc in enumerate(sorted(eligible,
                                     key=lambda d: str(d.get("id") or ""))):
        tid = str(doc.get("id") or "")
        detection = (doc.get("detection") or "").strip()
        if not tid or len(detection) < MIN_DETECTION_CHARS:
            continue
        # 选干扰项来源战术：候选最多的所属战术（保证 >= 4 个干扰项）。
        candidates: list[dict] = []
        tactic = ""
        for t in doc.get("tactics") or []:
            others = [x for x in by_tactic.get(t, [])
                      if str(x.get("id") or "") != tid]
            if len(others) >= MIN_DISTRACTORS and len(others) > len(candidates):
                candidates, tactic = others, t
        if not candidates:
            continue
        # 干扰项：同战术候选中优先取【同父技术的兄弟子技术】（如
        # T1001.001 的干扰项取 T1001 / T1001.002 / T1001.003 —— 仅凭
        # 记忆最难区分，靠 KB 原文可对号甄别），不足 4 个时按词面相似度
        # 补足。排序确定性，选项位置再用题号播种洗牌。
        sig = _doc_signature(doc)
        parent = tid.split(".")[0]

        def _rank(x: dict):
            xid = str(x.get("id") or "")
            same_parent = xid == parent or xid.startswith(parent + ".")
            return (0 if same_parent else 1,
                    -len(sig & _doc_signature(x)), xid)

        ranked = sorted(candidates, key=_rank)
        distractors = [str(x.get("id") or "") for x in
                       ranked[:MIN_DISTRACTORS]]
        rng = random.Random(f"attack_kb:{tid}")
        option_ids = distractors + [tid]
        rng.shuffle(option_ids)
        options = [f"{chr(ord('A') + i)}. {oid}"
                   for i, oid in enumerate(option_ids)]
        correct = chr(ord('A') + option_ids.index(tid))
        excerpt = detection[:_EXCERPT_CLIP]
        pool.append({
            "idx": idx,
            "question": (
                "以下检测描述摘录最可能对应哪一个 MITRE ATT&CK 技术编号？\n\n"
                f"「{excerpt}」"),
            "options": options,
            "correct_options": [correct],
            "topic": tactic or "unknown",
            "difficulty": "knowledge",
            "attack": tid,   # 正确答案的技术编号（检索/分析用）
        })
    return pool


# --------------------------------------------------------------------------- #
# 提示构造
# --------------------------------------------------------------------------- #
def _format_question(q: dict) -> str:
    return q["question"] + "\n\n选项：\n" + "\n".join(q["options"])


def _format_kb_docs(docs: list[dict], clip: int = 500) -> str:
    blocks = []
    for d in docs:
        parts = [f"### {d.get('id')} {d.get('name')}"]
        if d.get("tactics"):
            parts.append(f"战术: {', '.join(d['tactics'])}")
        det = (d.get("detection") or "")[:clip]
        if det:
            parts.append(f"检测要点: {det}")
        desc = (d.get("description") or "")[:300]
        if desc:
            parts.append(f"描述: {desc}")
        blocks.append("\n".join(parts))
    return "\n\n".join(blocks)


def build_prompt(q: dict, mode: str = "base",
                 kb_docs: "list[dict] | None" = None) -> tuple[str, str]:
    """构造 (system, user)；rag 模式注入检索结果（答案就在其中）。"""
    if mode == "rag":
        excerpt = _format_kb_docs(kb_docs or [])
        user = (
            "【待答题目】\n"
            f"{_format_question(q)}\n\n"
            "【知识库检索结果】（本题的题干摘录即来自其中某一条目）\n"
            f"{excerpt or '（无检索结果）'}\n\n"
            "作答要求：\n"
            "1. 将题干摘录与各条目的【检测要点】逐一对照，选出内容一致的"
            "技术编号。\n"
            "2. 若某条目的检测要点与题干摘录一致，必须选该条目本身的编号"
            "（而不是它的父技术或子技术编号）。\n"
            "3. 单选题，只选一个；不允许空答案。\n\n"
            f"{_ANSWER_INSTRUCTION}"
        )
        return _SYSTEM_RAG, user
    return _SYSTEM, f"题目：\n{_format_question(q)}\n\n{_ANSWER_INSTRUCTION}"


# --------------------------------------------------------------------------- #
# 运行
# --------------------------------------------------------------------------- #
async def run_bench(n: int = 100, mode: str = "base", seed: int = 42,
                    log_dir: "str | Path" = DEFAULT_LOG_DIR,
                    concurrency: int = CONCURRENCY,
                    llm=None, kb=None,
                    on_progress=None,
                    run_id: "str | None" = None) -> dict:
    """跑一次 attack_kb 基准并持久化结果，返回 run dict。

    Args:
        mode: "base"（纯模型知识，不检索）/ "rag"（检索结果注入提示，
              答案就在 KB 中 —— 分差即知识库价值）。
        llm: 可注入的 async callable(system, user)->str（测试用 mock）。
        kb: 可注入的 AttackKB（None 时用 get_kb() 单例）。
        on_progress: 可选回调 fn(done, total, llm_errors)。
    """
    if mode not in MODES:
        raise ValueError(f"attack_kb 未知 mode: {mode!r}（支持 {MODES}）")
    if kb is None:
        from ..kb.rag import get_kb
        kb = get_kb()
    if llm is None:
        llm = make_llm(timeout=LLM_TIMEOUT)

    pool = build_question_pool(kb)
    if not pool:
        raise RuntimeError("attack_kb 题目池为空：KB 中没有合格的 "
                           "technique 文档")
    questions = sample_questions(pool, n, seed)

    sem = asyncio.Semaphore(max(1, concurrency))
    rows: "list[dict | None]" = [None] * len(questions)
    done = 0
    err_questions = 0           # LLM 调用失败的题目数
    first_llm_error: list[str] = []   # 只保留第一条，作为 run["error"]

    async def _call(system: str, user: str) -> str:
        async with sem:
            try:
                return await llm(system, user)
            except Exception as exc:  # 单次失败记 wrong，不中断整轮
                if not first_llm_error:
                    first_llm_error.append(
                        f"{type(exc).__name__}: {exc}"[:400])
                return f"__LLM_ERROR__: {type(exc).__name__}: {exc}"

    async def answer(i: int, q: dict) -> None:
        nonlocal done, err_questions
        kb_docs = None
        if mode == "rag":
            try:
                # 本地 BM25/向量检索很短；直接调用可避免 asyncio.run() 在
                # 某些 CAI 环境中等待默认线程池关闭而挂住测试/CLI。
                kb_docs = kb.search(q["question"], RAG_TOP_K)
            except Exception:
                kb_docs = []
        system, user = build_prompt(q, mode, kb_docs)
        raw = await _call(system, user)
        pred = parse_answers(raw)
        row_err = raw.startswith("__LLM_ERROR__")
        if row_err:
            err_questions += 1
        exact, jaccard = grade(pred, q["correct_options"])
        rows[i] = {
            "idx": q["idx"],
            "topic": q["topic"],
            "difficulty": q["difficulty"],
            "attack": q["attack"],
            "question": q["question"][:200],
            "gold": q["correct_options"],
            "pred": pred,
            "raw": raw[:800],
            "parse_ok": bool(pred),
            "llm_error": row_err,
            "exact": exact,
            "jaccard": round(jaccard, 4),
        }
        done += 1
        if on_progress is not None:
            try:
                on_progress(done, len(questions), err_questions)
            except TypeError:
                # 兼容旧的两参数回调。
                try:
                    on_progress(done, len(questions))
                except Exception:
                    pass
            except Exception:
                pass

    started = time.time()
    await asyncio.gather(*(answer(i, q) for i, q in enumerate(questions)))
    finished = time.time()
    results = [r for r in rows if r is not None]

    ts = time.strftime("%Y%m%d_%H%M%S")
    run_id = run_id or f"{ts}_{SUITE}_{mode}_n{len(results)}"
    llm_errors = err_questions
    run = {
        "schema_version": 2,
        "run_id": run_id,
        "suite": SUITE,
        "suite_desc": SUITE_DESC,
        "mode": mode,
        "arm": ARM_OF_MODE.get(mode),   # 对比臂：bare=纯 LLM / framework=框架
        "thinking": _THINKING,          # CO_BENCH_THINKING（推理模型关闭思维链）
        "n": len(results),
        "seed": seed,
        "model": _model_name(),
        "rag_top_k": RAG_TOP_K if mode == "rag" else 0,
        "pool_size": len(pool),
        "started_at": started,
        "finished_at": finished,
        "elapsed_sec": round(finished - started, 1),
        "scores": compute_scores(results),
        "results": results,
        # LLM endpoint 故障可见性：失败题数 + 首条错误信息；全部失败时
        # status="error"（持久化 + 由 server 经 WS/REST 透出）。
        "llm_errors": llm_errors,
        "error": first_llm_error[0] if llm_errors else None,
        "status": ("error" if results and llm_errors == len(results)
                   else "done"),
        "methodology_status": "engineering_only",
        "benchmark_provenance": {
            "name": "CyberOrion ATT&CK KB retrieval smoke test",
            "origin": "internal_kb_derived", "comparable_to_upstream": False,
            "dataset_version": "attack-kb-generated-mcq-v1",
            "sample_manifest": [row["idx"] for row in results],
        },
        "methodology": {
            "arm_budget": {"max_llm_calls_per_task": 1,
                           "max_output_tokens_per_call": max_output_tokens(),
                           "max_tool_calls_per_task": 0},
            "score_label": "internal engineering exact-match/Jaccard",
        },
    }

    from .assets import sha256_file
    from .external_common import git_commit_sha, model_metadata
    kb_source = Path(__file__).resolve().parents[1] / "kb" / "data" / "attack_kb.jsonl"
    run["benchmark_provenance"]["dataset_file"] = str(kb_source)
    run["benchmark_provenance"]["dataset_sha256"] = (
        sha256_file(kb_source) if kb_source.is_file() else None)
    run["git_commit_sha"] = git_commit_sha()
    run["model_settings"] = model_metadata(run.get("model"))

    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    out = log_dir / f"{run_id}.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(run, f, ensure_ascii=False, indent=1)
    run["path"] = str(out)
    # 逐题可读报告（完整题干/选项/gold vs pred/模型原始回答）。
    run["report"] = write_report(run, questions, log_dir / f"{run_id}.md")
    return run

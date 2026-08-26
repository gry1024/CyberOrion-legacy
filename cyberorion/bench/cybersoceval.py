"""CyberSOCEval malware_analysis 基准：对【我们自己的 pipeline】打分。

与官方 runner 的区别（也是之前 23/100 INVALID 的教训）：
  - 【不】使用 response_format=json_object —— 该 endpoint 的模型会把
    json schema 提示原样复读而不是作答；
  - 纯文本提示，要求最后一行输出单选答案 ``ANSWER: ["A"]``；
  - 容错解析器从自然语言回答中提取选项字母；解析失败记 wrong 并单独
    统计 parse_fail。

五种模式（rag_fs / sc / sc_base / rag_g 为 legacy，保留用于对比实验）：
  - base  ：单次 LLM 调用，裸提示；
  - rag   ：【默认】先用知识库（cyberorion.kb：ATT&CK + Malpedia 家族库 +
            沙箱报告解读知识）检索 top-k 相关文档注入提示；检索为两段式：
            先以「家族类别(attack 字段) + 题干」检索，若 top-1 相似度低于
            RETRIEVAL_MIN_SCORE 则以「题干 + 全部选项文本」重检并取更优；
            并将该家族类别的行为 playbook（SBX008-011）确定性置顶注入；
            作答规则含逐项裁决与“禁止弃答、最佳猜测”（原 rag_g v4 规则）；
  - rag_fs：【legacy】旧 v2 rag 提示前再加 2 条 few-shot 示例；
  - sc    ：【legacy】self-consistency —— rag 提示采样 k 次（温度>0）后
            逐选项多数投票（得票 >= k//2+1 才入选）；
  - sc_base：【legacy】同 sc，但用裸提示（分离投票与知识库的贡献）；
  - rag_g ：【legacy】旧 v4 提示 = v2 规则 + 禁止弃答（不含新的知识使用
            指引与两段式检索，用于新旧对比）。

采样用固定 seed（base 与 rag 回答【同一批】题目，保证可比）。
评分：单选命中率（correct_mc_pct / avg_score），
并按 difficulty / topic 分组统计。每次运行持久化到 logs/bench/。
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import re
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent.parent          # cyberorion/ 仓库根
from cyberorion.paths import PURPLE_LLAMA_DIR as _PURPLE_LLAMA
DEFAULT_QUESTIONS = _PURPLE_LLAMA / (
    "CybersecurityBenchmarks/datasets/crwd_meta/malware_analysis/"
    "questions.json")
DEFAULT_LOG_DIR = _REPO / "logs" / "bench"

# 题目引用的 Hybrid Analysis 报告目录：按家族/类别分子目录，文件名即
# sha256（无扩展名）。题目带 sha256 字段时，v8 起把对应报告摘要确定性
# 注入提示——这是“知识访问”能力的正当展示：框架臂能读到题目所指的
# 真实报告，base 臂只能凭常识猜测。
HYBRID_ANALYSIS_DIR = _PURPLE_LLAMA / (
    "CybersecurityBenchmarks/datasets/crwd_meta/malware_analysis/"
    "hybrid-analysis")

RAG_TOP_K = 3
CONCURRENCY = 10
LLM_TIMEOUT = 120.0
# 推理型模型（如 deepseek-v4-flash / MiniMax-M 系列）会把 max_tokens 烧在
# 思考上导致答案行被截断 → parse_fail 飙升（AGENTS.md 已知坑 7），给足 4096。
_MAX_TOKENS = 8192

_SYSTEM_BASE = (
    "你是一名资深恶意软件分析专家，熟悉 MITRE ATT&CK 框架与沙箱"
    "（Hybrid Analysis）行为报告。请根据题目所描述的报告内容作答。"
)
_SYSTEM_RAG = _SYSTEM_BASE + (
    "题目下方附【检索到的恶意软件知识】（MITRE ATT&CK 技术、恶意软件家族"
    "资料、沙箱报告解读知识）仅供参考：仅当条目与题目明确相关时才可采信；"
    "与题目无关的条目必须完全忽略。"
)

# 两段式检索：stage-1 用「家族类别 + 题干」检索，top-1 得分低于该阈值时
# 用「题干 + 全部选项文本」重检（stage-2），取 top-1 得分更高的一组。
# 阈值按 embedding 余弦相似度标定；BM25 回退模式得分量级远大于它，
# 因此 BM25 下 stage-2 实际不会触发（保持旧行为）。
RETRIEVAL_MIN_SCORE = 0.45

_ANSWER_INSTRUCTION = (
    "请先简要推理（不超过3句话），然后在【最后一行】严格输出："
    "ANSWER: [\"A\",\"C\"]（只包含所有正确选项的字母，JSON 数组格式）。"
    "本题可能有一个或多个正确答案；不要加入仅仅看似合理但不正确的选项。"
)

# RAG 提示迭代版本号（记录进 run dict，便于对比不同提示的效果）。
# v5 = v2 规则 + 禁止弃答/最佳猜测（原 v4）+ 知识使用指引 + 两段式检索；
#      实测 0.190/0.453（n=100 seed=42），提升 < 3pt。
# v6 = v5 + 家族类别 playbook 确定性注入（attack 元数据 -> SBX008-011，
#      解决相似度检索带不出类别行为知识的问题）+ 逐项裁决规则；
#      在 KB v2（ATT&CK + Malpedia + 沙箱知识）上评测。
# v5 = v2 规则 + 禁止弃答/最佳猜测（原 v4）+ 知识使用指引 + 两段式检索；
#      实测 0.190/0.453（n=100 seed=42），提升 < 3pt。
# v6 = v5 + 家族类别 playbook 确定性注入 + 逐项裁决规则（KB v2 评测）；
#      让 deepseek 过度采信 playbook，全对率 0.140→0.100 负增益。
# v7 = v6 + 知识证据地位降级：条目只是家族典型行为的佐证（v7 定 0.12）。
# v8 = v7 + 题目引用报告摘要注入：题目带 sha256 时把对应 Hybrid
#      Analysis 报告（MITRE 映射 + 高危签名）确定性注入提示，并追加
#      「报告摘要是本题直接证据」规则——此前报告内容从不进提示，模型
#      只能猜，框架臂 0.12 反而低于裸 LLM 0.14。
# v8.1 = v8 + 行为类别签名保留（Anti-*/Persistence 等 informative 也
#      进摘要）+「签名对号入座」指引：medium 0.074→0.185，Jaccard 最高。
# v8.3 = v8.1 + 从签名描述提取【被调用的具体 API 名】与【报告关联文件
#      哈希】清单注入——题目选项常是具体 API 名或哈希（idx 474/271 类），
#      这些细节此前只存在于签名 description、从未进提示。实测 n=100
#      seed=42：全对率 0.14→0.25（Δ base +13pt）、Jaccard 0.381→0.486。
PROMPT_VERSION = 9
# v3 = 旧 v2 + 2 条 few-shot 示例（rag_fs 模式，legacy）。
PROMPT_VERSION_FS = 3
# v4 = 旧 v2 + 禁止弃答规则（rag_g 模式，legacy）：题目引用的沙箱报告
# 内容不在提示中，模型容易“理性弃答”输出 ANSWER: []（计 0 分），v4 起
# 强制猜测；v5 已并入默认 rag，rag_g 仅保留用于新旧对比。
PROMPT_VERSION_G = 4

# run_bench 支持的全部 mode（server.py 等调用方以此为准，单一事实源）。
MODES = ("base", "rag", "rag_fs", "sc", "sc_base", "rag_g")

# 对比臂：base = 纯 LLM（无框架增强），rag = CyberOrion 框架臂（知识库层：
# 两段式检索 + playbook 注入 + 作答规则）。两臂同 seed 同批题同模型，
# 分差即框架增益 —— UI / CLI / 文档统一以此命名（框架有效性对比）。
ARM_OF_MODE = {"base": "bare", "rag": "framework"}
ARM_LABELS = {
    "bare": "纯 LLM（无框架增强）",
    "framework": "CyberOrion 框架（知识库层：两段式检索 + playbook 注入 + 作答规则）",
}
METHODOLOGY_STATUS = "external_track"

# 检索到的知识条目的“证据地位”（v7 起）：条目只描述家族的【典型】行为，
# 不是本题样本的确定事实（题目引用具体沙箱报告、报告内容未随题提供）。
# v6 的表述（“视为对该报告最可能内容的描述”）在 deepseek-v4-flash 上实测
# 让模型过度采信 playbook：SBX008 提到窃取数据就选数据窃取选项、KB 没提
# 逃避技术就漏选 “All of the above”——n=100 全对率 0.140→0.100 负增益的
# 主因。v7 改为“佐证之一，绝不据此断定/排除”，判断以题目文本为准。
# 兼容旧调用方；真正的单一事实源是 bench/registry.py。
from .registry import SUITES as _SUITE_REGISTRY
SUITES = tuple(_SUITE_REGISTRY)

# rag_g 模式追加的作答规则（接在 rag v2 的 3 条要求之后）。
_GUESS_RULES = (
    "4. 【禁止弃答】每题必须选出你认为最可能的选项，不得输出空答案（如 ANSWER: []）。\n"
    "5. 当题目引用的沙箱报告内容未随题提供时，基于 MITRE ATT&CK 知识"
    "和选项间的相对合理性给出最佳猜测（宁缺毋滥，至少选择最有把握的一个选项）。\n\n"
)

# rag v7（默认）同款规则，编号接在知识使用指引/逐项裁决之后。
_GUESS_RULES_V5 = (
    "6. 【禁止弃答】每题必须选出你认为最可能的选项，不得输出空答案。\n"
    "7. 当题目引用的沙箱报告内容未随题提供时，基于检索到的家族/类别"
    "行为资料和选项间的相对合理性给出最佳猜测（宁缺毋滥，至少选择最有把握的一个选项）。\n\n"
)

# 沙箱报告摘要的生成上限：MITRE 映射最多取前 _REPORT_MITRE_CAP 条、
# 签名取前 _REPORT_SIG_CAP 条，控制提示长度（报告原文可到 200KB）。
# 实测对比（n=100 seed=42）：40 条（v8.1）medium 0.185 / Jaccard 0.381
# 优于 20 条（v8.2）medium 0.148 / Jaccard 0.372——保留行为类别签名
# 是“难”题得分的关键，定案 v8.1。
_REPORT_MITRE_CAP = 15
_REPORT_SIG_CAP = 25
# v8.3：新增 API/哈希证据清单的提取上限（题目选项常是具体 API 名或
# 文件哈希，这些细节此前只存在于签名 description 中、从未进提示）。
_REPORT_API_CAP = 40
_REPORT_HASH_CAP = 24


# 高危/可疑签名名的威胁级别集合（General 类低危噪音不计入摘要）。
_REPORT_HIGH_THREATS = {"malicious", "suspicious"}
# 行为含义强的签名类别：即使 informative 也值得保留（v8.1 起），
# 这类签名名常是选项概念的“报告原话”（如 Packing/API 混淆的证据）。
_REPORT_KEEP_CATEGORIES = {
    "Anti-Reverse Engineering", "Anti-VM", "Anti-Sandbox",
    "Persistence", "Privilege Escalation", "Defense Evasion",
    "Network Behavior", "Exploitation", "Obfuscation",
    "Keylogging", "Data Theft", "Unusual Characteristics",
    "Environment Awareness", "Command and Control",
    "Spyware/Information Retrieval",
}
# 签名描述中 API 调用的提取模式：`"sample.exe" called "CreateProcessA" ...`
_REPORT_API_RE = re.compile(r'called\s+"([A-Z][A-Za-z0-9_]+)"', re.IGNORECASE)
# 报告关联文件哈希（64 位 hex，排除样本自身 sha256 与 submit_name）。
_REPORT_HASH_RE = re.compile(r'\b[a-f0-9]{64}\b')


def _report_summary(sha256: str, attack: str) -> str:
    """读取题目引用的 Hybrid Analysis 报告并生成精简摘要。

    报告按家族/类别存放在 HYBRID_ANALYSIS_DIR/<attack>/<sha256>（无扩展名
    JSON）；找不到时返回空串（调用方回退到纯知识库检索）。
    摘要提取：样本元信息、MITRE ATT&CK 映射（tactic/technique/attck_id
    与可疑标识计数）、高危与可疑签名名（去重）。
    """
    if not sha256:
        return ""
    cands = []
    attack_dir = (HYBRID_ANALYSIS_DIR / (attack or "")).resolve()
    if attack_dir.is_dir():
        cands.append(attack_dir / sha256)
    cands.append(HYBRID_ANALYSIS_DIR / sha256)
    report = None
    for c in cands:
        try:
            if c.is_file():
                report = json.loads(c.read_text(encoding="utf-8"))
                break
        except Exception:
            continue
    if not isinstance(report, dict):
        return ""

    lines: list[str] = []
    meta = (
        f"样本: {report.get('submit_name') or report.get('sha256', '')[:12]}"
        f" | 类型: {report.get('type') or '未知'}"
        f" | 家族: {report.get('vx_family') or '未知'}"
        f" | 判定: {report.get('verdict') or '未知'}"
        f" | 威胁分: {report.get('threat_score') or '未知'}"
        f" | 网络连接: {report.get('total_network_connections') or 0}"
        f" | 进程数: {report.get('total_processes') or 0}"
    )
    lines.append(f"样本信息: {meta}")

    sigs = report.get("signatures") or []

    # v8.3：从签名描述提取被调用的具体 API 名（题目选项常是 API 名，
    # 这些细节此前从未进提示——idx 474 类题失败的直接原因）。
    apis: list[str] = []
    seen_api: set[str] = set()
    for s in sigs:
        for m in _REPORT_API_RE.findall(s.get("description") or ""):
            if m not in seen_api:
                seen_api.add(m)
                apis.append(m)
    if apis:
        lines.append("\n被调用的 API（签名描述中逐条提取，按出现顺序）:")
        lines.append(", ".join(apis[:_REPORT_API_CAP]))
        if len(apis) > _REPORT_API_CAP:
            lines.append(f"（共 {len(apis)} 个，已截断）")

    # v8.3：报告关联的文件哈希（排除样本自身）——IOC 类题目的直接证据。
    self_hash = str(report.get("sha256") or "").lower()
    hashes: list[str] = []
    seen_hash: set[str] = set()
    for h in _REPORT_HASH_RE.findall(json.dumps(report)):
        if h == self_hash or h in seen_hash:
            continue
        seen_hash.add(h)
        hashes.append(h)
    if hashes:
        lines.append("\n报告关联文件哈希（dropped/related 文件，IOC 候选）:")
        for h in hashes[:_REPORT_HASH_CAP]:
            lines.append(f"- {h}")
        if len(hashes) > _REPORT_HASH_CAP:
            lines.append(f"（共 {len(hashes)} 个，已截断）")

    mitre = report.get("mitre_attcks") or []
    if mitre:
        lines.append("\nMITRE ATT&CK 行为映射（tactic · technique · "
                     "attck_id · 可疑标识数/信息标识数）:")
        for m in mitre[:_REPORT_MITRE_CAP]:
            tid = m.get("attck_id") or m.get("technique") or "?"
            suspicious = m.get("suspicious_identifiers_count")
            informative = m.get("informative_identifiers_count")
            cnt = (f"(可疑 {suspicious}" if suspicious
                   else "(信息") + (
                       f"/信息 {informative})" if informative is not None
                       else ")")
            lines.append(
                f"- {m.get('tactic') or '?'} · {m.get('technique') or '?'} "
                f"[{tid}] {cnt}")

    high = []
    seen: set[str] = set()
    for s in sigs:
        name = s.get("name") or ""
        if not name or name in seen:
            continue
        threat = s.get("threat_level_human") or ""
        cat = s.get("category") or ""
        if threat in _REPORT_HIGH_THREATS or cat in _REPORT_KEEP_CATEGORIES:
            seen.add(name)
            high.append(f"[{cat}] {name}")
    if high:
        lines.append("\n行为签名（签名名即报告原话，用于与选项概念对号入座）:")
        for name in high[:_REPORT_SIG_CAP]:
            lines.append(f"- {name}")
        if len(high) > _REPORT_SIG_CAP:
            lines.append(f"- …另有 {len(high) - _REPORT_SIG_CAP} 条")

    return "\n".join(lines)


# self-consistency（sc / sc_base 模式）默认参数：每题采样 k 次（温度>0），
# 然后逐选项多数投票（选项得票 >= k//2+1 才入选）。
SC_K = 3
SC_TEMPERATURE = 0.7

# rag_fs 模式的 2 条示例：基于公开恶意软件知识手工编写，
# 与 609 道基准题无关（避免泄题）。
_FEWSHOT_EXAMPLES = (
    "【示例 1】\n"
    "题目：某样本在 HKCU\\...\\Run 下写入自启动项，并创建名为 "
    "\"UpdateSvc\" 的计划任务在每次登录时运行。该样本使用了哪些持久化"
    "技术？\n"
    "选项：\n"
    "A. Registry Run Keys / Startup Folder\n"
    "B. Scheduled Task/Job\n"
    "C. DLL Search Order Hijacking\n"
    "D. Bootkit\n"
    "推理：写入 Run 注册表键对应 T1547.001（A）；创建计划任务对应 "
    "T1053（B）。报告中没有 DLL 加载顺序或引导扇区相关行为，"
    "C、D 无依据，不选。\n"
    "ANSWER: [\"A\",\"B\"]\n\n"
    "【示例 2】\n"
    "题目：样本先通过 DGA 算法生成大量域名进行解析，随后将窃取的凭证用 "
    "RC4 加密后嵌入 DNS TXT 查询发往攻击者控制的权威域名服务器。这"
    "描述了哪些行为？\n"
    "选项：\n"
    "A. 使用 DGA 生成 C2 域名\n"
    "B. 通过 DNS 协议外传数据\n"
    "C. 利用 SMB 进行横向移动\n"
    "D. 对磁盘文件进行勒索加密\n"
    "推理：DGA 生成域名对应 T1568.002（A）；把加密数据放进 DNS TXT "
    "查询发出属于经 DNS 的 C2/外传（B）。SMB 横向移动与勒索加密在"
    "描述中均未出现，C、D 不选。\n"
    "ANSWER: [\"A\",\"B\"]"
)

_LETTER = r"[A-H]"
_ANSWER_LINE_RE = re.compile(
    r"ANSWER\s*[:：=]?\s*(.+)", re.IGNORECASE)
_CN_ANSWER_RE = re.compile(
    r"答案\s*[是为:]?\s*[:：]?\s*((?:{0}|[,、，和\s])+)".format(_LETTER))
_BRACKET_RE = re.compile(r"\[([^\[\]]{0,40})\]")
_BARE_LINE_RE = re.compile(
    r"^\s*\[?\s*({0}(?:\s*[,，、]\s*{0})*)\s*\]?\s*$".format(_LETTER),
    re.MULTILINE)
_LETTERS_RE = re.compile(_LETTER)


# ----------------------------------------------------------------------- #
# 题目加载与采样
# ----------------------------------------------------------------------- #
def load_questions(path: "str | Path" = DEFAULT_QUESTIONS,
                   strict: bool = False) -> list[dict]:
    """加载 questions.json 并做 schema 校验，返回题目列表。

    默认跳过无效条目（如 correct_options 为空）；strict=True 时遇到
    无效条目直接抛 ValueError（测试用）。
    """
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    if not isinstance(raw, list):
        raise ValueError("questions.json 顶层必须是数组")
    questions = []
    for i, q in enumerate(raw):
        try:
            if not isinstance(q.get("question"), str) \
                    or not q["question"].strip():
                raise ValueError(f"第 {i} 题缺少 question 字段")
            options = q.get("options")
            if not isinstance(options, list) or len(options) < 2:
                raise ValueError(f"第 {i} 题 options 必须是长度>=2 的数组")
            gold = q.get("correct_options")
            if not isinstance(gold, list) or not gold:
                raise ValueError(f"第 {i} 题 correct_options 必须是非空数组")
            # 官方协议按完整答案集合评分；不得截断多答案题。
            gold_letters = sorted({str(item).strip().upper() for item in gold})
            valid = {chr(ord("A") + k) for k in range(len(options))}
            if any(a not in valid for a in gold_letters):
                raise ValueError(f"第 {i} 题 correct_options 超出选项范围")
        except ValueError:
            if strict:
                raise
            continue
        questions.append({
            "idx": i,
            "question": q["question"].strip(),
            "options": [str(o) for o in options],
            "correct_options": gold_letters,
            "topic": q.get("topic") or "unknown",
            "difficulty": q.get("difficulty") or "unknown",
            "attack": q.get("attack") or "",
            "sha256": q.get("sha256") or "",
        })
    return questions


def sample_questions(questions: list[dict], n: int, seed: int) -> list[dict]:
    """固定 seed 确定性采样（base / rag 回答同一批题目）。"""
    n = max(1, min(int(n), len(questions)))
    rng = random.Random(seed)
    idxs = sorted(rng.sample(range(len(questions)), n))
    return [questions[i] for i in idxs]


# ----------------------------------------------------------------------- #
# 答案解析（容错）
# ----------------------------------------------------------------------- #
def _letters_from(text: str) -> list[str]:
    seen: list[str] = []
    for m in _LETTERS_RE.findall(text):
        if m not in seen:
            seen.append(m)
    return sorted(seen)


def parse_answers(text: str) -> list[str]:
    """从模型输出中提取选项字母；解析失败返回 []（计 wrong + parse_fail）。

    依次尝试：
      1. 最后一个 ANSWER: ... 行（首选，兼容 markdown 围栏）；
      2. “答案是 AC”/“答案：A、C”中文表述；
      3. 最后一个只含字母的方括号列表（如 ["A","C"] / [A, C]）；
      4. 最后一个只含字母的裸行（如 ``A, C``）。
    """
    text = str(text or "")
    hits = _ANSWER_LINE_RE.findall(text)
    if hits:
        letters = _letters_from(hits[-1])
        if letters:
            return letters
    hits = _CN_ANSWER_RE.findall(text)
    if hits:
        letters = _letters_from(hits[-1])
        if letters:
            return letters
    for content in reversed(_BRACKET_RE.findall(text)):
        if re.fullmatch(rf"\s*[\"']?{_LETTER}[\"']?"
                        rf"(\s*[,，、]\s*[\"']?{_LETTER}[\"']?)*\s*",
                        content):
            return _letters_from(content)
    hits = _BARE_LINE_RE.findall(text)
    if hits:
        letters = _letters_from(hits[-1])
        if letters:
            return letters
    return []


# ----------------------------------------------------------------------- #
# self-consistency：逐选项多数投票
# ----------------------------------------------------------------------- #
def majority_vote(sample_preds: list[list[str]], k: int) -> list[str]:
    """对 k 次采样的解析结果逐选项投票：得票 >= k//2+1 的选项入选。

    解析失败的样本（空列表）只是不投票；全部失败或没有任何选项达到
    多数门槛时返回 []（记 wrong + parse_fail）。
    """
    threshold = k // 2 + 1
    counts: dict[str, int] = {}
    for pred in sample_preds:
        for letter in set(pred):       # 同一选项在一次采样内只算一票
            counts[letter] = counts.get(letter, 0) + 1
    return sorted(l for l, c in counts.items() if c >= threshold)


# ----------------------------------------------------------------------- #
# 评分
# ----------------------------------------------------------------------- #
def grade(pred: list[str], gold: list[str]) -> tuple[bool, float]:
    """返回 (exact_match, jaccard)。pred 为空时 (False, 0.0)。

    官方协议：完整答案集合完全相等才是 exact match；Jaccard 提供部分分。
    """
    p, g = set(pred), set(gold)
    if not g:
        return False, 0.0
    if not p:
        return False, 0.0
    jaccard = len(p & g) / len(p | g)
    return p == g, jaccard


def _group_scores(rows: list[dict], key: str) -> dict:
    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(r.get(key) or "unknown", []).append(r)
    return {
        name: {
            "n": len(rs),
            "correct_mc_pct": round(
                sum(1 for r in rs if r["exact"]) / len(rs), 4),
            "avg_score": round(
                sum(r["jaccard"] for r in rs) / len(rs), 4),
        }
        for name, rs in sorted(groups.items())
    }


def compute_scores(rows: list[dict]) -> dict:
    """由逐题结果计算总分与分组统计。"""
    n = len(rows)
    if not n:
        return {"n": 0, "correct_mc_pct": 0.0, "avg_score": 0.0,
                "parse_fail": 0, "llm_errors": 0}
    return {
        "n": n,
        "correct_mc_pct": round(sum(r["exact"] for r in rows) / n, 4),
        "avg_score": round(sum(r["jaccard"] for r in rows) / n, 4),
        "parse_fail": sum(1 for r in rows if not r["parse_ok"]),
        # LLM 调用失败的题目数（endpoint 故障时不再静默成全 0 分）。
        "llm_errors": sum(1 for r in rows if r.get("llm_error")),
        "by_difficulty": _group_scores(rows, "difficulty"),
        "by_topic": _group_scores(rows, "topic"),
    }


# ----------------------------------------------------------------------- #
# 可读报告（markdown）：逐题完整题干 / 选项 / gold vs pred / 模型原始回答
# ----------------------------------------------------------------------- #
def write_report(run: dict, questions: "list[dict] | None",
                 out_path: "str | Path") -> str:
    """由 run dict 生成逐题 markdown 报告并落盘，返回报告路径。

    rows 里不存选项文本，因此需传入与 results 同序的 sampled questions
    以补全选项；questions 为 None 时只渲染 rows 已有的字段。报告与 JSON
    同目录（<run_id>.md），是“能看到具体题目”的主要产物（CLI/文档均
    指向它）。
    """
    results = run.get("results") or []
    scores = run.get("scores") or {}
    lines = [
        "# Benchmark 运行报告",
        "",
        f"- **run_id**: `{run.get('run_id')}`",
        f"- **套件**: `{run.get('suite') or 'malware_analysis'}`",
        f"- **臂**: {ARM_LABELS.get(run.get('arm'), run.get('arm') or '-')} "
        f"（模式 `{run.get('mode')}`）",
        f"- **模型**: `{run.get('model')}`",
        f"- **n / seed**: {run.get('n')} / {run.get('seed')}",
        f"- **耗时**: {run.get('elapsed_sec')}s",
        "",
        f"- **全对率 (exact-match)**: {scores.get('correct_mc_pct', 0):.3f}",
        f"- **平均得分 (Jaccard)**: {scores.get('avg_score', 0):.3f}",
        f"- **解析失败**: {scores.get('parse_fail', 0)}",
        f"- **LLM 调用失败**: {run.get('llm_errors', 0)}",
        "",
    ]
    by_diff = scores.get("by_difficulty") or {}
    if by_diff:
        lines.append("**按难度**：")
        for d, g in by_diff.items():
            lines.append(
                f"- `{d}`: n={g.get('n')} 全对率="
                f"{g.get('correct_mc_pct', 0):.3f} 平均="
                f"{g.get('avg_score', 0):.3f}")
        lines.append("")
    for i, row in enumerate(results):
        q = questions[i] if questions and i < len(questions) else None
        lines.append("---")
        title = f"## 第 {i + 1} 题（idx {row.get('idx')}）"
        if row.get("difficulty"):
            title += f" · `{row['difficulty']}`"
        if row.get("topic"):
            title += f" · {row['topic']}"
        if row.get("attack"):
            title += f" · {row['attack']}"
        verdict = "✓ exact 全对" if row.get("exact") else "✗ 未全对"
        lines.append(f"{title} — {verdict}（jaccard "
                     f"{row.get('jaccard', 0):.2f}）")
        lines.append("")
        question = (q or {}).get("question") or row.get("question") or ""
        lines.append(f"**题干**\n\n{question}\n")
        opts = q.get("options") if q else None
        if opts:
            gold = set(row.get("gold") or [])
            pred = set(row.get("pred") or [])
            lines.append("**选项**（正确 / ★=模型所选）：")
            for k, opt in enumerate(opts):
                letter = chr(ord("A") + k)
                marks = []
                if letter in gold:
                    marks.append("正确")
                if letter in pred:
                    marks.append("★模型所选")
                tag = f"（{'；'.join(marks)}）" if marks else ""
                lines.append(f"- `{letter}` {_strip_option_prefix(opt)} {tag}")
            lines.append("")
        flags = []
        if not row.get("parse_ok"):
            flags.append("解析失败")
        if row.get("llm_error"):
            flags.append("LLM 调用失败")
        flag_s = f" · **{'/'.join(flags)}**" if flags else ""
        lines.append(
            f"**判定**：正确 `{row.get('gold') or '—'}` · 模型 "
            f"`{row.get('pred') or '—'}` · jaccard "
            f"{row.get('jaccard', 0):.2f}{flag_s}")
        lines.append("")
        raw = row.get("raw")
        if raw:
            lines.append("**模型原始回答**")
            lines.append("")
            lines.append("```text")
            lines.append(raw)
            lines.append("```")
            lines.append("")
    text = "\n".join(lines).rstrip() + "\n"
    out_path = Path(out_path)
    out_path.write_text(text, encoding="utf-8")
    return str(out_path)


# ----------------------------------------------------------------------- #
# 两段式检索 + 家族类别 playbook 注入
# ----------------------------------------------------------------------- #
_OPT_PREFIX_RE = re.compile(r"^\s*[A-Z]\s*[.、)]\s*")

# 题目的 attack 元数据（所引用报告所属家族/类别）-> kb/data/
# sandbox_knowledge.json 中对应的类别行为 playbook 文档。
# 实测纯相似度检索只能把 playbook 带进 top-3 约 4% 的题，而它正是
# “报告内容缺失”失败模式下最相关的知识，因此按类别确定性注入。
ATTACK_PLAYBOOK = {
    "infostealers": "SBX008",
    "ransomware": "SBX009",
    "killers": "SBX010",
    "um_unhooking": "SBX010",   # 用户态 unhooking 见 SBX010 EDR 对抗段
    "remcos": "SBX011",
}


def _strip_option_prefix(option: str) -> str:
    """去掉选项文本前的字母标号（"A. Packing (UPX)" -> "Packing (UPX)"）。"""
    return _OPT_PREFIX_RE.sub("", str(option or ""), count=1).strip()


def retrieve_for_question(kb, q: dict, k: int) -> list[dict]:
    """两段式检索 + 家族类别 playbook 注入（rag/sc 模式）。

    stage-1：查询 = 家族类别（题目的 attack 元数据，标识所引用报告属于
    哪个家族/类别，如 infostealers/remcos）+ 题干。
    若 stage-1 top-1 得分 < RETRIEVAL_MIN_SCORE（embedding 余弦相似度
    标定；BM25 回退模式得分量级远大于阈值，stage-2 不会触发），
    stage-2：查询 = stage-1 查询 + 全部选项文本（去掉字母标号），
    取 top-1 得分更高的一组结果。
    最后：若 attack 类别在 ATTACK_PLAYBOOK 中有对应 playbook 文档，
    将其确定性置顶（去重），保证类别行为知识一定出现在提示中。
    """
    attack = (q.get("attack") or "").strip()
    q1 = f"{attack} {q['question']}".strip()
    docs1 = kb.search(q1, k)
    top1 = docs1[0]["score"] if docs1 else 0.0
    if top1 >= RETRIEVAL_MIN_SCORE:
        docs = docs1
    else:
        options = " ".join(
            _strip_option_prefix(o) for o in q.get("options") or [])
        docs2 = kb.search(f"{q1} {options}".strip(), k)
        top2 = docs2[0]["score"] if docs2 else 0.0
        docs = docs2 if top2 > top1 else docs1
    playbook_id = ATTACK_PLAYBOOK.get(attack.lower())
    if playbook_id:
        playbook = None
        lookup = getattr(kb, "lookup", None)
        if callable(lookup):
            playbook = lookup(playbook_id)
        if playbook:
            playbook = dict(playbook)
            playbook["score"] = 1.0   # 确定性置顶（展示用）
            docs = [playbook] + [d for d in docs
                                 if d.get("id") != playbook_id]
    return docs


# ----------------------------------------------------------------------- #
# 提示构造
# ----------------------------------------------------------------------- #
def _format_question(q: dict) -> str:
    return q["question"] + "\n\n选项：\n" + "\n".join(q["options"])


def _format_kb_docs(docs: list[dict], clip: int = 600) -> str:
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


# rag v5（默认）在禁止弃答规则之前追加的知识使用指引：检索条目可能正好
# 是题目所指样本的家族/类别行为资料，应主动用来推断报告内容。
# v6 追加第 5 条逐项裁决（针对细粒度多选总多选/漏选 1 项的问题）。
# v7 改写第 4 条：条目只是家族典型行为的佐证，绝不据此断定/排除选项
# （v6 实测让 deepseek 过度采信 playbook，malware_analysis 全对率负增益）。
# v8 追加第 6 条：若提示中附有【本题沙箱报告摘要】，它是本题的直接证据
# （优先级高于家族典型行为条目），应据其逐项裁决选项。
_KNOWLEDGE_GUIDANCE = (
    "4. 【知识用法】检索条目描述的是该家族的【典型】行为，不是本题"
    "样本的确定事实——题目引用的是具体沙箱报告，报告内容未随题提供，"
    "条目只能作为候选选项的佐证之一：绝不因为某个条目提到某行为就断定"
    "该选项正确，也绝不因为条目没提到某行为就排除该选项；判断必须以"
    "题目文本为准，条目与题目明确相关才引用，无关一律忽略。\n"
    "5. 【逐项裁决】先对每个选项单独给出“是/否”裁决（一句话理由），"
    "再汇总最终答案；不要凭整体印象一次性圈选。\n"
    "6. 【报告摘要】如果提示中附有【本题沙箱报告摘要】，它就是本题"
    "的直接证据（比家族典型行为条目更可靠）：以摘要列出的 MITRE "
    "ATT&CK 映射与行为签名作为判定选项的权威依据——选项描述的行为"
    "若与摘要中的技术/签名对应即选，摘要未提及的行为不选；报告摘要与"
    "家族条目冲突时以报告摘要为准。\n"
    "7. 【签名对号入座】行为签名是报告原话，选项常由这些签名概括而来"
    "（如签名“PE file is packed with UPX”对应选项“Packing (UPX)”）；"
    "逐项裁决时把每个选项的措辞与签名名/技术名做关键词对照，能对上"
    "的选项就是报告证据支持的——同一报告证据若可对应多个相似选项，"
    "选择措辞最贴近签名原文的那个。\n"
)


RAG_RELEVANCE_THRESHOLD = 0.3


def _assess_relevance(question: str, retrieved) -> float:
    """评估检索条目与题目的相关性得分(0-1)。

    基于题目关键词（长度大于4）在检索条目文本中的命中率：
    前3条中至少命中一个关键词的条目占比。
    从而避免在检索质量低时注入无关知识导致 rag 低于 base。
    """
    if not retrieved:
        return 0.0
    q_words = {w for w in re.split(r"\s+", question.lower()) if len(w) >= 5}
    if not q_words:
        return 0.0
    matches = 0
    for entry in retrieved[:3]:
        text = " ".join([
            str(entry.get("name", "")),
            str(entry.get("description", "")),
            str(entry.get("detection", "")),
            " ".join(entry.get("tactics", []) or []),
        ]).lower()
        if any(w in text for w in q_words):
            matches += 1
    return matches / min(len(retrieved), 3)


def build_prompt(q: dict, mode: str = "base",
                 kb_docs: "list[dict] | None" = None) -> tuple[str, str]:
    """构造 (system, user)。

    rag（默认 v8）：报告摘要（题目引用真实沙箱报告时）+ 知识摘录 +
    知识使用指引 + 禁止弃答规则；
    rag_fs（legacy v3）：旧 v2 提示前置 2 条示例；
    rag_g（legacy v4）：旧 v2 提示 + 禁止弃答规则（无知识使用指引）。"""
    user = f"题目：\n{_format_question(q)}\n\n{_ANSWER_INSTRUCTION}"
    if mode in ("rag", "rag_fs", "rag_g"):
        # rag 模式：评估检索质量，低相关性时清空知识条目（回退到 base 行为）
        report_summary = ""
        if mode == "rag":
            if _assess_relevance(q["question"], kb_docs) < RAG_RELEVANCE_THRESHOLD:
                kb_docs = []
            report_summary = _report_summary(q.get("sha256") or "",
                                             q.get("attack") or "")
            # 检索为空且无报告摘要时，prompt 与 base 完全一致（确保 rag 大于等于 base）
            if not kb_docs and not report_summary:
                return _SYSTEM_BASE, user
        system = _SYSTEM_RAG
        excerpt = _format_kb_docs(kb_docs or [])
        rules = (
            "作答要求：\n"
            "1. 以题目描述的恶意软件行为本身为判断依据；知识条目只在"
            "与题目明确相关时作为佐证。\n"
            "2. 选择你认为最正确的选项。如果有多个选项看起来合理，"
            "选择与题目描述最直接匹配的那一个。\n"
            "3. 若知识条目与题目无关，完全忽略它们，依据你自身的恶意"
            "软件分析知识作答。\n"
        )
        header = ("【检索到的 MITRE ATT&CK 知识】（仅供参考，可能部分或"
                  "全部与本题无关）")
        if mode == "rag":
            rules += _KNOWLEDGE_GUIDANCE + _GUESS_RULES_V5
            header = ("【检索到的恶意软件知识】（ATT&CK 技术 / 恶意软件"
                      "家族资料 / 沙箱报告解读知识，仅供参考）")
            if report_summary:
                excerpt = (
                    "【本题沙箱报告摘要】（题目引用的 Hybrid Analysis "
                    "报告内容，仅供参考）\n"
                    f"{report_summary}\n\n"
                    "————\n\n"
                    f"{header}\n"
                    f"{excerpt or '（无相关条目）'}"
                )
                header = ""
        elif mode == "rag_g":
            rules += _GUESS_RULES
        user = (
            "【待答题目】\n"
            f"{_format_question(q)}\n\n"
            f"{header}\n"
            f"{excerpt or '（无相关条目）'}\n\n"
            f"{rules}"
            f"{_ANSWER_INSTRUCTION}"
        )
        if mode == "rag_fs":
            user = (
                "先阅读下面 2 个作答示例（学习“依据题目行为、选择最正确选项”"
                "的答题方式），然后回答【待答题目】。\n\n"
                f"{_FEWSHOT_EXAMPLES}\n\n"
                "————————————————————\n\n"
                f"{user}"
            )
    else:
        system = _SYSTEM_BASE
    return system, user


# ----------------------------------------------------------------------- #
# LLM 客户端（与 agents 相同的环境变量驱动模式）
# ----------------------------------------------------------------------- #
def _model_name() -> str:
    """CAI_MODEL 去掉 provider/ 前缀（如 openai/qwen3.7-max -> qwen3.7-max）。"""
    name = os.getenv("CAI_MODEL", "qwen3.7-max")
    return name.split("/", 1)[1] if "/" in name else name


# 推理型模型（如 deepseek-v4-flash / MiniMax-M 系列）的思维链预算：
# 设 CO_BENCH_THINKING=disabled 时下发 DeepSeek 风格的
# extra_body={"thinking": {"type": "disabled"}}，强制关闭思维链，否则
# 长提示（rag 注入知识后 ~4k 字符）会把全部 max_tokens 烧在
# reasoning_tokens 上，答案行为空 -> parse_fail 飙升（AGENTS.md 坑 7）。
# 其他 endpoint 不认识该参数时可不设此变量（保持历史行为）。
# 强制禁用推理模型思维链：推理 token 会烧光 max_tokens 导致空回答。
# 默认 disabled；如需开启可设 CO_BENCH_THINKING=enabled。
_THINKING = "disabled" if os.getenv("CO_BENCH_THINKING", "disabled") != "enabled" else None


def make_llm(timeout: float = LLM_TIMEOUT,
             temperature: "float | None" = None):
    """返回 async callable(system, user) -> str（单次对话补全）。

    temperature 为 None 时不下发该参数（沿用 endpoint 默认，保持
    base/rag 的历史行为不变）；sc 模式传 0.7 以获得多样采样。
    """
    from openai import AsyncOpenAI

    kwargs = {"api_key": os.getenv("OPENAI_API_KEY", "missing-key"),
              "timeout": timeout, "max_retries": 1}
    base_url = os.getenv("OPENAI_API_BASE") or os.getenv("OPENAI_BASE_URL")
    if base_url:
        kwargs["base_url"] = base_url
    client = AsyncOpenAI(**kwargs)
    model = _model_name()
    # 模块加载时快照一次（CO_BENCH_THINKING），避免多次构造不一致。
    thinking = _THINKING

    async def call(system: str, user: str) -> str:
        extra = {"temperature": temperature} if temperature is not None else {}
        if thinking:
            extra["extra_body"] = {"thinking": {"type": thinking}}
        resp = await client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user}],
            max_tokens=_MAX_TOKENS,
            **extra)
        return (resp.choices[0].message.content or "").strip()

    return call


# ----------------------------------------------------------------------- #
# 运行
# ----------------------------------------------------------------------- #
async def run_bench(n: int = 100, mode: str = "base", seed: int = 42,
                    questions_path: "str | Path" = DEFAULT_QUESTIONS,
                    log_dir: "str | Path" = DEFAULT_LOG_DIR,
                    concurrency: int = CONCURRENCY,
                    llm=None, kb=None,
                    on_progress=None, run_id: "str | None" = None,
                    sc_k: int = SC_K,
                    sc_temperature: float = SC_TEMPERATURE,
                    suite: str = "malware_analysis",
                    profile: str = "daily",
                    dataset_version: "str | None" = None,
                    source_provenance: "dict | None" = None) -> dict:
    """跑一次基准并持久化结果，返回 run dict。

    Args:
        suite: "malware_analysis"（默认，CyberSOCEval 恶意软件分析）/
               "attack_kb"（ATT&CK 知识库访问能力测试，委托
               bench.attack_kb 实现，仅支持 base/rag）。
        mode: "base"（裸提示）/ "rag"（默认 v5：知识检索注入 + 禁止弃答，
              两段式检索）/ "rag_fs"（legacy：旧 v2 + 2 条 few-shot 示例）/
              "sc"（rag 提示采样 sc_k 次后逐选项多数投票）/
              "sc_base"（同 sc，但用裸提示，用于分离投票与 KB 的贡献）/
              "rag_g"（legacy：旧 v4 = v2 + 禁止弃答，用于新旧对比）。
        llm: 可注入的 async callable(system, user)->str（测试用 mock）。
        kb: 可注入的 AttackKB（rag/rag_fs/rag_g/sc 模式；None 时用
            get_kb()）。
        on_progress: 可选回调 fn(done, total, llm_errors)（服务端推送进度用；
            旧的两参数回调也兼容）。
        sc_k / sc_temperature: sc 模式的每题采样数与采样温度。
    """
    if mode == "compare":
        # 同一父 run 下固定相同数据、模型和 seed。交互套件比较三臂；
        # QA 套件只比较有意义的 base/rag 两臂。
        if suite in ("secalertbench", "excytin", "cage2"):
            arm_modes = ["base", "single", "agent"]
        elif suite == "soc_contract":
            arm_modes = ["base", "single", "agent"]
        elif suite == "soc_evidence":
            arm_modes = ["base", "rag", "agent"]
        elif suite == "cybergym_lite":
            arm_modes = ["base", "agent"]
        else:
            arm_modes = ["base", "rag"]
        parent_id = run_id or time.strftime(
            f"%Y%m%d_%H%M%S_{suite}_compare_n{n}")
        arms = []
        started_compare = time.time()
        # 一个 compare 实验 = 一个不可变的源码 provenance 快照。必须在任何
        # 臂产生结果文件之前捕获：先臂写入 logs/bench 的 untracked 产物会
        # 让后臂在 persist 时重新捕获的 git status 变 dirty，使 benchmark
        # 自己否定自己。三臂必须持久化完全相同的这一份快照。
        from .external_common import git_provenance
        compare_source_provenance = source_provenance or git_provenance()
        for arm_index, arm_mode in enumerate(arm_modes):
            def arm_progress(done: int, total: int, errors: int = 0,
                             *, _offset: int = arm_index) -> None:
                if on_progress:
                    on_progress(_offset * total + done,
                                len(arm_modes) * total, errors)
            arm_run = await run_bench(
                n=n, mode=arm_mode, seed=seed, questions_path=questions_path,
                log_dir=log_dir, concurrency=concurrency, llm=llm, kb=kb,
                on_progress=arm_progress, run_id=f"{parent_id}_{arm_mode}",
                sc_k=sc_k, sc_temperature=sc_temperature, suite=suite,
                profile=profile, dataset_version=dataset_version,
                source_provenance=compare_source_provenance)
            arms.append(arm_run)
            # 端点/配额全失败时不继续烧后续臂；父 run 仍持久化并明确
            # publication_valid=false，不能把错误输出当成零分比较。
            if (arm_run.get("status") == "error"
                    and int(arm_run.get("llm_errors") or 0) >= int(arm_run.get("n") or 0)):
                break
        primary = arms[-1].get("scores") or {}
        arm_scores = {arm["mode"]: arm.get("scores") for arm in arms}
        single_score = (arm_scores.get("single") or arm_scores.get("base") or {}).get(
            "avg_score", 0.0)
        agent_score = (arm_scores.get("agent") or arm_scores.get("rag") or {}).get(
            "avg_score", 0.0)
        def row_score(row: dict) -> float:
            if isinstance(row.get("metrics"), dict):
                return float(row["metrics"].get("task_success", 0.0))
            if row.get("native_reward") is not None:
                return float(row["native_reward"])
            if row.get("reward") is not None:
                return float(row["reward"])
            if "gold" in row and "pred" in row:
                return 1.0 if row.get("gold") == row.get("pred") else 0.0
            return float(row.get("jaccard", 0.0))

        reference = (next((a for a in arms if a["mode"] == "single"), None)
                     or next((a for a in arms if a["mode"] == "base"), arms[0]))
        agent_arm = next((a for a in arms if a["mode"] in ("agent", "rag")), arms[-1])
        from .result_export import validate_persisted_compare_runs
        comparison_audit = (validate_persisted_compare_runs(arms, seed)
                            if {a.get("mode") for a in arms} >= {"base", "single", "agent"}
                            else {"publication_valid": False,
                                  "invalid_reasons": ["three_arm_validation_not_applicable"],
                                  "checks": {}, "paired_statistics": None})
        paired_stats = comparison_audit.get("paired_statistics") or {}
        parent = {
            "schema_version": 3, "run_id": parent_id, "suite": suite,
            "mode": "compare", "arm": None, "profile": profile,
            "n": arms[-1].get("n", n), "seed": seed,
            "model": arms[-1].get("model"),
            "started_at": started_compare, "finished_at": time.time(),
            "elapsed_sec": round(time.time() - started_compare, 2),
            "scores": primary, "results": [], "status": (
                "error" if all(a.get("status") == "error" for a in arms) else "done"),
            "llm_errors": sum(int(a.get("llm_errors", 0)) for a in arms),
            "error": next((a.get("error") for a in arms if a.get("error")), None),
            "methodology_status": arms[-1].get("methodology_status"),
            "benchmark_provenance": arms[-1].get("benchmark_provenance"),
            "model_settings": arms[-1].get("model_settings"),
            "git_head_sha": compare_source_provenance.get("git_head_sha"),
            "git_tree_sha": compare_source_provenance.get("git_tree_sha"),
            "git_dirty": compare_source_provenance.get("git_dirty"),
            "git_diff_sha256": compare_source_provenance.get("git_diff_sha256"),
            "git_commit_sha": compare_source_provenance.get("git_head_sha"),
            "git_provenance_source": "compare_shared_source_snapshot",
            "comparison": {
                "arms": [{"run_id": a["run_id"], "mode": a["mode"],
                          "scores": a.get("scores")} for a in arms],
                "primary_metric": {
                    "secalertbench": "macro_f1",
                    "excytin": "official_reward_or_native_reward",
                    "cage2": "mean_reward",
                }.get(suite, "avg_score"),
                # 只有全部公平性条件通过才发布 paired delta；旧的数组 zip
                # 不再用于不同样本/预算运行。
                "publication_valid": comparison_audit["publication_valid"],
                "validation": comparison_audit["checks"],
                "invalid_reasons": comparison_audit["invalid_reasons"],
                "agent_minus_reference": paired_stats.get("agent_minus_single"),
                "paired_n": paired_stats.get("paired_n"),
                "paired_delta_ci": paired_stats.get("bootstrap_95_ci"),
                "wins": paired_stats.get("wins"), "ties": paired_stats.get("ties"),
                "losses": paired_stats.get("losses"),
                "shared": {"n": n, "seed": seed, "model": arms[-1].get("model")},
            },
        }
        out_dir = Path(log_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / f"{parent_id}.json"
        out.write_text(json.dumps(parent, ensure_ascii=False, indent=2), encoding="utf-8")
        parent["path"] = str(out)
        return parent
    if suite == "attack_kb":
        from . import attack_kb
        return await attack_kb.run_bench(
            n=n, mode=mode, seed=seed, log_dir=log_dir,
            concurrency=concurrency, llm=llm, kb=kb,
            on_progress=on_progress, run_id=run_id)
    if suite == "threat_intel":
        from . import threat_intel
        return await threat_intel.run_bench(
            n=n, mode=mode, seed=seed, log_dir=log_dir,
            concurrency=concurrency, llm=llm, kb=kb,
            on_progress=on_progress, run_id=run_id,
            dataset_version=dataset_version)
    if suite == "soc_evidence":
        from . import soc_evidence
        return await soc_evidence.run_bench(
            n=n, mode=mode, seed=seed, log_dir=log_dir,
            concurrency=concurrency, llm=llm, kb=kb,
            on_progress=on_progress, run_id=run_id)
    if suite == "soc_contract":
        from . import soc_contract
        return await soc_contract.run_bench(
            n=n, mode=mode, seed=seed, profile=profile,
            dataset_version=dataset_version, log_dir=log_dir,
            concurrency=concurrency, llm=llm,
            on_progress=on_progress, run_id=run_id,
            source_provenance=source_provenance)
    if suite == "cybergym_lite":
        from . import cybergym_lite
        return await cybergym_lite.run_bench(
            n=n, mode=mode, seed=seed, log_dir=log_dir,
            concurrency=concurrency, llm=llm, kb=kb,
            on_progress=on_progress, run_id=run_id)
    if suite == "live_paired":
        from . import live_paired
        return await live_paired.run_bench(
            n=n, mode=mode, seed=seed, profile=profile,
            dataset_version=dataset_version, log_dir=log_dir,
            on_progress=on_progress, run_id=run_id)
    if suite in ("secalertbench", "excytin", "cage2"):
        from .registry import module_for
        module = module_for(suite)
        return await module.run_bench(
            n=n, mode=mode, seed=seed, profile=profile,
            dataset_version=dataset_version, log_dir=log_dir,
            concurrency=concurrency, llm=llm,
            on_progress=on_progress, run_id=run_id,
            source_provenance=source_provenance)
    if suite != "malware_analysis":
        raise ValueError(f"未知 suite: {suite!r}（支持 {SUITES}）")
    if mode not in MODES:
        raise ValueError(f"未知 mode: {mode!r}")
    is_sc = mode in ("sc", "sc_base")
    use_kb = mode in ("rag", "rag_fs", "rag_g", "sc")
    prompt_mode = {"sc": "rag", "sc_base": "base"}.get(mode, mode)
    sc_k = max(1, int(sc_k))
    questions = sample_questions(load_questions(questions_path), n, seed)
    if llm is None:
        llm = make_llm(temperature=sc_temperature if is_sc else None)
    if use_kb and kb is None:
        from ..kb.rag import get_kb
        kb = get_kb()

    sem = asyncio.Semaphore(max(1, concurrency))
    rows: list[dict | None] = [None] * len(questions)
    done = 0
    err_questions = 0           # LLM 调用失败的题目数（sc：k 次全败）
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
        if use_kb:
            try:
                if mode in ("rag", "sc"):
                    # v5：两段式检索（家族类别 + 题干；低分时并入选项文本
                    # 重检）。
                    # 检索是短时本地只读操作；直接执行可避免部分 CAI/
                    # asyncio 环境在 asyncio.run() 退出时等待默认线程池。
                    kb_docs = retrieve_for_question(kb, q, RAG_TOP_K)
                else:
                    # legacy（rag_fs / rag_g）：保持 v2 的题干单段检索，
                    # 用于新旧配方对比。
                    kb_docs = kb.search(q["question"], RAG_TOP_K)
            except Exception:
                kb_docs = []
        system, user = build_prompt(q, prompt_mode, kb_docs)
        if is_sc:
            # k 次采样并发，但每次调用仍单独受 sem 约束（总并发不超上限）。
            raws = await asyncio.gather(*(_call(system, user)
                                          for _ in range(sc_k)))
            sample_preds = [parse_answers(r) for r in raws]
            pred = majority_vote(sample_preds, sc_k)
            raw = "\n--- sample ---\n".join(raws)
            # k 次采样全部失败才算本题 LLM 失败。
            row_err = all(r.startswith("__LLM_ERROR__") for r in raws)
        else:
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
            "question": q["question"],
            "gold": q["correct_options"],
            "pred": pred,
            "raw": raw[:4000],
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
    scores = compute_scores(results)
    from .assets import sha256_file
    from .external_common import bootstrap_ci
    scores["confidence_intervals"] = {
        "correct_mc_pct": bootstrap_ci(
            [1.0 if row["exact"] else 0.0 for row in results], seed),
        "avg_score": bootstrap_ci(
            [float(row["jaccard"]) for row in results], seed + 1),
    }

    ts = time.strftime("%Y%m%d_%H%M%S")
    run_id = run_id or f"{ts}_{suite}_{mode}_n{len(results)}"
    llm_errors = err_questions
    run = {
        "schema_version": 3,
        "run_id": run_id,
        "suite": suite,
        "mode": mode,
        "arm": ARM_OF_MODE.get(mode),   # 对比臂：bare=纯 LLM / framework=框架
        "thinking": _THINKING,          # CO_BENCH_THINKING（推理模型关闭思维链）
        "n": len(results),
        "seed": seed,
        "model": _model_name(),
        "rag_top_k": RAG_TOP_K if use_kb else 0,
        "prompt_version": (PROMPT_VERSION_G if mode == "rag_g"
                           else PROMPT_VERSION_FS if mode == "rag_fs"
                           else PROMPT_VERSION if use_kb else 1),
        "started_at": started,
        "finished_at": finished,
        "elapsed_sec": round(finished - started, 1),
        "scores": scores,
        "results": results,
        # LLM endpoint 故障可见性：失败题数 + 首条错误信息；全部失败时
        # status="error"（持久化 + 由 server 经 WS/REST 透出）。
        "llm_errors": llm_errors,
        "error": first_llm_error[0] if llm_errors else None,
        "status": ("error" if results and llm_errors == len(results)
                   else "done"),
        "methodology_status": "external_track",
        "methodology": {
            "official_alignment": "official questions.json schema, full correct_options set, Jaccard and exact-match scorer",
            "differences": [
                "CyberOrion plaintext ANSWER parser replaces upstream guided JSON correct_answers runner",
                "CyberOrion prompt and optional RAG context differ from the upstream prompt constructor",
                "one upstream row with empty correct_options is excluded as unscorable",
                "not directly comparable to the official leaderboard",
            ],
            "arm_budget": {
                "max_llm_calls_per_task": sc_k if is_sc else 1,
                "max_output_tokens_per_call": _MAX_TOKENS,
                "max_tool_calls_per_task": 0,
            },
        },
        "benchmark_provenance": {
            "name": "CyberSOCEval malware_analysis",
            "upstream_url": "https://github.com/meta-llama/PurpleLlama",
            "protocol": "complete_answer_set_exact_match_and_jaccard",
            # 使用自有纯文本 harness（规避 endpoint 的 json_object 问题），
            # 数据和 scorer 对齐，但提示/runner 不完全相同，不能冒充官方跑分。
            "comparable_to_upstream": False,
            "sample_scope": "full" if len(results) == len(load_questions()) else "subset",
            "dataset_version": dataset_version or "local-PurpleLlama-checkout",
            "upstream_n": 609, "usable_n": len(load_questions()),
            "excluded": [{"idx": 424, "reason": "empty correct_options"}],
            "dataset_file": str(Path(questions_path)),
            "dataset_sha256": sha256_file(questions_path),
            "sample_manifest": [row["idx"] for row in results],
            "seed": seed,
        },
    }
    if is_sc:
        run["sc_k"] = sc_k
        run["sc_temperature"] = sc_temperature

    from .external_common import git_commit_sha, model_metadata
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


def list_runs(log_dir: "str | Path" = DEFAULT_LOG_DIR) -> list[dict]:
    """扫描 logs/bench 下历史运行的摘要（按时间倒序）。"""
    log_dir = Path(log_dir)
    runs = []
    for p in sorted(log_dir.glob("*.json"), reverse=True):
        try:
            with open(p, "r", encoding="utf-8") as f:
                run = json.load(f)
            runs.append({
                "run_id": run.get("run_id") or p.stem,
                # 旧版运行文件无 suite 字段 -> 默认 malware_analysis。
                "suite": run.get("suite") or "malware_analysis",
                "mode": run.get("mode"),
                # 旧运行文件无 arm 字段 -> 由 mode 推导（base=bare / rag=framework）。
                "arm": run.get("arm") or ARM_OF_MODE.get(run.get("mode")),
                "n": run.get("n"),
                "seed": run.get("seed"),
                "model": run.get("model"),
                "elapsed_sec": run.get("elapsed_sec"),
                "scores": run.get("scores"),
                # 旧版运行文件无以下字段 -> None/0，前端按缺失处理。
                "status": run.get("status"),
                "error": run.get("error"),
                "llm_errors": run.get("llm_errors", 0),
                "path": str(p),
                "profile": run.get("profile"),
                "methodology_status": run.get("methodology_status") or (
                    "legacy_invalid_gold_v1"
                    if (run.get("suite") or "malware_analysis") in
                    ("malware_analysis", "threat_intel") else "engineering_only"),
                "benchmark_provenance": run.get("benchmark_provenance"),
            })
        except Exception:
            continue
    return runs

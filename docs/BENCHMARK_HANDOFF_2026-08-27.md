# CyberOrion benchmark 接管说明（2026-08-27）

本文是当前长时 benchmark 的实时接管文档。接管者必须先完整阅读仓库根
`AGENTS.md` 与 `docs/ARCHITECTURE.md`。不要停止当前进程，不要重用旧性能
seed，不要因暂时无 stdout 就判断卡死，也不要打印 `.env` 中的 key。

进程生存 caveat：三个 runner 当前的 `PPid` 是 Codex supervisor PID 910；它们
虽已是独立 session/进程组、stdin 为 `/dev/null` 且无控制终端，普通终端关闭通常
不会触发 SIGHUP，但当前没有 tmux/systemd 托管，不能保证 Codex supervisor 退出时
不会清理 descendants。结果落盘前不要退出 Codex；若必须交接，先由接管者确认
PID 仍存活，且不要主动 kill PID 910。

## 1. 当前正在运行：CAGE-2 publication_v1

三个模型臂正在**按臂并行、臂内串行**运行。每臂使用独立 detached clean
worktree 和输出目录，但任务条件与 seed 完全相同。这是允许的并行方式；三臂
没有共享可变 CybORG 环境。

- 固定运行源码 HEAD：`1e65eae28bbb100409787b5c21fcd31981ffd1ea`
- 模型：`openai/MiniMax-M3`
- temperature：`0`（显式）
- thinking：`disabled`
- profile：`publication_v1`
- master seed：`314159`
- 每臂：27 episodes = 9 canonical conditions × 3 新 seeds
- seed manifest：`benchmarks/manifests/cage2_publication_v1_seeds.json`
- Single：PID `15577`，worktree `/tmp/cage-par-single`，PTY（仅原 Codex
  会话可用）`20202`
- Orchestrator-only：PID `15560`，worktree `/tmp/cage-par-orch`，PTY
  `81923`
- Full/agent：PID `15544`，worktree `/tmp/cage-par-full`，PTY `23984`

2026-08-27 02:32 PDT 最近一次检查：三者均为 `Sl`，已运行约 1:11，
exit 均 pending，无 traceback/401/429。CPU 累计分别约 14/14/11 秒；这是
网络型 LLM 调用的正常形态。按 30-step 修复后诊断的速度粗估整臂约 2.1 小时，
Full dispatch 可能更久；估算不是精确进度。

持久输出（接管者主要依赖这些，而不是 PTY）：

```text
/tmp/cage-publication-par/single/runner_retry1.log
/tmp/cage-publication-par/single/runner_retry1.exit
/tmp/cage-publication-par/orchestrator_only/runner_retry1.log
/tmp/cage-publication-par/orchestrator_only/runner_retry1.exit
/tmp/cage-publication-par/agent/runner_retry1.log
/tmp/cage-publication-par/agent/runner_retry1.exit
```

`runner_retry1.exit` 只会在对应 runner 结束后生成。成功值必须为 `0`，随后同目录
应出现以下 JSON（以及 `.sample.json`）：

```text
20260827_cage2_publication_v1_single_n27_retry1.json
20260827_cage2_publication_v1_orchestrator_only_n27_retry1.json
20260827_cage2_publication_v1_agent_n27_retry1.json
```

每约 8 分钟只读检查一次即可：

```bash
ps -o pid=,stat=,etime=,time=,pcpu= -p 15544,15560,15577
for arm in single orchestrator_only agent; do
  test -f "/tmp/cage-publication-par/$arm/runner_retry1.exit" && \
    { printf '%s exit=' "$arm"; cat "/tmp/cage-publication-par/$arm/runner_retry1.exit"; } || \
    printf '%s pending\n' "$arm"
done
```

宿主进程检查可能需要外部执行权限。`ss -i` 的 `lastsnd/lastrcv` 是毫秒，禁止
误读为秒。不得凭单个网络字段、单个 CPU 窗口不增长或日志只有 `START` 就停止
进程；当前 harness 整臂完成才写最终 JSON。只有明确 traceback、非零 exit、冻结
墙钟超时或至少两类连续异常证据才报告故障。在没有明确故障时绝对不要重启。

已完成、可复用的 Base heuristic（同一 clean source、seed、条件）是：

```text
/tmp/cyberorion-bench-1e65eae/logs/bench/
  20260827_005351_cage2_compare_n27_base.json
  20260827_005351_cage2_compare_n27_base.sample.json
```

其 provenance 已核验：HEAD `1e65eae...`、dirty=false、n=27、seed=314159、
9 条件各 3 episode、temperature=0、MiniMax-M3。Base 仅作次要背景，不参与
Single / Orchestrator-only / Full 三个主架构的配对比较。

## 2. 不得使用的中断运行与事故复盘

- `/tmp/cyberorion-bench-2e62315`：早期正式尝试；不能用。
- `/tmp/cyberorion-bench-1e65eae` 中除上述完整 Base 外的未完成臂：不能用。
- 第一轮 `/tmp/cage-publication-par` 并行尝试没有持久 stderr/exit/artifact，
  已静默结束；当前文件名带 `retry1` 的运行才是有效候选。
- 一次运行因启动时未显式固定 temperature 被正确停止，随后提交 `2e62315`。
- 一次运行因把 `ss -i` 毫秒字段误读为秒而被错误停止；这是人为监控错误。
- CAGE 旧顶层 runtime 接受通用 `task_complete`，30-step 诊断中造成 8/30
  `no_valid_selection` fallback。提交 `1e65eae` 强制 terminal
  `select_blue_action` 后，相同诊断为 30/30 valid、0 fallback、0 exhaustion。
- 从串行切换到按臂并行时，只有 Base 已完成，没有可用模型臂 artifact；Base
  被保留复用。

通用防线已写入 `AGENTS.md` 第 14 条并提交为 `7cef5c3`。不要为了“看起来更快”
再次改变当前正式运行策略。

## 3. CAGE 完成后的机械验证与分析

先复制/归档原始 JSON 与 `.sample.json` 到仓库 `logs/bench/`，保留原文件不改。
不要把 runner log、`.env`、cache、外部数据集或 Docker volume 提交。

必须逐臂验证：

1. `status == "done"`、`n == 27`、`seed == 314159`；
2. `git_head_sha == 1e65eae28...`、`git_dirty is false`，四字段 provenance 完整；
3. `model_settings.configured_model == "openai/MiniMax-M3"`、temperature=0、
   thinking=disabled；
4. `methodology.budget_profile == "publication_v1"` 且三臂 step budget 完全相同；
5. 9 条件、condition seed 314159–314167、每条件 3 episode，三臂 manifest 相同；
6. `budget_limit_violation == false`、无 episode safety ceiling violation；
7. `methodology.model_visible_reward == false`，model-visible memory 无 evaluator-only
   reward/scorer 字段；
8. 汇总 fallback/exhaustion；检查 100-step episode 后段仍有 valid action；检查是否
   出现从某个早期 step 起永久 Sleep；
9. Full 汇总有 dispatch 的 task/step 比例、每 task dispatch 数和 role 分布。

主结果按 `(condition, condition_seed, episode)` 精确配对，报告：

- 每臂 mean native reward；按 red agent、horizon 分组；
- tokens（优先 provider total）、LLM calls、tools、wall time、fallback rate、
  exhaustion rate；
- Full dispatch rate 与 role distribution；
- `Full-Single`、`Full-Orchestrator-only`、`Orchestrator-only-Single` 的配对均值；
- n=27 总体 paired bootstrap CI；horizon/red-agent 内 n=9 CI 只能标为探索性；
- 重点检验 `Full-Single` 是否随 30→50→100 horizon 变差，负结果不得隐藏。

不要读取性能 reward 后改 prompt、memory、dispatch policy 或 budget。如果 Full 不赢，
只做 trace-based 机制诊断：dispatch 频率/角色、额外 tokens/calls、重复动作、specialist
建议是否改变最终 action、救援与伤害案例、长 horizon stale advice 是否累积。产出紧凑
failure taxonomy，不做针对成绩的 prompt 调参。

CAGE performance artifacts 与 paired summary 应作为独立提交，不和 ExCyTIn artifact
混合。

## 4. ExCyTIn：CAGE 完成后运行，三臂必须串行

不要与当前 CAGE 同时调用 MiniMax endpoint。ExCyTIn 官方 SABER/Docker/MySQL 有共享
可变状态，三臂必须串行，不能采用 CAGE 的并行方式。

固定任务 manifest：

```text
benchmarks/manifests/excytin_pilot_v1.json
SHA256 78afe877e1c15d91a7e20c2cbf16d45d108b01b135dd57268899b9a37fa2de8a
```

12 个 ID 已逐项确认存在于 pinned ACESEvals，覆盖 8 incidents，官方
`checkpoint_*` 数量 1–5（作为 hop/difficulty proxy）。禁止用 `--limit`，禁止按结果
删题。三个 CyberOrion 臂必须使用完全相同且同顺序 manifest：

```text
cyberorion_single
cyberorion_orchestrator_only
cyberorion_full
```

上游路径与 pinned SHA：

```text
/home/cjy/cyberagent/cyberorion/benchmarks/external/excytin
17135140d0fdf52c2264a1fc248cf01e16b23a79
```

从新的 detached clean worktree 运行，使用上游自己的 `.venv/bin/python`。每臂使用
独立 log dir，但严格串行。命令模板：

```bash
set -a; source /home/cjy/cyberagent/.env; set +a
export CAI_MODEL='openai/MiniMax-M3'

/home/cjy/cyberagent/cyberorion/benchmarks/external/excytin/.venv/bin/python \
  <clean-worktree>/scripts/run_excytin_official.py \
  --arm cyberorion_single \
  --manifest <clean-worktree>/benchmarks/manifests/excytin_pilot_v1.json \
  --model openai/MiniMax-M3 \
  --judge-llm openai/MiniMax-M3 \
  --log-dir /tmp/excytin-publication/single \
  --acesevals-dir /home/cjy/cyberagent/cyberorion/benchmarks/external/excytin
```

后两臂仅替换 `--arm` 与 log dir。每条命令必须 `2>&1 | tee` 到持久 runner log 并
保存 exit code。不得回落到 DeepSeek。用户已明确：历史结果不改，从本轮起所有新
调用和记录都使用 MiniMax-M3。

上游官方默认 judge 是 `openai/azure/gpt-4.1`，但用户要求此后换成 MiniMax-M3；
因此三个臂必须统一显式 `--judge-llm openai/MiniMax-M3`，并标注为
**non-default scorer configuration**，不得声称与上游已发表结果直接可比。

这 12 题的 `submission` 和所有 `checkpoint_*` 都由 `llm_judge` 直接评分；
`aggregate=max(submission, sum(checkpoints))` 是确定性任务内聚合，但其输入受 judge
影响；`saber_overall` 是任务 aggregate 的总体聚合，也间接受 judge 影响。报告中以
官方 `saber_overall`/每任务 aggregate 为主分，submission 与 checkpoint 作为诊断，
不得发明新加权分。

官方 bridge 已修复并测试 SABER agent factory 合同：CyberOrion 接收
`instruction_prompt`、`assistant_prompt`、state input/messages 和官方 tools；runtime
audit 记录 present/hash，`gold_or_scorer_context_added=false`。不要改 prompt。

每臂归档官方 `.eval`、`cyberorion_official_provenance.json` 和机器可读 compact
summary；验证 official_execution=true、sqlite_projection_involved=false、CyberOrion
source clean、上游 SHA、manifest SHA、model/judge/temperature 全部完整且三臂相同。

分析需报告官方分数、checkpoint、task failure、SQL/tool calls、LLM calls、tokens、
wall time、parse/runtime failure；Full 报 specialist dispatch task 数/比例、spawn role、
dispatch/task。按 checkpoint 数（hop proxy）拆分，并计算三组配对差。若 Full 不赢，
检查 hop×dispatch、hop×delta、重复 SQL、证据整合、救援/伤害轨迹；只诊断，不调 prompt。

## 5. 已完成提交、测试与剩余 Git 工作

当前主 worktree HEAD：`7cef5c3`。已有提交：

```text
bd4586a bench: 固化 ExCyTIn 配对清单与官方上下文来源
d365741 bench: 冻结 CAGE publication_v1 资源预算
2e62315 bench: CAGE publication 显式固定 temperature
1e65eae bench: CAGE 顶层决策强制 terminal selector
7cef5c3 docs: 固化长时 benchmark 运行纪律
```

最近 targeted tests：84 passed，命令：

```bash
~/cai_env/bin/python -m pytest \
  tests/test_superagent_runtime.py \
  tests/test_bench_external.py \
  tests/test_benchmark_results.py -q
```

最终还必须运行完整 `~/cai_env/bin/python -m pytest tests/ -q` 与 `git diff --check`。

主 worktree 有以下用户未跟踪文件，必须保留且绝不暂存/删除：

```text
.omc/
cyberorion/kb/data/enterprise-attack.json.download2
cyberorion/kb/data/enterprise-attack.json.part
game_document.pdf
```

剩余提交应至少保持 CAGE performance artifacts 与 ExCyTIn performance artifacts 分离。
不得运行 SecAlert n=600。所有预定提交完成、测试通过后，按用户原始要求推送：

```bash
GIT_SSH_COMMAND="ssh -o BatchMode=yes" \
  git push git@github.com:gry1024/CyberOrion-legacy.git bench-eval:bench-eval
```

最终报告必须覆盖 commits/pushed HEAD、tests、ExCyTIn manifest/judge/scores/deltas/
hop/dispatch/validity、CAGE calibration/budget/seed/validity/deltas/horizon/red/cost/dispatch、
Full underperformance 机制诊断，以及下一项由 traces 支持的架构实验建议。

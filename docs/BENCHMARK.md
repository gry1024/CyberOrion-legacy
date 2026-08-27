# CyberOrion 蓝队 Benchmark

Benchmark 为第三阶段 SUPER-AGENT 提供可复现证据：跨流量、主机、身份和日志
调查，自主工具调用，观察失败后重规划，以及无需人工介入的防御闭环。结果落盘
到 `logs/bench/<run_id>.json`；正式分数只能来自真实运行。

## 1. 证据分层

| 层级 | 套件 | 规模/协议 | 证明什么 |
| --- | --- | --- | --- |
| 外部公开轨 | `malware_analysis` | CyberSOCEval 609 原始题/608 可评分题 | 恶意软件分析；完整答案集 Exact Match + Jaccard；自有 runner 不可与官方榜单直比 |
| 公开认可主榜 | `threat_intel` | CyberSOCEval 588题 | 威胁情报推理；完整答案集 Exact Match + Jaccard |
| 外部公开轨 | `excytin` | ACESEvals YAML task + Docker/Inspect 协议 | 多表遥测调查、证据链、SQL 成本和 adapter native reward；非官方 scorer |
| 外部公开轨 | `cage2` | CAGE Challenge 2 Scenario2 | 3 步长 × 3 红队矩阵和原生 reward；adapter 非榜单提交 |
| 大规模外部轨 | `secalertbench` | 8,322条企业告警 | Macro-F1、Attack Recall、FPR 和规模稳定性 |
| 内部契约轨 | `soc_contract` | 12 个独立 runtime-loop 案例 | 工具失败恢复、证据约束、安全边界；不进入公开主榜 |
| 内部实战轨 | `live_paired` | 多 seed × 3 臂 | 同攻击计划、同初始 snapshot hash、安全重置后的 paired live Docker；显式 harness 才可运行 |
| 工程轨 | `attack_kb` | 内部 KB 派生 | 只验证检索链路，不证明开放世界安全能力 |
| 工程轨 | `cybergym_lite` | 3个修复任务 | 代码修复附录，不作为蓝队主证据 |

不把异构任务合成为一个可调权重的总分。UI 按能力维度展示原生指标，
SUPER-AGENT 的核心结论来自同模型、同样本、同总预算的 `agent - single` 配对差值。

## 2. 运行臂和公平性

- `base`：普通 LLM 或直接策略；
- `single`：拥有全部同源工具的单体有状态 ReAct；
- `agent`：指挥官按需派遣 watcher / analyst / responder / hunter；
- CyberSOCEval 不是交互任务，只比较 `base` 与 `rag`，不伪造团队臂；
- 三臂记录同一预算上限（token 32768、墙钟 300s、LLM 18 次、工具 12 次）；
  base 不使用工具但不会获得更宽的其它预算。provider 未返回 usage 时 token 仅标记为
  estimated，不冒充精确计量；
- 正式 Agent 轨迹必须来自 `superagent_runtime.py` 的真实 decision/tool/role
  事件，固定模板轨迹无效。
- `mode=compare` 在一个父 run 下生成全部臂的子 run，并记录共享模型、n、seed
  与 `agent_minus_reference`、paired bootstrap 95% CI；禁止手工拼接不同样本的成绩。

## 3. 数据体积与代表集

外部数据由 `cyberorion/bench/assets.py` 管理。服务端不会自动联网、下载、解包
或删除文件；资产缺失返回 `503 benchmark_asset_missing`。

- 默认根目录：`benchmarks/external/<suite>`；也可设置
  `CYBERORION_SECALERTBENCH_DIR`、`CYBERORION_EXCYTIN_DIR`、
  `CYBERORION_CAGE2_DIR`；
- `python scripts/setup_benchmarks.py` 只读显示资产状态；
- 单个官方资产超过 1GiB，或已选资产总和超过 5GiB 时，解析前即 fail-closed：
  只读取管理员放在资产根 `representative/` 下的固定种子无损任务子集；没有代表集
  就返回 `benchmark_asset_missing`，不会先把超大 JSON 读进内存，也不会自动下载、裁剪或删除；
- 强制代表集模式下显式 n 不被静默扩大到 daily 默认值：显式 n 小于默认时按
  显式执行（smoke 不会变成默认规模），超过默认时封顶到默认；未指定才用默认；
- 每次运行保存上游 URL、版本、文件 SHA256、完整/子集状态、抽样算法和全部
  样本 ID，以及 `git_head_sha`、提交树 `git_tree_sha`、`git_dirty`；脏树另存
  `git_diff_sha256`。代表集成绩不得标记为官方全量成绩；
- `daily` 使用代表集：SecAlertBench 600、ExCyTIn 64、CAGE-2 9 episodes；
  `publication` 在体积允许时运行目标协议，超限时只使用显式代表集并标记不可比。

CyberGym 安全解包仅允许写入明确 cache 子目录，拒绝当前目录/仓库目录、路径
穿越、软硬链接和设备节点；没有递归删除或 staging 清理逻辑。

## 4. 评分可信度

CyberSOCEval 从 schema v3 起保留上游全部正确选项，完整集合相等才算 Exact
Match，并计算 Jaccard。旧 harness 截断多答案且采用包含式评分，旧结果统一显示
为 `legacy_invalid_gold_v1`，不能与新结果或官方论文比较。

Arena 从 `metrics_version=3` 起：

- ATT&CK 只允许精确编号或真实父子技术关系匹配；
- 响应必须形成 attack→alert→action→effect 关联链：目标与时间匹配、
  `related_alert_id` 指向实际命中告警、动作类型有效、效果为 verified；旧无结构事件只展示不计分；
- 无已验证攻击时 `blue_score=null`，不赠送基础分；
- 蓝队仍严禁读取 attacks 表、场景 ground truth 或 `cyberorion.eval`。

SecAlertBench 额外计算 PR-AUC、Brier、10-bin ECE 和 FN=5/FP=1 的显式成本；
FPR 的分母是全部 gold-benign 行（含预测 unknown 的解析失败行——unknown 不是
真阴性，但仍是实际阴性样本）。模型可见告警在 base prompt 和 `get_alert` 共用
同一递归过滤出口，删除用于 gold 的 `Label/label/ground_truth/verdict/class` 及其它
evaluation-only aliases。当前固定上游 schema 的 19 个字段中只有 `Label` 是评估
字段，其余 18 个是遥测特征。解码温度可通过 `CO_BENCH_TEMPERATURE` 显式配置
（如 `0`），`model_settings` 持久化 `temperature` 与 `temperature_status`
（explicit/provider_default）；未配置时保持 provider 默认行为，绝不声称确定性
解码——样本 seed 与生成设置是两个独立概念。输出上限由
`CO_BENCH_MAX_TOKENS` 控制，默认 8192；同一有效值同时用于 API `max_tokens`
与 `model_settings.max_output_tokens`，非法或非正整数会在模型调用前明确失败。
ExCyTIn 记录证据数、SQL 查询数/成本和 adapter `native_reward`；CAGE-2 记录
Restore 次数、CyberOrion 自定义的非原生 `restore_cost_proxy`（−Restore 次数代理，
`restore_cost_proxy_status="non_native_proxy"`，不是官方 CAGE availability 组件）
与非法动作。CAGE-2 预算耗尽记账：token/调用预算一旦耗尽，后续环境步直接执行
文档化 fallback Sleep（不再反复调用 runtime 制造重复的 LLMBudgetExceeded 无效
决策）；当前步本身若无有效选择且 runtime 表明耗尽，该步即计为 fallback
（CASE A），若已选出有效动作则保留该步动作、只从下一步起 fallback（CASE B）。
逐 episode 持久化 `budget_exhausted_steps`/`budget_exhaustion_reasons`/
`token_budget_exhausted`；实际记账超过声明硬上限（如 after-response token 超限）
记 `budget_limit_violation=true` 与违规维度。episode 全局 18/12/32768 预算已被
证明不适合 30/50/100 步 episode，按环境步预算的方法学提案见
`docs/CAGE_BUDGET_PROPOSAL.md`（待批准，未实施）。ChallengeWrapper 不公开主机
失陷事件数时该字段为 `null` 并附状态，不从标量 reward 反推伪造。

`methodology_status`：`official_compatible` 仅表示 runner/scorer/协议均按上游执行；
`external_track` 表示真实外部数据的适配/代表集；`engineering_only` 表示内部工程
验证；`legacy_invalid_gold_v1` 表示历史方法学不可比。

## 5. 使用方法

```bash
# 只读查看外部资产
~/cai_env/bin/python scripts/setup_benchmarks.py

# CyberSOCEval 同题双臂
~/cai_env/bin/python scripts/run_bench.py \
  --suite malware_analysis --mode both --n 100 --seed 42

# 外部交互套件三臂；缺资产时明确失败，不产生模拟分
~/cai_env/bin/python scripts/run_bench.py \
  --suite excytin --mode compare --profile daily --n 64 --seed 42
```

`live_paired` 默认不可从 Web API 启动。必须在隔离环境由代码显式注入实现
`validate_environment / capture_initial_snapshot / reset_to_snapshot / run_trial`
的审计 harness；每臂 reset 返回的 SHA256 与初始 snapshot 不一致会立即失败。

API：

```text
GET  /api/bench/suites       套件、层级、模式、体积策略与资产状态
POST /api/bench/run          suite/mode/profile/n/seed/dataset_version
GET  /api/bench/runs         历史与运行中结果
GET  /api/bench/run/{id}     完整运行及 provenance
GET  /api/bench/questions    固定 seed 的题目/任务预览
```

正式发布前执行 `~/cai_env/bin/python -m pytest tests/ -q` 和
`cd web && npm run build`。外部套件的引用及许可证以运行产物中的
`benchmark_provenance` 和上游仓库为准。

## 6. 论文/演示结果导出

发布图表不读取 Markdown 报告，也不触碰 benchmark 资产、网络、Docker 或
LLM。先从 `logs/bench/*.json` 生成归一化事实层，再从该层绘图：

```bash
~/cai_env/bin/python scripts/export_benchmark_results.py
~/cai_env/bin/python scripts/plot_benchmarks.py
```

输出为 `results/manifest.json`、`benchmark_summary.{json,csv}`、
`per_task/*.jsonl` 和 `figures/figure{1..6}_*.png`。manifest 记录导出代码 SHA
和每个生成文件的 SHA256。旧 raw run 未持久化的 git SHA、模型设置、资源消耗
等字段保持 `null` 并进入 `completeness.missing_fields`，不会用当前环境猜测回填。

三臂 compare 只有同时满足以下条件才写 publication paired delta：dataset version
与 hash 相同、sample IDs 完全同序、模型名和完整 `model_settings` 相同、seed 相同、
single/agent 公平预算完全相等、每臂 Git provenance 完整并满足
`git_dirty=false`，且各臂实际记账未超过声明的硬上限
（`resource_limits_respected`；预算耗尽并走文档化 fallback 不算违规）。
compare 模式在产生任何结果文件之前捕获一次共享源码
provenance 快照，三臂持久化完全相同的 `git_head_sha`/`git_tree_sha`/
`git_dirty`（`git_provenance_source="compare_shared_source_snapshot"`）；基准自身
写入 `logs/bench/` 的产物不会让后续臂变 dirty。否则 `publication_valid=false`，
paired delta/CI/W-T-L 全部为 null。旧 run 即使存在 `git_commit_sha` 也归入
`historical_incomplete_provenance`，不会进入 publication 聚合。
配对 bootstrap 固定 seed；SecAlertBench 每次重采样后重新计算 macro-F1，其余支持
逐任务原生 reward 的套件计算逐任务差值。

## 7. 当前实验矩阵与安全门

```bash
# SecAlertBench：先 smoke，通过后才运行 final
~/cai_env/bin/python scripts/run_bench.py --suite secalertbench --mode compare --n 30 --seed 42
~/cai_env/bin/python scripts/run_bench.py --suite secalertbench --mode compare --n 600 --seed 42

# ExCyTIn adapter/native exact-match（非官方）：先显式指定经验证的 SQLite 投影
export CYBERORION_EXCYTIN_SQLITE_PATH=/absolute/path/to/telemetry.sqlite
~/cai_env/bin/python scripts/run_bench.py --suite excytin --mode compare --n 3 --seed 42
# n=3 telemetry/trace 全部有效后，至多再跑 n=8；本轮禁止 n=64

# CAGE-2：每个环境步重置同一公平预算；compare 含 Single、
# Orchestrator-only、Full；默认使用已预注册的 pilot_v1 逐步上限
CO_BENCH_TEMPERATURE=0 CO_BENCH_MAX_TOKENS=8192 \
  ~/cai_env/bin/python scripts/run_bench.py --suite cage2 --mode compare --n 9 --seed 42
```

SecAlertBench 显式 verdict 值按首个词前缀归一化（attack/malicious → attack，
benign/non-* → benign，允许解释性后缀，如 `attack (attempted but failed, ...)`、
`attack_attempt_unsuccessful`；`non-attack`/`non_attack` 恒为 benign）；绝不从
任意说明文字推断 verdict，畸形 JSON/空输出仍记 parse_fail。
SecAlertBench 代表集按 gold 类平衡抽样：attack=floor(n/2)、benign=n-attack
（奇数 n 固定把多出的 1 条给 benign），类内再按 `alert_type × enterprise` 固定
种子轮询分层，输出顺序为确定性类交错；源数据缺任一 attack/benign 类或类容量
不足配额时 fail closed，绝不静默改变类比例。真正的全量上游评估（官方资产且
count 覆盖全部可用行）选择全部行、不做类重采样、保留自然类分布，
`selection_policy="full_upstream_no_resampling"`；强制代表目录即使 count 等于
其行数也仍按代表子集类平衡处理。run 保存 `sampling_policy`、
`requested_class_counts`、`selected_class_counts` 和精确有序 selected IDs。
ExCyTIn run 保存 `official_harness_status` 和
`score_methodology_label`；当前 CyberOrion SQLite adapter 的 `native_reward` 是
非官方 exact-match，`official_reward` 保持 null。官方 ACESEvals/ExCyTIn telemetry
是 Inspect/SABER 管理的 MySQL Docker 服务，不是 SQLite；仓库中的
`docker/db/Dockerfile.db` 是 ASCII Dockerfile，绝不能作为数据库。adapter 只接受
显式路径或唯一候选，并验证 SQLite header、`SELECT sqlite_version()`、非空 table
清单后才构造 LLM。没有经验证投影时在首次模型调用前 fail closed。

CAGE-2 每步从实际 `ChallengeWrapper` / `EnumActionWrapper.possible_actions` 读取
Sleep/Monitor/Analyse/Remove/Restore 的安全 action ID，模型选择 `action_id` 后原样
执行该 index；非法 ID 显式降级到真实 Sleep，并在逐步记录同时保存
`requested_blue_action` 与 `executed_blue_action`。CAGE 的 terminal
`select_blue_action` 只授权 Single reference 或 Full commander；specialist 只返回
分析。第一次有效选择立即结束当前环境步，非法选择可在剩余逐步预算内纠正，零次
有效选择才执行一次 Sleep fallback。

Single、Orchestrator-only、Full 使用相同的确定性 bounded episode memory：只保存
已对策略可见的 observation transition、请求/执行动作与 controller 状态；不保存
reward、累计 reward、score、scorer feedback 或隐藏 chain-of-thought。逐步 artifact
保存 exact memory 序列化/hash、provider token（可用时）、估算 token、LLM/tool calls、
wall time、dispatch/roles、预算状态和 fallback；episode 只累计成本并设置线性 runaway
safety ceiling。当前 `diagnostic` profile 是校准上限，不是 publication budget。

`20260827_cage2_publication_v1_*` 原始产物生成于 specialist 动作契约修复之前：
当时无工具、禁止继续派遣的 specialist 仍会看到 `tool|dispatch|complete`，因此其中
Full 的 `DispatchNotAllowed`/`InvalidRole`、额外调用、fallback 与 reward 仅作为
pre-contract-fix 审计和资源校准证据保留，不作为最终 Full 性能证据；原始 JSON 不作
追溯修改。

Live paired 只允许显式本地 runner 和注入的审计 harness：

```bash
~/cai_env/bin/python scripts/run_live_bench.py \
  --local-only-confirmed \
  --harness-factory your_package:build_audited_harness \
  --attack-plan benchmarks/live/plans/credential_lateral_movement.json \
  --attack-plan-sha256 <人工核验的SHA256> \
  --seeds 42,43,44,45,46,47,48,49,50,51
```

runner 不包含通用 Docker reset；环境必须返回 `ok=true, isolated=true`，每臂 reset
必须复现相同 snapshot SHA，trial 必须回报相同 attack-plan SHA，并持久化检测、归因、
遏制、MTTD、处置时间、失陷/爆炸半径、误报、不安全动作、可用性与资源分解指标。

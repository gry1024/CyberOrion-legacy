# ExCyTIn 官方 ACESEvals Comparative Pilot v1

- ACESEvals：`microsoft/ACESEvals@17135140d0fdf52c2264a1fc248cf01e16b23a79`
- CyberOrion：`3787c807020840d7d9ea0bc7652252c3b9f3d8f2`，tree
  `0e3e207c977db6c8c45c73313db5dd04db7149ed`，三臂 clean
- manifest：`benchmarks/manifests/excytin_pilot_v1.json`，12 题，SHA256
  `78afe877e1c15d91a7e20c2cbf16d45d108b01b135dd57268899b9a37fa2de8a`
- dataset 参数：`latest_cleaned_test_set`（官方默认会筛到 train；该参数只修正数据集选择，
  不改变任务内容）
- 模型：`openai/MiniMax-M3`，temperature=0
- judge：`openai/MiniMax-M3`，固定用于三臂；这是 **non-default scorer configuration**，
  不能与上游默认 `openai/azure/gpt-4.1` 发布分数直接比较
- `official_execution=true`，`sqlite_projection_involved=false`

## 官方分数与资源

| arm | saber_overall | submission | aggregate | checkpoint_1…5 | agent LLM calls | tool / MySQL calls | Full dispatch | agent tokens | judge tokens | wall sec | parse/runtime errors | model-error tasks |
|---|---:|---:|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Single | 0.333 (stderr .142) | .333 | .333 | 0 / 0 / 0 / 0 / 0 | 79 | 55 / 48 | 0 | 946,110 | 7,911 | 636.7 | 17 | 5 |
| Orchestrator-only | 0.667 (stderr .142) | .667 | .667 | 0 / 0 / 0 / 0 / 0 | 85 | 59 / 53 | 0 | 1,198,259 | 7,823 | 666.6 | 14 | 0 |
| Full | 0.583 (stderr .149) | .583 | .583 | 0 / 0 / 0 / 0 / 0 | 90 | 59 / 56 | 2/12 tasks, 3 dispatches | 1,829,695 | 8,586 | 787.5 | 15 | 0 |

`tool / MySQL calls` 是官方 `execute` sandbox 调用数及其中包含 MySQL 命令的计数；
tokens 按 Inspect span 分为 CyberOrion agent 与 judge。三份 `.eval` 和 provenance 文件
在本目录归档。

## 评分字段政策

官方 task-level `aggregate` 是 `max(submission, checkpoint_* )`，`saber_overall` 是
所有 task aggregate 的总体聚合；二者是主报告字段。`submission` 与各
`checkpoint_*` 是诊断字段，且都由 `llm_judge` 直接或间接影响。本次所有 checkpoint
均为 0，因此 aggregate 恰好等于 submission；这只是观察结果，不是新增权重或替代 scorer。

## 配对效果（官方 task aggregate）

使用同一 task ID 的配对差和确定性 bootstrap 95% CI，不把 checkpoint 重新加权：

- Full − Single：`+0.250`，CI `[-0.167, +0.583]`，n=12
- Full − Orchestrator-only：`−0.083`，CI `[-0.417, +0.250]`，n=12
- Orchestrator-only − Single：`+0.333`，CI `[+0.083, +0.583]`，n=12

按官方 checkpoint 数（hop/difficulty proxy）的探索性均值：

| checkpoints | n | Single | Orchestrator-only | Full | Full−Single | Full−Orch |
|---:|---:|---:|---:|---:|---:|---:|
| 1 | 3 | .667 | 1.000 | 1.000 | +.333 | +.000 |
| 2 | 1 | .000 | .000 | 1.000 | +1.000 | +1.000 |
| 3 | 4 | .250 | .500 | .500 | +.250 | +.000 |
| 4 | 3 | .333 | .667 | .333 | +.000 | −.333 |
| 5 | 1 | .000 | 1.000 | .000 | +.000 | −1.000 |

hop 组样本很小，不能作稳定的难度结论；没有按分数删题或改 prompt。

## 基础设施/适配公平性审计

- 三臂均为完整官方运行：12/12 sample、同一 manifest、同一实际执行顺序、同一模型、
  temperature、judge、SABER/Docker/MySQL 环境；上游 pinned SHA 和 CyberOrion clean
  provenance 均完整。
- SABER factory 确实把 `instruction_prompt`、`assistant_prompt`、state 的官方 system/user
  内容和官方 `execute` tool 传入 bridge；三臂的 canonical role/content 消息完全一致。
  raw `task_input` hash 因 Inspect 动态 ChatMessage ID 不同而不同，但 ID 不改变语义内容。
- runtime audit 36/36 记录 prompt 存在，`gold_or_scorer_context_added=false`；expected answer
  只出现在 scorer/judge event，不出现在 CyberOrion runtime trace/model-visible payload。
- `single`、`orchestrator_only`、`full` 分别走 `run_reference`、`run_orchestrator_only`、
  `run_superagent`；Full 的 `dispatch_task` 是真实可执行路径，不是模拟字段。角色职责在
  runtime 中明确为 watcher（广域遥测）、analyst（证据关联）、responder（响应）、hunter
  （残余验证）。但 ExCyTIn 只有一个官方 `execute` 工具，角色差异是 prompt-level，
  不是 tool-level least privilege。
- Full 实际只在 2/12 任务 dispatch（均为 3/4-hop：task_40、task_1），全部派到 analyst；
  一个 specialist 达到 role-step budget，另一个先 parse error 后重派成功。故多 agent
  能力已被证明“可运行”，但本 benchmark 中没有发挥 100%：10/12 任务没有 dispatch，
  watcher/responder/hunter 未被使用。
- parse/runtime：Single 有 5/12 model-error trace、17 次 invalid JSON；Orchestrator-only
  0/12 model-error、14 次；Full 0/12 model-error、15 次。Inspect scorer 仍完成全部 sample，
  但这些错误是有效的适配/成本诊断，不应隐藏。

## Validity 与机制诊断

这是一个有效的 paired **非默认 judge** 官方 pilot，适合比较三臂相对差异，不适合声称
与上游 published default-judge 数值可直接比较。Full 相对 Single 为正但 CI 很宽，且相对
Orchestrator-only 为负；两次 dispatch 任务相对 Single 各救回 1 个 aggregate=1，但相对
Orchestrator-only 没有提升。Full 比 Single 多 11 agent calls、4.2 万 agent tokens、约
151 秒墙钟，且增加了 specialist budget/parse 失败链；未观察到重复 MySQL 查询数量暴增。
这支持“当前多角色桥接可执行，但低 dispatch 覆盖和 role budget/JSON 链断裂限制收益”的
机制判断。下一步应结构化记录 specialist recommendation→最终 action→score 的因果链，
以及修复已证实的 dispatch contract/解析问题；在此之前不要针对分数调 prompt。

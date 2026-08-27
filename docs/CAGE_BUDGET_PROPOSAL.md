# CAGE-2 逐步预算协议与校准计划

状态：**PUBLICATION_V1_FROZEN**。旧的 episode 全局预算已退出 CAGE 执行路径；
`pilot_v1` 保持不可发布的校准证据，`publication_v1` 仅依据资源尾部冻结，
未使用 reward/performance 选择上限。

## 1. 证据：现有 episode 全局预算在结构上不适用

旧 n=9 冒烟（`20260826_033455_cage2_compare_n9`，审计 artifact，已因
single 臂 1 局 est_tokens=32919 超过 32768 而不再具备发布资格）显示：

- token 预算（32768）是旧 smoke 中**最先实际触发**的约束：每个 episode
  在第 5–9 个环境步即耗尽；这不表示其它上限充分——按观测到的每步调用量，
  18 次 LLM / 12 次工具的 episode 全局上限即使 token 无限也同样只能覆盖
  episode 的前一小段；
- 每局只有 **4–8 个真实模型决策步**，之后 22–94 步全部退化（旧代码每步
  重复调用 runtime 产生相同的 `INVALID_LLM_DECISION[LLMBudgetExceeded]`；
  当前代码已改为直接文档化 fallback Sleep，但预算模型本身不变）；
- 一次成功的环境决策平均消耗约 **2 次 LLM 调用 + 1 次工具调用**：

| 指标（每成功决策步） | Single（56 步） | Agent（56 步） |
|---|---|---|
| LLM calls：median / p90 / p95 / max | 2 / 4 / 4 / 4 | 2 / 3 / 4 / 5 |
| tool calls：median / p90 / p95 / max | 1 / 2 / 2 / 2 | 1 / 2 / 3 / 4 |
| est tokens：median / p90 / p95 / max | 3973 / 8051 / 8247 / 8301 | 4131 / 6500 / 8828 / 11765 |

（token 为字符估算 len//4；provider usage 未持久化，精确 per-step token
增量不可重建，见 §6。）

结论：`max_llm_calls=18` 与 `max_tool_calls=12` 即使 token 无限，也只能
支撑约 6–9 个环境步（2 调用/步），无法支撑 30/50/100 步的官方
Scenario2 episode。一次性任务的 FAIR_ARM_BUDGET 概念对 CAGE 是错误的作用域。

## 2. 核心模型：按环境步预算（primary fairness scope）

对**每个环境步**：

```
observation
  -> Blue policy 获得一份全新的决策预算
  -> policy 恰好选择一个 Blue action
  -> 环境执行
  -> 下一个环境步获得全新预算
```

- `CAGE_STEP_TOKEN_BUDGET`：每步最大估算 token；
- `CAGE_STEP_MAX_LLM_CALLS`：每步最大 LLM 调用数；
- `CAGE_STEP_MAX_TOOL_CALLS`：每步最大工具调用数。

**每步预算重置**。某一步用满预算绝不永久禁用后续步的策略；该步按文档化
fallback 处理（Sleep + 原因记账），下一步照常获得全新预算。

Episode 级只保留：

- 累计 tokens / LLM calls / tool calls / wall time（成本记账）；
- 一条**线性安全上限**（防失控，非公平性范围，见 §4）。

## 3. 公平性规则

Single 与 CyberOrion 在每个环境步获得**相同的资源上限**
（相同 max tokens / max LLM calls / max tools / wall-time 上限），
不要求相同的实际用量。

- Single：reference 在本步预算内结合当前观测与共享 episode memory 选动作；
- CyberOrion：orchestrator 可先派发 specialist 分析，再由 orchestrator 选动作；

CyberOrion 多花的预算是架构取舍的一部分，作为成本如实上报，
不因用量不同而作废比较。

## 4. Episode 级安全上限（runaway protection，非主预算）

```
episode_token_safety_ceiling = environment_steps × CAGE_STEP_TOKEN_BUDGET
episode_llm_calls_safety_ceiling = environment_steps × CAGE_STEP_MAX_LLM_CALLS
episode_tool_calls_safety_ceiling = environment_steps × CAGE_STEP_MAX_TOOL_CALLS
episode_wall_time_ceiling = environment_steps × per_step_wall_ceiling
```

线性上限只是失控保护：一个"每步都合法但用满预算"的 episode 不应触发它。
触发安全上限时 episode 以明确的 `episode_safety_ceiling_reached` 状态终止
并记账，不与正常 fallback 混淆。

## 5. 数值校准边界

旧轨迹混入了两个尚未修正的结构性成本：成功选择动作后的冗余
`task_complete` 调用，以及每步重建 runtime 且缺少完整 episode memory。
因此不能直接用旧分位数宣布最终 per-step 上限，也不在本提案中选择 pilot 数值。

批准语义并实现 §7–§8 后，先用不参与最终报告的校准运行记录 per-step provider
usage（可用时）与估算值，再预注册预算。校准原则是：

- 正常 Single 决策应有余量，不因一次格式修复就系统性 fallback；
- CyberOrion 应能完成一次有用的 specialist 分析，但额外消耗仍是架构成本；
- 三个上限分别校准、分别审计，不能因为 token 最先触发就忽略 LLM/tool-call
  上限的结构充分性；
- per-step 上限与 episode 线性安全上限必须在正式比较前冻结，不能按结果调参。

## 6. 需要新增的 instrumentation

现有 artifact 无法重建精确 per-step token 增量（只有字符估算与
episode 累计）。新 CAGE 实现必须：

1. 每次 LLM 调用持久化 provider `usage`（prompt/completion tokens，可用时）
   与字符估算并列，`usage_accounting=provider` 或 `estimated` 明确标注；
2. 每个环境步持久化该步的 tokens/LLM calls/tool calls 增量
   （`step_resource_usage`），而不是只有 episode 累计；
3. 每步持久化 `step_budget`（本步上限）、`step_budget_status`
   （ok / exhausted / violation）与 fallback 原因。

## 7. 终端 select_blue_action 语义（已实现 B）

现状：runtime 在 `select_blue_action` 成功后通常还会再发一次 LLM 调用
输出 `task_complete` —— 对环境而言是冗余的（环境此刻已拿到动作）。

实现前比较过两种方案：

- **A：任一授权角色调用即终止整个环境步。** 这要求 specialist 深层调用能
  设置全局选中动作并跨 `_run_role` 递归栈向上短路；否则它只会完成 specialist，
  orchestrator 仍可能再次选动作。实现需要共享 terminal 状态/专用异常或逐层
  传播协议，并处理重复选择，扩大了通用 runtime 的隐式控制流。
- **B：specialist 只能返回分析，reference/orchestrator 独占最终选择权。**
  specialist 从可用工具集合中移除 `select_blue_action`；它完成后把可审计分析
  返回 orchestrator。reference 或 orchestrator 对当前 canonical action table
  成功调用 terminal `select_blue_action` 后，顶层 runtime 立即结束本环境步。

**采用 B。** 权限边界与控制流一致：reference 是 Single 的唯一决策者，
orchestrator 是 Agent 的唯一决策者；specialist 只提供建议。这样无需跨递归栈的
全局终止信号，也能明确保证每步至多一次成功选择。

实现边界：

1. `ToolSpec` 增加 `terminal: bool = False`；CAGE 的
   `select_blue_action` 标为 terminal，其它套件行为不变；
2. CAGE `role_tools` 对四个 specialist 不授予该工具，只让 reference 与
   orchestrator 可见；任务提示同步说明 specialist 不得决定动作；
3. handler 只有在 action ID 属于**本步** canonical safe set 且尚未选择时才返回
   `ok`；成功后 runtime 记录正常 tool trace/budget，立即返回 complete，不再请求
   `task_complete`；非法/重复选择返回 error 并允许在剩余本步预算内修正；
4. 环境适配器只接受该唯一选择；没有成功选择时才执行文档化 Sleep fallback，
   并区分 absent/invalid/budget-exhausted 原因。

“成功 terminal 调用立即结束”给出 exactly-one：第一次成功写入唯一动作槽后不再
进入任何 LLM/工具轮；失败调用不写槽；零次成功则由适配器恰好执行一次 fallback。

## 8. Episode 状态与公平记忆（已实现）

当前 `cage2.choose()` 每个环境步都调用全新的 `run_reference` / `run_superagent`；
runtime 消息历史不会跨步保留。传入的 `recent_actions` 只含最近最多 8 个**请求**
动作，且没有 executed action、reward、非法原因或上一步 observation。

上游 `ChallengeWrapper` 的 observation 并非完整 episode 历史。其
`BlueTableWrapper.blue_info` 会保留部分已观测的 compromise 状态（直到
Restore/Remove 改写），但 Activity 来自当前步相对 baseline 的异常，可能下一步
消失；最终 `OpenAIGymWrapper` 只返回固定长度向量。仅靠当前向量无法恢复：

- 当前 step index 与剩余 horizon；
- 上一步模型请求了什么、环境实际执行了什么；
- 非法动作与 fallback 原因；
- 已不再出现在当前向量中的早期 Activity/observation 变化。

因此 Single 当前不是预期的有状态 ReAct baseline，两个 LLM 臂都需要同一份共享、
有界、可审计的 episode memory 输入。建议由环境适配层在每次 `step` 后确定性追加
公开 transition，不让任一 arm 自行总结：

```json
{
  "memory_schema": "cage_observable_transition_v1",
  "episode": 1,
  "current_step": 12,
  "recent_transitions": [{
    "step": 11,
    "observation_before": "<确定性截断/编码的公开向量>",
    "requested_blue_action": {},
    "executed_blue_action": {},
    "controller_status": "selected",
    "fallback_reason": null,
    "valid": false,
    "invalid_reason": "...",
    "done": false
  }],
  "omitted_prefix_count": 0,
  "omitted_prefix_sha256": null
}
```

窗口固定为最近 12 条 transition，单条 observation 12000 字符、完整 memory
32000 字符，均采用确定性截断；超过窗口/字符上限时记录 omitted-prefix count/hash，
不用 LLM 生成摘要。每步 artifact 保存 exact serialized memory、schema 和 hash。
Single、Orchestrator-only 与 Full 对相同 history 接收字节一致的 memory。

模型可见内容只含 step/horizon、旧的模型可见 observation、requested/executed action、
controller fallback/invalid 状态和 done。**reward、cumulative reward、final score、
scorer/evaluator feedback 只留在评分 artifact，绝不进入 memory 或 prompt**；递归净化
还会剥离意外嵌入 observation 的 evaluator-only 字段。隐藏 chain-of-thought 不持久化。

## 9. 成本报告计划

未来 CAGE 结果表不再只报 native reward：

- 性能：native cumulative reward（官方语义不变）；
- 成本：total tokens、total LLM calls、total tool calls、wall time、
  tokens/环境步、calls/环境步；
- 架构结论按"性能增益 vs 额外推理成本"解读；
- **不构造任何加权合成分数。**

## 10. 状态

- [x] per-environment-step 公平预算；每步独立 meter，耗尽仅 fallback 当前步
- [x] B 方案 terminal selector；成功选择后不再调用 `task_complete`
- [x] 共享 observable-transition memory；无 evaluator reward/score
- [x] 1.25×线性 episode runaway ceiling（不是公平性预算）
- [x] Single / Orchestrator-only / Full 三种架构臂
- [x] 每步 provider/估算 token、calls/tools/wall、dispatch、memory 与 fallback 审计
- [x] 用三种 30-step 条件做真实诊断校准并冻结 `pilot_v1` 上限：
      16384 tokens / 4 LLM calls / 3 tools / 2 dispatches / 60s per step
- [ ] 在冻结上限下运行 9 条 canonical condition 的 Single / Full pilot

## 11. calibration_v2 与 publication_v1（2026-08-26）

校准输入是旧 `pilot_v1` 三臂全部 30/50/100 horizon × 三种 red-agent 的
1,620 条逐环境步 trace。校准程序只读取
`step_resource_usage` / dispatch 字段，未读取 episode reward 或 condition mean。
机器可读分布见 `logs/bench/20260826_cage2_calibration_v2_resources.json`。

跨臂观测最大值为：provider tokens 18,288、LLM calls 4、tools 2、dispatches 2；
正常完成步 wall time 最大 35.89s，另有一次 60.04s 的旧 60s timeout 边界事件。
因此冻结三臂完全相同的 `publication_v1`：24,576 tokens / 5 LLM calls /
4 tools / 3 dispatches / 4 role steps / 90s。token 相对最高观测值留 34.4%
headroom；其它离散资源均高于观测最大值；90s 把旧 timeout 边界与正常尾部
分开。该 profile 在任何新性能 seed 运行之前提交，之后不得按结果调整。

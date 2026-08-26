# CAGE-2 预算方法学提案（待批准，尚未实现）

状态：**PROPOSAL_ONLY** —— 本文件只提出设计；在得到批准前不修改任何预算数值，
不重跑 CAGE。当前代码仍使用旧的 episode 全局 FAIR_ARM_BUDGET
（18 LLM calls / 12 tool calls / 32768 tokens），并已如实记录其不足。

## 1. 证据：现有 episode 全局预算在结构上不适用

旧 n=9 冒烟（`20260826_033455_cage2_compare_n9`，审计 artifact，已因
single 臂 1 局 est_tokens=32919 超过 32768 而不再具备发布资格）显示：

- token 预算（32768）是唯一紧约束：每个 episode 在第 5–9 个环境步即耗尽；
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

- Single：1 次 LLM → 选动作；
- CyberOrion：orchestrator → 派发 analyst/… → 选动作；

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

## 5. 建议 pilot 预算（待评估，非最终值）

候选：**8192 tokens/步、4 LLM calls/步、3 tool calls/步**。

与现有痕迹对照：

- **LLM calls=4/步**：Single p95/max=4 ✓ 恰好覆盖；Agent p95=4、max=5，
  极端步超 1 次。若先实现 §7 的终端 `select_blue_action` 语义
  （成功选择立即终止，省掉后续 task_complete 调用），典型消耗降到
  1–2 次/步，4 的余量充足。**未实现终端语义前，建议 pilot 用 5。**
- **tool calls=3/步**：Single max=2 ✓；Agent max=4（1 个离群步）。
  终端语义下工具调用降到 1 次/步。3 可作为 pilot，但需记录离群步。
- **tokens=8192/步**：Single p95=8247 略超；Agent p95=8828、max=11765
  明显超过。**在终端语义实现前 8192 偏紧**；两个选择：
  (a) 先实现终端语义再评估 8192；
  (b) 未实现终端语义的 pilot 用 12288（12k），覆盖 Agent p95 并留
     一次重试空间。

原则校验：

- 正常 Single 决策（median ~4k tokens、2 调用）应舒适落入；
- CyberOrion 应有空间做一次有用的专家派发（Agent p95 ~8.8k tokens）；
- 一次畸形响应/重试不应立刻杀死该步（上限按 p95+1 次调用余量设计）；
- 失控编排仍会被每步上限 + episode 线性上限双重兜底。

## 6. 需要新增的 instrumentation

现有 artifact 无法重建精确 per-step token 增量（只有字符估算与
episode 累计）。新 CAGE 实现必须：

1. 每次 LLM 调用持久化 provider `usage`（prompt/completion tokens，可用时）
   与字符估算并列，`usage_accounting=provider` 或 `estimated` 明确标注；
2. 每个环境步持久化该步的 tokens/LLM calls/tool calls 增量
   （`step_resource_usage`），而不是只有 episode 累计；
3. 每步持久化 `step_budget`（本步上限）、`step_budget_status`
   （ok / exhausted / violation）与 fallback 原因。

## 7. 终端 select_blue_action 语义（可行性分析与最小实现设计）

现状：runtime 在 `select_blue_action` 成功后通常还会再发一次 LLM 调用
输出 `task_complete` —— 对环境而言是冗余的（环境此刻已拿到动作）。

可行性：`superagent_runtime._call_tool` 已支持工具返回值与 observation
回填；`complete` 分支只是返回 summary。工具成功后立即以"工具选择即完成"
结束当前 runtime 调用是安全的本地位改动。

最小实现设计（CAGE 专用，不动全局 ToolSpec/runtime）：

- 给 `ToolSpec` 增加可选 `terminal: bool = False`（默认 False，全局行为
  不变）；或引入 CAGE 专用的 `TERMINAL_TOOLS={"select_blue_action"}` 集合
  由 cage2 传入 runtime；
- runtime 在 `_call_tool` 成功（status=="ok"）且工具为 terminal 时，
  直接返回 `("complete", f"selected action ...")`，不再发起下一轮 LLM；
- 审计不变：tool_calls 记录该次调用，预算照常记账。

预期收益：每成功决策步从 ~2 调用/1 工具降至 ~1 调用/1 工具，
pilot 预算（8192/4/3）的余量显著变大。**本方案不改变任何非 CAGE 套件
的行为；是否实施待批准。**

## 8. 成本报告计划

未来 CAGE 结果表不再只报 native reward：

- 性能：native cumulative reward（官方语义不变）；
- 成本：total tokens、total LLM calls、total tool calls、wall time、
  tokens/环境步、calls/环境步；
- 架构结论按"性能增益 vs 额外推理成本"解读；
- **不构造任何加权合成分数。**

## 9. 状态

- [ ] 待批准：per-step 预算模型与数值（pilot 候选见 §5）
- [ ] 待批准：终端 select_blue_action 语义
- [ ] 待批准：episode 线性安全上限
- [x] 已实现：预算耗尽/违规的如实记账与 publication 校验
      （`budget_exhausted_steps`、`budget_exhaustion_reasons`、
      `token_budget_exhausted`、`budget_limit_violation`、
      `resource_limits_respected`）

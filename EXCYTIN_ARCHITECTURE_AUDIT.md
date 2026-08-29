# ExCyTIn × CyberOrion 架构审计

审计日期：2026-08-28<br>
审计阶段：代码修改前，只读审计<br>
CyberOrion：`60438fbd0ea7dbdba71a0da13241e11c8af18a37`，tree `eb89fc8071b2794a81ea6154a515dc2a304fb78c`<br>
ACESEvals：`17135140d0fdf52c2264a1fc248cf01e16b23a79`，tree `4d5dea0db0073189210d5cdf1d56eab86780ce0e`<br>
SABER/ACES：`a9bdce1343fd1c331aafda3119cbf0d48f215382`（`uv.lock`）<br>
Inspect fork：`f94a85b6b3a246d2e4417b49bdda96fd8f04b93a`（`uv.lock`）

## 审计结论

当前 `excytin_official_agent.py` 保留了官方 Task、Docker/MySQL 环境、YAML prompt、工具来源和 scorer，但没有忠实保留 CyberOrion 的生产多代理架构，也没有忠实使用 Inspect 的原生工具执行语义。它把官方工具转换成 `ToolSpec`，再通过 `superagent_runtime.py` 的第二套 JSON action loop 直接调用 Python handler。这会绕过 Inspect 的原生 schema 校验、approval、tool-call limit、工具消息轨迹和 scorer 所读取的 `TaskState.messages`，因此桥协议本身可能决定得分。

架构有效性门目前为 **FAIL**。在改为原生 Inspect 工具循环、补齐强三臂、共享调查状态、结构化报告、并发派遣和全局计量之前，不得启动性能实验。

## 官方执行路径

固定上游的实际路径是：

1. `domains/excytin/excytin.py::excytin` 原样调用 `saber.task.create_task`；
2. SABER 读取官方 YAML、Jinja prompt、每题 `max_steps`、工具和 scoring；
3. `ToolRegistry` 将每题声明的 `bash`、`python` 等工具解析成原生 Inspect `Tool`；
4. `create_saber_solver` 在 sample 运行时注入官方 instruction/assistant prompt、完整 task metadata 和官方工具；
5. 官方 `react` agent 通过 `get_model().generate(..., tools=...)` 与 `execute_tools(...)` 运行；
6. Inspect 在 sample 根作用域累计模型 token、tool calls 和时间，工具调用写入消息与 transcript；
7. SABER scorer 从最终 submission 和 `TaskState.messages` 中的原生工具轨迹执行官方 scorer，并按官方配置聚合。

Headline 路径必须继续走以上 Task、sandbox、tools 和 scorer。`cyberorion/bench/excytin.py` 的 SQLite projection 与 exact-match 只可保留为明确标注的非官方 legacy adapter。

## 1. 当前保留了哪些生产 CyberOrion 部分

- 有 `single`、`orchestrator_only`、`full` 三个注册名称，并由同一 SABER agent factory 入口创建；
- Full 暴露 watcher、analyst、hunter、responder 四个角色名称；
- `superagent_runtime` 有共享的步数、LLM call、tool call、dispatch 计数器；
- reference 的工具集合与 Full 团队工具并集有静态一致性检查；
- dispatch、工具调用和角色事件生成可审计 trace；
- 官方 task、官方 Docker/MySQL 环境和官方 scorer 没有被源码替换；
- 桥只组合 SABER 已提供的 instruction、assistant prompt 与 task input，没有主动加入 gold/scorer 内容。

这些是外形和部分安全性质，并不足以证明生产架构被忠实评估。

## 2. 当前丢失或失真的部分

### 2.1 双重且冲突的模型协议

官方 prompt 要求 Thought/Action 并允许原生工具调用；桥又要求每一步只返回自定义 JSON decision。模型同时看到两种控制面，且桥把官方 prompt、历史、工具 schema 再次序列化进一个新的 user prompt。这不是官方 React/SABER 的交互语义。

### 2.2 官方工具被旁路执行

`_tool_spec()` 把 Inspect Tool 降格为普通 handler；`superagent_runtime::_call_tool()` 直接调用 handler。该路径没有经过 Inspect `execute_tools()`，从而可能绕过：

- 官方工具 schema 解析与参数校验；
- approval/security policy；
- sample 根 `tool_call_limit`；
- Inspect ToolEvent、ChatMessageTool 和 transcript；
- SABER scorer 的原生 trajectory extraction。

这是当前最严重的架构有效性问题。

### 2.3 Full 只是串行嵌套 JSON loop

- 子代理只收到 commander 手写的 `mission`；
- 不自动收到官方任务上下文、当前证据、先前工具结果或共享状态；
- dispatch 永远串行执行；
- 没有独立会话级共享 investigation notebook；
- 没有结构化 specialist report schema 或 empty/parse/budget/tool-failure分类；
- 四个角色 prompt 只是简短职责句，不是生产角色语义的 ExCyTIn 适配；
- commander 不具备生产中的团队组织、证据汇总、复核工作流。

### 2.4 Single 与 orchestrator-only 过弱

`reference` 只有一句泛化身份说明，没有 ExCyTIn schema discovery、多表关联、假设检验、证据引用和最终核验 SOP。Orchestrator-only 同样没有强调查 prompt。它们不是强基线。

### 2.5 资源计量不完整且限制可能人为绑定

- 自定义计数器统计 JSON loop 次数，不等价于 Inspect 的真实轨迹；
- provider token 没有进入自定义全局硬预算；
- `max_role_steps=6`、`max_observation_chars=4000`、消息截断 12000 字符会压缩多表调查；
- 子调用绕过 Inspect 根 tool counter；
- 当前没有并发情况下原子计量或防止并发越界的证据。

### 2.6 scorer 可见轨迹失真

SABER scorer 从 `TaskState.messages` 提取工具步骤。当前桥把整个自定义 runtime 结果压成一个最终 `ModelOutput`，官方工具执行不会以正常 Assistant → Tool 消息对出现在 scorer 输入中。trajectory/checkpoint 分可能主要反映桥的缺失，而非调查能力。

## 3. 可以删除哪些自定义桥层

官方 ExCyTIn 路径应删除：

- Inspect Tool → `ToolSpec` 的 `_tool_spec` 转换；
- `build_official_context` 的 JSON 序列化作为模型任务输入（可保留 hash audit，但不改变原始 prompt）；
- 自定义 `llm(system, user)` 二次拼 prompt；
- `run_reference` / `run_orchestrator_only` / `run_superagent` 的 JSON action parser；
- 自定义 tool/dispatch/complete 虚拟 schema；
- 自定义 tool handler 直接执行；
- 固定 4000/12000 字符的观察与上下文截断。

`superagent_runtime.py` 可继续服务 CAGE、SecAlertBench 或 legacy adapter，但不应再被官方 ExCyTIn agent import。

应保留的薄桥职责只有：

- 在 SABER registry 中注册三臂；
- 为三臂组合不同的 agent prompt 与 dispatch 能力；
- 将 SABER 已解析的原生 Inspect Tool 对象原样传给 agent；
- 维护不含隐藏环境事实的共享 investigation state；
- 记录角色、报告、dispatch 与资源审计信息；
- 把最终 agent output 原样交回官方 Task/scorer。

## 4. 是否能使用原生 Inspect/SABER 工具执行

**能，而且必须这样做。** 固定 Inspect fork已经提供 `react()`、`execute_tools()`、sample 根 limit tree、agent/handoff/run primitives、原生 usage 和并行安全工具执行。

拟采用的桥架构：

- Single：原生 `react` + 全部官方工具 + 强 ExCyTIn 单体调查 SOP；
- Orchestrator-only：原生 `react` + 相同官方工具并集 + 与 Full commander 相同的规划/证据 SOP，但无 dispatch；
- Full：原生 commander `react` + 全部官方工具 + 四个 native dispatch tools；每个 dispatch 启动隔离的 specialist `react`，specialist 仍直接使用同一批官方 Tool 对象；
- dispatch tool 只承担协调，不代理、改写或重实现环境工具；
- dispatch tool 标记为可并行，Inspect 仅在模型同一轮自然请求多个独立派遣时并行；不强制 dispatch 或并行；
- specialist 必须调用结构化 report submit tool。报告作为 dispatch 的原生 Tool 输出完整返回 commander，并写入共享状态；
- 子工具轨迹保留 Inspect transcript；报告包含实际命令、证据与来源，供官方 trajectory scorer读取。

如果测试发现普通 dispatch tool 无法让官方 trajectory scorer看到足够的子工具证据，应优先使用 Inspect 原生 handoff 的消息拼接语义或显式、无损地把子对话消息合并回 commander；不得回退到平行 JSON loop。

## 5. 子代理工具与 LLM 使用如何全局计量

采用两层互相核验的全局计量：

1. **硬限制层**：全部 commander/child 调用运行在同一个 Inspect sample context 下。根 `token_limit`、`time_limit` 和 `tool_call_limit` 统一累计；子任务继承根节点，不能用每角色私有预算绕过总 ceiling。
2. **审计层**：sample-scoped ledger 对 commander/child model call、官方 tool call、dispatch、provider tokens、角色和时间做并发安全记录，并与 Inspect log 对账。

硬限制以 Inspect 原生根计量为权威。每角色只允许有防失控的宽松局部 safety cap，且必须通过 resource-only calibration 证明不绑定。需要测试证明两个并发 child 的 usage 都进入同一个根计数；如果 Inspect 没有 model-call hard limit，薄桥可增加一个并发安全的全局 model-call counter，但不得改变工具执行或 prompt 语义。

## 公平性与信息边界

- 三臂收到同一官方 instruction、assistant prompt、task input 和官方工具集合；
- Single 官方工具集合等于 Full commander/团队官方工具并集；
- Full 的额外工具只能是 delegation/report coordination，不得提供环境事实；
- shared state 只记录官方 task context、公开假设和官方工具输出；
- shared state 禁止存储 target、gold、scorer 配置、judge prompt、隐藏数据库状态；
- specialist 自动收到 task context、mission、shared state snapshot、相关先前证据与官方工具；
- responder 只做核验、反方审查和有证据支持的修复建议，不宣称执行 ExCyTIn 未暴露的处置动作。

## 拟定共享状态 schema

```json
{
  "task_context_sha256": "...",
  "discovered_schema": [],
  "executed_commands": [],
  "evidence": [],
  "hypotheses": [],
  "unresolved_questions": [],
  "specialist_reports": [],
  "provenance": []
}
```

每条记录包含稳定 ID、来源角色、来源类型、原始 tool-call ID/命令、时间顺序和是否截断。状态更新来自已存在的消息/tool output 或结构化报告，不调用额外 LLM，不读取 sandbox 内部对象。

## 拟定结构化报告 schema

```json
{
  "role": "watcher|analyst|hunter|responder",
  "findings": [],
  "evidence": [{"claim": "...", "source": "...", "snippet": "..."}],
  "commands_or_queries": [],
  "confidence": "low|medium|high",
  "uncertainties": [],
  "recommended_next_investigation": [],
  "candidate_answer_implications": []
}
```

运行时分别计数 `successful_report`、`empty_report`、`parse_failure`、`role_budget_exhaustion` 和 `tool_failure`。空报告不能作为成功派遣写入 notebook。

## 角色语义适配

- watcher：宽范围 schema discovery、表清单、字段/时间范围与遥测概览；
- analyst：关联多表证据、重建事件链、验证竞争假设；
- hunter：针对性深挖相关或残留证据，扩展实体/IP/账户/主机/时间窗；
- responder：最终事实核验、反方审查、遗漏检查，并仅在任务证据支持时提出 remediation 建议。

这些语义来自生产 `blue_team.py` 的巡检、研判、狩猎、处置边界，但移除 Docker 主机名、iptables、文件删除等与 ExCyTIn 无关的生产指令。

## 修改前门禁状态

| 项目 | 当前状态 |
| --- | --- |
| 官方 Task/环境/scorer 未替换 | PASS |
| 无主动 gold/scorer 注入 | 部分 PASS，需更强证明 |
| Single 完整官方工具 | 名义 PASS，执行路径 FAIL |
| Full 团队工具并集等于 Single | 名义 PASS |
| 强 Single / Orch-only | FAIL |
| 四角色忠实语义 | FAIL |
| specialist 自动收到上下文/证据/state | FAIL |
| specialist 原生官方工具执行 | FAIL |
| commander 完整收到结构化报告 | FAIL |
| 独立/并行派遣 | FAIL |
| child usage 全局硬计量 | FAIL |
| 无正常调查截断 | FAIL |
| parser/contract error = 0 | FAIL：存在第二套 parser/contract |
| Full 无额外隐藏信息 | 尚未完整证明 |

结论：`EXCYTIN_ARCHITECTURE_VALIDITY = FAIL`。下一步只能实现和测试上述薄桥；通过 mandatory validity tests 与小规模真实模型机制/资源校准后，才可提出 publication budget。昂贵性能运行必须等待用户下一条指令。

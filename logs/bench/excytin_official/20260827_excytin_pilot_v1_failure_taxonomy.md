# ExCyTIn pilot：Full 机制诊断

本诊断只读取官方 `.eval` runtime trace，不据此调 prompt 或改变 scorer。

- Full 在 12 题中仅 2 题 dispatch（`incident_134...task_40`，3 checkpoints；
  `incident_322...task_1`，4 checkpoints），共 3 次，全部为 `analyst`。两题相对
  Single 都从 aggregate=0 变为 1，但相对 Orchestrator-only 都是 0→1 的同分，未
  产生额外收益；5-checkpoint 题没有 dispatch。
- 第一个 specialist 运行达到 `role_steps=6` budget；第二题一次 analyst JSON parse
  error 后重派才完成。三臂均没有 scorer/runtime sample 失败，但 Single 有 5/12
  runtime `model_error`（17 次 invalid JSON），Orchestrator-only 0/12（14 次），
  Full 0/12（15 次）。
- Full agent 为 90 次 LLM call、59 次官方 tool call、1,829,695 provider tokens、
  787.5 秒；Single 为 79/55/946,110/636.7，Orchestrator-only 为 85/59/1,198,259/
  666.6。Full 的额外调用/成本没有转化为相对 Orchestrator-only 的分数收益。
- `execute` 是 ExCyTIn 唯一官方工具；三臂工具调用均能落到真实 sandbox/MySQL，Full
  没有出现 SQL 工具不可用或重复查询爆炸。角色分工由 watcher/analyst/responder/
  hunter 的 system prompt 明确，但没有 tool-level 权限差异。
- 结论：Full 的多 agent 路径在 Bench 中真实可执行，但触发覆盖低，且 role budget、
  invalid JSON 和 specialist→final-action 的结构化衔接限制了收益。当前证据不足以
  声称专家建议导致所有差异；下一步应先结构化记录 recommendation→final action→score
  和失败救援/伤害，再做受控架构实验，不做成绩导向 prompt 调参。

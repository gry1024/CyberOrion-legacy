# ExCyTIn batch 001 Full 失败分析

> **INTERIM — NOT FINAL INFERENCE**
>
> 本文只分析预冻结 `batch_001` 的 12 个配对任务，用于后续架构开发。
> 不得把本文当作完整 ExCyTIn 结论，也不得据此删除或重选正式样本。

## 1. 实验身份

- CyberOrion 执行 HEAD：`6de7debd8452690c3f2c57684f9300c9652d7e13`
- CyberOrion 执行 tree：`54e7b0cf987ab93a1632c8b7eb252945d286008f`
- ACESEvals commit：`17135140d0fdf52c2264a1fc248cf01e16b23a79`
- ACESEvals tree：`4d5dea0db0073189210d5cdf1d56eab86780ce0e`
- SABER commit：`a9bdce1343fd1c331aafda3119cbf0d48f215382`
- batch manifest SHA-256：
  `0950acf37be0b453ac95cb266bfcb7d28cb1aadbb344c68abb8c7f6232589d78`
- 模型与 judge：`openai/MiniMax-M3`
- temperature：`0`
- thinking：`disabled`
- 三臂共同上限：32 model calls、64 root tool calls、320000 provider
  tokens、240 秒、8 dispatches、4 parallel dispatches；官方 task
  `max_steps=25` 保持不变。

三臂均完成 12/12 个官方 ExCyTIn scorer 样本，task ID 和 manifest 顺序
一致。没有 SQLite projection、重复任务、`DispatchNotAllowed`、
`InvalidRole` 或 `ToolNotAvailable`。

## 2. Interim 配对结果

| 比较 | paired mean delta | paired bootstrap 95% CI |
|---|---:|---:|
| Full − Single | -0.1667 | [-0.4375, 0.1250] |
| Full − Orchestrator-only | -0.2500 | [-0.4792, -0.0625] |
| Orchestrator-only − Single | +0.0833 | [0.0000, 0.2500] |

三臂平均官方 `saber_overall` 分别为：Single `0.7500`、
Orchestrator-only `0.8333`、Full `0.5833`。

## 3. 主要失败机制：worker 耗尽共享预算

Full 相对 Single 落后的五个任务中，有四个触发了 Full 的全局资源限制：

| 任务 | Full 终止状态 | final submit | Full − Single |
|---|---|---:|---:|
| incident_134 task_1 | token 320000 | 无 | -1.00 |
| incident_38 task_2 | custom 32 model calls；同时记录 token event | 无 | -1.00 |
| incident_55 task_1 | custom 32 model calls | 无 | -0.25 |
| incident_5 task_1 | token 320000 | 无 | -0.25 |
| incident_39 task_2 | 未触顶 | 有，但答案未得分 | -0.50 |

四个受限任务对 Full − Single 的总贡献为 `-2.5`；其余八个未受限任务
的总贡献为 `+0.5`。全部十二题合计为 `-2.0`，即均值 `-0.1667`。
这个分解仅用于诊断，不得把排除受限任务后的均值当成新的正式结果。

当前 Full 的 commander 与所有 workers 共用同一个硬模型调用计数器。worker
没有独立硬额度，提示中的“约 20 次工具调用”只是软目标。失败轨迹显示：

- incident_134 的 triage 执行 27 次 bash 后耗尽 token；
- incident_38 task_2 的 triage 执行 30 次 bash 后触及全局上限；
- incident_5 task_1 的 triage 执行 26 次 bash 后耗尽 token；
- incident_55 先得到 triage 报告，随后 escalation 继续调查并耗尽剩余
  32-call 全局预算。

`LimitExceededError` 会直接离开 dispatch 路径。因此 worker 已经执行的工具
结果仍保留在 Inspect 原始审计日志中，但不会经过正常的 shared-state ingest、
structured report 和 commander synthesis，最终也没有 `submit`。这形成了
“一个 worker 触顶，整个样本归零”的资源悬崖。

## 4. Full 的调查成本膨胀

| arm | solver model calls/题 | solver tools/题 | provider tokens/题 | wall time/题 |
|---|---:|---:|---:|---:|
| Single | 12.42 | 13.67 | 117230 | 66.62s |
| Orchestrator-only | 10.25 | 10.25 | 69551 | 57.91s |
| Full | 22.00 | 24.83 | 212001 | 122.10s |

Full 共执行 255 次 bash，Single 为 152，Orchestrator-only 为 111。Full
的 255 次 bash 中只有 4 次是完全相同的重复命令，说明主要问题不是字面重复，
而是调查分支过宽、worker 未及时收敛。

当前 frozen ceiling 的实际绑定情况：

- Single：1 个 token-limit 样本；
- Orchestrator-only：0 个限制样本；
- Full：4 个限制样本，其中 2 个最终 token marker、2 个最终 custom=32
  model-call marker；
- Full 最大 solver tool calls 为 43/64，最大 wall time 为 168.953/240s，
  最大单样本 dispatch 数为 2/8；这些维度未绑定。

## 5. 角色使用没有形成稳定的多角色分解

Full 的 17 次 dispatch 为：

- triage：12；
- threat_hunter：4；
- escalation：1；
- lateral_analyst：0。

正式 batch 中没有观察到真正的并行派遣。多数任务实际上是：

```text
commander -> 一个长时间运行的 triage -> report -> submit
```

因此 Full 经常只是把 monolithic investigation 移入 triage，再叠加 commander、
shared-state 和 report 序列化开销，没有稳定获得并行调查、多角色交叉验证或
更好的停止行为。

## 6. Report 接口有瞬时 schema 失败

Inspect 原始事件中共有 8 个 Full ToolCallError：

- 4 个 `type=limit`：worker 资源耗尽；
- 4 个 `type=parsing`：结构化 report 的首次参数不符合 schema，随后重试成功。

因此 13 次 triage report 工具调用中，9 次为有效报告，4 次为已恢复的解析
失败；4 次 threat-hunter report 均有效。运行时 `report_counts.parse_failure=0`
仅表示没有最终未恢复的 report parse failure，不代表从未发生 report schema
重试。没有发现模型可见 contract 与运行时 tool contract 不一致。

## 7. 未触顶时仍存在最终综合质量问题

`incident_39 task_2` 没有触发资源限制。triage 使用了 21/23 次模型调用，
成功返回带 4 条 evidence 的报告，commander 也执行了 final submit，但 Full
得分为 0，Single 与 Orchestrator-only 均为 0.5。这说明除了资源悬崖外，
还存在 report 到 final answer 的压缩、复核或证据选择损失。

另一方面，Full 唯一显著超过 Single 的 `incident_38 task_1` 中，Single 因
token limit 得 0，而 Full 和 Orchestrator-only 都得 1。因此这个样本证明
delegation 可以完成任务，但尚不能证明 specialist 相对 commander/planning
shell 有独立增益。

## 8. 解释与下一协议边界

当前 batch 支持的机械解释是：

> 在当前冻结资源协议下，Full 的组织方式对 ExCyTIn batch 001 是低效的。
> 主要原因是 worker 调查扩张与共享硬预算形成资源悬崖，其次是结构化报告
> 重试和 commander 缺少独立复核余量。

这不能直接证明多代理架构本身无效，因为 Full 的资源上限明显绑定。后续修复
应建立新的 protocol/version，不得与本 batch 混合。优先机制方向是：

1. commander 可见全局余额并保留最终综合额度；
2. commander 为每个 worker 分配独立、计入全局账户的硬额度；
3. worker 在自己的额度耗尽前主动提交报告；
4. 角色 mission 有清晰范围、交付条件和停止条件；
5. commander 能根据 evidence provenance 做复核并派遣窄验证任务；
6. 独立 worker 支持并行，但并发不能绕过全局资源计数。

## 9. 原始产物身份

本地原始产物目录：

`/tmp/cyberorion_cage_runs/excytin_publication_20260829_minimax_v2/batch_001/`

归档 SHA-256：

- Single eval：`f7aa7bdc74984219b73044637a4c75f09173eaa4570a5b27ea5407abef868ea2`
- Orchestrator-only eval：
  `37fe204aed446db8cd551e723c5d163f311297aebb932007f77865fcdd743445`
- Full eval：`792c3ca44ccc0e6e4c6c0d2b185635494297732d4a25b1b9e10411b6d86a7d90`
- preflight provenance：
  `f9a725be4c4d27830c8096b6432a2e6566f4a4b75f1bed9197b800117dcb6e0a`


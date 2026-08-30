# ExCyTIn incident-5 Full-only 资源分配归因

> 本文是对一次 Full-only、带官方 scorer 的 12-task 诊断运行的资源审计。
> 它不是三臂配对实验结论，也不使用得分来决定资源或修改协议。

## 1. 运行身份

- Full-only 原始运行：
  `/tmp/cyberorion_cage_runs/excytin_full_incident5_12task_scorer_20260829/eval_003/`
- Eval：
  `2026-08-30T03-34-55-00-00_excytin_4DwvarW2iM9d5xD9jgZs5d.eval`
- Eval SHA-256：
  `c29772ea48594e1a38d75f1a62903557b014433c1faa1499fce42744b50e5be0`
- CyberOrion HEAD：`415323c3eba743491ddf3fdd269ef951440037fd`
- CyberOrion tree：`cee8d39fc09a92ef3c1c60f4cfea0be69473ca09`
- ACESEvals：`17135140d0fdf52c2264a1fc248cf01e16b23a79`
- SABER：`a9bdce1343fd1c331aafda3119cbf0d48f215382`，版本 `0.2.0`
- 模型：`openai/MiniMax-M3`；temperature `0`；thinking `disabled`
- 任务：incident-5 的 12 个 task，按 manifest 顺序运行

资源上限为每个 sample：1,000,000 provider tokens、64 global model
calls、64 root tool calls、300 秒 wall time、16 dispatches，最大并行
dispatches 为 4；官方 task `max_steps=25`。

## 2. 直接观测

可见 runtime trace 的 11 个 sample 中，共记录 60 个已接受的 dispatch：

- 5 个 `successful_report`
- 55 个 `role_budget_exhaustion`
- 角色分布：`triage=17`、`threat_hunter=40`、`escalation=2`、
  `lateral_analyst=1`
- 没有最终 `empty_report`、`parse_failure` 或 `tool_failure`

55 个资源耗尽报告进一步分解为：

- 51 个 token limit；
- 4 个 worker-local model-call limit。

这不是根据得分推断，而是直接读取每条 specialist report 的
`worker_allocation`、`worker_local_usage` 和 `error.type/value/limit`。

## 3. 为什么可以判定 worker 本地分配确实不足

51 个 token 失败都记录了 `error.type=token` 且实际 provider token
`value > limit`。实际/分配比例最低约为 1.013，中位数约为 1.265，最高
约为 2.795。代表性记录如下：

| task | role | 分配 token | 实际 token | 结果 |
|---|---|---:|---:|---|
| task 3 | threat_hunter | 3,000 | 8,385 | token limit |
| task 9 | threat_hunter | 4,000 | 8,435 | token limit |
| task 4 | threat_hunter | 60,000 | 75,919 | token limit |
| task 10 | threat_hunter | 60,000 | 65,172 | token limit |
| task 12 | threat_hunter | 50,000 | 51,281 | token limit |

因此至少对这 51 个 worker，当前分配不足以完成“调查加结构化报告”
这一完整工作单元。部分失败甚至在记录的 worker tool calls 为 0 时就
发生，说明一次 provider 调用自身的上下文/输出消耗已经超过了分配。

另外 4 个失败不是 token 不足，而是 worker-local model-call ceiling
恰好触顶：

| task | role | model calls | token 使用/分配 | tool 使用/分配 |
|---|---|---:|---:|---:|
| task 2 | escalation | 3/3 | 28,542/60,000 | 3/6 |
| task 5 | threat_hunter | 5/5 | 51,324/80,000 | 5/15 |
| task 6 | threat_hunter | 4/4 | 32,193/40,000 | 4/8 |
| task 12 | threat_hunter | 4/4 | 36,121/80,000 | 4/8 |

这些记录表明，worker 在提交报告前消耗完了调用额度；它们不证明增加
调用额度一定会提高答案质量，但明确证明当前调用分配不足以保证完成该
调查流程。

## 4. 全局上限没有解释这 55 次失败

有 runtime trace 的 sample 中，全局最大观测值为：

- provider tokens：`726,452 / 1,000,000`
- model calls：`42 / 64`
- root tool calls：`35 / 64`
- dispatches：`11 / 16`

所以大多数 worker 失败时，全局账户仍有明显余额，瓶颈在 commander
分配给当前 worker 的本地额度，而不是全局 token、tool 或 dispatch ceiling。

唯一的全局硬限制事件是 task 7 触发 300 秒 wall-time limit；该 sample
没有 runtime trace，不能把它的 worker 资源状态臆测为某种 report 失败。

## 5. 不能把所有问题都归结为“数字太小”

当前证据支持“本地额度不足”是 55 次失败的直接触发原因，但还显示出两个
并行问题：

1. commander 的分配序列经常从明显偏小的额度开始，并通过多次 dispatch
   尝试寻找可行额度；这造成大量失败报告和额外调查成本。
2. 4 个 model-call ceiling 失败发生时仍有 token/tool 余额，说明部分
   worker 也没有及时收敛并为最终 report 预留调用。它是 worker 调查策略
   或模型自我控制问题，不能仅靠提高 token 上限解释。

成功记录也说明不存在一个对所有任务都适用的固定阈值：

- task 4 的成功报告使用 `100,000` token 分配，实际 `23,424`；
- task 5 的成功报告使用 `100,000` token 分配，实际 `21,850`；
- task 12 的成功报告使用 `20,000` token 分配，实际 `18,699`。

这说明任务路径、上下文长度和模型是否及时提交都会影响所需额度。

## 6. 结论

严格归因如下：

> 当前 60 次 dispatch 中的 55 次失败，确实被 worker 自身的硬资源分配
> 截断；其中 51 次是 token allocation 不足，4 次是 model-call
> allocation 不足。全局资源并未普遍触顶。因此“分配不足”是真实主因，
> 但 commander 的分配策略、上下文膨胀和 worker 未及时提交报告也是并行
> 原因。不能把这 55 次失败归类为官方工具失败或 report parser 失败，
> 也不能据此断言仅提高全局上限就会自动改善 Full 表现。

本次分析只读核对原始报告完成，未修改实验数据、未重跑样本、未删除任何
产物。

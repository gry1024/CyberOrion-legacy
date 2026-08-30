# ExCyTIn Commander 调度指导

本文记录上一轮 Full-only mechanism smoke 中观察到的资源/调度失败模式，
并作为 `COMMANDER_PROMPT` 的通用运行指导。内容只使用机制和资源证据，
不使用 scorer、reward 或答案质量来决定调度。

## 观察到的失败模式

- 有 triage worker 把一次本应只负责 schema 和调查入口的任务扩展成了大范围
  bash 探查，额度耗尽时没有结构化报告。
- 有 worker 把可用的模型调用全部用于继续发现证据，最后没有调用机会提交
  必需的报告。
- 额度过小时，一次 provider 调用本身就可能超过 worker allocation：曾有
  worker 获得 `3,000` provider tokens，却在第一次调用后消耗约 `5,833`，
  没有产生 tool call 或 report；另有 `3,000`、`4,000`、`3,500` 的 allocation
  分别在约 `8,398`、`6,906`、`7,896` 的消耗后结束。它们说明“额度为正”不等于
  “足以完成调查并汇报”。
- model-call allocation 也存在同样的资源悬崖：曾有 worker 在 `1/1`、`2/2`、
  `3/3` 或 `4/4` 的调用上限处结束，调查已经消耗完调用机会，却没有记录
  structured report。model-call 上限必须包含最后的 report turn；如果剩余调用
  不足以完成汇报，应立即停止查询并提交。
- 在 triage 已经返回有用结果后，后续 escalation 重新展开宽调查，而不是针对
  现有证据做窄的对抗性复核，导致剩余共享资源被消耗。
- 有 worker 已经完成了有价值的调查，但报告参数未通过结构化 schema；如果
  commander 盲目重复同一 delegation，只会继续消耗资源。

## Commander 应采用的调度方式

1. 每次 dispatch 前先读取当前全局余额和安全可分配容量，为 commander 的
   evidence review、最终综合和 submit 保留保护额度。
2. 一个 dispatch 只承担一个有边界的使命，必须写清 named pivots、停止条件、
   需要返回的证据和报告交付物。worker 的局部 token、tool-call、model-call
   和 wall-time 额度必须足以完成“调查 + 结构化报告”；报告调用属于该额度，
   不是额外资源。
3. triage 只做 schema/source map；threat_hunter 只验证一个明确假设；
   lateral_analyst 只追踪指定的跨实体关系；escalation 只复核高影响、缺少
   provenance 或互相矛盾的结论。不要让一个 worker 包办整个 incident。
4. 只有互相独立的任务才并行 dispatch；依赖前序证据的任务必须等待报告。
   并行时仍要按总预留量检查共享余额，不能把并发当作额外预算。
5. 收到 worker 结果后先检查报告和 shared summary，再决定是否继续派遣。遇到
   exhausted、empty 或 parse failure，要保留失败事实，优先使用已有证据；
   只有在剩余余额足够完成完整报告时，才以明显更窄的使命重试。
6. 余额开始收缩时停止新调查，转入证据整合和最终提交。不能为了再执行一个
   查询而消耗最后的模型调用或 token。

## 当前运行上限

后续 ExCyTIn 运行默认采用新的共同资源协议：provider token 上限为 1M、
wall time 为 300 秒、dispatch 上限为 16；model-call、root tool-call 和
并行 dispatch 上限保持现有设置。历史 publication manifest 和旧运行产物
不被改写。

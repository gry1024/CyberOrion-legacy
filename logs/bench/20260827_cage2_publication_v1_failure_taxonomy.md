# CAGE-2 publication_v1：Full 机制失败分类

本文件只做轨迹诊断，不改变 prompt、memory、dispatch policy 或预算。两次运行均为
同一 27 条 `(condition, seed, episode)` 清单的独立执行；不把它们合并成 n=54。

## 分类

1. **资源预算耗尽（主要可复现成本项）**：Full 首轮 38 个 step fallback（36 个
   `step_resource_budget_exhausted`、1 个 wall-time、1 个 no-valid-selection），
   retry1 为 36 个（全部 resource exhaustion）；Single/Orchestrator-only 两次均为
   0。Full 每次仍未超过 publication_v1 硬上限，故按仓库契约仍
   `publication_valid=true`，但耗尽后的 documented fallback 是性能/机制警告。
2. **派遣尝试被拒绝或角色无效**：Full 首轮轨迹含 197 次 `dispatch_error`，其中
   179 次为 `DispatchNotAllowed`、17 次空角色 `InvalidRole`、1 次把 orchestrator
     当作角色；retry1 为 195 次（184/11/0）。这会消耗决策轮次，却不给 specialist
     有效工具结果。
3. **派遣改变轨迹但未稳定改善结果**：两次执行中 Full 的 27/27 个 episode 的
     action 序列均与同 seed 的 Single 不同。首轮 Full-Single 在 3 个 episode 为正、
     14 个为负；retry1 为 12 个正、6 个负（其余为零）。这表明派遣影响很大，但方向
     依赖模型采样/环境轨迹，不能将“有 dispatch”解释为收益。
4. **长 horizon 风险并非单调稳定**：按精确配对的 Full-Single horizon 均值，首轮为
     30: −23.1、50: −31.9、100: −158.4；retry1 为 30: +22.5、50: +107.7、
     100: +36.0。首轮的 100-step 负向结果主要由 B_line/RedMeander 极端 episode
     主导，retry1 中 RedMeander-100 仍有负向 episode，而 B_line-100 转为正。因此
     “Full 随 horizon 必然恶化”在本 n=27 双次复核中不稳定，但长 horizon 的高成本/耗尽
     风险仍存在，不能据此调参。
5. **专家报告/最终决策链断裂**：部分 specialist `done` 报告为空或仅报告
     `global budget exhausted`；首轮/ retry1 分别有 139/141 个非空 role reports，
     且大量 analyst/watcher 事件在工具不可用或 dispatch 被拒后由语言模型直接补全。
     当前轨迹没有可靠的“专家建议被最终 action 采纳”结构化字段，因此不把文本中的
     recommendation-like 字样当作因果证据。

## 解释边界

所有 Full episode 都有 dispatch（首轮 25/27、retry1 27/27）；100-step episode
后段仍持续产生 valid action，未发现从早期 step 起永久 Sleep。上述分类支持“Full
引入显著额外调用/派遣成本，且在部分困难长轨迹中因预算耗尽和无效派遣而伤害结果”
的诊断，但不支持在没有新的受控架构实验前声称 specialist advice 本身造成了全部
差异。下一步应优先结构化记录 specialist recommendation→final action→reward 的
因果链，并修复可证明的 dispatch contract 问题；不要用本试验分数反向调 prompt。

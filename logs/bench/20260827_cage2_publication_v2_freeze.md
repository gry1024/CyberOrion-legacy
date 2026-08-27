# CAGE-2 publication_v2 预算冻结

状态：**publication_v2 已冻结**（resource-only；未读取/使用 reward 选择上限）。

## 冻结值

`cyberorion/bench/cage2.py::CAGE_STEP_BUDGETS["publication_v2"]`：

| 字段 | 值 |
| --- | ---: |
| max_steps | 7 |
| max_llm_calls | 5 |
| max_tool_calls | 4 |
| max_dispatches | 3 |
| max_role_steps | 4 |
| token_budget | 24,576 |
| wall_clock_sec | 90.0 |

与 `publication_v1` 每步上限逐字段相同。测试
`tests/test_benchmark_results.py::test_publication_v2_is_frozen_and_matches_v1_ceiling`
锁定这一契约。

## 冻结依据（resource data only）

最终动作契约（源 `2a0c7063b5c4528213b95d061c7f5d41b3e61c68`）下 MiniMax-M3
h30 三臂资源校准（见
`20260827_cage2_minimax_contract_v2_calibration_summary.md`）观测到的最大值：

- provider tokens 最大 **12,000**（Full 臂）→ 24,576 为 **2.048x** 余量；
- wall time 最大 **27.68s**（Full 臂）→ 90s 为 **3.25x** 余量；
- LLM calls 最大 3、tools 最大 1、dispatches 最大 1、role steps 最大 4；
- 契约错误计数：DispatchNotAllowed 0、InvalidRole 0、ToolNotAvailable 0；
- 校准期间无 budget-limit violation。

## 未覆盖的 tail（不改变当前冻结值）

h50/h100 的最终契约资源尾部未运行。它们属于后续资源数据补充；当前 h30 观测
相对冻结上限已保留 >=2x 的 token 余量与 >3x 的 wall-time 余量，冻结值不需要
因此上调。任何后续性能 run 必须使用 `publication_v2` 且不得在运行后按结果
调整。

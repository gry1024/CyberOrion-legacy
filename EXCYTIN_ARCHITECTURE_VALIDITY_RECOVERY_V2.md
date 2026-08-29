# ExCyTIn Architecture Validity — Recovery v2

当前结论：**FAIL**。本文件是恢复阶段的新结论；历史的
`EXCYTIN_ARCHITECTURE_VALIDITY.md` 与 `.json` 保持不变，仍表示早期
架构门失败证据。本轮没有启动 scorer 或性能实验。

## Provenance

- CyberOrion HEAD：`2de01ff1c448b805a81d46184f5bffaf195bc375`
- HEAD tree：`079d0ec5ea583c8cc23e3f3bf565eeb1cafec203`
- 当前已跟踪修改 diff SHA-256：
  `c7eb371091aa67a749c08c2f5228638c89f67bcb8f298d65009a70a73e49b9fd`
- ACESEvals：`17135140d0fdf52c2264a1fc248cf01e16b23a79`
- Inspect：`f94a85b6b3a246d2e4417b49bdda96fd8f04b93a`
- SABER：`a9bdce1343fd1c331aafda3119cbf0d48f215382`
- SABER correctness patch SHA-256：
  `93d847dfa0c3f0976f412ae74652df139c5415351e395b9a9031e08db8d8fe6f`

## 当前架构状态

当前桥接层使用原生 Inspect `react` 与官方 Tool 对象，不再把官方工具
降级为自定义 `ToolSpec`，也不再运行并行 JSON action loop。Full commander
只负责规划、共享调查状态、dispatch 和最终提交；官方 bash/python 由
delegated workers 使用。Single 和 Orchestrator-only 直接获得相同的官方
调查工具并集。worker 自动收到 task context、mission、边界内共享证据和
官方工具，并通过确定性结构化 report 返回实际证据及 provenance。

共享 workspace 是有界、非 LLM 专属的调查记录，包含 schema/evidence/
hypothesis/report/query provenance；原始官方工具轨迹仍保留在 Inspect
审计记录中。没有加入 scorer、gold、target 或隐藏数据库状态。

离线 native probe 和回归测试已证明：worker 可以执行真实 Inspect 工具，
两个独立 worker 可以并行，报告可返回 commander，且 child usage 进入
同一全局 ledger。最终源代码的 real-model Full 机制样本中也观察到自然
dispatch、worker 官方工具调用和 evidence-bearing reports；但其中一个
样本被取消，因此不能替代完整的三臂资源校准。

## 测试/机制证据

- architecture/native probe 与相关回归：`126 passed, 1 skipped in 23.79s`；
- 完整本地套件：`613 passed, 2 skipped, 10 failed`；失败为既有的外部
  `cai-latest` 文件缺失、默认环境变量、框架文档、ReportLab 与场景断言，
  未涉及本次 ExCyTIn bridge 修改；
- native bridge：未使用 custom JSON action protocol；
- contract/parser errors：已完成样本为 0；
- 16 KiB truncation：Single 与 Orchestrator-only v5 完成样本为 0；
- child/global accounting：离线 probe 通过，Full real-model 机制路径有
  global model gate；
- Full v5 resource calibration：**未完成**，不是模型/桥接错误，而是
  官方 `incident-55` 服务启动失败。

## Gate 结果

| 门 | 结果 | 说明 |
| --- | --- | --- |
| 官方 task/tool/scorer 未修改 | PASS | 仅使用 pinned ACESEvals/Inspect/SABER 路径 |
| 无 scorer/gold 泄漏 | PASS | 静态检查与 context 审计通过 |
| Single/Full 工具公平性 | PASS | Full team union 与 Single 官方调查工具并集一致 |
| 原生 delegation / worker report | PASS（机制证据） | probe 与部分 real-model Full trace |
| 全局 child 资源计量 | PASS（机制证据） | native probe 与 gate 记录通过 |
| 三臂最终资源校准 | **FAIL** | Full 因官方服务 healthcheck 失败无样本 |
| 性能实验授权 | **NO** | 资源/架构 validity 未闭合，按规则停止 |

## 资源校准结论

固定 manifest：
`benchmarks/manifests/excytin_architecture_resource_calibration_v5.json`

SHA-256：
`6adcaec758b14cfb4eaa5fdf3507f7020adbe38c87e052bb131e2f186528284c`

Single 和 Orchestrator-only 均使用 `openai/MiniMax-M3`、temperature 0、
thinking disabled、同一三任务 manifest、`episodes_per_task=1` 以及
8,000,000 tokens / 1,800 秒 / 256 tool / 256 model 的诊断上限。Orchestrator-only
重试成功，但 Full 的最终校准没有启动任何模型调用。因而没有足够证据选择
共同 publication budget，也没有修改 publication_v2。

详细数字和每次失败路径见
`EXCYTIN_RESOURCE_CALIBRATION_V5.md`。原始日志在：

`/tmp/cyberorion_cage_runs/excytin_architecture_recovery_restart2_20260829/`

本轮保持工作树变更供后续接手，不提交、不推送。

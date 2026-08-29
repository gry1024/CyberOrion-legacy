# ExCyTIn 资源校准 v5

结论：**FAIL（校准未完成）**。这不是性能结论，也没有启动 scorer 或
性能实验。原因是官方 SABER permanent-service 启动阶段反复无法让
`incident-55` 通过 healthcheck，Full arm 没有产生任何 episode 产物。

## Provenance

- CyberOrion HEAD：`2de01ff1c448b805a81d46184f5bffaf195bc375`
- HEAD tree：`079d0ec5ea583c8cc23e3f3bf565eeb1cafec203`
- 当前已跟踪修改（桥、runner、测试）的 diff SHA-256：
  `c7eb371091aa67a749c08c2f5228638c89f67bcb8f298d65009a70a73e49b9fd`
- 固定资源校准 manifest：
  `benchmarks/manifests/excytin_architecture_resource_calibration_v5.json`
- manifest SHA-256：
  `6adcaec758b14cfb4eaa5fdf3507f7020adbe38c87e052bb131e2f186528284c`
- 模型：`openai/MiniMax-M3`
- temperature：`0`
- thinking：`disabled`
- `episodes_per_task`：`1`
- official scorer：未执行
- ACESEvals：`17135140d0fdf52c2264a1fc248cf01e16b23a79`
- Inspect：`f94a85b6b3a246d2e4417b49bdda96fd8f04b93a`
- SABER：`a9bdce1343fd1c331aafda3119cbf0d48f215382`
- SABER 资源限制修复：`benchmarks/patches/saber-resource-limit-correctness.patch`
  （SHA-256：`93d847dfa0c3f0976f412ae74652df139c5415351e395b9a9031e08db8d8fe6f`）

## 临时诊断上限

三个 arm 使用同一宽松配置：

```text
token_limit              = 8,000,000
time_limit_sec           = 1,800
global_tool_call_limit   = 256
global_model_call_limit  = 256
```

这些是资源校准上限，不是 publication budget；没有根据 reward 或答案质量
选择任何数值。

## 已完成 arm

### Single

目录：`/tmp/cyberorion_cage_runs/excytin_architecture_recovery_restart2_20260829/resource_calibration_v5/single/`

3/3 样本成功归约：57 次官方工具调用、60 次模型调用、667,341 provider
tokens、222.979 秒总墙钟时间；单样本最大值为 41 次官方工具调用、42 次
模型调用、576,952 tokens、177.309 秒。契约错误、模型无效工具参数、
16 KiB 截断、native compaction 事件和硬限制事件均为 0。

### Orchestrator-only

首次目录因官方服务尚未 ready 而失败，未产生 episode；失败日志保留在：

`/tmp/cyberorion_cage_runs/excytin_architecture_recovery_restart2_20260829/resource_calibration_v5/orchestrator_only/`

官方 `incident-55` 完成初始化并稳定后，使用新目录做了一次有界重试：

`/tmp/cyberorion_cage_runs/excytin_architecture_recovery_restart2_20260829/resource_calibration_v5/orchestrator_only_retry1/`

3/3 样本成功归约：0 dispatch、29 次官方工具调用、28 次模型调用、
150,276 provider tokens、201.316 秒总墙钟时间；单样本最大值为 11 次
官方工具调用、12 次模型调用、72,100 tokens、132.532 秒。契约错误、
模型无效工具参数、16 KiB 截断、native compaction 事件和硬限制事件均为 0。
0 dispatch 是该 arm 的预期定义。

## Full arm 启动失败证据

以下目录均为本次新建，未覆盖其他运行：

- `/tmp/cyberorion_cage_runs/excytin_architecture_recovery_restart2_20260829/resource_calibration_v5/full/`：官方 permanent services 启动失败，无 `.eval`；
- `/tmp/cyberorion_cage_runs/excytin_architecture_recovery_restart2_20260829/resource_calibration_v5/full_startup_warmup1/`：专用 Docker network 标签错误，SABER 五次启动尝试均被 Docker 拒绝，无模型调用；
- `/tmp/cyberorion_cage_runs/excytin_architecture_recovery_restart2_20260829/resource_calibration_v5/full_startup_warmup2/`：network 标签修正后，官方服务被创建，但 `incident-55` 在约 10–11 分钟 SQL 初始化后仍未通过 healthcheck；SABER 报告 `Failed to start permanent services after 5 attempts`，无 `.eval`、无模型调用、无 episode。

healthcheck 报告的是官方数据库服务的初始化/健康状态问题（包括 schema
完整性检查未通过以及后续 MySQL ping 失败）。没有修改官方数据库、工具、
任务或 scorer 来绕过它。

## 资源校准判定

Single 与 Orchestrator-only 的数字只能作为观察记录；由于 Full 没有完成同一
manifest 的校准，无法证明三臂的共同全局资源上限，也不能冻结 publication
budget。没有检查 reward，没有启动 Single/Orchestrator-only/Full 性能实验，
也没有把启动失败伪装成样本或纳入资源平均值。

所有失败日志和成功 `.eval` 均保留在上述 `/tmp/cyberorion_cage_runs/*`
目录中；本轮不提交、不推送。

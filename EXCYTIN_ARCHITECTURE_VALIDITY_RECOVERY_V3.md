# ExCyTIn Architecture Validity — Recovery v3

`EXCYTIN_ARCHITECTURE_VALIDITY = PASS`（仅表示架构、机制和资源校准门通过）。
按照当前任务要求，尚未运行 scorer 或昂贵的性能实验；性能实验等待下一步
指令。

## 固定 provenance

- CyberOrion HEAD：`2de01ff1c448b805a81d46184f5bffaf195bc375`
- HEAD tree：`079d0ec5ea583c8cc23e3f3bf565eeb1cafec203`
- ACESEvals：`17135140d0fdf52c2264a1fc248cf01e16b23a79`
- Inspect：`f94a85b6b3a246d2e4417b49bdda96fd8f04b93a`
- SABER：`a9bdce1343fd1c331aafda3119cbf0d48f215382`，version `0.2.0`
- SABER resource-limit patch：`93d847dfa0c3f0976f412ae74652df139c5415351e395b9a9031e08db8d8fe6f`
- SABER startup retry patch：`c9d7e6423ea4864448febd56128ebdb73acb4dc21ffe7dddbf2136fd5b6056de`
- patched `saber/sandbox.py`：`0abfa204d9050b3569256b3c7c8f8df6b3669455ac6bbe3b3129465d8a32e8a8`
- resource manifest：`benchmarks/manifests/excytin_architecture_resource_calibration_v5.json`
- manifest SHA-256：`6adcaec758b14cfb4eaa5fdf3507f7020adbe38c87e052bb131e2f186528284c`

模型设置固定为 `openai/MiniMax-M3`、temperature `0`、thinking `disabled`，
每个 task 1 个 episode；官方 scorer 未执行。

## Full 架构拓扑

- commander：规划、读取 bounded investigation summary、dispatch、接收报告、
  负责最终提交；不直接持有 bash/python。
- workers：当前生产对齐的 `triage`、`threat_hunter`、`lateral_analyst`、
  `escalation`；各自使用官方 ExCyTIn bash/python，并提交确定性结构化报告。
- Single：直接使用 Full team 可用的官方环境工具并集（bash/python）。
- Orchestrator-only：使用同一官方工具并集，关闭 dispatch。
- Full 的唯一额外能力是组织、delegation、专门化和共享调查状态，不获得额外
  环境真相。

共享 workspace 只保存 task-local 的 schema、压缩 evidence、hypothesis、
unresolved questions、worker reports 及 provenance（role/tool/query/source/
sequence）；raw 官方 transcript 留在 Inspect 审计轨迹中，不进入默认共享
上下文。没有 scorer、gold、target 或隐藏数据库状态。

## 机制 trace

脚本化 trace：`scripts/excytin_architecture_probe.py` 已证明 commander → 两个
独立 worker 并行 → 官方 tool → evidence → structured report → commander 继续，
并且 child model/tool usage 计入同一个 root ledger。

本次 final-source Full real-model trace：

| task | natural dispatch | roles | official worker tools | valid/evidence reports | commander consumed |
| --- | ---: | --- | ---: | ---: | ---: |
| incident 134 | 1 | triage | 15 | 1/1 | 1 |
| incident 166 | 2 | triage, threat_hunter | 23 | 2/2 | 2 |
| incident 55 | 1 | triage | 6 | 1/1 | 1 |

合计 4 次 dispatch、44 次官方环境工具调用、4 个 evidence-bearing reports，
全部被 commander 后续模型调用消费。contract error 为 0；另有 1 次正确
工具契约下的 `submit_threat_hunter_report` 无效参数动作，已单独记录为 model
behavior，没有阻断报告或伪装成成功。

## 资源校准与候选冻结上限

三臂 v5 的共同单样本峰值为：19 model calls、29 root tool events、171,864
provider tokens、132.532 秒墙钟、2 dispatches。观测中没有 token/tool/time
硬限制、16 KiB 截断或 contract error。

| dimension | observed max | proposed common ceiling | headroom |
| --- | ---: | ---: | ---: |
| global model calls | 19 | 32 | 40.6% |
| root tool events | 29 | 64 | 54.7% |
| provider tokens | 171,864 | 262,144 | 34.4% |
| wall time | 132.532s | 180s | 26.4% |
| dispatches | 2 | 4 | 50.0% |

这些上限只由资源数据确定；官方 task 的 `max_steps=25` 未修改，
`max_parallel_dispatches=4` 保持 bounded setting。详细三臂数字和启动补丁
差异见 `EXCYTIN_RESOURCE_CALIBRATION_V5_RETRY_SABER7.md`。

Single/Orchestrator-only 的 v5 `.eval` 早于启动重试补丁，Full retry 使用了
该 setup-only 补丁；桥、任务、工具、scorer、模型和 sample 资源上限没有改变，
两套 provenance 均保留。未来性能运行必须统一使用已验证的 SABER 7-retry
setup，并在首个计费请求前重新写入完整 provenance。

## 验证状态

- architecture/SABER targeted tests：`126 passed, 1 skipped`
- startup patch tests：`11 passed, 1 skipped`
- full local suite：`613 passed, 2 skipped, 10 failed`；失败属于既有外部
  `cai-latest` 文件缺失、默认环境变量、框架文档、ReportLab 和场景断言。
- `git diff --check`：PASS
- scorer/reward：未执行/未读取
- 性能实验：未启动
- 本轮：不提交、不推送

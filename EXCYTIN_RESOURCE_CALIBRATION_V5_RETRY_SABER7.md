# ExCyTIn 资源校准 v5：SABER 7 次启动重试后的 Full 重试

结论：**Full 资源校准成功**。3/3 episode 完成，未执行 scorer，未查看
reward。Full 的自然多代理、worker 官方工具调用和 evidence-bearing report
均出现；没有资源硬限制或 16 KiB 工具输出截断。

## Provenance

- CyberOrion HEAD：`2de01ff1c448b805a81d46184f5bffaf195bc375`
- HEAD tree：`079d0ec5ea583c8cc23e3f3bf565eeb1cafec203`
- v5 manifest：`benchmarks/manifests/excytin_architecture_resource_calibration_v5.json`
- manifest SHA-256：`6adcaec758b14cfb4eaa5fdf3507f7020adbe38c87e052bb131e2f186528284c`
- 模型：`openai/MiniMax-M3`
- temperature：`0`
- thinking：`disabled`
- `episodes_per_task`：`1`
- ACESEvals：`17135140d0fdf52c2264a1fc248cf01e16b23a79`
- Inspect：`f94a85b6b3a246d2e4417b49bdda96fd8f04b93a`
- SABER：`a9bdce1343fd1c331aafda3119cbf0d48f215382`，version `0.2.0`
- 启动补丁：`benchmarks/patches/saber-startup-health-retry.patch`
- 启动补丁 SHA-256：`c9d7e6423ea4864448febd56128ebdb73acb4dc21ffe7dddbf2136fd5b6056de`
- patched `saber/sandbox.py` SHA-256：
  `0abfa204d9050b3569256b3c7c8f8df6b3669455ac6bbe3b3129465d8a32e8a8`
- 该补丁只将 permanent-service startup retry 从 5 改为 7；资源限制正确性
  补丁保持独立且未改变。

Full provenance 中记录：`saber_startup_health_retry_patch.action=already_patched`。

## 临时诊断配置

```text
token_limit              = 8,000,000
time_limit_sec           = 1,800
global_model_call_limit  = 256
global_tool_call_limit   = 256
official_task_max_steps  = 25 (unchanged)
```

## 三臂 v5 资源观察

| arm | samples | dispatch | official env tools | root tool events | model calls | provider tokens | wall time total (s) | max model/root tools/tokens/wall (s) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| Single | 3/3 | 0 | 33 | 36 | 31 | 170,780 | 115.207 | 13 / 14 / 76,268 / 60.728 |
| Orchestrator-only | 3/3 | 0 | 29 | 32 | 28 | 150,276 | 201.316 | 12 / 12 / 72,100 / 132.532 |
| Full（本次重试） | 3/3 | 4 | 44 | 58 | 45 | 383,800 | 241.395 | 19 / 29 / 171,864 / 123.913 |

三臂均使用同一 v5 task IDs、模型、解码设置和诊断上限。Single 与
Orchestrator-only 的 `.eval` 生成于启动重试补丁加入前；Full 重试使用 7 次
启动重试补丁。该差异只影响官方数据库 startup retry，不改变 task、tool、
agent bridge、scorer 或 sample 内资源计量；两种 provenance 均已由各自
`cyberorion_official_provenance.json` 保存。

共同峰值来自：19 model calls、29 root tool events、171,864 provider tokens、
132.532 秒单样本墙钟时间，最大 dispatch 为 2。三臂的 contract errors、
16 KiB truncation、sample hard-limit events 均为 0；Full 有 1 次
`submit_threat_hunter_report` 模型无效参数动作，属于正确契约下的 model
behavior，不是 contract error，且该样本仍返回 2 个有效 evidence-bearing reports。

## 资源余量候选

以下只由上述资源峰值计算，未使用 reward 或答案质量；这是性能运行前的
候选共同上限，不在本文件中启动性能实验：

| dimension | observed max | candidate ceiling | unused margin |
| --- | ---: | ---: | ---: |
| global model calls / sample | 19 | 32 | 40.6% |
| root tool events / sample | 29 | 64 | 54.7% |
| provider tokens / sample | 171,864 | 262,144 | 34.4% |
| wall time / sample | 132.532 s | 180 s | 26.4% |
| dispatches / sample | 2 | 4 | 50.0% |

`official_task_max_steps=25` 保持 pinned task 原值；候选共同 root tool ceiling
仍单独设为 64，不改官方 task 定义。`max_parallel_dispatches=4` 保持现有
bounded setting。

## 原始产物

Full 重试目录：

`/tmp/cyberorion_cage_runs/excytin_architecture_recovery_restart2_20260829/resource_calibration_v5/full_retry_saber7/`

其中包含 `.eval`、`mechanism_summary.json`、provenance、stdout 和 stderr。
reducer 的 `score_or_target_fields_read` 为 false，未读取官方 scorer 或
reward。性能实验尚未启动。

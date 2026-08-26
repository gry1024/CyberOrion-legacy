# ExCyTIn 官方 ACESEvals/Inspect/SABER 冒烟（1 任务 × 4 臂）

- 上游：microsoft/ACESEvals @ `17135140d0fdf52c2264a1fc248cf01e16b23a79`
- 执行方式：上游 uv 环境内 `inspect eval domains/excytin`，真实 Docker/
  SABER sandbox（saber/excytin/* 镜像）+ 官方 MySQL telemetry + 官方 scorer；
  CyberOrion 臂经 `scripts/run_excytin_official.py` +
  `cyberorion/bench/excytin_official_agent.py` 注册为 SABER agent
- 模型：openai/deepseek-v4-flash（temperature=0）；judge 同模型
- SQLite adapter 全程未参与（sqlite_projection_involved=false）

## 结果（官方 scorer）

| 臂 | 任务 | saber_overall | submission | checkpoint_1 | checkpoint_2 | aggregate |
|---|---|---|---|---|---|---|
| react（官方基线） | incident_55_*_task_1 | 1.000 | 1.000 | 0.000 | 0.500 | 1.000 |
| cyberorion_single | incident_134_*_task_1 | 1.000 | 1.000 | 0.000 | 0.000 | 1.000 |
| cyberorion_orchestrator_only | incident_134_*_task_1 | 1.000 | 1.000 | 0.000 | 0.000 | 1.000 |
| cyberorion_full | incident_134_*_task_1 | 1.000 | 1.000 | 0.000 | 0.000 | 1.000 |

react 基线跑在 incident_55（--limit 1 默认抽样），CyberOrion 三臂跑在
incident_134（task_filter 钉住同一任务）。三臂之间任务完全相同。

## Artifact 清单与 SHA256

- react eval log：`2026-08-26T15-31-33-00-00_excytin_k4YwQfXGY6ubXb5ZDvahi9.eval`
  `5a822c084b5c91a0bf234cc156ed22c234bc606833f612bb92482734e2eb977e`
- single eval log：`2026-08-26T16-45-23-00-00_excytin_kaiqy3NitS4rFpUZtiQvXN.eval`
  `9a5bb7ed1ae24a0be728f4816dab725f3b79dffb3fe774cb83a3a0db8d4be3a7`
- orchestrator_only eval log：`2026-08-26T16-54-50-00-00_excytin_HsJoZX7usZPfRBQSkKD2gD.eval`
  `858962554dad6a36e67e7e3f6748f2d97341b7eaa23c6dbcf31de07feab81f66`
- full eval log：`2026-08-26T17-01-13-00-00_excytin_HNPuWKSsDjQYPhcFxMxQN8.eval`
  `a26336d94f9fc59de47729a565e395f30299ce0cd87fa74705805daebc8a1648`
- 每臂 `cyberorion_*.provenance.json`：官方执行 provenance
  （official_execution=true、上游 commit、CyberOrion commit、arm/model）

每个 eval log 的 sample store 中持久化了 `cyberorion_arm` 与
`cyberorion_runtime_trace`（决策轨迹/工具调用/角色事件，用于审计）。

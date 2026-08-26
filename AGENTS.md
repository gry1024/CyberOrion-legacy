# AGENTS.md — AI 接管开发指南

> 写给下一个接手本仓库的 AI agent：你没有任何上下文，本文档是你唯一的启动盘。
> 读完本文档 + `docs/ARCHITECTURE.md` 即可独立开工。所有路径均已验证存在（2026-07-31）。

---

## 1. 项目一句话

**CyberOrion 2.0**：基于 CAI framework（`cai-framework` 0.5.10）构建的自主红蓝 LLM 对抗平台——红方自主渗透真实 docker 靶场、蓝方 SUPER-AGENT 团队只靠遥测独立检测，服务端裁判 + 三维对齐指标引擎客观评分（TP/FP/FN/MTTD/0-100 分）。

**当前状态快照**：

- 代码包 `cyberorion/`，服务入口 `server.py`（FastAPI，:8000），前端 `web/`（React 19 + Vite + Tailwind v4，构建产物 `web/dist` 由 server 托管）；
- **测试解释器**：`~/cai_env/bin/python`；外部 benchmark 缺资产时结构化跳过/报错，禁止伪造分数；
- 最近里程碑：① 蓝队 SUPER-AGENT 团队；② 分层基准（CyberSOCEval、ExCyTIn、CAGE-2、SecAlertBench、内部契约轨）；③ 前端 v3 八视图；
- 文档体系：`docs/ARCHITECTURE.md`（架构与扩展指南）、`docs/BENCHMARK.md`（基准）、`docs/REVIEW.md`（验收）、`docs/CAI_IMPROVEMENTS.md`（CAI 复用 vs 自建）、`docs/FRAMEWORK.md`（框架文档，前端「文档」tab 经 `GET /api/about` 渲染）。

---

## 2. 环境事实（先读这一节，能省你两小时）

- **操作系统是 WSL2**（Windows 宿主的 Linux 子系统）。仓库在 `<cai-repo>/cyberorion`。
- **Python 环境在仓库外**：`~/cai_env` 是 Python venv。**不要**在仓库里建 venv，**不要**用系统 python 跑项目代码。统一用 `~/cai_env/bin/python`。
- **LLM 配置在 `<cai-repo>/cyberorion/../.env`**（即 `<cai-repo>/.env`，CAI 仓库根，不在本仓库内）。关键变量：`CAI_MODEL`（带 provider 前缀，如 `openai/MiniMax-M3`）、`OPENAI_API_KEY`、`OPENAI_API_BASE` / `OPENAI_BASE_URL`（BASE 优先）。模板见本仓库 `.env.example`。`server.py` 启动时自动加载该 `.env`（setdefault 语义）。
- **外部网络**：连外部 LLM 服务（`OPENAI_API_BASE` 的 OpenAI 兼容端点）或 GitHub 的命令必须在**外部网络**下执行——受限（沙箱）网络会拦截这类请求导致长时间卡顿；卡住先怀疑网络权限再怀疑端点，详见"已知坑"第 13 条。
- **Docker = Windows 上的 Docker Desktop**（`E:\Program Files\Docker\Docker Desktop.exe`，WSL 里路径 `/mnt/e/Program Files/Docker/Docker Desktop.exe`）。WSL 里的 `docker` CLI 只是它的壳。**`docker` 命令挂了一般是 Desktop 没起**——启动 Desktop 后 `/var/run/docker.sock` 才存在。改镜像加速器要改 Windows 侧 `C:\Users\<user>\.docker\daemon.json` 再重启 Desktop。
- **靶机容器**（`docker compose up -d` 起 3 台）：`cyberorion_dvwa`（172.29.0.10，宿主 28080→80）、`cyberorion_weak_ssh`（.12，22222→22）、`cyberorion_log4j`（.20，8983）。红方攻击一律走 **127.0.0.1 + 宿主端口**（见"已知坑"）。
- **外部基准在 `<cai-repo>/benchmarks/`**：
  - `cybersoceval/`：PurpleLlama 数据集（malware_analysis 609 题 JSON）；
  - CyberGym Lite 运行依赖本地/生产的 artifact 缓存；生产已使用 `/tmp/cyberorion_cybergym_cache` 作为离线缓存，避免 HuggingFace 下载失败导致 bench 无分。
- 前端：`web/` 下 node 20 + npm 可用；`web/dist` 已随仓库构建好，**只看不改前端就不需要 Node**。

---

## 3. 生产环境与部署规范

**先确认你操作的是生产，不是本机。** 用户说“网址上 / 部署服务器 / 作战台”时，默认指生产站点 [https://corleone.xin/cyberorion/](https://corleone.xin/cyberorion/)，不是 `http://127.0.0.1:8000`。

- **生产入口**：公网 URL 是 `https://corleone.xin/cyberorion/`；API 前缀是 `https://corleone.xin/cyberorion/api/*`；WebSocket 走同站点 `/cyberorion/ws`。
- **SSH 入口**：使用 SSH config alias `treehole`，实际为 `root@118.178.145.162`、端口 `13522`、密钥 `~/.ssh/treehole_key`。不要把私钥内容写进日志、回复或文档。
- **生产路径**：代码根目录 `/opt/cyberorion`；后端服务 `cyberorion.service`；后端进程形态 `/opt/cyberorion/cai_env/bin/python /opt/cyberorion/server.py`；Nginx 服务 `nginx.service`。
- **部署原则**：不要在生产上 `git pull` 覆盖脏工作区；不要整目录覆盖 `/opt/cyberorion`；只上传本次修改需要的文件或 `web/dist`，并先备份到 `/tmp/cyberorion_deploy_backups/`。
- **后端单文件部署**：本地测试通过 → `ssh treehole 'cp /opt/cyberorion/<file> /tmp/cyberorion_deploy_backups/<file>.before_<stamp>'` → `scp <file> treehole:/opt/cyberorion/<file>` → 远端 `py_compile` → `systemctl restart cyberorion.service` → `systemctl is-active cyberorion.service` → 查 `journalctl -u cyberorion.service -n 80 --no-pager`。
- **前端部署**：本地 `cd web && npm run build` 通过 → 打包 `web/dist` → 生产备份 `/opt/cyberorion/web/dist` → 替换 dist → 验证 `https://corleone.xin/cyberorion/` 加载的是新 hash 资源。
- **Benchmark 验证**：生产 Benchmark 结果必须查 `https://corleone.xin/cyberorion/api/bench/runs` 和 `/opt/cyberorion/logs/bench/*.json`；CyberGym Lite 至少确认 agent/base 两臂都有 `scores.avg_score`，不能只看本地文件。
- **GitHub 规则**：默认只改工作区，不 `git commit`、不 `git push`。只有用户明确要求“提交/推到 GitHub”时才执行；执行前必须 `git status`、跑相关测试、确认没有 `.env`/密钥/生产私有文件进入暂存区。**提交/推送优先走 SSH**（`git@github.com:owner/repo.git`），不要用 HTTPS remote——SSH 免交互认证、不受 HTTPS 凭据/代理问题影响；本地 `~/.ssh/config` 已配好 github.com（ed25519 密钥）。例如 CyberOrion-legacy 的 bench-eval 分支用 `GIT_SSH_COMMAND="ssh -o BatchMode=yes" git push git@github.com:gry1024/CyberOrion-legacy.git bench-eval:bench-eval` 推送。

---

## 4. 代码地图

```
cyberorion/                        # 仓库根
├── server.py                      FastAPI：REST /api/* + WS /ws + 托管 web/dist（:8000）
├── run.py                         legacy CLI 入口（旧同步 Arena，无遥测/评分，别用它验证新功能）
├── docker-compose.yml             靶机编排（webgoat/vampi 在 web_plus profile）
├── scenarios/*.yaml               web_basic / web_plus / cve_log4j / cve_cve-2024-4323
├── weak_ssh/                      SSH 靶机 Dockerfile
├── cyberorion/                    # 源码包
│   ├── agents/
│   │   ├── blue_team.py           蓝队 SUPER-AGENT 团队（指挥官 + dispatch_task + _ROLE_SPECS）
│   │   ├── blue.py                旧单体蓝队（15 业务工具 + Skill，回退路径）
│   │   └── red.py                 红方渗透者（6 业务工具 + Skill + 草稿板）
│   ├── core/
│   │   ├── controller_v2.py       V2 会话生命周期：重置→遥测→红蓝并发→评分
│   │   ├── agent_runner.py        Runner.run_streamed 流式运行 + 事件转播
│   │   ├── event_bus.py           asyncio pub/sub 总线（前端所有实时数据的源头）
│   │   └── session_state.py       全局/会话状态 + 漏洞台账
│   ├── telemetry/
│   │   ├── store.py               每会话一个 SQLite（events/alerts/attacks/snapshots 4 表）
│   │   ├── collectors.py          容器日志 tail + 30s 快照 + auth/web/docker_logs 解析器
│   │   └── binding.py             会话级 store 绑定（set_store/get_store）
│   ├── eval/
│   │   ├── ground_truth.py        红方地面真值通道（写 attacks 表 + 事件总线）
│   │   ├── metrics.py             指标引擎：TP/FP/FN/检测率/MTTD/评分公式（纯函数）
│   │   ├── judge.py / report.py   LLM 裁判报告（模板兜底）+ finalize_session 落盘
│   │   └── benchmarks/            CybORG 适配器（可选、懒加载）
│   ├── bench/
│   │   ├── cybersoceval.py        malware_analysis + attack_kb 套件 harness（SUITES 注册表）
│   │   └── attack_kb.py           attack_kb 套件（KB 检测摘录 → 技术编号 MCQ）
│   ├── kb/
│   │   ├── build_kb.py / rag.py   KB 构建器 / 检索器（embedding npz 缓存 + BM25 回退）
│   │   ├── service.py             KB HTTP API 纯函数层（ATT&CK v18 = 13 战术的树）
│   │   └── data/                  attack_kb.jsonl(3204) / *_vecs.npz / 原始语料
│   ├── skills/registry.py         Skill 目录发现、摘要目录与按需全文加载
│   ├── scenarios/loader.py        YAML → 校验过的 dataclass
│   ├── session_detail.py          历史会话详情构建器（前端复盘页数据源）
│   ├── storyline.py               故事线复盘（LLM 渲染 + 模板兜底，缓存 storyline.md）
│   ├── tools/
│   │   ├── _common.py             场景常量（CO_* 环境变量覆盖）、docker 辅助
│   │   ├── blue/                  蓝队 15 业务工具 + load_skill
│   │   ├── red/                   红队 6 业务工具 + load_skill
│   │   └── dvwa.py                DVWA 专用工具（旧单体蓝队用）
│   ├── traffic/                   流量分析流水线（4 阶段 Agent + 规则引擎）
│   │   ├── pipeline.py            run_traffic_analysis_pipeline SSE
│   │   ├── detector.py            TrafficDetector 规则引擎
│   │   └── feeder.py / synthetic.py / loaders.py
│   ├── hostguard/                 主机卫士流水线（4 阶段 SSH 扫描）
│   │   ├── pipeline.py / ssh_client.py / key_store.py
│   ├── arena_reset.py             靶场重置（每会话恢复易受攻击基线）
│   ├── agent.py / arena.py        legacy 兼容层（run.py 用）
│   └── logs.py / viz.py           legacy 会话日志 / 终端可视化
├── web/                           前端（React 19 + Vite + Tailwind v4，见 web/README.md）
├── skills/{red,blue}/*/SKILL.md   红蓝隔离的渐进式 Skill 内容
├── tests/                         pytest 459 项（2026-08-20 实测：459 passed）
├── scripts/                       e2e_smoke / e2e_fight / run_bench / gen_cve_scenario /
│                                  cve_target.sh / reset_targets.sh / smoke_* / run_cyborg
├── docs/                          ARCHITECTURE / BENCHMARK / REVIEW / CAI_IMPROVEMENTS / FRAMEWORK
└── logs/                          运行产物：session_<ts>/{telemetry.db,metrics.json,report.md}、bench/*.json
```

**"改 X 先看 Y"速查**：

| 要改什么 | 先看 |
| --- | --- |
| 蓝队团队/角色/派遣 | `agents/blue_team.py` + `docs/ARCHITECTURE.md` §3 |
| 红方行为/裁判规则 | `agents/red.py` + `tools/red/claim.py` + ARCHITECTURE §4 |
| 评分/指标 | `eval/metrics.py`（纯函数，先读测试 `tests/test_metrics.py`） |
| 遥测事件/解析器 | `telemetry/collectors.py` + `tests/test_telemetry.py` |
| 基准套件 | `bench/cybersoceval.py` + `docs/BENCHMARK.md` |
| 前端某面板 | `web/src/components/` 对应文件 + `web/src/arena.tsx`（WS 事件分发中枢） |
| API 端点 | `server.py` 头注有全部端点签名清单 |
| 场景/靶机 | `scenarios/loader.py` + 对应 yaml + `docker-compose.yml` |
| 流量分析逻辑 | `cyberorion/traffic/pipeline.py` + `docs/ARCHITECTURE.md` §10 |
| 主机卫士逻辑 | `cyberorion/hostguard/pipeline.py` + `docs/ARCHITECTURE.md` §11 |
| AI 复盘/storyline | `cyberorion/storyline.py` + `cyberorion/session_detail.py` |

---

## 5. 铁律与约定（违反任意一条都算破坏）

1. **信息隔离**：蓝队（含子代理、蓝队工具）**绝不**接触地面真值——不 import `cyberorion.eval`、不读场景 `ground_truth` 字段、不查 `attacks` 表。约束写在 `tools/blue/__init__.py` 头注，`tests/test_blue_tools.py` 有静态测试看守。改蓝队代码前先想这条。
2. **红队无特权**：红方只有网络攻击面，**禁止 `docker exec`**、禁止宿主机访问。唯一例外是 `claim_success` 裁判（`tools/red/claim.py::_referee_read_flag`）为比对 flag 读容器文件——裁判行为，内容绝不返回给 agent。
3. **工具诚实**：工具失败返回错误字符串，**绝不向 agent loop 抛异常**、绝不谎报成功。红方工具经 `@_gt_record` 自动落地面真值；蓝方处置工具埋点 `source='response'` 事件。
4. **禁止伪造评分数据**：metrics 必须来自 `eval/metrics.py` 对 telemetry.db 的真实计算；bench 结果必须来自真实运行落盘。`logs/bench/` 里 `model=fake-model` 的文件是测试夹具产物，引用结果时排除。
5. **高内聚低耦合**：每个工具单文件、`@function_tool` + 中文 docstring（Args/Returns）、输出 `_clip` 截断（1200 字符）；会话级资源走 **binding 模式**（`telemetry.binding.set_store` / `eval.ground_truth.set_ground_truth` / `blue_team.set_event_bus`），不穿透工具签名；未绑定时返回解释性字符串。
6. **模型构造统一走环境变量**：`CAI_MODEL` / `OPENAI_API_KEY` / `OPENAI_API_BASE‖OPENAI_BASE_URL`，参照 `agents/red.py::_model`。
7. **降级优先**：docker 缺失/LLM 失败/embedding 不可用都不允许让核心链路（指标、报告）无产出——采集器重试、裁判模板兜底、BM25 回退、e2e 无 key 自动 SKIP。
8. **改完必须跑测试**：`~/cai_env/bin/python -m pytest tests/ -q`，全绿才算完。

---

## 6. 常用任务食谱

**加蓝队工具**：`tools/blue/` 选模块写 `@function_tool`（store 经 `get_store()` + `_require_store()` 守卫）→ `tools/blue/__init__.py` 导出 → `agents/blue_team.py::_ROLE_SPECS` 加进目标角色 tools 并更新该角色 prompt → 加测试（参照 `tests/test_blue_tools.py`）。

**加红队工具**：`tools/red/` 写工具，用 `@_gt_record(technique, target, judge)` 装饰（`tools/red/_helpers.py`，judge 谓词从返回文本判 success）→ `tools/red/__init__.py` 导出 → `agents/red.py` 工具清单 + prompt → 测试参照 `tests/test_red_tools.py`。

**加团队角色**：`_ROLE_SPECS` 加一条（title/tools/prompt + `_CONCLUSION_BLOCK`）→ 指挥官 `_ORCHESTRATOR_TEMPLATE` 团队清单登记 → 测试参照 `tests/test_blue_team.py`。

**加 Agent Skill**：在 `skills/red/<name>/` 或 `skills/blue/<name>/` 新建 `SKILL.md`，frontmatter 必须包含与目录一致的 `name` 和非空 `description`；全文不超过 1200 字符。初始 prompt 只注入摘要，Agent 调用对应阵营的 `load_skill` 后才读取全文；`scripts/` / `references/` 首版只允许在文档中说明能力，不会自动加载或执行。

**加场景**：写 `scenarios/<name>.yaml`（network + targets：container/ip/services/logs/ground_truth）→ `docker-compose.yml` 加服务（重靶机挂 profile）→ UI 下拉框自动列出（`GET /api/scenarios`）。CVE-Bench 场景用 `scripts/gen_cve_scenario.py <CVE-ID> --variant one_day` 生成。

**加 benchmark 套件**：实现 `run_bench(...) -> dict` → 登记 `bench/registry.py`；外部数据再登记 `bench/assets.py` → 结果包含 provenance/methodology_status/真实 runtime 轨迹 → API/CLI/前端同步 → 测试参照 `tests/test_bench_external.py`。禁止自动下载/删除；单资产 >1GiB 或总量 >5GiB 时，只读管理员在 `representative/` 准备的固定种子分层代表集，缺代表集必须结构化失败，禁止先整体载入超大文件。

**改前端**：`cd web && npm run dev`（HMR，后端另起 `python server.py`）→ 改 `src/components/` → **`npm run build`（tsc + vite）** → server.py 直接托管新 `dist`。WS 事件分发在 `src/arena.tsx::handleEvent`。注意 `web/README.md` 的组件清单滞后于代码（以 `src/components/` 实际文件为准）。

**起服务三部曲**：

```bash
source ~/cai_env/bin/activate
set -a; source <cai-repo>/.env; set +a    # server.py 也会自动加载，此步可省
cd <cai-repo>/cyberorion && python server.py   # → http://localhost:8000
```

---

## 7. 验证清单（交付前逐项过）

- [ ] `cd <cai-repo>/cyberorion && ~/cai_env/bin/python -m pytest tests/ -q` → 全绿（数量随开发增长，关键是无 fail）；
- [ ] 改了前端 → `cd web && npm run build` 成功；
- [ ] 改了会话/对抗链路 → `docker compose up -d` 后 `python scripts/e2e_smoke.py`（真实 LLM + docker，无 key 自动 SKIP 不算失败，断言失败才是失败）；
- [ ] 改了 bench → 跑小 n 真实验证：`python scripts/run_bench.py --n 5 --mode base`；
- [ ] 改了生产可见功能 → 验证生产 URL/API，不要只验证本地 `127.0.0.1`；
- [ ] 改了场景/重置 → `scripts/reset_targets.sh` 能跑通（会真实 ssh 登录 weak_ssh 验证）。

---

## 8. 已知坑清单（都是踩过的，别重踩）

1. **bench/对抗出现全 0 或全错，先查 LLM 端点，别怀疑代码**。历史教训：DashScope 欠费导致所有调用静默失败、分数全 0。排查顺序：`curl $OPENAI_BASE_URL/models -H "Authorization: Bearer $OPENAI_API_KEY"` → 看 run json 里 `llm_errors` / raw 输出。`<cai-repo>/.env.bak.*` 是历次端点切换的备份，可对照。
2. **busybox vs procps 的 ps 格式不同**：快照解析 `parse_ps_aux` 必须兼容两种布局（`telemetry/collectors.py:281` 有注释）。weak_ssh 是 busybox——早年只支持 procps 导致其进程快照恒为空。改快照解析时两种都要测。
3. **容器里没有/用不了 iptables**：`block_ip` 在某些容器上失败是**环境问题**（容器缺 NET_ADMIN，`tools/blue/respond.py:81` 明确返回此提示），不是 bug。工具已如实返回失败，别去"修"它谎报成功。
4. **靶机状态污染**：上一轮蓝队加固（关 ssh 密码认证、DVWA 调 high、删后门）会让新一轮"没东西可打"。`ControllerV2.start_session` 会自动 `arena_reset.reset_all`，手动用 `scripts/reset_targets.sh`。遇到"红队突然打不进"先想这个。
5. **WSL2 里容器 IP（172.29.x.x）从 Windows 侧/某些路径不可直连**：红方工具一律走 **127.0.0.1 + 宿主映射端口**（28080/22222/8983），蓝方遥测走 docker exec/容器网段。新增目标时两条路径都要配对设置，`CO_TARGET_*_IP` / `CO_*_HOST_PORT` 环境变量可覆盖。
6. **ATT&CK v18 是 13 个战术**：Defense Evasion 已拆成 Stealth + Defense Impairment（`kb/service.py` 注释）。写死 12 战术的代码/测试都是错的。
7. **推理型模型（MiniMax-M 系列 / deepseek-v4-flash）会把 max_tokens 全烧在思维链上导致答案行缺失** → bench `parse_fail` 飙升被判 wrong。实测 deepseek-v4-flash 在 rag 长提示（注入知识后 ~4k 字符）下 4096 甚至 8192 tokens 全部变成 reasoning_tokens、content 为空（finish_reason=length）；`extra_body={"thinking": {"type": "disabled"}}` 可关闭思维链（5s 出完整答案）。`bench/cybersoceval.py` 支持 `CO_BENCH_THINKING=disabled` 下发该参数（DeepSeek 兼容端点；其他端点不认识时不要设）。`_MAX_TOKENS=4096`（2026-08 起）。换模型跑 bench 先看 parse_fail。
8. **官方 CyberSOCEval runner 不可直接用**：endpoint 会把 `response_format=json_object` 的 schema 提示复读出来（历史上 100 题 23 题 INVALID）——这就是自有 harness 存在的原因，别回退。
9. **CyberGym Lite 生产可能因外网下载失败无分**：优先查 `/opt/cyberorion/logs/bench/<run>.json` 的 `llm_errors` / artifact 错误；生产离线缓存位置是 `/tmp/cyberorion_cybergym_cache`，缺 artifact 时先补缓存再重跑，不要伪造分数。
10. **`run.py` 是降级路径**：不起遥测/评分，蓝队工具会返回"store 未绑定"。验证功能一律用 `server.py`。
11. **`cai_env` 里的 CAI SDK 带 4 个本地补丁（重装 cai-framework 会被冲掉，必须重打）**，全部是 2026-08-02 蓝队"无声卡死"排查的产物：
    ① `cai/sdk/agents/models/openai_chatcompletions.py`：`if reasoning_content:` 块里 yield `SimpleNamespace(type="response.reasoning_summary_text.delta", ...)`（否则 DeepSeek 推理流只打印到控制台，run_streamed 消费者永远看不到 thinking）；
    ② 同文件 `_fetch_response` 的 `except litellm.exceptions.BadRequestError`：`retry_count += 1` + 耗尽即 raise（原代码 fall-through 不 raise 也不计数，`while retry_count < max_retries` 变成**无限重试循环**，实测一次 400 重发 613 次）；
    ③ 同文件 `_fetch_response`/`get_response`：`message_history` 只在 `isinstance(input, str)` 时前置（run-item list 已含全量对话；否则并行工具调用的历史被复制成"每个 call 一条 assistant + 孤儿 tool 消息"，DeepSeek 直接 400）；
    ④ `cai/util.py::fix_message_list` 第二遍：前一条是同一 assistant 的兄弟 tool 消息也算合法序列（原逻辑在两个并行 tool 响应间**乒乓死循环**，CPU 占满、事件循环冻结）。
12. **DeepSeek 推理轮很慢（单轮 30-120s）**：四个 `_model()` 的 `AsyncOpenAI(timeout=300.0)` 不能降回 60；子代理墙钟 `_SUBAGENT_TIMEOUT=420`、指挥官 900、红方 600。超时判死必须用 `asyncio.wait` + 显式 cancel（`core/agent_runner.py::run_with_timeout`）——**`asyncio.wait_for` 无效**：SDK `result.py::stream_events` 会吞 `CancelledError`，wait_for 拿到部分结果正常返回，超时分支是死代码。
13. **受限环境（沙箱）网络连外部 LLM 服务会长时间卡顿**：AI agent 在受限网络环境（sandbox）里执行需要访问外部 LLM 端点（`OPENAI_API_BASE` 的 OpenAI 兼容 API、`curl .../models` 检查模型）或 GitHub 的 Bash 命令时，请求被沙箱网络策略拦下/挂起，表现为长时间无响应甚至"卡死"。解决：这类命令必须**直接请求外部网络**执行（在 Claude Code 中即允许 Bash 绕过网络沙箱/`dangerouslyDisableSandbox`，或先向用户请求网络权限），不要反复重试或加长 timeout 硬等。凡跑真实 LLM 的 bench（`scripts/run_bench.py` 等）、`server.py` 端到端、`curl` 探测端点的命令都属于此类；纯本地命令（pytest、本地文件操作）才可以在沙箱里跑。

---

## 9. 会话礼仪（与用户的相处规则）

- **不要 `git commit` / `git push`**，除非用户明确要求；其它 git 变更操作同理。
- **不要杀用户的 docker 容器 / server 进程 / 后台任务**，除非诊断确认且先告知用户。靶机容器（cyberorion_*）可能被用户的会话占用。
- **`.env` 永不打印 key**：读配置时只看变量名/端点，不在输出、日志、文档里回显 `OPENAI_API_KEY` 的值。
- 文档用中文、代码 docstring 用中文、commit message 随仓库既有风格。
- 改了架构/约定/命令，同步更新对应文档（`docs/*`、本文件）；本文件描述的命令都应当可执行——改了就验证。

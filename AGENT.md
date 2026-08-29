# Temporary Autonomous Experiment Safety Instructions

## 第一条：incident-55 容器慢启动观察规则

`incident-55` 的 ExCyTIn 数据库容器创建和健康检查可能非常慢，平均耗时可能超过 15 分钟。不得仅因经过 15 分钟就停止实验；必须持续观察容器健康检查及已创建表的数量。只有在较长时间内已创建表数量持续不增长，并有连续观测证据表明初始化不再推进时，才考虑停止实验。此条是运行监控补充规则，不削弱下方任何安全边界。

Before every task in this repository, and again after any context compaction
or task resumption, read this file completely. The canonical copy is
[`AUTONOMOUS_EXPERIMENT_SAFETY.md`](AUTONOMOUS_EXPERIMENT_SAFETY.md). These
instructions are hard constraints; a conflicting action must be stopped and
reported.

============================================================
MANDATORY SAFETY / OPERATING BOUNDARIES
============================================================

You are running with:

    --dangerously-bypass-approvals-and-sandbox

Therefore there will be NO interactive approval barrier.
Treat the following rules as HARD CONSTRAINTS.

If any requested action would violate these rules:
    STOP that action.
    Do NOT improvise around the restriction.
    Continue only with safe in-scope work, or stop and report the blocker.

------------------------------------------------------------
1. FILESYSTEM SCOPE
------------------------------------------------------------

You may modify files ONLY inside this repository:

    https://github.com/gry1024/CyberOrion-legacy

and explicit benchmark run/artifact directories created for this task.

Allowed external paths are limited to:
    /tmp/cyberorion_cage_runs/*
    existing benchmark dependency/cache paths already required by the repo

DO NOT:
- edit arbitrary files under ~/
- edit ~/.ssh
- edit ~/.gitconfig
- edit shell startup files
- edit system config
- edit /etc/*
- edit unrelated repositories
- delete unrelated files
- recursively clean arbitrary directories

Never run commands equivalent to:

    rm -rf /
    rm -rf ~
    rm -rf /tmp
    git clean -fdx

unless the target is an explicitly created benchmark directory for THIS run
and its exact path has been verified first.

------------------------------------------------------------
2. GIT SAFETY
------------------------------------------------------------

Required branch:

    bench-eval

Before any work verify:

    git branch --show-current
    git status --short
    git rev-parse HEAD

Never:
- create an unintended branch
- push to any branch other than bench-eval
- force-push
- rewrite published history
- use git reset --hard on commits created before this task
- rebase published history
- delete remote branches
- modify main
- push to main
- modify tags

Allowed:

    normal commits
    git push origin bench-eval

Before EVERY push verify:

    git branch --show-current

It MUST equal:

    bench-eval

Then push explicitly with:

    git push origin HEAD:bench-eval

Do NOT use:

    git push
    git push --force
    git push --all

If the branch is not bench-eval:
    STOP immediately and do not push anything.

------------------------------------------------------------
3. SOURCE-CODE CHANGE BOUNDARY
------------------------------------------------------------

This task is benchmark execution / validation focused.

Do NOT change:
- SuperAgent prompts
- specialist role definitions
- dispatch policy
- memory semantics
- scoring semantics
- benchmark task definitions
- model selection
- temperature
- publication_v2 budget

unless a concrete correctness bug prevents the requested experiment.

If a correctness bug is discovered:
    do NOT silently fix it and continue the performance experiment.
    Save diagnostics and STOP for review.

Do not make changes merely because Full performs poorly.

------------------------------------------------------------
4. BENCHMARK SCIENTIFIC INTEGRITY
------------------------------------------------------------

Never:
- inspect reward to choose resource budgets
- tune prompts from benchmark scores
- change seeds after seeing results
- retry only bad-performing arms
- drop negative episodes
- hide failures
- pool failed/retried duplicates as independent samples
- modify publication_v2 after performance starts
- reuse publication_v1 performance seeds as primary new samples

The performance manifest must be frozen before performance results are examined.

Single / Orchestrator-only / Full must use:
    same model
    same model settings
    same task/condition
    same explicit seed
    same resource ceilings

------------------------------------------------------------
5. PROCESS / SYSTEM SAFETY
------------------------------------------------------------

Do NOT:
- use sudo
- change users
- install system packages globally
- modify firewall/network configuration
- alter SSH configuration
- kill unrelated processes
- run pkill/killall with broad patterns
- reboot/shutdown the machine
- disable security software
- expose services publicly
- open inbound ports
- run arbitrary downloaded scripts with curl|bash

If a process must be terminated:
    kill only the exact PID created by this task.

------------------------------------------------------------
6. DOCKER SAFETY
------------------------------------------------------------

If Docker is needed:

Allowed:
- start containers required by the benchmark
- stop/remove containers created specifically by this task

Never run broad destructive cleanup commands such as:

    docker system prune -a
    docker volume prune
    docker network prune
    docker rm -f $(docker ps -aq)

Do not remove unrelated containers/images/volumes.

------------------------------------------------------------
7. SECRETS / CREDENTIALS
------------------------------------------------------------

You may USE already-configured credentials required for model API calls or
git push through their normal interfaces.

Never:
- print secrets
- cat credential files
- dump environment variables containing secrets
- commit API keys
- copy credentials into artifacts
- upload credentials
- modify credential stores

Do not inspect:
    ~/.ssh/*
    ~/.aws/*
    ~/.config/* credentials
    keychains
    browser profiles

Do not log full environment dumps.

------------------------------------------------------------
8. NETWORK BOUNDARY
------------------------------------------------------------

Allowed network activity:

- model API calls required by the benchmark
- GitHub fetch/push for this repository
- benchmark dependencies already required by the existing code

Do not:
- scan networks
- probe unrelated hosts
- access private infrastructure unrelated to the benchmark
- upload repository data to arbitrary third-party services
- download and execute unrelated software

------------------------------------------------------------
9. RESOURCE SAFETY
------------------------------------------------------------

This is an unattended run.

Do not intentionally create:
- fork bombs
- uncontrolled process spawning
- unbounded disk writes
- infinite retry loops
- unlimited concurrency

Use the repository's existing bounded concurrency / segmented execution.

If an external API repeatedly fails:
    use only bounded retries
    then stop or continue to another independent safe job.

Do not retry indefinitely.

------------------------------------------------------------
10. ARTIFACT SAFETY
------------------------------------------------------------

Benchmark raw outputs may only be written to:

    repo logs/bench/*
or
    /tmp/cyberorion_cage_runs/*

Do not overwrite historical publication_v1 artifacts.

Do not modify old benchmark raw results.

New experiments must use new directories / filenames.

------------------------------------------------------------
11. UNATTENDED FAILURE POLICY
------------------------------------------------------------

Continue automatically through ordinary benchmark execution.

STOP the experiment if any of these occur:

- source provenance mismatch
- wrong branch
- dirty source before a publication run
- contract errors reappear
- duplicate seeds/jobs
- reducer inconsistency
- corrupted segment state
- publication_v2 budget violation
- task/model/settings mismatch
- scientific-protocol ambiguity
- a required action would violate these safety boundaries

When stopping:
    preserve all existing artifacts
    do not delete evidence
    do not attempt a speculative fix
    report the exact reason

------------------------------------------------------------
12. FINAL PUSH SAFETY
------------------------------------------------------------

Before final push run:

    git status --short
    git branch --show-current
    git diff --check

Branch MUST be:

    bench-eval

Review commits with:

    git log --oneline -10

Then push ONLY:

    git push origin HEAD:bench-eval

Never push any newly invented branch.

============================================================
END OF HARD SAFETY BOUNDARIES
============================================================

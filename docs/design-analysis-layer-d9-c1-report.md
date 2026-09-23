# PL-DA-D9-C1 — Frozen Agent Adoption Re-observation

状态：**D9-C1 = HOLD / experiment invalid: agent transport unavailable**  
分支：`feature/analysis-layer`  
日期：2026-09-23

## 1. Frozen execution

按 D8 原始 runner 执行了一次且仅一次完整批次：6 cases × B/D 两臂 = **12 real
Agent run attempts**，产物位于
`experiments/analysis_layer/d8/results/D8-20260923T061603Z/`。

冻结项保持为 D8 的 fixture、task prompts、neutral system prompt、allowed tools、
working-directory pattern、timeout（240 s）、B/D arm preparation 和 oracle。产品侧唯一
预期差异是 D9-B3 后 `explain-symbol --help` 的已冻结 capability description。
实验前记录了 `cli.py`、`query.py`、runner 和 audit script 的 SHA-256，并通过 runner/
reattribution/audit script 的 `py_compile`。批次启动后未修改产品、prompt、help、
selector、analysis implementation 或实验条件。

## 2. Invalidity finding

所有 12 attempts 在任何 tool call 前失败：

```text
API Error: Connection refused — a firewall or proxy may be blocking it
```

每个 run 的 `exit_reason=agent_nonzero_exit`、`tool_calls_total=0`、tokens 为空。
因此 runner 的 D-arm rows 全部按既有 D8 纪律标为 `INFRA_EXCLUDED`；它们不是
`ADOPTION_GAP`、`NO_INCREMENTAL_VALUE` 或 `CAPABILITY_GAP`。

这也是唯一一次 C1 batch；没有为获得可用样本而追加第三批/第四批或修改 runtime。

## 3. Required observation record

冻结审计器 `d9_c1_audit.py` 将 discovery、request 与 answer use 拆开，并写入
`d9-c1-observation.json`。六个 D-arm records 的共同结果为：

| Case | task success | discovered | requested | feature / surface / symbol | used in answer | verdict |
| --- | --- | --- | --- | --- | --- | --- |
| A1 | no | no | no | — | no | INFRA_EXCLUDED |
| A2 | no | no | no | — | no | INFRA_EXCLUDED |
| B1 | no | no | no | — | no | INFRA_EXCLUDED |
| B2 | no | no | no | — | no | INFRA_EXCLUDED |
| C1 | no | no | no | — | no | INFRA_EXCLUDED |
| C2 | no | no | no | — | no | INFRA_EXCLUDED |

`DISCOVERY_OBSERVED=0` here is not evidence that B3 is ineffective: the Agent
never reached a command/help invocation. It is an unobservable field under this
transport failure.

## 4. D8 comparison

| Case | D8 verdict | C1 verdict | D8 analysis used | C1 analysis used | Change interpretation |
| --- | --- | --- | --- | --- | --- |
| A1 | UTILITY_OBSERVED | INFRA_EXCLUDED | yes | no telemetry | not comparable |
| A2 | ADOPTION_GAP | INFRA_EXCLUDED | no | no telemetry | not comparable |
| B1 | UTILITY_OBSERVED | INFRA_EXCLUDED | yes | no telemetry | not comparable |
| B2 | ADOPTION_GAP | INFRA_EXCLUDED | no | no telemetry | not comparable |
| C1 | UTILITY_OBSERVED | INFRA_EXCLUDED | yes | no telemetry | not comparable |
| C2 | UTILITY_OBSERVED | INFRA_EXCLUDED | yes | no telemetry | not comparable |

Counts:

```text
D8:    UTILITY_OBSERVED=4, ADOPTION_GAP=2,
       NO_INCREMENTAL_VALUE=0, CAPABILITY_GAP=0
C1:    INFRA_EXCLUDED=6 D-arm cases; valid discovery/request/use observations=0
```

No conclusion can be drawn about whether B3 improved A2/B2 adoption, whether the
four existing utility cases regressed, or whether a new capability gap/DFG signal
exists. The observed failure precedes all analysis behavior and is not causally
attributable to B3.

## 5. Artifacts and stop condition

- Batch summary: `experiments/analysis_layer/d8/results/D8-20260923T061603Z/batch-summary.json`
- Per-run records: `runs/<case>-<arm>/run.json` and `agent-output.txt`
- Structured C1 audit: `d9-c1-observation.json`
- Audit instrumentation: `experiments/analysis_layer/d8/d9_c1_audit.py`

The required 12 frozen attempts are complete and the experiment is auditable, but
none is a valid Agent run. Stop here. A future retry requires external recovery of
the Claude/backend firewall or proxy path and a newly authorized frozen batch;
until then, do not alter discovery, prompts, analysis, or start D9-C2.

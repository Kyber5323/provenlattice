# D9 Context Bundle Formal Harness

Scope: the formal harness line that started as the D9-C5 execution contract and
was extended through D9-C6…C10. The original C5 content below is retained
unchanged; the C6–C10 timeline records each stage's final status and primary
evidence path only.

## D9 C6–C10 timeline（最终状态 + 主要 evidence path）

| 阶段 | 最终状态 | 主要 evidence path |
| --- | --- | --- |
| C6 formal execution | treatment 未暴露 → adoption gap（no exposure → 0 adoption）；adoption funnel L1–L5 由此冻结 | `formal_c6b.py`、`formal_c6c1.py`…`formal_c6c4.py`；funnel 契约冻结于 `d9-c7/d9-c7-a-exposure-contract.json`（`basis_frozen_from_d9_c6`） |
| C7 discovery exposure | passive discovery 仍无调用：C7-B 一行 pointer 36/36 cells 投递、0/36 agent 执行任何 PL 命令；C7-D 结构化 tool card 投递、0 action（均 PERMANENT_NEGATIVE_RESULT / RETAIN） | `d9-c7/c7-b-negative-finding.json`、`d9-c7/c7-d-negative-finding.json`、`d9-c7/c7-c-discovery-surface.json` |
| C8 native MCP exposure | 成立：C8-A native 单工具 `mcp__provenlattice__query` 暴露合约冻结合规（stdio MCP，`--mcp-config` + `--strict-mcp-config`，probe 实证、无模拟 registry；C8-B 正式批注册可见 36/36 cells）；同批 L0–L5 调用仍为 0，negative finding 在案保留 | `d9-c8/d9-c8-a-native-tool-exposure-contract.json`、`d9-c8/c8-b-negative-finding.json`；服务端 `provenlattice_mcp_server.py` |
| C9 normalization-v2 | raw≡normalized closure 成立：raw facts → normalization-v2 → returned facts 闭环，fixtures 为 P0b 归档 raw tool response 的逐字节副本（sha256 锚定） | `formal_c9c1.py`、`formal_c9c2_runner.py`、`test_formal_c9c1.py`、`d9-c9/c9-c1-normalization-contract-report.json` |
| C10 controlled capability protocol | 受控协议 + 双臂隔离 + oracle-v2 成立；P2（6 case × 2 arm × r1）= 12/12 cells、6/6 pairs protocol VALID、Arm A facts=0 ×6 / Arm B facts>0 ×6、0 leakage / 0 hidden retry → PASS（2026-10-09） | `d9-c10/controlled-capability-protocol.json`、`d9-c10/assignment-sealed.json`、`d9-c10/c10_oracle_v2.py`、`d9-c10/oracle-v2/`、`formal_c10d_p2.py`；综合见 `docs/context-bundle-d9-c6-c10-synthesis.md` |

Natural adoption 与 controlled capability 是不同结论：C6/C7/C8-B 的 negative
findings（passive surface 下 0 主动调用）与 C10 的 controlled PASS（受控双臂下
semantic-facts 能力可达且稳定）并存，互不覆盖；任何一方不得改写另一方。

---

以下为原始 D9-C5 formal harness 内容（保留不变）：

Artifact namespace after authorization: `artifacts/formal/<cell_id>/` holds
immutable `raw/stream.jsonl`, `normalized-trace.json`, `review-packets/`,
independent reviewer submissions, evaluator output, and a cell record. The
root `formal-manifest.json` contains the 36 planned provenance records.

`formal_harness.py` schemas are: `d9-c5-formal-manifest-v1`,
`d9-c5-normalized-trace-v1`, `d9-c5-blinded-packet-v1`, and reviewer schema
`d9-c5-review-v1`. Qualification tests use synthetic strings only and never
open, checkout, or execute a frozen ContextBench case.

`formal_runner.py` has no `--config` mode. Its only formal input route is
`--formal-root <canonical-formal-root> --binding <execution-binding> --output
<artifacts> --cell-id <id>` followed by either `--execute` or
`--materialize-only`. `--validate-invocation` uses the same canonical and
binding validation but stops before workspace materialization. The runner verifies the canonical artifact hashes, then
checks `--binding-freeze` against the exact binding SHA, then resolves the
local binding into a sealed environment. Before any worktree operation it
requires a wrapper-issued `--dispatch-attestation` validated against a local
`--dispatch-context-freeze`; this verifies the approved runner identity and
binding SHA without modelling UAC elevation. Raw `runtime_roots` and `target_repositories` are never formal CLI
inputs. Synthetic qualification helpers remain internal to `formal_harness.py`.

Each dispatch materializes `worktrees/<stable-cell-hash>/` as a detached, clean
checkout at the manifest base commit. Existing workspaces and either completed
or incomplete artifacts for the selected cell fail closed; completed artifacts
for other cells do not block a later cell.

`--materialize-only --formal-root <canonical-formal-root> --binding <binding> --binding-freeze <freeze-evidence> --cell-id <id>` invokes the
same `prepare_cell_execution()` lifecycle used by dispatch, then writes a
readiness artifact under `readiness/materialization/`. It does not enter the
executor branch and never writes `cells/*/cell-record.json` or `incomplete.json`.

Every formal Git operation uses command-scoped `git -c safe.directory=<exact
canonical approved path>`. Source and worktree paths are independently
canonicalized and validated; the harness never writes global/system Git config
and never uses wildcard trust.

# D9 Context Bundle C6–C10 Synthesis

> 汇总日期：2026-10-10 · 基线：main（post-V1.2 development checkpoint）
> 性质：对 `experiments/context_bundle_v0/formal_harness/` C6–C10 各阶段
> **最终状态**的汇总，只引用既有冻结 artifact，不产生新实验结论。
> 各阶段运行器、合约与 negative finding 明细见
> `experiments/context_bundle_v0/formal_harness/README.md`。

## 1. 阶段概括

| 阶段 | 最终状态 |
| --- | --- |
| **C6 formal execution** | treatment 未暴露 → adoption gap：no exposure → 0 adoption。adoption funnel（L1 CLI discovered → L2 CLI invoked → L3 task-context requested → L4 facts returned → L5 facts used）自 C6 冻结，后续阶段共用同一检测器。 |
| **C7 discovery exposure** | passive discovery 仍无调用：C7-B 一行 pointer 经 `--append-system-prompt` 字节投递、四项对称不变量 36/36 通过，0/36 agent 执行任何 ProvenLattice 命令；C7-D 结构化 tool card 投递、0 action。两条 negative finding 均 PERMANENT_NEGATIVE_RESULT / RETAIN。 |
| **C8 native MCP exposure** | 成立：C8-A 冻结合约将 ProvenLattice 作为一等 native MCP 工具（`mcp__provenlattice__query`，stdio 传输，`--mcp-config` + `--strict-mcp-config`，probe 实证注册、无模拟 registry，byte-identical envelope）；C8-B 正式批中注册对 36/36 cells 可见，同批 L0–L5 实际调用仍为 0（negative finding 在案保留）。 |
| **C9 normalization-v2** | raw≡normalized closure 成立：raw facts → normalization-v2 → returned facts 闭环；fixtures 为 P0b 归档 raw tool response 的逐字节副本（sha256 锚定），raw≡normalized 校验可机器复验。 |
| **C10 controlled capability protocol** | 受控协议成立：双臂（Arm A = Base Graph、Arm B = + semantic facts）、逐臂独立 home/tmp 内存隔离、assignment sealed、oracle-v2（BUG_PRESENT/FIX_PRESENT 区分，gold diff 锚定）、workspace v2；0 leakage / 0 hidden retry 纪律。 |

## 2. P2 受控结果

D9-C10 P2（2026-10-09，6 case × 2 arm × r1，zstd 六 case）：

- **12/12 cells、6/6 pairs protocol VALID**；
- Arm A semantic facts = 0 ×6、Arm B facts > 0 ×6、raw≡normalized ×6；
- oracle-v2 12/12；0 leakage / 0 hidden retry → **PASS**；
- Case 6 Arm B 因磁盘耗尽阻断后按 RESUME_CASE6_B_ONLY 补跑完成
  （fresh sealed repo/home/session，attempt_count=1）。

## 3. 最终解释（边界声明）

- **Capability reachability / stability = supported**：在受控双臂协议下，
  semantic-facts 面（task-context-v0 → normalization-v2 → 返回事实）可达、
  稳定且可由 oracle-v2 判别——这是 C10 PASS 支持的唯一命题。
- **Natural adoption / outcome improvement = separate question**：C6/C7/C8-B
  的 negative findings 表明，在自然使用面（passive discovery、无指令）下
  Agent 主动调用 ProvenLattice 的行为并未因暴露面增强而出现；该问题仍未闭合，
  属于后续 natural-use 实验（如 colleague 16-case rerun、真实任务观察）的范畴。
- 因此：**controlled PASS 不得表述为真实任务 outcome win**，也不得与
  ContextBench zstd 16-case 重验（14/16 resolved、2 infra-excluded、0 真实失败，
  diagnostic）或 D8-RV1（CASE_SOLVED_WITHOUT_PROVENLATTICE ×2）的观察互相改写。

## 4. Evidence 索引

| 主题 | 位置 |
| --- | --- |
| 各阶段运行器 | `experiments/context_bundle_v0/formal_harness/formal_c6*.py`、`formal_c7*.py`、`formal_c8*.py`、`formal_c9*.py`、`formal_c10*.py` |
| C7/C8 negative findings | `experiments/context_bundle_v0/formal_harness/d9-c7/`、`d9-c8/` |
| C9 归一化合约报告 | `experiments/context_bundle_v0/formal_harness/d9-c9/c9-c1-normalization-contract-report.json` |
| C10 协议 / 隔离 / oracle-v2 | `experiments/context_bundle_v0/formal_harness/d9-c10/` |
| 阶段时间线 | `experiments/context_bundle_v0/formal_harness/README.md` |
| Post-V1.2 综合报告 | `docs/report-post-v1.2.md` |

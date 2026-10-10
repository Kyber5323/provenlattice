# ProvenLattice Post-V1.2 能力与实验综合报告

> 汇报日期：2026-10-10 · 基线：`main@6df7f32`
> 性质：**development checkpoint reconciliation**——不是正式 V1.3 qualification。
> 最新正式发布仍为 **SQI-V1.2 = QUALIFIED / RELEASE READY**（2026-09-20，tag
> `provenlattice-sqi-v1.2`）；V1.3 尚未打 tag、尚未举行正式 qualification 汇总。
> V1.2 正式资格一键验证：`python experiments/query_interface_v1/tools/verify_release.py`。
> 本报告只汇总既有冻结/在案 artifact，不产生新实验结论。

---

## 1. 时间线总览（V1.2 → Post-V1.2）

| # | 阶段 | 日期 | 内容 | 状态 / 性质 |
| --- | --- | --- | --- | --- |
| 1 | SQI-V1.2 成本优化资格 | 2026-09-20 | SQI 18/18、9/9 frozen floors、单 backend 36-cell clean batch；input −36% / output −58% / cache-read −73% | **QUALIFIED / RELEASE READY（formal）** |
| 2 | Analysis Layer D0–D8 | 2026-09-22 | 可选函数级分析层：`analyze --scope --features ast,cfg` + `analysis` 六态只读查询 + `explain-symbol --analysis` | **QUALIFIED（Python-only，formal）** |
| 3 | D8-RV1 | 2026-10-09 | D8 历史 ADOPTION_GAP case（A2/B2）新版本自然使用重验 | CASE_SOLVED_WITHOUT_PROVENLATTICE ×2（**diagnostic**，如实在案） |
| 4 | Context Bundle / task-context（D9-C1/C2） | 2026-09-23 | `task-context <symbols> --json` → task-context-v0 事实包 | implemented（**diagnostic** probe；formal 协议内验证见 #6） |
| 5 | Native MCP（D9-C7/C8） | — | 单工具 `mcp__provenlattice__query` 透明桥接，C8-A 冻结合约 byte-identical；C8-B 正式批漏斗证据 | implemented（**formal** 合约） |
| 6 | normalization-v2（D9-C9） | — | Agent trace → 事实级规范化，raw≡normalized 校验（C10 内 ×6） | implemented（**formal**） |
| 7 | D9-C10 P2 受控能力稳定性 | 2026-10-09 | 6 case × 2 arm × r1（zstd 六 case），oracle-v2 | **PASS（formal）**：12/12 cells、6/6 pairs VALID、Arm A facts=0 ×6 / Arm B facts>0 ×6、0 leakage / 0 hidden retry |
| 8 | ContextBench zstd 16-case 重验 | 2026-10-09 | 新版本自然使用重跑 16 case | **14/16 resolved、0 真实失败、2 infra-excluded**（**diagnostic**；≠16/16，对比历史 13/16 无回归） |
| 9 | C 分析链 A1–A3.6 | 2026-10-10 | C normalized AST + function-local CFG V1 + 短路 + 调用语义分类 + definition-preference 紧凑上下文（`codex/c-analysis-a1` 已并入 main） | 已集成 main（**diagnostic**；全量回归 254/254） |
| 10 | Colleague R1 revalidation | pending | 16-case 环境受控重跑（wheel/manifest/guide 已冻结） | **PENDING**（结果不预写） |

---

## 2. 各阶段要点

### 2.1 起点：SQI-V1.2（formal，最新正式发布）

- SQI **18/18**（T01–T06 全部 3/3）、9/9 frozen floors PASS、verdict QUALIFIED；
  C4-R4 clean 36-cell batch、单 backend（deepseek-flash x36）、逐 cell provenance。
- 成本 vs V1.1 baseline：input −36% / output −58% / cache-read −73%；
  cache amplification 15.3 → 6.4；SQI calls 59 → 49；envelope bytes −26%。
- 详证：`docs/releases/sqi-v1.2.md`、`docs/report-v1.2.md`（V1.2 历史汇报）。

### 2.2 Analysis Layer D0–D8（formal，Python-only）

- Foundation Graph 契约不变：analysis 表惰性创建、基础源码零修改、
  Base Graph 零影响回归 137/137（D0–D7 全链 + 六态只读查询 + Agent 消费）。
- D8 真实 Agent 任务观察：UTILITY_OBSERVED 4/6、ADOPTION_GAP 2/6、
  CAPABILITY_GAP 0。
- 报告：`docs/design-analysis-layer-d0-audit.md` … `design-analysis-layer-d8-report.md`；
  结果：`experiments/analysis_layer/d8/results/D8-20260922T091934Z/`、
  `D8-20260923T061603Z/`。
- 复现入口：`experiments/analysis_layer/d8/d8_runner.py`。

### 2.3 D8-RV1（diagnostic）

- 历史 ADOPTION_GAP case（A2 merge/fallthrough、B2 break-vs-return）在
  新版本下自然使用重验：两例均 **CASE_SOLVED_WITHOUT_PROVENLATTICE**。
- 如实在案、不计能力 win；证据：`experiments/analysis_layer/d8/results/
  D8-RV1-20261009T083159Z/`、`D8-RV1-20261009T083452Z/`。
- 复现入口：`experiments/analysis_layer/d8/d8_rv1_runner.py`。

### 2.4 Context Bundle / task-context（D9-C1/C2）

- `task-context <symbols> --json` → task-context-v0：primary_targets /
  source_spans / relations / candidates / unresolved，每条 fact 带
  fact_id + evidence_ids（可追溯，无合成 ID）。
- D9-C1 传输门 + T3 三跑 probe 在案：`experiments/analysis_layer/d8/results/
  D9-C1-T3-20260923T074912Z/`、`D9-C1-TRANSPORT-*/`。
- 设计：`docs/design-analysis-layer-d9-c1-report.md`、
  `docs/design-context-bundle-d9-c2-prereg.md`。

### 2.5 Native MCP + normalization-v2（D9-C7/C8/C9）

- MCP：单工具 `mcp__provenlattice__query`（`command`+`args` 透传），
  C8-A 冻结合约 byte-identical；C8-B 正式批 L0–L5 漏斗证据。
  服务端：`experiments/context_bundle_v0/formal_harness/provenlattice_mcp_server.py`。
- normalization-v2：Agent trace → 事实级观测；raw≡normalized 校验
  （C10 内 ×6）。实现：`formal_c9c1.py`、`formal_c9c2_runner.py`。

### 2.6 D9-C10 P2 受控能力稳定性（formal，PASS）

- 6 case × 2 arm × r1 = **12/12 cells、6/6 pairs protocol VALID**；
  Arm A semantic facts=0 ×6、Arm B facts>0 ×6、raw≡normalized ×6、
  oracle-v2 12/12、0 leakage / 0 hidden retry → **PASS（2026-10-09）**。
- Case 6 Arm B 因磁盘耗尽阻断后按 RESUME_CASE6_B_ONLY 补跑完成
  （fresh sealed repo/home/session，attempt_count=1）。
- 协议与证据：`experiments/context_bundle_v0/formal_harness/d9-c10/
  controlled-capability-protocol.json`、`assignment-sealed.json`、
  `c10-b1-verdict.json`、`c10-b2-amendment.json`。
- 复现入口：`experiments/context_bundle_v0/formal_harness/formal_c10d_p2.py`。

### 2.7 ContextBench zstd 16-case 重验（diagnostic，≠16/16）

- provenlattice 臂新版自然使用：**14/16 resolved、0 真实失败、
  2 case infra-excluded**——**14 PASS + 2 INFRA ≠ 16/16**。
- 2 个 infra-excluded case（35ed56f6 / 8bffb1b1）：2016 版 CLI benchmark 在
  MSYS2 管道下挂死，官方 reference fix 同样挂死（证据存档于批目录
  `evidence-infra-limited/`），如实归因为验证环境限制。
- 对比 2026-09-21 历史 13/16：**无回归**；历史 3 个失败 case
  （zstd_002/003/004）对账 = 全部 test_patch_apply_failed 苛判产物
  （HARNESS_OR_DATA_FAILURE_RESOLVED + SOLVED_WITHOUT_PL）。
- 证据（外部批目录）：`benchmark-contextbench-zstd/`
  （`revalidation-summary.json`、`infra-limited-classification.json`、
  `historical-failure-attribution-audit.json`）。
- 复现入口：`benchmark-contextbench-zstd/rv1-harness.py`
  （原 harness 在 2026-10-08/09 磁盘恢复中丢失，按冻结指南协议重建；
  agent patch 字节精确应用到 verify tree，历史结果原样保留）。

### 2.8 C deep analysis A1–A3.6（diagnostic，已集成 main）

| 步骤 | commit | 内容 |
| --- | --- | --- |
| A1 | `235ef4e` | Optional C normalized AST（`.c`/`.h`，`ast:c`；tree-sitter kinds 仅存于 `parser_kind`） |
| A2 | `d0297dc` | 函数内 statement 级 CFG（`cfg:c`）：sequence、if/else、循环、break/continue、switch/fallthrough、goto/label、implicit exit |
| A2.1 | `90db29f` | CFG 消除 raw parser 依赖 + 表达式级短路（`&&`/`||`/`?:`） |
| A3 | `61b5012` | 调用语义分类（direct/indirect/unresolved）——indirect 调用永不 name-bind；foundation `call_kind` metadata（schema 不变） |
| A3.5 | `ebecea0` | 真实 zstd diagnostic：两个 INFRA_LIMITED case（35ed56f6 / 8bffb1b1）上 current facts appear sufficient；def-use 未被真实 case 证明必要 |
| A3.6 | `af6d66d` | definition-preference retrieval + compact analysis summary（符号级 AST/CFG/调用摘要，只读不构建） |

- 集成审计：`docs/c-analysis-release-r1-integration-audit.md`
  （`codex/c-analysis-a1` 完整 unsquashed chain 已并入 main）。
- 验证：全量回归 **254/254 PASS**；真实 zstd 索引/查询 diagnostic 通过
  （false direct bindings = 0）。
- **诚实边界**：
  - A3 将间接调用**如实分类为 indirect（永不 name-bind）**；这是调用语义
    识别，**不是精确 function-pointer target 解析**。
  - def-use（A4）= **not justified yet**（`A4_DEFUSE_NOT_YET_JUSTIFIED`），
    真实 case 未证明必要；**不是已规划必做项**。
  - 明确未支持：精确 function-pointer target 解析、points-to、def-use、
    taint、Clang 语义增强、compile_commands 感知分析、跨过程 CFG、
    C++ deep analysis。
- 报告：`docs/design-analysis-layer-c-a1-report.md` … `c-a36-report.md`
  （含各 audit）+ `docs/design-analysis-layer-c-a35-zstd-diagnostic.md`。

---

## 3. 待办与下一步

- **Colleague R1 revalidation = current next experiment**：环境受控重跑全部
  16 个 ContextBench-zstd case，与 2026-10-09 本地批（14/16、2 infra-limited、
  0 真实失败）对比。入口：
  - manifest：`revalidation/c-contextbench-zstd-r1-manifest.json`
    （commit `8e194ab`、wheel sha256 在案、结果不预写）
  - guide：`docs/c-analysis-colleague-revalidation-guide.md`
  - 包：`revalidation/packages/provenlattice-1.0.0-py3-none-any.whl`
- V1.3 正式 qualification 汇总 + tag：待 revalidation 之后再收敛；
  在此之前**不宣称正式 V1.3 qualification**。
- 未来候选（未排期、不承诺）：def-use / local data flow（not justified yet）、
  points-to / 精确 function-pointer target 解析、taint、Clang /
  compile_commands 语义增强、C++ deep analysis、Overlay V1.1 正式资格。

## 4. 证据索引

| 主题 | 位置 |
| --- | --- |
| V1.2 历史汇报 | `docs/report-v1.2.md` |
| 分析层 D0–D8 报告 | `docs/design-analysis-layer-d*-report.md` |
| D8/D8-RV1 结果 | `experiments/analysis_layer/d8/results/` |
| D9 C6–C10 formal harness | `experiments/context_bundle_v0/formal_harness/` |
| zstd 16-case 外部批目录 | `benchmark-contextbench-zstd/` |
| C 链 A1–A3.6 报告/审计 | `docs/design-analysis-layer-c-a*-report.md` / `*-audit.md` |
| C 集成审计 | `docs/c-analysis-release-r1-integration-audit.md` |
| R1 revalidation | `revalidation/` + `docs/c-analysis-colleague-revalidation-guide.md` |
| 复现矩阵总入口 | `REPRODUCIBILITY.md` §3 |

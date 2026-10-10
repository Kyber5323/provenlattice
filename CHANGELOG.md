# Changelog

所有已发布里程碑按时间倒序记录。tag 命名遵循 `provenlattice-*` 前缀约定；
各阶段的"已完成 / 明确未包含"以对应发布文档为准（`docs/releases/`）。

## Post-V1.2 C Analysis Development（2026-10-10，codex/c-analysis-a1 分支）

**Post-V1.2 development checkpoint**：在 V1.3 开发态（analysis layer +
context bundle）之上扩展 C deep analysis。这是开发进展记录，**不是新的
formal V1.3 qualification**；尚未完成同环境 16-case Agent revalidation。

- **C 能力链（A1→A3.6，commits 235ef4e→af6d66d）**：C deep analysis 已从
  基础 symbol graph 扩展到 AST / CFG / call semantics：
  - A1 `235ef4e`：Optional C normalized AST（`.c`/`.h`，`ast:c`，
    tree-sitter kinds 仅存于 `parser_kind`）。
  - A2 `d0297dc`：函数内 statement 级 CFG（`cfg:c`）——sequence、if/else、
    while/do/for、break/continue、switch/case/default/fallthrough、
    goto/label、implicit exit。
  - A2.1 `90db29f`：CFG 消除 raw parser 依赖 + 表达式级短路
    （`&&`/`||`/`?:`）。
  - A3 `61b5012`：调用语义分类（direct/indirect/unresolved）——indirect
    调用永不 name-bind；foundation `call_kind` metadata（schema 不变）。
  - A3.5 `ebecea0`：真实 zstd diagnostic —— 两个 INFRA_LIMITED case
    （35ed56f6 / 8bffb1b1）上 **current facts appear sufficient**；
    def-use 未被真实 case 证明必要（`A4_DEFUSE_NOT_YET_JUSTIFIED`）。
  - A3.6 `af6d66d`：definition-preference retrieval + compact analysis
    summary（符号级 AST/CFG/调用摘要，只读不构建）。
- **明确未支持**：精确 function-pointer target 解析、points-to、def-use、
  taint、Clang 语义增强、compile_commands 感知分析、跨过程 CFG。
- **验证**：全量回归 254/254 PASS；真实 zstd 索引/查询 diagnostic 通过
  （false direct bindings = 0）。
- **待办**：同环境 16-case Agent revalidation（见
  `revalidation/c-contextbench-zstd-r1-manifest.json` 与
  `docs/c-analysis-colleague-revalidation-guide.md`）。

## provenlattice-v1.3（2026-10-09，experiment/context-bundle 分支）— ANALYSIS LAYER + CONTEXT BUNDLE（开发态）

**V1.3 Optional Function Analysis Layer + Context Bundle**：Foundation Graph
契约不变的前提下新增五层能力栈中的 L2–L5。未打 tag（正式 qualification 收敛后发布）。

- **分析层（D0–D8）**：`analyze --scope --features ast,cfg` 派生物化 + `analysis`
  六态只读查询 + `explain-symbol --analysis` Agent 消费；Python-only
  （C/C++ 解析器已有、分析器待接入，README 如实记录）；Base Graph 零影响回归
  137/137 → 全仓 165/165。
- **上下文事实包（D9-C1/C2）**：`task-context <symbols> --json` →
  task-context-v0（primary_targets / source_spans / relations / candidates /
  unresolved，全量 fact_id + evidence_ids 可追溯）。
- **Native MCP 暴露（D9-C7/C8）**：单工具 `mcp__provenlattice__query` 透明桥接
  （C8-A 冻结合约 byte-identical）；D9-C8-B 正式批 L0-L5 漏斗证据。
- **normalization-v2（D9-C9）**：Agent trace → 事实级规范化，raw≡normalized 校验。
- **D9-C10 P2 受控能力稳定性**：6 case × 2 arm × r1（zstd 六 case），12/12 cells、
  6/6 pairs protocol VALID；Arm A semantic facts=0 ×6、Arm B facts>0 ×6、
  raw≡normalized ×6、oracle-v2 12/12、0 leakage / 0 hidden retry → **PASS**。
  Case 6 Arm B 因磁盘耗尽阻断后按 RESUME_CASE6_B_ONLY 补跑完成（fresh sealed
  repo/home/session，attempt_count=1）。
- **ContextBench zstd 16-case 重验（2026-10-09）**：provenlattice 臂新版自然使用，
  **14/16 resolved、0 真实失败、2 case 因 2016 版 CLI benchmark 在 MSYS2 管道下
  挂死而 infra-excluded**（官方 reference fix 同样挂死，证据存档）；对比 2026-09-21
  历史 13/16 无回归。历史 3 个失败 case（zstd_002/003/004）对账 = 全部
  test_patch_apply_failed 苛判产物 → HARNESS_OR_DATA_FAILURE_RESOLVED +
  SOLVED_WITHOUT_PL（审计文档见 benchmark 批目录）。
- **D8-RV1**：D8 历史 ADOPTION_GAP case（A2/B2）新版本自然使用重验 →
  CASE_SOLVED_WITHOUT_PROVENLATTICE ×2（如实在案）。
- **明确未包含**：分析层 C/C++ 接入（next）、Def-Use/Data Flow（V1.4）、
  Taint（V1.5）、正式 tag/qualification 汇总文档。

## provenlattice-sqi-v1.2（2026-09-20）— QUALIFIED / RELEASE READY

**SQI-V1.2 Cost Optimization Qualification**（结构化查询接口 V1.2 成本优化资格认证）：

- **SQI 18/18**（T01-T06 全部 3/3），9/9 frozen capability floors PASS，
  verdict **QUALIFIED**；完整 clean 36-cell batch（C4-R4，
  `SQI-FORMAL-C4-20260920T093328Z`），单 backend（deepseek-flash x36），
  逐 cell model provenance，零 sensitive leakage。
- **成本收益（vs V1.1 baseline）**：input -36%（962,640 → 616,598）、
  output -58%（230,508 → 97,249）、cache-read -73%（14.74M → 3.95M）、
  cache amplification 15.3 → 6.4、SQI calls 59 → 49、envelope bytes -26%。
- **V1.2-NR2 通用负区修复**（无任务特判、无硬编码 required id、无 oracle
  变更）：空结果 call-type-scoped 解析语义指导（result_meta）、同 anchor
  换预算重查的子集条款、knowledge-to-code 复合查询接口文档、会话级
  usage_discipline 第三条规则。T05.sqi 调用数 6-20/rep → 5/2/6。
- **验证状态**：97/97 tests（含 NR1×6、NR2×11）、implementation +
  secondary seal 重封、verify_release PASS、isolation baseline
  `27e40df5…` 不变。评审：`experiments/query_interface_v1/reviews/
  v1.2-final-qualification-review.md`；综合：
  `results/formal/C4-R4-SYNTHESIS-20260920-175458.json`。
- 明确未包含：oracle/contract 语义变更、S3+ scale bands、第四个
  benchmark 仓库、Overlay V1.1。

## 历史里程碑

| Tag | 日期 | 内容 |
| --- | --- | --- |
| `provenlattice-sqi-v1-repro` | 2026-09-17 | SQI-V1 reproducibility baseline（RQ1/RQ2/SQI-V1 全 QUALIFIED） |
| `provenlattice-sqi-v1` | 2026-09-17 | SQI-V1 结构化查询接口正式资格（Native vs SQI 36 cells） |
| `provenlattice-v1.0` | 2026-09-13 | Knowledge Layer / Cross-Layer Evidence 基线 |
| `provenlattice-core-v0.4` | — | Branch / Session Overlay |
| `provenlattice-core-v0.3` | — | Core 增量/shard 基线 |

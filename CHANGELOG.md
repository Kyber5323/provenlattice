# Changelog

所有已发布里程碑按时间倒序记录。tag 命名遵循 `provenlattice-*` 前缀约定；
各阶段的"已完成 / 明确未包含"以对应发布文档为准（`docs/releases/`）。

## ProvenLattice 1.7.0 — 并发图谱事实与缺陷精度（2026-11-05）

> 状态：待发布（tag 待 Owner 确定；基线 commit `2cb54466`）。PyPI 发布独立决策。

**1.7.0 面向并发类漏洞检测，补齐图谱的并发语义事实、关联能力与精度护栏**。在 1.6.0
的语法/控制流结构之上，让 race、lock_order、resource_lifetime、间接调用四类并发缺陷
从「能看结构」变为「能证事实」——每个候选可溯源、可排序、可克制（fail-closed）。

### 并发语义事实（事实层）

- 一等访问类型（R1）：`access_kind`（READ / WRITE / READ_MODIFY_WRITE），
  并发冲突判定的基础事实。
- 锁集合与锁身份（R4）：per-access `held_locks` + 锁身份关系
  （SAME_LOCK / DIFFERENT_LOCK / PROTECTS）。
- 对象身份与受限地址流（R2/R3）：canonical storage identity +
  query-time `DEREF_RESOLVES_TO` + must-only `same_storage`，消除指针别名下
  「同一对象」的歧义（不做完整 points-to / StorageObject 重构）。
- 锁/资源生命周期（R7）：生命周期序 + `is_alive_at_access`（spawn→join 顺序；
  前置 FE-5 THREAD_JOIN handle 身份）。
- 间接调用目标（R6）：间接 field-callee 的 `is_indirect` READ 事实 +
  查询期 `MAY_CALL` 派生。
- Scenario 归属（R8 + FE-8）：`candidate.span` + `supporting_locations[]`；
  结构化 `#pragma provenlattice scenario` → `scenario_annotations[]`。

### 关联与查询能力（能力层）

- 竞争访问配对（R5）：访问级 `competing_access[]` 配对 + confidence 排序 + top-K。
- 证据链（R9）：候选级 `trace_evidence[]` + E-DEFECT 关联，告警可溯源。
- 统一证据口径（R0）：Evidence Schema V2 五块（access / lock / competing /
  trace / confidence-limitation）。
- 非竞态机制暴露（R10）：additive `mechanism_facts` 块。
- 场景关联（FE-8 F2）：query-time `scenario_access_relations[]`
  （`CONTAINS_ACCESS`）+ `function_scenario_groups[]`。

### 精度护栏（可信层）

- confidence gate（R11）：`DEFAULT_CONFIDENCE_THRESHOLD = 0.3` 默认抑制低
  confidence 候选；被抑制项回填 `suppressed`，可审计、可 `confidence_gate=None`
  还原；冻结 FP 回归控制集（read/read、同锁、不同对象、unknown alias）。

### 兼容性

- additive、无 SQL schema / 物理 edge 变更；旧 `candidates` / `evidence` /
  `evidence_bundles` 字段零回退（集成红线回归核验）。
- analyzer 版本 bump：`ast:c` 3→4、`sparse_cfg:cpp` 1→4（缓存自动失效）。

### 明确限制（fail-closed 边界）

- 直接调用解析语义不变；间接调用**永不**升级为 definite `CALLS`。
- `MAY_CALL` 仅同文件 method-local 简单赋值；跨 TU / 多目标 / unresolved 一律 `{}`。
- scenario 仅支持显式白名单 pragma；普通注释 / TODO / 文本相似度不映射。
- FE-8 scenario relations 与 `MAY_CALL` 为查询期、默认 OFF、不注册 CAPABILITY。
- 候选仍为 Candidate（resolved ≠ Verified Finding）。

### 升级说明

由 1.6.x 升级：执行一次完整 `provenlattice index`（`ast:c` 3→4、`sparse_cfg:cpp`
1→4 会使既有缓存失效并自动重物化）。

## ProvenLattice 1.6.0 — Java Sparse CFG/DFG（2026-10-23）

> 已发布：GitHub Release [provenlattice-v1.6.0](https://github.com/sunSUNQ/provenlattice/releases/tag/provenlattice-v1.6.0)（tag 指向 main commit 5d51f1f；wheel/sdist SHA256 见 REPRODUCIBILITY.md 与 Release assets）。PyPI 发布独立决策。

**1.6.0 在 1.5.0 Java Public Beta 基础上新增 Java Sparse CFG/DFG 能力线**。
**Java 仍为 Public Beta，非 Stable**。

### 新能力

- Java Sparse CFG（`sparse_cfg:java`）：方法级稀疏控制流（CHECK/BRANCH/RETURN
  + READ/WRITE 访问点 + lexical scoping/shadowing），复用 C/C++ Sparse CFG 能力。
- Java Sparse DFG（`sparse_dfg:java`）：方法内 def→use（DATA_FLOW_TO），复用
  `mint_dataflow_edges`，无第二套 DFG 算法。
- Java Field Data Flow V1：`this.field` / `obj.field` 读写 def-use（receiver 可
  绑定；identity=(receiver-root, member-path)，不同对象字段不合并）。
- Java Interprocedural Parameter/Return Data Flow V1：同文件短名唯一匹配下，
  `caller argument → callee parameter` 与 `callee return → caller call` 的跨方法
  DATA_FLOW_TO（复用 method-local DFG，无第二套算法）。

### 兼容性

- 既有 Python/C/C++ 能力与行为不变（金样与回归一致）。
- 升级要求：执行一次完整 `provenlattice index`（跨函数数据流需重新物化
  `sparse_dfg`）。

### 明确限制（Public Beta 边界）

- Java 跨函数数据流仅同文件短名唯一匹配；跨文件 / 重载 / 非 identifier 实参
  不绑定（不臆测）。
- **不支持** points-to、alias 分析、完整 heap flow、完整跨文件 interprocedural
  flow（这些属于后续 Roadmap，本版本不宣称）。
- Java Sparse CFG/DFG 为 `local-only, diagnostic` 级别，不宣称 formal
  qualification。
- 其余限制同 1.5.0（146 UNKNOWN 保守保留、query-time 结果非持久化、RQ1 formal
  未取得、JDT 依赖用户自备等）。

### 升级说明

由 1.5.x 升级：执行一次完整 `provenlattice index`，并按需重跑
`provenlattice analyze --features sparse_cfg,sparse_dfg` 物化新能力。

## ProvenLattice 1.5.0 — Java Public Beta（2026-10-19）

> 已发布：GitHub Release [provenlattice-v1.5.0](https://github.com/sunSUNQ/provenlattice/releases/tag/provenlattice-v1.5.0)（tag 指向 main commit 0ec6364；wheel/sdist SHA256 见 REPRODUCIBILITY.md 与 Release assets）。PyPI 发布独立决策。

**1.5.0 新增 Java 支持（Public Beta）**：默认轻量确定性 L1 + 可选 Eclipse JDT
精确语义增强（query-time）。**Java 当前为 Public Beta，非 Stable**。

### 新能力

- Java L1 deterministic indexing：`.java` 扫描/解析/符号/引用/调用，复用冻结
  schema 与三态引用契约（resolved / ambiguous / unresolved）；默认索引**无 JVM
  依赖**
- Java import/reference/resolver 支持：R1 安全收紧（receiver 类型不可知的
  0.75/0.98 启发式绑定降为 ambiguous，候选保留）——已知 8 个历史错误绑定形态
  全部修复，新鲜分层审计 225 站点 Wrong Resolution = 0（四模式）
- Optional JDT semantic analyzer：`java-semantic` CLI + `semantic_java`
  capability（query-time；独立 JVM 进程按需启动；recovered 绑定过滤；保守
  冲突合并；provenance `semantic:jdt-v1` + classpath 指纹）
- Classpath manifest：手工 classpath 契约（校验/去重/顺序敏感指纹/fail-closed/
  BOM 兼容）
- Driver 编译缓存按源码 + classpath 指纹失效（升级不再执行旧 driver）

### 兼容性

- 既有 Python/C/C++ 能力与行为不变（金样四表一致；metadata 门控隔离）
- **升级要求**：执行一次完整 `provenlattice index`（CALL_KIND_VERSION 3 自动
  重解析）
- MCP frozen contract 未变更

### 明确限制（Public Beta 边界）

- **Linux JDT：VERIFIED**（2026-10-20 更新，PR #14：CI ubuntu-latest + Temurin 21 +
  Maven 钉扎套件 9/9，含跨平台 golden 字节一致；发布时为 NOT_VERIFIED；fixture 级
  对比，JDK 其他组合未测，见 docs/linux-jdt-verification-report.md）
- **146 UNKNOWN 保守保留**：跨文件/外部类型流站点无法建立独立真值
- **query-time 结果非持久化**：JDT 分析为查询时视图，不写入 L1 图谱
- **RQ1 formal 尚未取得**：Java 能力等级为工程验证（local-only, diagnostic），
  不宣称 formal qualification
- JDT 依赖由用户自备（JDK 21 已验证；Eclipse JDT EPL-2.0）；Maven/Gradle
  永不自动执行；SCIP Java INFRA_BLOCKED

### 升级说明

由 1.4.x 升级：执行一次完整 `provenlattice index`（CALL_KIND_VERSION 3 会使
既有引用缓存失效并自动重解析）。

## ProvenLattice V1.4 — Selective PR1 Integration（2026-10-13）

**V1.4 = Agent Development 基础平台 + 按需 Code Inspection（八类缺陷候选查询）**

### Added

- **Semantic Events**：15 类词汇表驱动的操作事件（CALL/ALLOC/RELEASE/LOCK/UNLOCK/
  RETURN/THROW/READ/WRITE/DEREFERENCE/CHECK/BRANCH/ATOMIC/THREAD_SPAWN/THREAD_JOIN），
  TOML 词汇表驱动（cpp.toml；Python 词汇表 python.toml 未迁入），按需物化到 AnalysisStore。
- **Sparse CFG**：CONTROL_REACHES 控制可达边 + 结构事件（CHECK/BRANCH/DEREFERENCE）
  + 访问事件（READ/WRITE），事件点级稀疏图，按需物化。
- **Sparse DFG**：DATA_FLOW_TO（方法内 def→use）+ CONSUMES（分配直接传入调用），
  别名链 + via 中间体，按需物化。
- **八类 Typed Defect Query**：double_free、use_after_free、null_flow、
  resource_lifetime、lock_order、race_condition、taint_path、error_handling。
  每个候选携带 E-DEFECT-* 证据链、missing_evidence、三态可达性。
- **Evidence Bundle**：candidate_id、defect_type、subject、facts、uncertain_facts、
  paths、source_evidence、missing_evidence、resolution_status、confidence（heuristic）。
- **Ownership Contract Engine**：ContractTable + inferred ownership rules（代码中
  实现）；declared TOML 数据文件（`semantics/contracts.toml`）未迁入，declared
  路径暂不可用。供 resource_lifetime 和 race_condition 消费。
- **Capability Contract**：统一能力注册（八类查询 + events + sparse_cfg + sparse_dfg），
  按 Capability × Scope 双维控制，dependency freshness 统一管理。
- **统一 dependency freshness**：`dependency_staleness()` 单一规则，存储层和查询层
  共用，上游分析版本变化自动失效下游缓存。
- **复合主键 (id, feature)**：analysis_nodes/edges 支持跨 analyzer 同 ID 共存，
  旧库自动迁移。
- **三态可达性证据**：true（路径存在）/ false（连接端点无路径）/ unknown（端点
  为 edgeless 访问点或 parse partial）——不把 unknown 当作确定性不可达。
- **查询 grade/level/provenance**：每个查询结果标注 `local-only, diagnostic` 等级
  和 `resolved ≠ Verified Finding` 语义。

### Changed

- **analysis_nodes/edges 主键**从 `id` 单列扩展为 `(id, feature)`——支持跨 analyzer
  同 ID 共存，旧库自动迁移（derived cache，行数保留）。
- **analysis_query.py** 六态查询面扩展：events/sparse_cfg/sparse_dfg/defect feature
  支持，events flat view，integrity checks。
- **orchestrator.py**：`defect` 作为 query-time capability，请求时物化上游链并返回
  `defect_query_hint`。
- **CLI**：新增 `defect` 命令（纯查询），`--type` 支持 8 类 + matrix keys + all；
  help text 更新为完整查询列表。
- **pyproject.toml**：version 1.4.0，semantics 包 TOML 数据文件 package-data。
- **test_cfg_analysis.py**：硬编码 contract 字符串改为 `PYTHON_CFG_CONTRACT` 常量。

### Preserved

- **L1 Foundation Graph** 完全不变（graph.py/scanner.py/resolver.py/shard.py/
  overlay.py/mcp_server.py 零 diff），L1 parity 逐 ID 验证通过。
- Python AST/CFG 分析层、task-context、normalization-v2、native MCP 全部保留。
- RQ1/RQ2/SQI 资格口径不变。

### Deferred

以下 PR #1 能力已归档于 `archive/pr1-full-merge` 分支，可通过 Capability Contract
按需启用：

- **冷/热存储分离**（查询加速 2×，但改变默认布局，影响备份/status/工具链）
- **流式增量**（update 峰值内存 -23%；可在 legacy 单库布局下独立移植，待独立
  A/B 验证；不依赖冷存储）
- **declarations 重构**（L1 符号集合变化，RQ1 资格口径失效，需重新资格）
- **事件提取嵌入 index**（默认路径 +157%，改为按需触发）

### Validation

- 全量测试 **408/408 PASS**（254 main 回归 + 154 新增）
- L1 parity **PASS**（node/edge id 逐 ID 相等）
- 八类 CLI smoke **8/8 available**
- 旧库 PK 迁移 **8/8 PASS**
- 默认 index 零检测产物 **CONFIRMED**
- 默认性能 index **6.77s** / RSS **218.0MB**（5 轮配对中位数；合并后单次验证 = 6.94s / 218.1MB）

### Known Limitations

- 全部输出为 **Candidate**（resolved ≠ Verified Finding）；confidence 为 heuristic score
- `error_handling`/`taint_path` 仅 SMOKE + DIAGNOSTIC 级
- 别名精度有限（q=p 拷贝后 deref 不与 p 的 free 配对）
- CONSUMES 仅覆盖直接传递形状
- goto 目标不建模；短路坍缩为单 BRANCH
- 无 SSA / points-to / 跨函数数据流边
- Python 缺陷检测链未验证
- `if(p){*p=1}` null_flow 已知误报保留

---

## Post-V1.2 C Analysis Development（2026-10-10，codex/c-analysis-a1 → 已集成 main）

**Post-V1.2 development checkpoint**：开发分支 `codex/c-analysis-a1` 的完整
commit chain（无 squash）已并入 main，A1–A3.6 能力链现已在 main 上
（集成审计见 `docs/c-analysis-release-r1-integration-audit.md`）。在 V1.3
开发态（analysis layer + context bundle）之上扩展 C deep analysis。这是开发
进展记录，**不是新的 formal V1.3 qualification**；尚未完成同环境 16-case
Agent revalidation。

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

## provenlattice-v1.3（2026-10-09，experiment/context-bundle 线，已集成 main）— ANALYSIS LAYER + CONTEXT BUNDLE（开发态）

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

# ProvenLattice Standalone Roadmap

## Product boundary

ProvenLattice 是面向 Code Agent 的独立工程证据基础设施，不是 TASCO 的上下文压缩组件。两个项目分别维护版本、接口、数据模型和实验结论；在两者各自成熟前，不安排组合实验，也不让 ProvenLattice 的研发依赖 TASCO。

ProvenLattice 的核心问题是：代码在哪里、谁依赖谁、为什么这样设计、哪个需求对应哪段代码、谁修改过它、哪个版本包含它、以及运行时错误可能指向哪里。

## Gold Target（长期产品化目标，canonical planning entry）

> 定位：本节是 ProvenLattice 长期产品化目标的**权威规划入口**（2026-10-10 冻结）。
> 设计原则同步于 [docs/design-optional-analysis-layer-vision.md](design-optional-analysis-layer-vision.md)。
> 注意：vision 文档 §3 的 L0–L4 是分析能力阶梯的**历史命名**，与本节 L1–L5
> pipeline 分层编号不同（映射见其文档头注记）。
> 本节是**规划与架构原则**，不是当前实现状态声明（对照见 G7）。

### G1. 核心目标

ProvenLattice 的目标不是默认构建所有静态分析能力，而是形成：

```text
Always-on lightweight foundation
            +
On-demand advanced capabilities
            +
Task-scoped context delivery
```

核心原则：

```text
L1 = 默认开启（默认基础服务）
L2+ = 按需增强
```

准确解释（防止误读）：

- 不是把 L2–L5 作为一个整体开关；
- 每层内部能力**独立启用**；
- 高成本能力同时受 **capability** 与 **scope** 两个维度控制（见 G3）。

### G2. 五层运行模型

| 层 | 运行模型 | 内容与要求 |
| --- | --- | --- |
| **L1 Foundation / Knowledge** | **默认开启，Always-on** | Repository/File/Symbol、Definition/Reference/Call、Dependency/Impact、Knowledge/Requirement/Document、基础 provenance。即使不启用任何高级分析，ProvenLattice 仍是可用的基础代码/工程知识服务（symbol / definition / callers / callees / references / related code / impact / knowledge）。必须保持：stable、deterministic、相对低成本、可增量维护、适合默认部署。 |
| **L2 Optional Analysis** | **按需；Analyzer Capability Registry（非单开关）** | 已有 analyzer：Python AST、Python CFG、C AST、C CFG、C short-circuit CFG、C call semantics。未来候选见 §4。**新增 analyzer 不新增 architecture layer。**未来能力组合概念形如 `features = ast,cfg` → `ast,cfg,defuse` → `ast,cfg,defuse,points-to` → `security`——这些是概念示例，**未实现的接口不存在**，不假装其已存在。 |
| **L3 Task Context** | **request-time / task-time** | 默认基础服务不要求生成 task-context。需要时：`L1 facts + available selected L2 facts → relevance selection → budget/compactness → task-context`。优先复用已存在的分析事实；只有明确策略允许时才触发额外 analysis materialization；普通 task-context 不无限制触发昂贵 analyzer。 |
| **L4 Evidence / Normalization** | **Core Provenance = baseline；Advanced Evidence = optional** | Core provenance 默认保留、不关闭（基础可信性契约）：source span、evidence id、fact identity、resolved/ambiguous/unresolved、source provenance。Advanced evidence 可选：Agent trace normalization（normalization-v2）、raw≡normalized 校验、runtime/validation evidence、formal experiment telemetry、扩展 audit material。 |
| **L5 Exposure** | **Adapter 层；CLI = baseline** | 至少保留一个基础访问面（CLI 已实现）。其余 adapter 可选：native MCP 已实现（可按配置启用）；Plugin / SDK 尚未实现；REST/API 为候选。"L5 optional" ≠ 系统可以没有任何访问入口。 |

### G3. 双重按需原则（Capability × Scope）

高级能力成本控制必须同时具备：

```text
Capability selection  ×  Scope selection
（selected analyzer）     （selected function / file / module / task scope）
```

而不是 `enable CFG → 为整个仓库构建 CFG`。目标成本模型：

```text
Total Cost ≈ Foundation Cost + Σ( Selected Capability × Selected Scope )
```

> **whole-repository deep analysis 不应成为默认行为。**

### G4. Layer ≠ Maturity Level（不规划 L6）

五层表示**数据生命周期 / pipeline stages**（repository state → derived
analysis → task selection → provenance governance → consumption interface），
不是功能复杂度等级：

```text
AST → CFG → Def-use → Points-to → Taint   能力递进，仍全部属于 L2 Analysis
CLI → MCP → Plugin → SDK                  形态递进，仍全部属于 L5 Exposure
```

**L6 判定标准**（未来必须同时满足，才考虑新增层）：

1. 出现现有五层无法自然容纳的新数据处理阶段；
2. 有独立输入/输出；
3. 有独立持久状态；
4. 有独立生命周期；
5. 有自己的失效/更新规则；
6. 有独立消费契约；
7. 已有真实需求证据证明需要该阶段。

仅新增 analyzer、新数据类型、新 CLI、新索引、新安全规则，均不足以建立 L6。

**Finding 特别说明**：Security/Detection Finding 当前表示为
`L2 analysis artifact → L4 evidence → L3 task context`，**不创建 Finding
Layer**。只有当 Finding 未来成为真正一等长期对象并拥有独立生命周期
（identity / deduplication / suppression / severity / triage / ownership /
validation / remediation state / regression tracking）时，才重新评估是否
需要新阶段。**当前：NO L6 PLANNED。**

### G5. Capability Profiles（future productization candidate，未实现）

> 明确：这是**规划**，不是当前已实现功能。不实现任何 profile 代码。

未来产品化候选：`Unified Capability / Profile Configuration`，概念映射：

| Profile | 能力组合 |
| --- | --- |
| basic | L1 |
| development | L1 + selected AST/CFG + task-context |
| deep（未来） | L1 + AST + CFG + def-use + points-to + task-context |
| security（未来） | L1 + AST + CFG + def-use + points-to + taint/security + task-context |

> profiles 是能力组合的 UX shortcut，**不改变五层架构**；高级用户未来仍应
> 能够单独选择 capability。

### G6. 推荐架构图

```text
               ProvenLattice Gold Target

                Default / Always-on
                       │
                       ▼
          ┌────────────────────────┐
          │ L1 Foundation/Knowledge│
          │ graph + basic evidence │
          └────────────┬───────────┘
                       │
                capability-select
                       │
                       ▼
          ┌────────────────────────┐
          │ L2 Optional Analysis   │
          │ AST / CFG / future ... │
          │   (Capability × Scope) │
          └────────────┬───────────┘
                       │
                  task request
                       │
                       ▼
          ┌────────────────────────┐
          │ L3 Task Context        │
          │ select / budget / pack │
          └────────────┬───────────┘
                       │
                       ▼
          ┌────────────────────────┐
          │ L4 Evidence            │
          │ core provenance        │
          │ + optional advanced    │
          └────────────┬───────────┘
                       │
                       ▼
          ┌────────────────────────┐
          │ L5 Exposure            │
          │ CLI/MCP/Plugin/SDK     │
          └────────────────────────┘
```

### G7. 当前实现 vs Gold Target

**Already implemented**：

- L1 Foundation Graph 默认基础能力
- optional AST / CFG materialization
- scoped analysis（`analyze --scope`）
- analysis cache / freshness
- Python / C analyzer dispatch
- task-context request-time 入口
- core provenance
- normalization-v2
- CLI
- native MCP

**Partially implemented**：

- 整体 optional capability model 已存在，但尚未形成统一配置模型
- L3 已按调用使用，但尚无完整 profile-driven policy

**Not implemented**：

- unified capability config
- profile system
- automatic capability planning
- def-use / points-to / taint / Clang
- Plugin / SDK
- distributed / cross-repo infrastructure

> Gold Target 是方向声明，**不等于当前已全部实现**；实现状态以 §1–§2 的
> 冻结证据与 §3 的 pending 项为准。

## Version line（2026-10-10 reconciliation）

| 分区 | 内容 | 状态 |
| --- | --- | --- |
| Gold Target | 长期产品化目标（canonical planning entry） | 见下节 |
| Qualified releases | 正式资格与发布（有冻结 artifact） | 见 §1 |
| Post-V1.2 implemented development | 已集成 main 的开发态能力 | 见 §2 |
| Pending validation | 进行中 / 待收敛的验证 | 见 §3 |
| Future capability candidates | 候选能力（未排期、不承诺） | 见 §4 |

> 旧版 roadmap 中的 V1.0-R1 "Next" 与 V1.1/V1.2/V1.3/V1.4/V2.0 "Planned" 路线
> 已失效删除：实际演进路径为 RQ1/RQ2 → SQI-V1/V1.1/V1.2 → Analysis Layer →
> Context Bundle/MCP → C10 → zstd 16-case → C 分析链（见 §1–§2）。

## 1. Qualified releases（正式资格）

| 里程碑 | 范围 | 结论 |
| --- | --- | --- |
| Core V0.3 / Core V0.4 | Shard / Incremental；Branch / Session Overlay（工程级 parity） | Complete |
| V1.0 | Knowledge Layer / Cross-Layer Evidence | Complete |
| RQ1 | Graph Fidelity Qualification V1 | QUALIFIED（2026-09-15） |
| RQ2 | Systems + Scale Qualification V1 | QUALIFIED（2026-09-16） |
| SQI-V1 / SQI-V1.1 | Structured Query Interface + Hardening | QUALIFIED / PASS（2026-09-17） |
| SQI-V1.2 | Cost Optimization Qualification | QUALIFIED / RELEASE READY（2026-09-20） |

**SQI-V1.2 = 最新正式发布**（tag `provenlattice-sqi-v1.2`）。

诚实边界：Overlay V1.1 正式资格 NOT STARTED（工程级 parity/conflict 证据已在案）；
S3+ scale bands / concurrency、第四个 benchmark 仓库 NOT STARTED。

## 2. Post-V1.2 implemented development（已集成 main，development checkpoint）

以下能力均已集成 main。这是**开发进展记录，不是正式 V1.3 qualification**；
V1.3 尚未打 tag、尚未举行正式 qualification 汇总。

| 项 | 状态 |
| --- | --- |
| Analysis Layer D0–D8（Python AST/CFG，可选派生层） | ✅ QUALIFIED（Python-only，2026-09-22；Base Graph 零影响回归 137/137） |
| D8-RV1 历史 ADOPTION_GAP case 重验 | ✅ 已闭合：CASE_SOLVED_WITHOUT_PROVENLATTICE ×2（如实在案，不计能力 win） |
| Context Bundle / `task-context`（D9-C1/C2） | ✅ implemented（task-context-v0 事实包，fact_id + evidence_ids 可追溯） |
| Native MCP 暴露（D9-C7/C8） | ✅ implemented（单工具透明桥接，C8-A 冻结合约 byte-identical） |
| normalization-v2（D9-C9） | ✅ implemented（Agent trace → 事实级观测，raw≡normalized 校验） |
| D9-C10 P2 受控能力稳定性 | ✅ PASS（2026-10-09；12/12 cells、6/6 pairs VALID、0 leakage） |
| ContextBench zstd 16-case 重验 | ✅ 14/16 resolved、0 真实失败、2 infra-excluded（≠16/16；对比历史 13/16 无回归） |
| C 分析链 A1/A2/A2.1/A3/A3.5/A3.6 | ✅ 已集成 main（C normalized AST、function-local CFG V1 + `&&`/`||`/`?:` 短路、调用语义分类 direct/indirect/unresolved、definition-preference 紧凑上下文；全量回归 254/254） |
| R1 revalidation 包 / manifest / colleague guide | ✅ 冻结（wheel sha256 在案；结果不预写） |

## 3. Pending validation

- **Colleague 16-case rerun（R1 revalidation）= current next experiment**：
  环境受控重跑全部 16 个 ContextBench-zstd case，与 2026-10-09 本地批
  （14/16 resolved、2 infra-limited、0 真实失败）对比。入口：
  `revalidation/c-contextbench-zstd-r1-manifest.json` +
  `docs/c-analysis-colleague-revalidation-guide.md`。结果不预写。
- V1.3 正式 qualification 汇总 + tag：待上述 revalidation 之后再收敛；
  在此之前不宣称正式 V1.3 qualification。

## 4. Future capability candidates（按五层映射；候选，未排期、不承诺）

> **Vector/semantic index = implementation-dependent**：若实现为
> repository-level canonical semantic index 属 **L1**；若仅作为 task-context
> candidate retrieval accelerator 属 **L3**。最终归属由其数据生命周期决定，
> 不预绑定单一层。
> Runtime Log / Commit History / Validation Result ingestion 同时对应
> **L1 ingestion/knowledge + L4 provenance/evidence**。

| 层 | 候选 | 状态备注 |
| --- | --- | --- |
| L1 | S3+ scale bands / concurrency；cross-repository indexing；distributed indexing；canonical vector index（仅当未来作为 repository-level index）；Runtime Log ingestion；Commit History ingestion；Validation Result ingestion | not started |
| L2 | Def-Use / Local Data Flow；Points-to / precise function-pointer resolution；Taint / Security Analysis；C++ deep analysis；Clang semantic enrichment；compile_commands-aware analysis；Interprocedural CFG | **def-use not justified yet**——真实 case 未证明必要（`A4_DEFUSE_NOT_YET_JUSTIFIED`），出现真实需求信号再启动，非已规划必做；其余 future candidate（当前 C indirect 调用仅如实分类、永不 name-bind，不做 target 解析） |
| L3 | semantic/vector retrieval accelerator（仅当 vector index 服务 task-context relevance 而非 canonical index）；smarter context selection；context budgeting / profile selection | future candidate |
| L4 | Runtime evidence provenance；Validation evidence；richer normalization；audit/verification extensions | future candidate |
| L5 | Plugin；SDK；REST/API service；additional Agent adapters | evidence-driven decision（MCP 已落地，CLI = baseline） |
| 跨层 | Overlay V1.1 正式资格（candidate，工程级证据已在案）；第四个 benchmark 仓库（not started） | 见 §1 诚实边界 |

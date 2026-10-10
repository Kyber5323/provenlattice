# ProvenLattice 后续版本实现方向设计：Optional Function Analysis Layer

> 版本定位：在保持 V1.2 Foundation Graph 能力与契约不变的前提下，引入可选的函数内分析层（Analysis Layer），面向代码检测、漏洞检测、安全分析与高级 Agent 任务。

## 1. 目标

当前 ProvenLattice 的基础图以函数/符号级工程语义为主，已经能够支持：
- symbol.lookup / callers / callees / references
- impact.frontier
- code.related / bundle.explain
- 日常代码理解、代码评审、需求到代码、影响分析、Agent 开发任务

但对于以下任务仍存在结构性缺口：
- 函数内控制流分析（CFG）
- 定义-使用链（Def-Use）
- 局部数据流
- source-to-sink 漏洞路径
- 资源生命周期、空指针路径、危险 API 路径
- 代码检测 finding 的上下文增强与修复辅助

因此下一阶段建议引入：

> Base Graph + Optional Analysis Layer

其中：
- **Base Graph**：默认启用、常驻、低成本，继续面向日常开发与通用 Agent 任务；
- **Analysis Layer**：按需启用、按能力构建、独立缓存与失效，面向检测 / 安全 / 高级语义任务。

---

## 2. 总体架构

```text
ProvenLattice
├─ Foundation Graph（默认）
│  ├─ Repository / Module / File / Shard
│  ├─ Symbol / Definition / Call / Reference
│  ├─ Impact / Requirement / Evidence
│  └─ SQI（lookup / callers / references / impact / related）
│
└─ Analysis Layer（可选）
   ├─ Function IR
   ├─ Local CFG
   ├─ Def-Use
   ├─ Local Data Flow
   ├─ Taint / Security Analysis（后续）
   └─ Detection Finding Enrichment
```

设计原则：
1. **默认 OFF**：不增加普通用户默认成本；
2. **按需构建**：只为目标函数 / 目标文件 / 目标任务构建；
3. **独立缓存**：Analysis Overlay 与 Base Graph 分离；
4. **独立失效**：基于 source_hash、analysis_profile、analysis_version 失效；
5. **契约隔离**：不污染现有 Base Graph canonical state；
6. **能力渐进**：先 Local CFG，再 Def-Use，再 Data Flow，再安全分析。

---

## 3. 分层能力与适配任务

### L0 — Foundation Graph（当前默认能力）

内容：
- Repository / Module / File / Shard / Symbol
- Definition / Reference / Call
- Requirement / Cross-layer Link / Evidence
- Impact Frontier

适配任务：
- 代码理解
- Caller / Reference 查询
- 需求到代码定位
- 影响范围分析
- Agent 日常开发问答
- Review / Refactor / Bug 定位

### L1 — Function Structure

内容：
- Parameter
- Local Variable
- Statement
- Expression
- CallSite
- Branch / Loop / Return

适配任务：
- 函数内部结构理解
- 复杂函数 Review
- 结构化代码讲解
- Refactor 辅助

### L2 — Local CFG

内容：
- CFG_ENTRY / BASIC_BLOCK / CFG_EXIT
- CFG_TRUE / CFG_FALSE / CFG_NEXT / CFG_LOOP / CFG_EXCEPTION
- Statement-to-BasicBlock 映射

适配任务：
- 控制流理解
- 分支路径分析
- 不可达路径
- 错误处理分支
- 资源释放是否覆盖所有路径

### L3 — Def-Use / Local Data Flow

内容：
- DEF / USE
- DEFINES_VALUE / USES_VALUE / DEF_USE / DATA_FLOW
- source-to-variable-to-callsite 路径

适配任务：
- 参数污染路径
- 危险 API 输入路径
- 空指针传播
- 未初始化使用
- 本地数据传播分析

### L4 — Security / Detection Analysis

内容：
- source / sink / sanitizer 模型
- taint edges
- finding path
- evidence-enriched finding

适配任务：
- 漏洞检测
- 安全规则分析
- Finding 上下文增强
- 检测结果解释
- Agent 辅助修复

---

## 4. 存储与失效策略

建议将存储分离为：

```text
BaseGraphStore
└─ base.db

AnalysisStore
├─ function_ir.db
├─ cfg.db
├─ dataflow.db
└─ security.db
```

或逻辑上分为不同 namespace / table group。

### Analysis Overlay Identity

每个分析结果绑定：
- repo_id
- commit
- graph_generation
- file_id
- symbol_id
- source_hash
- analysis_profile
- analysis_version

### 失效规则

当以下任一变化时，Analysis Overlay 失效：
- 函数源码变化（source_hash 变化）
- 基础图 generation 变化
- analysis_profile 变化
- analysis_version 变化

---

## 5. 查询接口建议

### 保留原有 SQI

原有接口继续用于 Base Graph：
- symbol.lookup
- symbol.callers
- symbol.callees
- symbol.references
- impact.frontier
- code.related
- bundle.explain

### 新增分析接口

建议新增：
- function.structure
- function.cfg
- function.defuse
- function.dataflow
- finding.enrich

或统一为：

```json
{
  "query": "function.analyze",
  "symbol_id": "sym:...",
  "capabilities": ["structure", "cfg", "def_use"]
}
```

### Analysis Profile

建议引入：
- `basic`：仅 Base Graph
- `structure`：Base + Function Structure
- `control-flow`：Base + Structure + CFG
- `security`：Base + Structure + CFG + Def-Use + Data Flow

---

## 6. 与代码检测的结合方式

输入：

```json
{
  "file": "src/foo.cc",
  "line": 128,
  "rule": "unsafe-api"
}
```

增强路径：

```text
Finding
  ↓
Symbol
  ↓
Function Structure / CFG / Def-Use
  ↓
Impact / Requirement / Evidence
  ↓
Evidence-enriched Finding
```

输出建议包括：
- rule_id
- file / line / symbol
- source / sink
- path summary
- impacted callers / impacted shards
- related requirement / related document
- evidence ids

---

## 7. 方法论约束

这一层必须延续 V1.2 的治理方法：
1. **Base Graph 行为字节语义不变**；
2. **新增能力单独 freeze contract**；
3. **新增能力单独 qualification**；
4. **外部系统优先进入 candidate / oracle 层，不直接进入事实层**；
5. **默认不做 whole-program CFG / interprocedural taint**；
6. **默认不把 parser CST 当持久节点模型**。

---

## 8. 版本路线建议

### V1.3
- Optional Function Analysis Layer Contract Freeze
- Function Internal IR
- Local CFG
- Analysis Profile
- On-Demand Materialization + Cache

### V1.4
- Def-Use
- Local Data Flow
- Finding Enrichment MVP
- Detection Context API

### V1.5
- Taint / Security Analysis
- Agent-assisted finding explanation
- source / sink / sanitizer rule layer

### V1.6+
- Interprocedural Data Flow
- Cross-function security path
- More languages / more detectors

---

## 9. 下一步建议执行任务

**V1.3-A Optional Function Analysis Layer Contract Freeze**

需要冻结：
- Base Graph 与 Analysis Layer 的边界；
- Analysis Overlay identity；
- analysis_profile；
- 第一批 node / edge 类型；
- source hash / generation 失效规则；
- function.structure / function.cfg 接口；
- 默认 OFF 契约；
- Local CFG correctness oracle；
- 明确不做项（如 interprocedural dataflow / taint / whole-program CFG）。

完成 contract freeze 后，再进入：

**V1.3-B Local CFG Implementation**

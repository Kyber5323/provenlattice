# ProvenLattice 后续版本实现方向设计：Optional Function Analysis Layer

> 版本定位：在保持 V1.2 Foundation Graph 能力与契约不变的前提下，引入可选的函数内分析层（Analysis Layer），面向代码检测、漏洞检测、安全分析与高级 Agent 任务。
>
> **文档地位：项目当前最终目标（North Star）。** §1–§9 为目标设计原文；§10 为 2026-09 的实现状态对照（D0–D8 已实证交付部分与设计差异），实现层命名以已冻结的 D0–D8 契约为准。

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

---

## 10. 实现状态对照（2026-09，branch `feature/analysis-layer`，D0–D8）

本节把上文目标设计与已完成的 D0–D8 实证交付对齐。**实现层当前命名以 D0–D8 冻结契约为准**（各阶段报告：`docs/design-analysis-layer-d0-audit.md` … `d8-report.md`）；设计中的命名（profile、多库布局等）作为 V1.3 收敛候选，是否迁移由后续证据决定，不做无谓重命名。

### 10.1 已交付能力映射

| 设计目标 | 状态 | 实现物 | 证据 |
| --- | --- | --- | --- |
| 默认 OFF / 契约隔离 / Base 零影响 | ✅ 已实证 | analysis 表惰性创建；基础 index/update 零改动、零创建 | D1/D2/D3 各零影响测试；`git diff` 基础源码为空 |
| L1 Function Structure | ✅ 已实现（口径：AST 层） | `analysis_ast.py`，feature=`ast:python`，contract `python-ast-v1`；真实 CST 全量节点（含函数体）、parent 链可恢复层级 | D2 报告 §9；D2 测试 11/11 |
| C Function Structure A1 | ✅ 已实现（`.c`/`.h`，normalized AST） | `analysis_c_frontend.py` + `analysis_c_ast.py`，feature=`ast:c`，contract `c-ast-v2`；Tree-sitter kinds 仅保存在 `parser_kind`；for/if/loop 等子角色保存在 `part_role` | C-A1 audit/report + golden tests |
| L2 Local CFG | ✅ 已实现（口径：statement 级、intraprocedural） | `analysis_cfg.py`，feature=`cfg:python`，contract `python-cfg-v1`；entry/exit/condition/callsite/merge/statement/return/break/continue + next/true/false/back/return/break/continue 边 | D4 报告；D4 测试 12/12 |
| C CFG V1 A2 | ✅ 已实现（函数内控制流） | `analysis_c_cfg.py`，feature=`cfg:c`，contract `c-cfg-v2`；构建于 A1 normalized C AST 之上（builder 无 parser 依赖），复用 AnalysisStore/analysis_v1；sequence/if/else/while/do/for/break/continue/return/switch/case/default/fallthrough/goto/implicit exit | C-A2 audit/report + CCFG-01..15 golden tests |
| C Short-Circuit CFG A2.1 | ✅ 已实现（表达式级 `&&`/`\|\|`/`?:`） | `_condition_flow`/`_value_flow` 短路 lowering；operand 节点复用 normalized AST identity；嵌套布尔与 loop/return/assignment 集成 | C-A2.1 audit/report + SCFG-01..10 golden tests |
| C Call Semantics A3 | ✅ 已实现（direct/indirect/unresolved 分类） | foundation extractor `call_kind`/`callee_shape` metadata（schema 不变）；indirect 永不 name-bind；`get_call_semantics` + `explain-symbol` 暴露；candidate 有界且 ≠ resolved | C-A3 audit/report + CALL-01..15 golden tests |
| C 精确 indirect target 解析 | ⬜ 未实现（A3 边界） | `h = foo` 不传播；无 points-to/def-use；INDIRECT_CALL 识别 ≠ points-to 分析 | C-ANALYSIS-A3 boundary |
| 表达式级 short-circuit CFG 之外的高级表达式流 | ⬜ 未实现（A2.1 边界） | 逗号运算符、GCC statement-expression、switch 条件内短路、算术表达式内嵌套短路保持 opaque | C-ANALYSIS-A2.1 boundary |
| 独立缓存 + 失效 | ✅ 已实现（键维度见 §10.2） | `analysis.py`：`file × analyzer` 原子替换、fail-closed freshness、payload 硬上限 | D1 + D3 报告；contract 测试 9/9 |
| 按需构建（目录级） | ✅ 已实现 | `orchestrator.py` + CLI `analyze <repo> --scope <dir> --features ast,cfg`；feature/scope 校验先于一切 mutation；`cfg→ast` 依赖编排 | D5 报告；CLI 测试 13/13 |
| 查询接口（function.structure / function.cfg） | ✅ 已实现（命名：`analysis` 查询层） | `analysis_query.py` `query_analysis(feature, file_path / symbol_id)` + CLI `analysis` 命令；六态契约 available/not_built/stale/unsupported/not_available/corrupt | D6 报告；查询测试 12/12 |
| Agent 消费（code.related / bundle.explain 附带 analysis） | ✅ 已实现（explain-symbol） | `explain_symbol(analysis=…)` 附 `analysis` section + size guard；默认调用零变化 | D7 报告；tool 测试 13/13 |
| 真实 Agent 价值观察 | ✅ 已完成一轮 | 6 case × 2 臂真实 claude 运行 | D8 报告：UTILITY_OBSERVED=4, ADOPTION_GAP=2, CAPABILITY_GAP=0 |
| L3 Def-Use / Local Data Flow | ⬜ 未开始（V1.4） | — | D8 观察未出现需求信号 |
| L4 Taint / Finding Enrichment | ⬜ 未开始（V1.5） | — | 同上 |

### 10.2 设计与当前实现的命名/粒度差异（诚实记录）

| 设计原文 | 当前实现 | 说明 |
| --- | --- | --- |
| AnalysisStore 多库（function_ir.db / cfg.db / …） | 单 `codegraph.db` 内 `analysis_*` 表族 | D1 决策：避免多库迁移与 join 成本；`git status` 隔离已实证。多库布局保留为 V1.3 收敛候选 |
| analysis_profile + analysis_version | analyzer id `<feature>:<language>` + `ANALYZER_VERSIONS` 注册表 + contract 字符串（如 `python-ast-v1`） | D3 冻结。语义等价且更强（语言维度）；`basic/structure/control-flow/security` profile 可实现为 analyzer 组合，未引入独立机制 |
| 失效键含 graph_generation 变化 | 失效键 = file_id + analyzer + source_hash（来自 base `file_state`）+ feature_version | generation 变化但文件未变时 analysis 仍 fresh（更精确）；D1/D6 契约 |
| L2 节点：BASIC_BLOCK / CFG_EXCEPTION / CFG_LOOP | statement 级节点（entry/exit/statement/condition/callsite/merge/return/break/continue），边 next/true/false/back/return/break/continue | D4 冻结：statement 级、无异常边、无 async；basic-block 合并与 CFG_EXCEPTION 属后续扩展 |
| 构建粒度"目标函数/目标文件/目标任务" | 文件 × analyzer 全量替换（文件内函数级 unit 组装） | D1 原子性契约；函数级增量留待证据支持 |
| L1 Function IR | AST structural layer（真实语法节点 + structural_path + span） | D2 冻结；不把 CST 当持久节点模型的原则以"派生缓存可随时重建"方式满足（§7.6） |
| 查询 function.structure / function.cfg | `query_analysis(feature=ast/cfg, symbol_id/file_path)` + `explain-symbol --analysis` | D6/D7 冻结；function.defuse / dataflow / finding.enrich 为 V1.4 扩展位 |

### 10.3 方法论约束兑现情况（§7）

1. Base Graph 字节语义不变：**已证明**（D1–D7 每阶段零影响测试 + 全量回归 137/137）。
2. 新增能力单独 freeze contract：**已执行**（D1 store、D3 analyzer/payload、D2 AST、D4 CFG、D6 查询五份契约）。
3. 新增能力单独 qualification：**部分**——各阶段有门禁测试，但尚未按 V1.2 SQI 标准做正式 qualification 批次（V1.3 收尾项）。
4. 外部系统进 candidate/oracle 层：**未触碰**（本轮无外部系统）。
5. 不做 whole-program CFG / interprocedural taint：**遵守**（D4 明确禁止项，D8 观察零 CAPABILITY_GAP）。
6. 不把 parser CST 当持久节点模型：**遵守**（CST 只作为 materializer 输入，落库的是派生 AST/CFG 结构）。

### 10.4 下一步（按本设计 §8/§9 对齐）

1. **V1.3 收尾（对应已完成的 V1.3-A/B）**：将 D0–D8 契约正式更名为本设计的 `analysis_profile` / `function.structure` / `function.cfg` 对外口径（或冻结现状命名），补一次正式 qualification 批次（对标 V1.2 SQI 门禁）。
2. **D9（observation 驱动）**：按 D8 结论优先扩 Agent 消费覆盖面（其余 evidence 命令 + bundle 内结构化 analysis evidence）并观察 A2/B2 类任务的 adoption 修复；DFG（V1.4/L3）等真实需求信号出现再启动。

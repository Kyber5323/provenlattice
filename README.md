# ProvenLattice

**A Software Intelligence Graph for Code Generation, Repair, and Quality Inspection**

> **版本状态**
>
> - 当前 tagged 版本：**v1.7.0**（`main` = `9feae792`，`pyproject.toml` = 1.7.0）
> - 上一版稳定发布：v1.6.0（Java Sparse CFG/DFG）
> - GitHub Release 页面与 PyPI 发布由 owner 决定；当前 GitHub "Latest Release" 仍为 v1.6.0，v1.7.0 的 Release 页尚未创建。

## 项目定位

ProvenLattice 是一个面向三类研发任务的软件智能图谱（Software Intelligence Graph）。它以轻量、确定性、可增量的代码知识图谱为底座，把代码仓库的结构、语义与缺陷信息组织成可查询、可追溯、可增量的分层证据结构，为 AI Agent 和开发者提供任务相关的最小必要上下文与按需深度分析：

1. **代码生成（Code Generation）**：调用关系、影响分析、需求-代码关联、任务上下文，为生成式编码提供结构化事实。
2. **代码修复（Code Repair）**：缺陷候选 + 证据链（`E-DEFECT-*`），让修复建议可溯源、可验证。
3. **质量检测（Quality Inspection）**：Semantic Events、Sparse CFG/DFG、并发图谱事实、八类缺陷查询、Evidence Bundle。

三条任务线共享同一底层事实（L1 Foundation Graph）、同一分析引擎与同一证据体系。长期规划见 [docs/roadmap.md](docs/roadmap.md) Gold Target V2 节。

## Motivation

当前 Code Agent 主要通过文件读取、代码搜索和工具调用理解大型代码仓库。随着代码规模和工程复杂度增加，这种方式容易带来：

* 大量重复搜索与文件读取
* 跨文件、跨模块关系定位成本较高
* 代码与需求、设计、历史信息相互割裂
* Agent 上下文快速膨胀
* 缺陷检测依赖独立工具，证据不可追溯

ProvenLattice 探索一种新的工程上下文组织方式：

> 将代码仓库的结构、语义和缺陷信息组织成可查询、可追溯的分层证据结构，为 AI Agent 提供任务相关的最小必要上下文和按需深度分析。

## Architecture

```text
L1 Foundation Graph（默认启用，零检测开销）
│   index/update → Repository/File/Symbol/Call/Reference/Impact/Knowledge
│   Python + C/C++ + Java 确定性解析，resolved/ambiguous/unresolved 三态引用
│
L2 Unified Optional Analysis（按 Capability × Scope 按需物化）
│   ├─ Agent Analysis：AST/CFG · 调用语义 · 短路
│   └─ Inspection Analysis：Semantic Events · Sparse CFG/DFG · 并发 Profile
│                          · 8 类 Typed Defect Query
│
L3 Task Services（Agent task-context · Defect query · Concurrency profile）
│
L5 Exposure（CLI baseline · MCP passthrough）

L4 Evidence & Trust Governance（横向治理面，贯穿 L2–L5）
   证据契约 · Candidate ≠ Verified · 三态可达性 · confidence gate · freshness
```

> Full CFG 和 Sparse CFG 当前为两种独立实现；Canonical Control-Flow Model 属于长期演进目标。

## Capability × Scope

按需物化的能力统一以 **Capability × Scope** 控制：请求某个能力时只对指定 scope 物化，上游变化经 `dependency_staleness()` 自动失效下游缓存。

| 能力 | scope 粒度 | 语言 | 来源 |
|---|---|---|---|
| AST | file/symbol | Python / C / C++ / Java | L2 Agent |
| CFG | function | Python / C | L2 Agent |
| Semantic Events | function | C / C++ | L2 Inspection |
| Sparse CFG | function | C / C++ / Java | L2 Inspection |
| Sparse DFG | function | C / C++ / Java | L2 Inspection |
| Concurrency Profile | repo/scope | C / C++ | v1.7 |

**不同缺陷所需的证据组合**（示例）：

| 缺陷 | 依赖的图谱能力 |
|---|---|
| double_free / use_after_free | Semantic Events + resource lifetime + `DEREF_RESOLVES_TO` |
| resource_lifetime | Events + `THREAD_SPAWN`/`THREAD_JOIN` + `is_alive_at_access` |
| lock_order | per-access `held_locks` + 锁身份关系 |
| race_condition | `access_kind` + `held_locks` + `competing_access[]` |
| null_flow | DEREF 事件 + Sparse CFG |

## Quick Start

```bash
pip install -e .

# 1. 构建基础图谱（L1，默认轻量）
provenlattice index /path/to/repo --json

# 2. 基础查询
provenlattice symbol <name> --database /path/to/repo/.provenlattice/codegraph.db --json
provenlattice callers <symbol> --database /path/to/repo/.provenlattice/codegraph.db --json

# 3. Agent 分析（AST/CFG）
provenlattice analyze /path/to/repo --scope src --features ast,cfg --json
provenlattice analysis /path/to/repo --feature cfg --file-path src/foo.c --json

# 4. 任务上下文
provenlattice task-context <symbol> --database /path/to/repo/.provenlattice/codegraph.db --json

# 5. Code Inspection（按需，触发上游链 + 缺陷查询）
provenlattice analyze /path/to/repo --scope src --features defect --json
provenlattice defect /path/to/repo --type race_condition --json
provenlattice defect /path/to/repo --type lock_order --json
provenlattice defect /path/to/repo --type all --json

# 6. 并发图谱 Profile（v1.7，按 scope 物化并发语义事实）
provenlattice concurrency-profile /path/to/repo \
  --profile concurrency-v1 \
  --scope src \
  --json

# 7. MCP（透明桥接，需 MCP 客户端配置后使用；见 Limitations）
#    mcp__provenlattice__query 为 MCP 协议内工具名，不是 CLI 子命令。
```

## 三语言统一能力基线（v1.6）

v1.6.0 把 Python / C/C++ / Java 三语言收敛到同一套 L1 事实 + L2 能力契约上：

* **L1 Foundation Graph**：Python / C/C++ / Java 三语言确定性解析（Java 以 tree-sitter 语法事实接入，含 Class/Interface/Method 节点、DEFINES/CONTAINS/IMPORTS/CALLS/REFERENCES/IMPLEMENTS 边、三态引用边界）。
* **Sparse CFG/DFG 统一**：C/C++ 与 Java 复用同一套稀疏控制流 / 数据流（`sparse_cfg:java` / `sparse_dfg:java` / Field Data Flow / 同文件 interprocedural param-return），无第二套算法。
* **可选 Java 精确解析**：`java-semantic`（Eclipse JDT，独立 JVM 进程，按需启动；默认 index 永不启动 JVM）提供查询时精确绑定，fail-closed 报告 unavailable。
* **8 类 Typed Defect Query + Evidence Bundle**：`double_free` / `use_after_free` / `null_flow` / `resource_lifetime` / `lock_order` / `race_condition` / `taint_path` / `error_handling`。

> Java 能力等级为 `local-only, diagnostic`（Public Beta，未取得 RQ1 formal 资格）。

## 并发图谱事实与缺陷精度（v1.7）

v1.7.0 面向并发类漏洞检测，补齐图谱的并发语义事实、关联能力与精度护栏，让 race / lock_order / resource_lifetime / 间接调用四类并发缺陷从「能看结构」变为「能证事实」。

### 并发语义事实（事实层）

| 能力 | 事实 |
|---|---|
| 一等访问类型 | `access_kind`（READ / WRITE / READ_MODIFY_WRITE） |
| 锁集合与锁身份 | per-access `held_locks` + 锁身份关系（SAME / DIFFERENT / PROTECTS） |
| 对象身份 | canonical storage identity + query-time `DEREF_RESOLVES_TO` + must-only `same_storage` |
| 生命周期 | 生命周期序 + `is_alive_at_access`（spawn→join） |
| 间接调用 | 间接 field-callee `is_indirect` READ + query-time `MAY_CALL` |
| Scenario 归属 | `candidate.span` + `supporting_locations[]` + `scenario_annotations[]` |

### 关联与查询能力（能力层）

* 竞争访问配对：访问级 `competing_access[]` + confidence 排序 + top-K。
* 证据链：候选级 `trace_evidence[]` + `E-DEFECT` 关联，告警可溯源。
* 统一证据口径：Evidence Schema V2 五块（access / lock / competing / trace / confidence-limitation）。
* 非竞态机制暴露：additive `mechanism_facts` 块。
* 场景关联：query-time `scenario_access_relations[]`（`CONTAINS_ACCESS`）+ `function_scenario_groups[]`。

### 精度护栏（可信层）

* confidence gate：默认 `confidence_gate=0.3` 抑制低 confidence 候选；被抑制项回填 `suppressed`，可审计、可 `confidence_gate=None` 还原；冻结 FP 回归控制集（read/read、同锁、不同对象、unknown alias）。

### 明确限制（fail-closed 边界）

* 间接调用**永不**升级为 definite `CALLS`；`MAY_CALL` 仅同文件 method-local 简单赋值，跨 TU / 多目标 / unresolved 一律 `{}`。
* scenario 仅支持显式白名单 pragma（`#pragma provenlattice scenario`），普通注释 / TODO / 文本相似度不映射。
* FE-8 scenario relations 与 `MAY_CALL` 为查询期、默认 OFF、不注册 CAPABILITY。
* 候选仍为 Candidate（`resolved` ≠ Verified Finding）。

## Validation

| 检查项 | 结果 |
|---|---|
| 全量测试（v1.7 基线，`pytest tests`） | **853 passed, 3 subtests passed** |
| CI | test-linux / test-windows / linux-jdt-verify 三 job 全绿 |
| 集成红线回归 | 默认 index / race / lock_order / CLI-MCP envelope / 旧字段 全绿，零回退 |
| 冻结 FP 控制集 | read/read、同锁、不同对象、unknown alias 强断言 |

> 历史性能数据（1.4.0 基线，aria2 语料 1,393 文件 / 165k LOC）：默认 index 6.77s / RSS 218MB。性能数据基于特定测试语料和环境，不代表所有仓库的通用表现。

## Limitations

- 全部输出为 **Candidate**（`resolved` ≠ Verified Finding；`confidence` 为 heuristic score）
- `error_handling` 和 `taint_path` 仅 SMOKE + DIAGNOSTIC 级
- CONSUMES 仅覆盖直接传递形状（`f(malloc(n))`）
- goto 目标不建模；短路坍缩为单 BRANCH
- 无 SSA / 完整 points-to / 跨函数数据流边
- Python 缺陷检测链未验证
- Java：L1 已交付（Public Beta）；可选 JDT 精确解析已交付；record 组件、注解元素、匿名类体、隐式默认构造不提取；Java 不具备 Stable 资格，RQ1 formal 未取得
- Java Sparse CFG/DFG：跨函数数据流仅同文件短名唯一匹配；能力等级 `local-only, diagnostic`
- **v1.7 产品化边界**：GitHub Release 页 / PyPI 尚未发布（owner 决策）；完整 happens-before / 内存模型 / 完整 points-to 尚未实现；v1.7 尚未进行正式 Release Qualification
- MCP defect 查询已完成端到端客户端验证（stdio JSON-RPC 真实 server 子进程，全链路 4/4 PASS，见 `docs/v1.4-mcp-e2e-report.md`）

## Documentation

| 文档 | 说明 |
|---|---|
| [docs/capability-matrix.md](docs/capability-matrix.md) | 当前能力状态统一入口 |
| [docs/defect-query-guide.md](docs/defect-query-guide.md) | 缺陷查询使用指南 |
| [docs/roadmap.md](docs/roadmap.md) | Gold Target V2 长期路线 |
| [REPRODUCIBILITY.md](REPRODUCIBILITY.md) | 资格验证证据链 |
| [CHANGELOG.md](CHANGELOG.md) | 变更日志 |
| [docs/](docs/) | 其余设计、证据、发布与整合报告归档 |

## Research Direction

* 大规模代码结构建模
* 分层工程信息组织
* 跨层工程证据关联
* 增量图更新
* 面向 Code Agent 的结构化检索
* Graph-guided Context Retrieval
* 按需缺陷检测的 Evidence 语义

## Standalone Roadmap

```text
─── v1.6 Stable Baseline ───
Python / C/C++ / Java 三语言 L1 + Sparse CFG/DFG 统一基线          ✅ 已交付
8 类 Typed Defect Query + Evidence Bundle                          ✅ 已交付
可选 Java JDT 精确解析（diagnostic）                               ✅ 已交付

─── v1.7 Delivered（并发图谱事实 + 缺陷精度）───
access_kind / held_locks / storage identity / alias / thread ctx   ✅ 已交付
per-access lockset / race / lock scenarios                          ✅ 已交付
lifetime / DEREF / MAY_CALL                                         ✅ 已交付
Evidence Schema V2 + trace_evidence 链                              ✅ 已交付
confidence gate / top-K / suppression                               ✅ 已交付

─── Future ───
Canonical CFG / Full-Sparse 双视图收敛                             规划（Gold Target V2 Phase 3）
精确 alias / 完整 points-to / 跨函数数据流                          future candidate
完整 happens-before / 内存模型                                      future candidate
完整 Taint Analysis / sanitizer                                     future candidate
Python 缺陷检测链                                                   future candidate
Cold Store / Streaming Incremental                                  deferred (opt-in candidate)
CI / Plugin / SDK / REST                                            future candidate
```

## Status

✅ **v1.7.0 = 并发图谱事实 + 缺陷精度**（当前 tagged 版本，`main` = `9feae792`）

- 全量回归 853 passed + 3 subtests；CI 三项全绿
- 七文件图谱改进程序 R0–R11 + FE-5 / FE-8 已合入
- 上一版 v1.6.0：三语言统一能力基线（Java Sparse CFG/DFG），[provenlattice-v1.6.0](https://github.com/sunSUNQ/provenlattice/releases/tag/provenlattice-v1.6.0)
- **v1.7.0 的 GitHub Release 页与 PyPI 发布由 owner 决定，尚未发布**

详细能力矩阵见 [docs/capability-matrix.md](docs/capability-matrix.md)。

---

**ProvenLattice**

*A Software Intelligence Graph for Code Generation, Repair, and Quality Inspection*

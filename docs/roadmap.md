# ProvenLattice Standalone Roadmap

## Product boundary

ProvenLattice 是面向 Code Agent 的独立工程证据基础设施，不是 TASCO 的上下文压缩组件。两个项目分别维护版本、接口、数据模型和实验结论；在两者各自成熟前，不安排组合实验，也不让 ProvenLattice 的研发依赖 TASCO。

ProvenLattice 的核心问题是：代码在哪里、谁依赖谁、为什么这样设计、哪个需求对应哪段代码、谁修改过它、哪个版本包含它、以及运行时错误可能指向哪里。

## Version line（2026-10-10 reconciliation）

| 分区 | 内容 | 状态 |
| --- | --- | --- |
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

## 4. Future capability candidates（候选，未排期、不承诺）

| 候选 | 状态 |
| --- | --- |
| Def-Use / Local Data Flow / Finding Enrichment | **not justified yet**——真实 case 未证明必要（`A4_DEFUSE_NOT_YET_JUSTIFIED`）；出现真实需求信号再启动，非已规划必做 |
| Points-to / 精确 function-pointer target 解析 | future candidate（当前 C indirect 调用仅如实分类、永不 name-bind，不做 target 解析） |
| Taint / Security Analysis | future candidate |
| Clang / compile_commands 语义增强 | future candidate |
| C++ deep analysis | future candidate |
| Overlay V1.1 正式资格 | candidate（工程级证据已在案） |
| S3+ scale bands / concurrency、第四个 benchmark 仓库、跨仓统一索引、向量索引 | not started |
| Plugin / SDK | evidence-driven decision（MCP 已落地） |

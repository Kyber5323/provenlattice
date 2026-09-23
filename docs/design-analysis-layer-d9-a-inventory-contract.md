# PL-DA-D9-A — Analysis Evidence Surface Inventory & Contract Freeze

状态：**READY_FOR_D9_B_IMPLEMENTATION**  
分支：`feature/analysis-layer`  
日期：2026-09-23  
范围：仅盘点和契约冻结；本文件不实现任何接线。

## 1. 决策与边界

D8 的结论是 `READY_FOR_D9_PRODUCTIZATION`，其中两个零采用案例为
`ADOPTION_GAP`，不是 `CAPABILITY_GAP`。因此 D9-B 的对象是已存在的
L1 AST 与 L2 CFG 的发现和交付路径；不启动 L3 Def-Use/DFG，也不启动
L4 taint/security。

本次盘点区分两个边界：

1. **D9-B 范围**：主产品 CLI 的 bundle/evidence surfaces（本报告矩阵中的
   `provenlattice` 命令）。
2. **显式不在 D9-B 范围**：`experiments/query_interface_v1/tools/sqi_cli.py`
   的冻结 SQI-V1.2 实验接口。它有自己冻结的 envelope、预算和 qualification
   契约；不得将产品化接线混入该实验面。若未来需要，应另立兼容性任务。

默认调用必须继续不出现 `analysis` 键；没有显式请求不得查询、构建、刷新或
写入 derived data。故默认路径保持 byte/semantic compatible（忽略既有的
`query_time_ms` 计时差异）。

## 2. 当前 surface / contract 矩阵

| surface / command | 当前 analysis support | 当前 schema location | transformation path | gap | D9-B minimal contract |
| --- | --- | --- | --- | --- | --- |
| `explain-symbol` / `GraphQuery.explain_symbol` | **有**；`--analysis ast,cfg` | 顶层 `result.analysis[feature]`；section 是 D6 六态结果加 AST tree 或 CFG nodes/edges | CLI → `explain_symbol` → `_bundle_result` → `_analysis_sections` → `query_analysis` | 唯一已有实现；其 feature-map 形状尚未显式带 target identity/version | 作为单-symbol inline 的基线，迁移为下文 `analysis_v1`，仅在显式请求时 |
| `find-related-documents` / `GraphQuery.find_related_documents` | 无 | 无；基础 bundle 在 `result.bundle` | CLI → `find_related_documents` → `EvidenceBundle.create` → `_bundle_result` | anchor 是可解析 code symbol，却没有 optional analysis 通道 | 支持单-symbol inline：调用者显式请求后，对已解析 anchor 返回 `analysis_v1.targets[0]` |
| `explain-module` / `GraphQuery.explain_module` | 无 | 无；bundle 的 `related_entities` 为多个 symbols/依赖/文档 | CLI → `explain_module` → `EvidenceBundle.create` → `_bundle_result` | 没有唯一 symbol target；若对全部模块符号 inline 会失控且绕开预算语义 | 仅支持显式 selected target（必须是本响应的 code symbol）；不隐式 fan-out。未选 target 时保持当前 bundle |
| `find-related-code` / `GraphQuery.find_related_code` | 无 | 无；bundle 的 `related_entities` 是文档关联的多个 code target | CLI → `find_related_code` → `EvidenceBundle.create` → `_bundle_result` | document anchor 不是 analysis owner，且可有多个 resolved code target | 仅支持显式 selected target；目标必须来自 resolved related code entity。没有 target 时保持当前 bundle |
| `trace-evidence` / `GraphQuery.trace_evidence` | 无 | 无；返回一条 evidence 的 bundle | CLI → `trace_evidence` → `EvidenceBundle.create` → `_bundle_result` | evidence id 不一定定位唯一 symbol（也可能是 doc、edge、shard、raw ref） | **不接入 D9-B**；调用者先使用 symbol-oriented surface 获得可分析 target，避免猜测 owner |
| SQI `bundle.explain` | 无，且当前调用 `_q.explain_symbol(...)` 时不传 analysis | SQI 顶层 `data` 是二次转换后的 bundle，`evidence` 亦为投影 | `sqi_cli` → `SQIAdapter.bundle_explain` → `GraphQuery.explain_symbol` → SQI envelope | 若直接接入，会改变冻结的 SQI budget / envelope / evaluator surface | 排除；另开 qualification 后才能改变 |
| SQI `code.related` | 无 | SQI 顶层 `data` 为 document-to-code 行投影 | `sqi_cli` → `SQIAdapter.code_related` → `get_document_targets`，可附 code DB definition | 是多 target、跨数据库的 composite surface | 排除；另开 qualification 后才能改变 |

非 bundle 的基础图查询（`symbol`、`definition`、`callers`、`callees`、
`references`、`dependencies`、`dependents`、`document`、`implemented`、
`requirements`、`evidence`、`shard`、`subgraph`）返回 graph rows 或关系数据，
不构造 Agent evidence bundle；它们不属于 D9-B 的 analysis evidence surface。
`analysis` 命令仍是 D6 的直接诊断/查询 API，不是 bundle adapter。

## 3. 当前 schema 与 drift 结论

当前主产品中，`analysis` schema 只有一处定义：
`GraphQuery._analysis_sections`。因此**不存在多个 production surface 各自
定义 analysis schema 的既有 drift**。但它与 bundle schema 是平行顶层对象，
且没有 target metadata；若 D9-B 在各 command 内自行复制该装配逻辑，将立刻
产生 drift 风险。

当前 D7 section 的语义是：

```json
{
  "status": "available|not_built|stale|unsupported|not_available|corrupt|unavailable",
  "reason": null,
  "coverage": {"type": "scoped", "scope": "src"},
  "nodes": [],
  "edges": []
}
```

`available` 时，AST 另有 `root`，CFG 另有 `entry_id` / `exit_id`；两者可带
`truncated` 和 returned/total counters。该六态与 payload 形状在 D9-B 中保留，
不重新实现 AST/CFG 算法。

## 4. 冻结的最小统一契约：`analysis_v1`

仅当调用者显式传入 analysis 请求时，bundle envelope 顶层增加：

```json
{
  "analysis": {
    "version": "analysis-v1",
    "requested_features": ["ast", "cfg"],
    "targets": [
      {
        "symbol_id": "symbol:...",
        "qualified_name": "pkg.module.function",
        "features": {
          "ast": {"status": "available", "reason": null, "coverage": {}},
          "cfg": {"status": "not_built", "reason": "...", "coverage": {}}
        }
      }
    ]
  }
}
```

Contract rules:

1. `analysis` is **absent**, not `null`, when no analysis is requested. Existing
   bundle fields and their ordering/meaning remain unchanged.
2. A target is an exact, resolved base-graph code symbol. It includes both
   `symbol_id` and `qualified_name`; no analysis is inferred from a document,
   evidence ID, shard, or a name-only ambiguous match.
3. `features` is the D7/D6 section verbatim by feature name. Each target can
   independently return the existing fail-closed statuses. A missing/stale/
   corrupt feature returns its status with empty payload; it never triggers
   `analyze`, refresh, or writes.
4. The caller's requested-feature order is normalized deterministically; target
   order is deterministic. D9-B must not scan every module/document target or
   silently add analysis to `related_entities`.
5. For multi-target surfaces, analysis requires an explicit selected target
   parameter. Omitting it is a valid base-only request and produces no
   `analysis` key. A selected target that is not a returned/resolved code symbol
   fails closed with a structured request error, not a best-effort substitution.
6. AST/CFG payloads remain inline only for this explicitly selected, bounded
   target. Their existing 400-node truncation guard and counters remain the
   payload-size authority. No new evidence ID or persisted analysis reference is
   introduced in D9-B.

This contracts exactly one shared assembly helper beneath the bundle surfaces;
commands must not call analysis tables directly. `query_analysis` remains the
only source of derived data.

## 5. Agent discovery finding

D8 shows that Agent discovery currently happens only through
`provenlattice explain-symbol --help`, which exposes `--analysis`; the default
`explain-symbol` JSON deliberately has no capability marker. The other native
evidence commands have no `--analysis` option, so they provide neither a
discoverable request path nor an analysis payload. D9-B should add the explicit
request surface described above, but capability-description or prompt wording
changes are deliberately deferred to the later adoption experiment.

## 6. D9-B regression gates

| Gate | Why it is the right regression entry |
| --- | --- |
| `tests/test_analysis_tool.py` G1, G4–G11 and CLI test | Directly proves default absence/compatibility, six-state fail-closed propagation, no implicit rebuild/write, source API routing, target isolation, and truncation. Extend this first for `analysis_v1` and every newly wired native surface. |
| `tests/test_evidence.py` | Covers `EvidenceBundle.create`, deterministic bundle composition, tracing, and existing evidence-query behavior; protects existing base bundle shape and IDs. |
| `tests/test_analysis_query.py` and `tests/test_analysis_contract.py` | Protect the D6 read-only analysis query contract and derived-store isolation; D9-B must not bypass either. |
| `tests/test_analysis_cli.py` | Protects CLI parsing/validation for explicit feature and selected-target arguments. |
| `experiments/query_interface_v1/tests/test_sqi_v1.py` and `test_sqi_formal_wiring.py` | **Non-modification gates** for the excluded SQI surface: run only to demonstrate D9-B has not altered its frozen contract. They are not D9-B implementation targets. |

## 7. Completion checks

- No L3/DFG or L4/taint work was designed or started.
- This task changed documentation only; it did not change production behavior,
  default graph behavior, D0–D8 implementation, tests, or the SQI interface.
- The proposal remains optional and fail-closed: no request means base-only;
  unavailable analysis reports a status while the base evidence bundle succeeds.

**Conclusion: `READY_FOR_D9_B_IMPLEMENTATION`.** The current native bundle
surfaces and their target-identity constraints are fully inventoried, and
`analysis_v1` gives D9-B one bounded, explicit assembly contract without
mixing it with D0–D8 baseline changes or the frozen SQI qualification surface.

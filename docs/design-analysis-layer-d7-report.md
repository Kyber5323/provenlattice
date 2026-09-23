# PL-DA-D7 — Agent Deep-Analysis Tool Integration V1（实现报告）

状态：**READY_FOR_D8**
分支：`feature/analysis-layer`
日期：2026-09-22
前置：PL-DA-D0…D6（均闭合）

---

## 1. 实际 Agent/tool 接入点与新增/修改文件

| 文件 | 类型 | 说明 |
| --- | --- | --- |
| `src/provenlattice/query.py` | **修改（唯一查询层文件）** | `GraphQuery.explain_symbol` 新增 keyword-only 参数 `analysis=None`；新增 `_analysis_sections`（组装 analysis section + size guard）与模块级 `_parse_analysis_request` / `_prune_tree` / `_count_tree`。全部**加性**：不传 `analysis` 时代码路径与 D6 前完全一致 |
| `src/provenlattice/analysis_query.py` | **修改（仅加常量）** | `ANALYSIS_RESPONSE_NODE_LIMIT = 400` |
| `src/provenlattice/cli.py` | **修改** | `explain-symbol` 新增 `--analysis`（默认 None），仅此命令；其余 evidence 命令零改动 |
| `tests/test_analysis_tool.py` | **新增** | G1–G12 十三个测试 |

选择 `explain_symbol`（CLI `explain-symbol`）的理由：它是现有 Agent tool 面中最常用的 symbol 中心查询（callers/callees/references/documents bundle），`analysis` section 自然附加在其 envelope 上。未新造 `deep_ast_tool`/`deep_cfg_tool`。基础图源码（graph/incremental/storage/resolver/shard/overlay/knowledge/identity/scanner/parser/analysis_cfg）修改数 = 0。

## 2. 新增参数 Contract

```python
GraphQuery.explain_symbol(symbol, ..., analysis=None)
# analysis ∈ {None, "none", "ast", "cfg", "ast,cfg", ["ast","cfg"]}
#   None / "none" / 空值 → 结果中【不存在】"analysis" 键（不是 null 堆砌）
#   其他值   → result["analysis"] = {<feature>: <section>, ...}
```

section 形状（六态 + 载荷）：

```json
{"status": "available|not_built|stale|unsupported|not_available|corrupt|unavailable",
 "reason": null | "…", "coverage": {"type": "scoped", "scope": "src"},
 "nodes": [...], "edges": [...],
 "entry_id": "…", "exit_id": "…",        // cfg available
 "root": {...},                          // ast available（D6 tree）
 "truncated": false, "returned_nodes": n, "total_nodes": n,
 "returned_edges": n, "total_edges": n}  // available 时
```

**数据来源证明（G2）**：测试 monkeypatch `provenlattice.query.query_analysis` 并断言其被调用、参数为 `(root_path, "ast")` + `symbol_id=<foo 的基础图精确 id>`——tool 只经 D6 query API，绝不直接读 analysis tables（query.py 亦无任何 analysis 表 SQL）。

## 3. 默认调用零变化证明（G1/G11）

- `explain_symbol(symbol)` 与 `explain_symbol(symbol, analysis=None)` 结果逐键相等，且均**无** `analysis` 键；
- analyze 前后（derived layer 从不存在到存在）默认调用结果 `semantic-equivalent`（剔除 `query_time_ms` 后逐键相等）——analysis layer 的存在不改变默认查询路径；
- 全量回归 137/137 含全部既有 tool/evidence/evaluator 测试。

## 4. Response Size Guard

```python
ANALYSIS_RESPONSE_NODE_LIMIT = 400
```

- 触发时按**整节点粒度**截断（pre-order 取前 400 个节点），边仅保留两端点均在保留集内的边——不存在半 node / 半 edge；
- 显式标记：`"truncated": true` + `returned_nodes / total_nodes / returned_edges / total_edges`，JSON 恒合法（G9：502 节点函数 → truncated、returned=400、total=502、`json.dumps` 成功）；
- AST symbol 查询返回树形：超限时先计数（`_count_tree`），再按同粒度剪枝（`_prune_tree`），shape 保持稳定。

## 5. G1–G12 结果（tests/test_analysis_tool.py，13/13 OK）

| 测试 | 结果 |
| --- | --- |
| G1 test_g1_default_behavior_unchanged | 无/有 derived layer 时默认调用逐键相等；结果无 analysis 键 ✓ |
| G2 test_g2_ast_explicit_consumption | monkeypatch 证明经 D6 API；foo 自身子树（`function_definition[3]`）✓ |
| G3 test_g3_cfg_explicit_consumption | entry/exit + 全部 nodes/edges owner==foo ✓ |
| G4 test_g4_not_built_keeps_base_result | cfg=not_built 空载荷；base bundle evidence 照常返回 ✓ |
| G5 test_g5_stale_passthrough | stale + 空载荷；base 正常；无自动 refresh ✓ |
| G6 test_g6_corrupt_isolation | corrupt 透传；base bundle 不受影响 ✓ |
| G7 test_g7_exact_symbol_isolation | 三个同名 foo 按 explain→D6 链路各自返回本文件/本 owner 数据 ✓ |
| G8 test_g8_ast_and_cfg_independent | ast=available 与 cfg=corrupt 同一调用中独立共存 ✓ |
| G9 test_g9_response_size_guard | truncated/returned/total + 端点完整 + JSON 合法 ✓ |
| G10 test_g10_zero_write | available/stale/corrupt/unknown_feature/symbol_not_found 五类调用前后 base+analysis snapshot 逐字节不变 ✓ |
| G11 test_g11_tool_compatibility | 默认与 analysis=None 显式等价；CLI 旧调用方式（无 --analysis）回归 ✓ |
| G12 test_g12_dispatch_branch_exclusivity | 见 §6 ✓ |
| 附加 test_cli_explain_symbol_analysis | CLI `explain-symbol --analysis cfg` 全链路 ✓ |

**全量 ProvenLattice：137/137 OK**（33.2s，连续两次）= D6 后 124 + D7 13。

## 6. 真实"基础图信息 vs CFG 附加信息"示例（G12）

fixture：

```python
def dispatch(x):
    if x.retry:
        retry(x)
    else:
        commit(x)
def retry(x): return x
def commit(x): return x
```

- **基础图（无 analysis）只能给出**：`dispatch --CALLS--> retry`、`dispatch --CALLS--> commit` 两条扁平 CALLS 关系（测试断言 `get_callees(dispatch) == {retry, commit}`）。
- **请求 `analysis=cfg` 后额外可读出**：condition 节点的 `true` 边指向 `resolved_symbol_id == retry` 的 callsite、`false` 边指向 `resolved_symbol_id == commit` 的 callsite；两个 callsite 之间无任何边、二者经各自分支汇入同一 merge——即 **retry 与 commit 位于互斥分支**。这一信息在基础图中不存在（基础图无分支语义），查询一次即得，且不影响默认查询。

## 7. Zero-write 与生命周期

```text
index/update → base write        analyze → derived write（显式）
explain-symbol (无 analysis)      → 纯 base read（路径零变化）
explain-symbol (analysis=…)       → base read + D6 read-only query（零 mutation，G10）
```

停止条件核查：未修改默认 `code.related`/`explain-symbol` 语义（G1）、查询不隐式触发 analyze（G4/G5/G10）、optional layer 失败时基础查询照常成功（G4/G5/G6）、未绕过 D6 query API 直读 derived tables（G2 monkeypatch 反证）——全部未命中。

## 8. 已知限制（非阻塞）

1. `analysis` section 目前仅接入 `explain-symbol`；`find-related-code`/`explain-module` 等其余 evidence 命令未接（保持最小面，后续按使用证据扩展）。
2. size guard 为节点数口径（400），未按字节计费——与 evidence bundle 的条数预算风格一致。
3. 截断的 AST 树为剪枝树（shape 不变但子树缺失），Agent 需依赖 `truncated/total` 判断完整性——已在 contract 中显式。

## 9. 结论

**READY_FOR_D8** — Agent 现在可以在默认行为完全不变的前提下，显式按需消费 AST/CFG 深度分析，并获得诚实的状态（六态）与体积治理。D7 候选方向：DFG 作为第三 feature、`--analysis` 扩展到其余 evidence 命令、或先做一次真实 Agent 任务的质量观察再定。

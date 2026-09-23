# PL-DA-D4 — Python Intraprocedural CFG V1（实现报告）

状态：**READY_FOR_D5**
分支：`feature/analysis-layer`
日期：2026-09-22
前置：PL-DA-D0/D1/D2/D3（均闭合）

---

## 1. 新增 / 修改文件

| 文件 | 类型 | 说明 |
| --- | --- | --- |
| `src/provenlattice/analysis_cfg.py` | **新增** | CFG V1 materializer（本步主体） |
| `src/provenlattice/analysis_ast.py` | **修改（仅重构）** | 抽出公共 identity helper：`ast_node_id(file_id, structural_path)` 与 `structural_paths(root)`，供 CFG 消费；`_materialize` 改调用公共 helper，行为不变（D2 全部测试回归通过） |
| `tests/test_cfg_analysis.py` | **新增** | 12 个测试（G1–G11 + unsupported 专项） |

基础图源码（graph/incremental/storage/resolver/shard/query/overlay/knowledge/identity/scanner/parser）修改数 = 0（`git diff` 为空）。禁止事项全部遵守：无 DFG/SSA/use-def/alias、无异常 CFG、无 async 调度、无 generator CFG、无跨函数/模块级 CFG、无 changed_ranges、无 CLI、无 GraphView 融合、无 overlay/ENTITY_TABLES/parity 改动。

## 2. CFG V1 Node Schema

`analysis_nodes`（feature='cfg:python'）：列 `(analysis_node_id, file_id, feature, kind, payload)`。

```text
kind（冻结枚举）: entry | exit | statement | condition | return | break | continue | callsite | merge
payload = {
  owner_symbol_id,          # 必填，D2 AST 层 resolved 对齐的函数/方法 symbol
  ast_node_id,              # 能对应 AST 时 = anode:<sha256>（并在 AST 行集存在性校验）；entry/exit/merge 为 null
  start_byte / end_byte,    # 真实 CST 节点 span；entry/exit/merge 等合成节点为 null
  structural_role,          # entry | exit | statement | if_condition | loop_header | branch_merge | loop_exit
  structural_path,          # 与 AST 层同源的 type[index] 路径
  metadata,                 # callsite: {resolved_symbol_id, target_scope}；其余 {}
}
```

## 3. CFG Edge Schema

`analysis_edges`：`(analysis_edge_id, file_id, feature, src_id, dst_id, kind, payload)`。

```text
kind（冻结枚举）: next | true | false | back | return | break | continue
payload = { owner_symbol_id }   # 每条边强制与端点同 owner，禁止跨函数边
analysis_edge_id = "cedge:" + sha256(src_id, dst_id, kind, ordinal)   # ordinal 按 (src,dst,kind) 去重序号
```

## 4. cfg_node_id 规则

```text
cfg_node_id = "cnode:" + sha256(file_id ␟ owner_symbol_id ␟ structural_role ␟ structural_path)
```

不依赖 source_hash / 行号 / byte offset（span 只存 payload 不进 ID）。同 snapshot + 同 `python-cfg-v1` 重复构建完全确定（G6 两轮 DROP→重建 rows/IDs 全等）。

## 5. Owner Symbol 对齐规则（消费 D2，不重新发明）

```text
前置：file 的 ast:python 必须 fresh（见 §6）
从 AST derived 行中取 kind ∈ {function_definition, async_function_definition} 的节点
  → (start_byte, end_byte) → owner_symbol_id（alignment=resolved）或 None（unmatched）
CST function_definition 按 byte span 精确匹配：
  AST 无该 span            → skipped_ast_node_missing（fail-closed）
  alignment = unmatched    → skipped_unmatched_owner，不构建该函数 CFG（G10）
  resolved                 → 构建该函数 CFG，全部节点/边 owner = 该 symbol
```

禁止匿名猜测 CFG；禁止按名字猜 owner——完全继承 D2 已冻结的 span+kind fail-closed 对齐。

## 6. AST Dependency 规则

```text
analyze_cfg_scope 逐文件守门（顺序）：
  非 Python                    → skipped_unsupported
  未入基础图 / base hash 过期   → skipped_not_in_base / skipped_base_out_of_date
  ast:python 无 state 或 contract ≠ python-ast-v1 → skipped_ast_missing
  ast:python not fresh         → skipped_ast_stale（绝不基于 stale AST 重建）
  cfg cache fresh              → reused（不执行 replace）
  否则                         → 逐函数构建 → file × cfg:python 原子替换
```

CFG 的 `ast_node_id` 在写入前对文件 AST 行集做存在性校验，缺失则置 null——依赖不是声明而是逐节点校验。**不自动重建 AST**（单能力职责，dependency orchestration 留给未来）。

## 7. Supported / Unsupported Constructs

| Supported（V1） | 语义 |
| --- | --- |
| 顺序语句 | `next` 链 |
| if / elif / else | condition 节点，`true`/`false` 出边，`branch_merge` 汇合 |
| for / while | `loop_header`（condition），`true`→body，`false`→`loop_exit` merge，body 末→`back` 真回边 |
| return | `return` 节点，`return` 边直达 exit，无 fallthrough |
| break | `break` 边 → 所在最内层循环的 `loop_exit` |
| continue | `continue` 边 → 循环 header |
| 直接调用语句 | `callsite` 节点 + `{resolved_symbol_id, target_scope=internal/external/unknown}`（来自 RawReference 只读对齐）；**绝不跳入 callee、无 caller→callee 边**（同 scope 也不行） |
| def/class/decorated 作为语句 | 顺序 `statement` 节点；嵌套 def 由文件级收集独立建 CFG |

| Unsupported（conservative fail-closed，整函数跳过 `skipped_unsupported_construct`） |
| --- |
| try / except / finally；with；match；raise；yield / yield from；async 函数；async for/while；loop else |

**重要 grammar 发现**：当前安装的 tree-sitter-python 新版已把 `async def` 合并为 `function_definition`（带匿名 `async` token 子节点），`async_function_definition` 节点类型不再产生。async 检测据此实现为匿名 token 检查（`_is_async`），否则 async 函数会被错误地构建 CFG（实现过程中实际发现并修复了该问题）。D2 的 `ALIGNABLE_KINDS` 中 `async_function_definition` 条目因此不可达（保留无害）。

模块级控制流：不构建、不报错——文件级 CFG 只含函数；模块级 if/for 不产生任何 CFG 节点（G9 断言 `module/if_statement[1]` 不存在）。

## 8. 实际 CFG 示例（任务书 fixture，实测输出）

```python
def branch(x):
    if x: a()
    else: b()
    c()
    return x
```

```text
NODES: entry, exit, condition(if_condition), merge(branch_merge),
       callsite1(a), callsite2(b), callsite3(c), return1
EDGES:
  entry     --next-->   condition
  condition --true-->   callsite1
  condition --false-->  callsite2
  callsite1 --next-->   merge
  callsite2 --next-->   merge
  merge     --next-->   callsite3
  callsite3 --next-->   return1
  return1   --return--> exit
```

与任务书的目标图逐边一致。callsite metadata 实测携带 `{resolved_symbol_id, target_scope}`（a/b/c 未定义 → unknown，fail-closed 口径正确）。

## 9. G1–G11 测试结果（tests/test_cfg_analysis.py，12/12 OK）

| 测试 | 结果 |
| --- | --- |
| G1 test_g1_sequential_flow | entry→a→b→return→exit 链逐边断言 ✓；callsite metadata/ast_node_id 校验 ✓ |
| G2 test_g2_if_else_merge | condition 双出边 true/false、merge 入度 2、merge→c→return ✓ |
| G3 test_g3_loop_back_edge | work→header `back` 真回边、header false→loop_exit→done ✓ |
| G4 test_g4_break_continue | break→loop_exit（break 边）、continue→header（continue 边）；continue 后继仅 header，不流入 work ✓ |
| G5 test_g5_return_termination | 首个 return 后无到 work() 的边；两 return 均收敛 exit（exit 入边恰 2 条 return）✓ |
| G6 test_g6_deterministic_rebuild | DROP→重建（含 AST 依赖重建）rows/IDs 全等 ✓ |
| G7 test_g7_cache_reuse | 二次 analyze：reused=3 文件、materialized=[]、updated_at 不变（未执行 replace 的硬证明）✓ |
| G8 test_g8_ast_dependency_stale | 更新后 AST stale + CFG stale；analyze_cfg → skipped_ast_stale、不重建、旧行完整；AST 重建后 CFG 恢复 fresh ✓ |
| G9 test_g9_scope_isolation_and_module_level | scope=src 下 tests/ 无 CFG；模块级 if 不产生节点；configured() 正常 ✓ |
| G10 test_g10_unmatched_owner_skipped | 重复定义第二 def → skipped_unmatched_owner=1，仅首 def 有 CFG，路径全在 function_definition[0] 下 ✓ |
| G11 test_g11_base_graph_zero_impact | CFG 构建前后基础四表 snapshot 逐字段相等 + GraphQuery 正常 ✓ |
| 附加 test_unsupported_constructs_fail_closed | try/async/yield 三函数全部 fail-closed 跳过，文件 metadata 记录 functions_built=0 / skipped=3 / contract=python-cfg-v1 ✓ |

**全量 ProvenLattice：99/99 OK**（7.4s），连续多次运行稳定（修复了 G2 中一处按 hash id 排序导致的测试不确定断言，改为集合比较）。

## 10. 证据汇总

- **cache reuse**：G7 updated_at 逐字节不变 = 未执行 replace。
- **stale dependency fail-closed**：G8 三段式——update 后 CFG `read_current`=stale；analyze_cfg 全文件 `skipped_ast_stale` 且零重建；AST 重建后 CFG 才恢复。
- **基础图零变化**：G11 + `git diff` 为空 + 全量 99/99。
- **原子性**：复用 D1 `file × analyzer` 事务替换 + D3 事务前 payload 预验证（超限 → `skipped_payload_too_large`，文件级跳过）。

## 11. 停止条件核查

| 条件 | 结果 |
| --- | --- |
| 必须修改基础 Graph schema / identity | 未命中——cfg_node_id/cedge_id 独立 namespace，base symbol 只读消费 |
| 必须加入 overlay / parity | 未命中——analysis 表不在 snapshot()/ENTITY_TABLES |
| 无法可靠归属唯一 owner_symbol_id | 未命中——owner 完全继承 D2 resolved 对齐，unmatched 即跳过 |
| 需要 stale AST 才能完成 CFG | 未命中——AST stale → 文件级拒绝，无自动重建 |
| return/break/continue 错误 fallthrough | 未命中——G4/G5 逐边证明 |
| 循环无法形成确定性 back-edge | 未命中——G3/G6 证明 |

## 12. 已知限制（非阻塞）

1. 条件表达式内部的调用（如 `if stop(x):`）在 V1 中并入 condition 节点，不单独建 callsite——documented 口径。
2. 死代码（return 后语句）仍建节点但无入边——确定性保留，不做可达性裁剪。
3. `lambdas`、推导式随所属语句建模，无独立 CFG。
4. callsite 的 target_scope 以 analyze scope 的文件集合为界（internal/external/unknown）。

## 13. 结论

**READY_FOR_D5** — ProvenLattice 已具备第二条真实能力链：函数级 intraprocedural CFG，可信（owner 继承 fail-closed 对齐）、可缓存（updated_at 硬证明）、可失效（AST 依赖 + hash/version 失效联动）、不污染基础图。D5 候选方向：CFG 查询 API（GraphQuery/CLI）、跨 analyzer 编排（AST→CFG 自动依赖重建）、或更大 scope 的 payload/性能治理。

# PL-DA-D2 — Python AST Materialization V1（实现报告）

状态：**READY_FOR_D3**
分支：`feature/analysis-layer`
日期：2026-09-22
前置：PL-DA-D0（READY_FOR_D1）、PL-DA-D1（READY_FOR_D2）

---

## 1. 新增 / 修改文件

| 文件 | 类型 | 说明 |
| --- | --- | --- |
| `src/provenlattice/analysis_ast.py` | 新增（唯一源码文件） | scope 锁定 + CST→AST materializer + fail-closed 对齐 |
| `tests/test_ast_analysis.py` | 新增 | 11 个测试 |
| `docs/design-analysis-layer-d0-audit.md` / `d1-report.md` / `analysis.py` / `test_analysis.py` | D0/D1 已有，未变更 | — |

**现有源码修改数 = 0**（`git status` 仅 untracked 新文件，`git diff` 为空）。禁止事项全部遵守：无 CFG/DFG、无跨函数分析、无 `changed_ranges`、无 overlay/ENTITY_TABLES/parity 改动、无 CLI、无跨版本 node identity、无整坨 CST JSON。

## 2. AST V1 数据契约

存储映射：`analysis_nodes` 列承载 `(analysis_node_id, file_id, feature='ast', kind, payload)`；契约字段与列/payload 对应：

```text
analysis_node_id → 列 PK（生成规则见 §3）
file_id          → 列（join 基础图 files/nodes 的锚点）
feature          → 列，恒为 'ast'
kind             → 列，真实 tree-sitter node type（function_definition / if_statement /
                   for_statement / call / attribute / identifier / return_statement /
                   block / parameters / argument_list / expression_statement / ...）
parent_id        → payload（父 anode id；root 为 null）
structural_path  → payload（见 §3）
start_byte / end_byte / start_row / start_col / end_row / end_col
                 → payload（tree-sitter 原始 span；row 0-based，与基础图 1-based 行号
                   换算关系 ast_line = row + 1）
owner_symbol_id  → payload（可唯一对齐时 = 基础图 node id；否则 null）
alignment        → payload（'resolved' | 'unmatched'，fail-closed，见 §6）
```

**payload 白名单**（test 2 强制断言，无 text、无源码、无 CST JSON）：上列 10 个 key 之外一律不写。

**版本冻结**：`PYTHON_AST_CONTRACT = "python-ast-v1"` 写入 `analysis_state.metadata.contract`；`analysis_state.feature_version = 1`（D1 的 INTEGER contract 不变，字符串契约落在 metadata）。reuse 判定**同时**校验 `feature_version` 整数与 `contract` 字符串（analysis_ast.py:110-114）。grammar/mapping 将来变化 → bump 版本 → version invalidation，不修改旧数据语义。

## 3. analysis_node_id 生成规则

```text
analysis_node_id = "anode:" + sha256(file_id ␟ "file" ␟ structural_path)   # ␟ = \x1f
structural_path  = "module" + ("/" + f"{child.type}[{index}])*              # index = named_children 序号
```

- **不混入** source_hash / byte offset / 行号（D0 纠正落实）
- analysis_unit 固定为 `"file"`（V1 单元粒度 = 整文件 AST，函数体节点与文件单元同属一树，跨函数父链天然连通）
- 同一 source snapshot + 同一 parser contract 下重复 analyze → ID 完全确定（test 3 两轮 DROP→重建后 rows/IDs 全等）

## 4. Scope 实际选择规则（锁死）

`_normalize_scope`（analysis_ast.py:31-44）+ parts 匹配（analysis_ast.py:53-55）：

```text
合法 scope ::= repo-relative directory
  拒绝：绝对路径 / 盘符 / 含 ".." / 空与"."（=全仓，禁止 silently 扩大）/
        含空白路径组件（Windows 尾随空格归一化陷阱）/ 指向文件 / 目录不存在
文件集合 = { f ∈ scan_repository(root) : f.parts[:len(scope.parts)] == scope.parts }
```

- **parts 级匹配防前缀陷阱**：scope=`src` 不匹配 `srcfoo/escape.py`（test 4 专项验证）
- 递归覆盖 `src/a.py`、`src/sub/b.py`；`tests/c.py` 不入 scope（test 4）
- 文件枚举复用 `scan_repository`（IGNORED_DIRS/SUPPORTED_EXTENSIONS），**与基础图同一文件判定函数，无第二套过滤规则**

## 5. CST → AST Materialization 路径

`analyze_ast_scope(repo, scope, database=None)`（analysis_ast.py:47-131）→ `_materialize`（analysis_ast.py:152-190）：

```text
scan_repository(root) → scope 过滤
→ 逐文件守门（顺序即 fail-closed 链）：
    language != 'python'            → skipped_unsupported（不构造假 AST）
    file_state 无该文件             → skipped_not_in_base（基础图先行）
    disk hash != base file_state hash → skipped_base_out_of_date（防旧 base span 误配新 AST）
    state 存在 + contract 匹配 + is_analysis_fresh → reused（绝不执行 replace）
    否则                            → materialize + replace_analysis_for_file
→ parse_bytes（复用 parser_for_language("python")，无第二套 parser runtime）→ ParseOutcome.tree
→ 显式栈 DFS（无递归深度风险）：root(module) + 全部 named descendants（函数体内部完整进入）
→ 节点装配（kind/parent_id/path/span/alignment）→ 单文件×ast 原子替换（D1 事务语义）
```

守门设计说明：**analyze 只消费与基础图一致的源码视图**（disk hash == base hash 才分析）。这保证 alignment 用的 base spans 与 AST spans 描述同一份源码，从根上排除"旧 span 碰巧命中新结构"的假 resolved。

## 6. Base Symbol Alignment 规则（fail-closed）

`_symbol_spans`（analysis_ast.py:141-149，只读 `nodes` 表）：

```text
alignable AST kinds:
  function_definition / async_function_definition → base kinds {Function, Method}
  class_definition                                → base kinds {Class, Interface}
对齐键 = (start_row+1, end_row+1) 与 base (start_line, end_line) 精确相等（同源同树，span 恒一致）
唯一候选 → owner_symbol_id = <base node id>, alignment = 'resolved'
0 个或 ≥2 个候选 → owner_symbol_id = null, alignment = 'unmatched'
非 alignable kind → 恒 unmatched
```

- **禁止按函数名猜**：匹配只用 span + kind 兼容性，不用 name/qualified_name
- 重复定义（同 signature → base graph 合并为单 node、仅保留首个 span）：首个 def resolved，第二个 def unmatched（test 10 验证）
- 非 def 节点全部 unmatched（test 10 验证）

## 7. Cache hit/miss 证据（test 6）

- 第一次 `analyze scope=src`：`materialized=[a.py, sub/b.py]`
- 第二次：`reused=[a.py, sub/b.py]`、`materialized=[]`、`nodes_written=0`
- **未执行 replace 的硬证明**：两轮 `analysis_state.updated_at` 逐字节相等（replace 必然刷新 updated_at）

## 8. Stale → reanalyze 证据（test 7）

1. analyze → fresh；`a.py` 节点数 N
2. 修改 a.py（追加 `extra`/`zip` 调用）→ `incremental_update` → `read_current_analysis` = `fresh=False, reason 含 source_hash_changed`（D1 fail-closed 语义在真实 AST 上成立）
3. 重新 analyze → `materialized=[src/a.py]`、`reused=[src/sub/b.py]`（跨文件缓存不受影响）
4. `read_current_analysis` = fresh；`state.source_hash == file_state 新 hash`；节点数 > N；`module/function_definition[1]` 出现（AST 内容对应新 source）

## 9. 真实 AST 示例树（fixture `def foo(x): if/for/bar(item)/return x`，实测输出）

```text
module
  function_definition  [resolved]
    identifier                 (name)
    parameters
      identifier
    block                      (body)
      if_statement
        identifier             (condition)
        block
          for_statement
            identifier         (item)
            identifier         (x)
            block
              expression_statement
                call
                  identifier   (bar)
                  argument_list
                    identifier (item)
      return_statement
        identifier
```

函数体内部 hierarchy（if→for→call、return）完整进入 derived store，父链可从任意节点回溯（test 1 逐 parent 断言）。

## 10. 测试数量与结果

**新增 `tests/test_ast_analysis.py`：11/11 OK**（0.87s）

| # | 测试 | 验收场景 |
| --- | --- | --- |
| 1 | test_real_ast_covers_function_body_hierarchy | 场景 1 真实 AST + 层次恢复 |
| 2 | test_payload_contract_no_source_text | payload 白名单 / 无源码文本 |
| 3 | test_analyze_is_deterministic | 场景 2 确定性（DROP→重建全等） |
| 4 | test_scope_isolation_and_prefix_trap | 场景 3 scope 隔离 + 前缀陷阱 |
| 5 | test_invalid_scope_rejected | 8 种非法 scope 全拒 |
| 6 | test_cache_reuse_does_not_replace | 场景 4 reuse（updated_at 硬证明） |
| 7 | test_stale_then_refresh | 场景 5 stale→refresh |
| 8 | test_zero_impact_on_base_snapshot | 场景 6 基础 snapshot 零变化 |
| 9 | test_unsupported_language_skipped | 场景 7 .c 显式 skipped |
| 10 | test_alignment_fail_closed | resolved/unmatched/重复定义/非 def |
| 11 | test_file_not_in_base_graph_skipped | 未入基础图的文件显式 skip |

**原有全量套件：78/78 OK**（5.25s）。

## 11. 零影响证据

- test 8：真实 AST analyze（src + tests 两个 scope）前后，`nodes/edges/shards/raw_references` 四表 + boundary_edges 投影逐字段相等；`GraphQuery.find_symbol` 正常
- 基础图零修改（git diff 空）；analysis 表惰性创建（D1 test 1 持续有效）
- D1 全部 11 测试继续通过（freshness contract 未被 D2 破坏）

## 12. 停止条件核查

| 条件 | 结果 |
| --- | --- |
| 必须修改基础 Graph identity/schema | 未命中——file_id 用 `path_id` 只读派生，alignment 只读 `nodes` 表 |
| 必须加入 overlay/parity | 未命中——未触碰 ENTITY_TABLES / snapshot |
| 无法从真实 ParseOutcome.tree 获取函数体结构 | 未命中——`parse_bytes(...).tree` 直接可用 |
| scope 无法可靠限制 | 未命中——parts 匹配 + 8 类非法输入拒绝 |
| AST freshness 无法沿用 D1 contract | 未命中——is_analysis_fresh / read_current_analysis 原样复用 |

## 13. 已知限制（非阻塞，D3 注意）

1. `FEATURE_VERSIONS["ast"]` 当前不分语言；接入 cpp-ast 时需 per-language 版本或整体 bump（bump = 全量失效，安全方向）。
2. payload 体积未做上限截断（fixture 级 ~20 节点/文件）；D3 上大仓前需加 unit 上限 + `truncated` 标注（D0 §9 风险 1）。
3. 被删除文件的旧 analysis 行物理保留但恒 stale（D1 语义，未做 GC）。
4. 非 source 文件（如 .md）对 scanner 不可见，不计入 skipped 统计。

## 14. 结论

**READY_FOR_D3** — ProvenLattice 已具备"基础图 + 按目录开启的深度语法层"第一条真实能力链，且关闭时完全等价于原 ProvenLattice。D3 候选方向：CFG V1（同一 derived store、feature='cfg'）或 AST 层的查询 API + 大仓 payload 治理。

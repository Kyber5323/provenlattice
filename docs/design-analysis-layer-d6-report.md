# PL-DA-D6 — Derived Analysis Query Surface V1（实现报告）

状态：**READY_FOR_D7**
分支：`feature/analysis-layer`
日期：2026-09-22
前置：PL-DA-D0…D5（均闭合）

---

## 1. 新增 / 修改文件

| 文件 | 类型 | 说明 |
| --- | --- | --- |
| `src/provenlattice/analysis_query.py` | **新增** | 只读查询层 `query_analysis(repo, feature, *, file_path=None, symbol_id=None, database=None)` |
| `src/provenlattice/cli.py` | **修改（唯一既有源码文件）** | 新增只读命令 `analysis <repo> --feature <f> (--symbol-id ID \| --file-path PATH)`（mutually exclusive required），复用 `--database/--json` 惯例 |
| `tests/test_analysis_query.py` | **新增** | G1–G12 + CLI 共 12 个测试 |

基础图源码修改数 = 0。**查询生命周期隔离**：`analysis_query.py` 不 import `AnalysisStore`（其 init 会惰性建表）——全部读取走带 `sqlite_master` 存在性守卫的直接 SQL，**对从未 analyze 过的数据库执行查询不会创建任何 analysis 表**（G3 动态证明）。禁止事项全部遵守：无 DFG、无 callsite 自动跟随、无自动 analyze/refresh、无模糊 symbol 搜索、无 GraphViesw ranking、无自然语言查询。

## 2. Query API 与状态契约

```python
query_analysis(repo, feature, *, file_path=None, symbol_id=None, database=None) -> dict
```

```text
status 冻结枚举:
  available     fresh 数据 + 载荷（nodes/edges/root/entry_id/exit_id）
  not_built     state 不存在（reason: analysis_not_built / file_not_in_base_graph）
  stale         freshness 门未过 → nodes=[]、edges=[]（fail-closed，reason 含
                source_hash_changed / file_missing_from_base / feature_version_changed）
  unsupported   feature ∉ {ast,cfg}（unsupported_feature）或文件语言非 Python（unsupported_language）
  not_available 选择器级落空（symbol_not_found / analysis_not_available）
  corrupt       持久化完整性校验失败（entry_missing / exit_missing / edge_endpoint_missing /
                parent_missing / ambiguous_owner_nodes）→ 拒绝返回半份数据，不做自动修复
coverage:
  {"type": "scoped", "scope": "<记录于 state metadata 的原始 scope>"}
  {"type": "derived_only"}   metadata 无 scope 时不反向猜测
  {"type": "none"}
```

- **freshness 硬门与 D1 contract 同构**：`status=='current'` ∧ `state.source_hash==file_state.source_hash` ∧ `feature_version==ANALYZER_VERSIONS[analyzer]`，任一不满足即 stale 且载荷置空。D1 的 forensic `allow_stale=True` **不暴露**在查询层。
- **query 永不隐式 analyze**：无任何写路径；G4 证明 stale 查询后 rows/updated_at 逐字节不变。

## 3. Symbol Identity Lookup（精确，无猜测）

```text
SELECT * FROM nodes WHERE id=<symbol_id>        # 基础图精确 id，无名字匹配
  不存在                       → not_available / symbol_not_found
  file 分析 fresh：
    cfg → nodes 按 payload.owner_symbol_id == symbol_id 精确过滤
    ast → owner 对齐的 function_definition（恰 1 个）→ structural_path 前缀取子树
  owner 无任何行（如 try/async/yield 被跳过的函数）→ not_available / analysis_not_available
```

G6 用三个同名 `foo`（main.py / dup.py / twin.py，基础图三个不同 symbol id）证明按 id 精确返回各自函数、互不串扰；dup 的 merged symbol 只返回首个 def 的 CFG（第二个 def 在 D2 即 unmatched）。

## 4. AST Tree Reconstruction 规则

- 只消费 derived store 已 materialize 的行：子树 = `structural_path` 前缀匹配 + parent_id 链
- **child 顺序确定性**（G8）：按 structural_path 末段 `[index]` 数值 + start_byte 排序，不依赖 SQL 返回顺序；两次查询 JSON 全等
- 完整性：子树内非根节点 parent 必须在子树内，否则 `corrupt`
- **G1 的 store 来源证明**：analyze 后改写磁盘源码（不 update）→ 查询仍返回原树（含 if_statement、无 while_statement）——证明读的是 materialized AST 而非重新 parse

## 5. CFG Read Integrity 规则

读取时逐 owner 校验：entry 恰 1、exit 恰 1、每条边端点存在、边 owner 与源节点 owner 一致——任一失败 → `status=corrupt` + 具体 reason（G9：删除 entry 行 → `entry_missing`；删除 callsite 行 → `edge_endpoint_missing`）。callsite 的 `resolved_symbol_id` / `target_scope` 原样保留返回，查询层不跟随。

## 6. G1–G12 结果（tests/test_analysis_query.py，12/12 OK）

| 测试 | 结果 |
| --- | --- |
| G1 test_g1_query_real_ast_from_store | 层级恢复 ✓；改源不 update 仍返回原树（store 来源证明）✓ |
| G2 test_g2_query_real_cfg_matches_store | nodes 集合与 store 逐 id 相等 ✓；callsite metadata 保留 ✓ |
| G3 test_g3_missing_analysis_not_built | not_built；基础 snapshot 不变；**analysis 表根本未创建** ✓ |
| G4 test_g4_stale_fail_closed | stale + 空载荷；analysis_snapshot 逐字节不变（无自动重建）✓ |
| G5 test_g5_refresh_restores_available | analyze 后 available，节点数增长 ✓ |
| G6 test_g6_exact_symbol_identity | 3 个同名 foo 按 id 精确隔离；bogus id → symbol_not_found ✓ |
| G7 test_g7_file_isolation | 文件查询所有行 file_id 恒等 ✓ |
| G8 test_g8_ast_tree_deterministic | 两次查询 root JSON 全等 ✓ |
| G9 test_g9_cfg_integrity_corrupt | 两类损坏均 corrupt + 稳定 reason，无自动修复 ✓ |
| G10 test_g10_read_only_across_statuses | available/unsupported 查询前后 base+analysis snapshot 全等 ✓ |
| G11 test_g11_invalid_inputs_fail_closed | dfg→unsupported；bogus symbol→not_available；缺省/双选择器→invalid_request；全程零 mutation ✓ |
| 附加 test_cli_analysis_command | CLI 全链路 + coverage scoped ✓ |

**全量 ProvenLattice：124/124 OK**（9.1s，连续两次）= D5 后 112 + D6 12。

## 7. CLI 输出示例（实测）

`provenlattice analysis <repo> --feature cfg --symbol-id <foo> --json`（节选）：

```json
{
  "status": "available",
  "feature": "cfg",
  "analyzer": "cfg:python",
  "coverage": {"type": "scoped", "scope": "src"},
  "entry_id": "cnode:9cb4d6dc...",
  "exit_id": "cnode:e6113b93...",
  "nodes": [{"kind": "condition", "structural_path": "module/function_definition[0]/block[2]/if_statement[0]", "...": "..."},
            {"kind": "callsite", "metadata": {"resolved_symbol_id": null, "target_scope": "unknown"}}],
  "edges": [{"src": "cnode:9cb4...", "dst": "cnode:4263...", "kind": "next"},
            {"src": "cnode:4263...", "dst": "cnode:7468...", "kind": "true"}]
}
```

`--feature dfg` → `{"status": "unsupported", "reason": "unsupported_feature", "nodes": [], "edges": []}`。

## 8. 生命周期闭环

```text
index/update → Base Graph（write，行为不变）
analyze      → Derived Analysis（write，显式 scope+features）
analysis     → Derived Analysis（read-only，freshness 硬门，零 mutation）
```

停止条件核查：查询不重 parse 源码（G1 反证）、不触发 analyze（G4 反证）、能区分 not_built 与 stale（G3/G4）、能用基础图精确 symbol id 限定（G6）——全部未命中。

## 9. 结论

**READY_FOR_D7** — "按需构建 → 可控缓存 → 精确读取"链路完整闭合。D7 决策依据已齐备：可做 DFG（third feature，contract 就位），或优先让 Agent 工具消费 AST/CFG（`explain-symbol` 系列与 query_analysis 的整合），建议以后者先行的证据决定。

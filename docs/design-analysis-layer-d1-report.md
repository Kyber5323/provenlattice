# PL-DA-D1 — Derived Analysis Store Foundation（实现报告）

状态：**READY_FOR_D2**
分支：`feature/analysis-layer`
日期：2026-09-22
前置：PL-DA-D0（docs/design-analysis-layer-d0-audit.md，READY_FOR_D1）

---

## 1. 新增 / 修改文件

| 文件 | 类型 | 说明 |
| --- | --- | --- |
| `src/provenlattice/analysis.py` | 新增（唯一源码文件） | 派生分析 store + fail-closed freshness API |
| `tests/test_analysis.py` | 新增 | 11 个测试，覆盖 G1–G5 全部验证组 |
| `docs/design-analysis-layer-d0-audit.md` | 新增（D0 报告，未变更） | — |

**现有源码文件修改数 = 0**（`git status`：仅 3 个 untracked 新文件；`git diff` 为空）。`replace_snapshot`、`ENTITY_TABLES`、overlay、Node/Edge/RawReference schema、基础 parity contract 全部未触碰——停止条件零命中，无需任何兼容性绕过。

## 2. Analysis Schema（独立派生表，惰性创建）

```sql
analysis_state(file_id, feature, source_hash, feature_version,
               status CHECK(current|stale), metadata, updated_at,
               PK(file_id, feature))
analysis_nodes(analysis_node_id PK, file_id, feature, kind, payload)
analysis_edges(analysis_edge_id PK, file_id, feature, src_id, dst_id, kind, payload)
+ idx (file_id, feature) on nodes/edges
```

关键设计：

- **schema 定义在 `analysis.py` 的 `ANALYSIS_SCHEMA`，不在主 `SCHEMA` 中**：仅当显式打开 `AnalysisStore` 时才建表。基础 index/update 全生命周期**从不创建** analysis 表（test 1 动态证明）。
- 纯新模块：`AnalysisStore` 只经由 `SQLiteStorage` 的公开接口（`row/rows/transaction/connection`）操作，复用其事务语义，未改动 storage.py 任何一行。

## 3. Storage API（analysis.py:79-227）

```text
get_analysis_state(file_id, feature) -> dict | None
is_analysis_fresh(file_id, feature, source_hash, feature_version) -> bool
base_source_hash(file_id) -> str | None            # 唯一哈希来源 = file_state
freshness_against_base(file_id, feature) -> dict   # {fresh, reason, stored/base hash, versions}
replace_analysis_for_file(file_id, feature, source_hash, feature_version,
                          nodes, edges, *, metadata) -> None   # 单文件×单feature 原子替换
delete_analysis_for_file(file_id, feature) -> bool
read_analysis_for_file(file_id, feature, *, source_hash, feature_version,
                       allow_stale=False) -> dict | None    # 显式比对读
read_current_analysis(file_id, feature, *, allow_stale=False) -> dict | None
                                                    # 对 base file_state 哈希 fail-closed 读
```

- `FEATURE_VERSIONS = {"ast": 1, "cfg": 1}`；未知 feature 一律 `ValueError`（fail-closed，`dfg` 等未定义 feature 无法写入/读取）。
- `freshness_against_base` 的 reason 枚举：`not_built / status_not_current / file_missing_from_base / source_hash_changed / feature_version_changed`。

## 4. Fresh/Stale 判定代码路径

`is_analysis_fresh`（analysis.py:100-109）：

```text
fresh := state 存在
     AND state.status == 'current'
     AND state.source_hash == <调用方/基图提供的当前 hash>
     AND state.feature_version == <当前 feature_version>
```

**fail-closed 读取语义**（read_* 两个入口共同走 `_read_payload`，analysis.py:210-217）：

- `fresh=True` → 返回 `{fresh: True, nodes, edges, state}`
- `fresh=False` 且未显式 `allow_stale=True` → 返回 `{fresh: False, reason, nodes: [], edges: []}`——**旧 payload 永不出现在结果中**
- `allow_stale=True` 为显式取证逃生口，返回值仍带 `fresh: False` 标记
- `read_current_analysis` 的当前哈希**不信任调用方**，直接取自基础图 `file_state`（analysis.py:119-124）——"analysis 表存在"绝不充当 freshness 证据

## 5. 原子替换行为

`replace_analysis_for_file`（analysis.py:139-177）在单个 `storage.transaction()` 内完成：

```text
DELETE nodes WHERE file_id=? AND feature=?
DELETE edges WHERE file_id=? AND feature=?
DELETE state WHERE file_id=? AND feature=?
INSERT state(current) / executemany nodes / executemany edges
```

- 作用域精确到 `(file_id, feature)`：同文件其他 feature、其他文件的一切行不被触碰（test 6/7 逐行字节比对证明）
- 中途失败（如 PK 冲突）→ 事务整体回滚，旧数据完整保留（test 8：注入重复 node id 触发 `IntegrityError`，替换前状态与 cfg 邻居均不变）

## 6. 测试数量与结果

**新增套件 `tests/test_analysis.py`：11/11 OK**（0.88s）

| # | 测试 | 验证组 |
| --- | --- | --- |
| 1 | `test_analysis_tables_are_lazy_and_absent_from_base_infrastructure` | 基础设施排除证明 |
| 2 | `test_writing_synthetic_analysis_does_not_change_base_snapshot` | **G1** 写入不改变四表 snapshot |
| 3 | `test_dropping_analysis_tables_restores_base_equivalence` | **G1** DROP 后 snapshot 等价 + GraphQuery 正常 |
| 4 | `test_incremental_update_preserves_rows_but_fails_freshness` | **G2**（最重要测试，见 §7） |
| 5 | `test_cache_reuse_and_invalidation_triggers` | **G3** reuse / hash 变 / version 变 / 重建 |
| 6 | `test_feature_isolation_on_same_file` | **G4** ast 更新后 cfg 逐行不变 |
| 7 | `test_file_isolation_across_files` | **G5** A 更新后 B 逐行不变 |
| 8 | `test_replacement_is_atomic_and_scoped` | 原子性 + 作用域 + 失败回滚 |
| 9 | `test_delete_analysis_for_file_removes_all_rows` | 删除幂等 + 全行清理 |
| 10 | `test_unknown_feature_is_rejected` | 未知 feature fail-closed |
| 11 | `test_missing_base_file_is_stale` | 文件删除后 `file_missing_from_base` |

**原有 ProvenLattice 全量套件：67/67 OK**（4.35s，`unittest discover -s tests`），含 D0 前全部既有测试（integration/cpp/overlay/knowledge/evidence/core/retrieval/evaluator）。

## 7. 关键测试：源文件改变后旧 analysis 不得作为当前结果返回

`test_incremental_update_preserves_rows_but_fails_freshness` 的完整链条：

1. `service.py` (hash H_A) 全量建图 → 写入 synthetic AST（绑定 H_A, v1）→ `read_current_analysis` = fresh，返回 a:1/a:2
2. 修改为 REVISION_B → `incremental_update` → 基础图正常更新（`find_symbol("beta")` 命中）
3. **物理层面**：`analysis_nodes` 仍 2 行、`analysis_state.source_hash` 仍 = H_A（`replace_snapshot` 的 DELETE 白名单未触碰 analysis 表——D0 结论的动态验证）
4. **逻辑层面**：`file_state` 新 hash ≠ H_A → `freshness_against_base` = False（reason 含 `source_hash_changed`）
5. **fail-closed 读取**：`read_current_analysis` → `fresh=False, nodes=[], edges=[]`——旧 payload 拒绝返回；`allow_stale=True` 取证模式可见旧数据但恒带 `fresh=False`
6. 以新 hash 重新 `replace` 后 → fresh 恢复，且只返回新节点 b:1/b:2

## 8. 排除性证明（snapshot / overlay / ENTITY_TABLES）

- **snapshot**：test 2/3 用与 `test_integration.snapshot` 同口径的四表 + boundary_edges 投影，写入 analysis 前后、DROP 后均逐字段相等；基础表零行变化。
- **ENTITY_TABLES**：test 1 断言 `{analysis_state, analysis_nodes, analysis_edges} ∩ ENTITY_TABLES 值域 = ∅`。
- **overlay 语义**：`overlay.py` 零改动；analysis 表不在任何 materialize/conflict 路径上。
- **惰性创建**：test 1 证明纯 `SQLiteStorage` 打开（index/update 的实际路径）后 `sqlite_master` 无任何 analysis 表。

## 9. 边界遵守核查

禁止项全部未触碰：无 `analyze` CLI；无 CST 遍历；无 AST taxonomy 定义（payload 为 synthetic `test_ast_node/test_ast_edge`）；无 CFG/DFG；Node/Edge/RawReference schema 未改；未入 ENTITY_TABLES；未入 parity snapshot；overlay/tombstone 未改；index/update 行为零变化；未使用 `changed_ranges`。D1 粒度严格为 **file × feature 全量派生替换 + hash/version 缓存**。

## 10. 结论

**READY_FOR_D2** — 下一步按既定计划接入现成 CST：AST-only、单语言（Python）、单文件/目录 scope 的第一条真实能力链，payload schema 与 `analysis_nodes.kind` 枚举在 D2 冻结。

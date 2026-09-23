# PL-DA-D3 — Analysis Contract Hardening（实现报告）

状态：**READY_FOR_D4**
分支：`feature/analysis-layer`
日期：2026-09-22
前置：PL-DA-D0/D1/D2（均闭合）

---

## 1. 新增 / 修改文件及原因

| 文件 | 类型 | 修改原因 |
| --- | --- | --- |
| `src/provenlattice/analysis.py` | **修改**（D1 建立、本次收紧） | analyzer identity 注册表、payload 硬上限、fail-closed 注册校验。这是 derived store 自身契约的本体，属 D3 任务目标 |
| `src/provenlattice/analysis_ast.py` | **修改**（D2 建立） | 改用 `ast:python` analyzer id、metadata 增加 analyzer 字段、oversize 捕获为 per-file skip |
| `tests/test_analysis.py` | **修改**（D1 测试） | feature 字符串 → analyzer id；新增语法非法 id 用例 |
| `tests/test_ast_analysis.py` | **修改**（D2 测试） | SQL helper 与直调改用 `ast:python` |
| `tests/test_analysis_contract.py` | **新增** | G2–G6 九个 contract 测试 |

**基础图源码（graph/incremental/storage/resolver/shard/query/overlay/knowledge/identity/scanner/parser）修改数 = 0**（`git diff` 为空）。禁止事项全部遵守：无 CFG/DFG、无 AST taxonomy 变更、无 changed_ranges、无 CLI、无基础 schema/overlay/ENTITY_TABLES/parity 改动、无静默截断、基础 index/update 仍从不创建 analysis 表。

## 2. 最终 Analyzer Identity Schema

```text
analyzer id        = "<feature>:<language>"        例：ast:python
  feature          ∈ KNOWN_FEATURES = {ast, cfg}   （D0 冻结枚举；dfg 拒绝）
  language         匹配 [a-z0-9_-]+
analyzer version   = 整数，注册表 ANALYZER_VERSIONS = {"ast:python": 1, "cfg:python": 1}
analyzer contract  = "<language>-<feature>-v<version>"  例：python-ast-v1
```

- 存储映射：**`analysis_state/nodes/edges` 的 `feature` 列现在承载 analyzer id**（值为 `ast:python` 等）。列名不变 → **零 schema 变更、零迁移**；`PK(file_id, feature)` 自然升级为 **file × analyzer**。
- **最终 replacement 粒度（对 D1 的显式演进）**：D1 为 file × feature；D3 起为 **file × analyzer**，analyzer 唯一确定 feature + language + contract/version。同 analyzer 升级 = 全量替换（强于 stale）；跨 analyzer = 行级完全隔离。
- `analysis_state.metadata` 冻结字段：`analyzer`、`contract`、`language`、`scope`、`node_count`。
- `register_analyzer(id, version)`：语法校验 + 冲突拒绝（同 id 不同 version → ValueError）。

## 3. Cache Key / Freshness Key / Invalidation 规则

```text
cache 行键     = (file_id, analyzer_id)
freshness 键   = file_id + source_hash + analyzer_id + feature_version
                 （registry 校验先行：未注册 analyzer 一律 ValueError，绝不猜）
invalidation:
  source_hash 变化            → source_hash_changed → stale
  注册表 version bump         → feature_version_changed → stale（全量，无迁移）
  文件离开基础图               → file_missing_from_base → stale
  analyzer 升级 = 同键全量替换；跨 analyzer 永不互读
```

fail-closed 强化（本步新增）：`read_current_analysis` / `freshness_against_base` 在 **state 查找之前**先做注册表校验——未注册 analyzer 即使有残留 state 也硬性 ValueError，而非伪装成 `not_built`（这是 G3 的关键：`ast:python-v999` 不可能 fallback 到 v1 或读取其他 cache）。

## 4. Payload Byte Limit

```python
MAX_ANALYSIS_NODE_PAYLOAD_BYTES = 4096
```

**取值依据（实测，非拍脑袋）**：D2 fixture（两个真实 Python 文件）35 个 AST 节点，payload 编码尺寸 **192–409 字节**；4096 ≈ 实测 max 的 10 倍余量，可容纳 ~140 层表达式嵌套（当前最深自然结构 ~30 层）。同时远小于"塞入整函数源码"所需的量级，语义上仍是结构层而非文本层。

**计算方式**：`encoded_payload_size(payload) = len(json.dumps(payload, sort_keys=True).encode("utf-8"))`——与落库编码**完全同函数**（store 内部统一走 `_payload_json`），字节口径即持久化口径（test G4 用同一 helper 构造 exact/limit+1 两例）。

## 5. Oversize Fail-Closed 行为与原子性

**校验位置：`replace_analysis_for_file` 事务开始之前**（analysis.py:198-214）——逐 node/edge 预验证，任一超限立即 `PayloadTooLargeError`（携带 analyzer/file_id/item_id/size/limit），**任何 DELETE 都未发生**。原子性因此是构造性保证而非回滚恢复：

```text
materialize → 全量 payload 预验证 → 任一超限 ⇒ 零写入（连事务都不开启）
                                  ⇒ 全部通过   ⇒ 原事务内 file × analyzer 原子替换
```

两层行为：

- **store 层**（保护所有现在/未来 analyzer）：raise，调用方数据完整保留（G5 store 级测试：失败前后 state + nodes 逐字节相等）
- **scope 层**（`analyze_ast_scope`）：per-file 捕获 → `skipped_payload_too_large` 列表 + 明确 reason，**不产生半棵 AST**，scope 内其他文件继续正常处理（G5 scope 级测试）

无静默截断——超限即拒绝，reason 恒为 `payload_too_large`。

## 6. 验证结果（tests/test_analysis_contract.py，9/9 OK）

| 测试 | 覆盖 |
| --- | --- |
| test_analyzer_helpers_and_registry | id/contract 构造、注册冲突拒绝、非法 feature/language 拒绝 |
| test_analyze_result_carries_analyzer_identity | analyze 结果与 state metadata 携带完整 analyzer 身份 |
| test_g2_same_analyzer_version_upgrade_invalidates | v1→v2：`fresh(v1)=False`、旧 payload 拒读、行内无 v1 残留 |
| test_g2_cross_analyzer_isolation | `ast:python` 与 `ast:zlang` 同 hash 同 version=1：互不命中、各自 payload、删一个另一个完好 |
| test_g3_unknown_analyzer_fail_closed | `ast:python-v999` read/freshness 均 ValueError；显式写入 version=999 → `feature_version_changed` stale、payload 拒读、**无 v1 fallback**；恢复 v1 → fresh |
| test_g4_payload_boundary_exact | `size=4096` 接受；`size=4097` 拒绝（字节级，同落库编码） |
| test_g5_store_level_oversize_is_atomic | 超限 replace 后 state+nodes 与失败前逐字节相等 |
| test_g5_scope_level_oversize_file_fails_closed_and_recovers | 150 层嵌套文件 → `skipped_payload_too_large`、旧行完整；还原源码后 **hash 相同 → reuse 恢复 fresh**（内容寻址缓存的正确行为） |
| test_g6_base_graph_zero_impact | 真实 analyze + 跨 analyzer 写入 + 超限拒绝前后基础四表不变 |

## 7. G1 — D2 AST Regression

`tests/test_ast_analysis.py` 11/11 OK（仅 analyzer id 字符串随 contract 更新）：函数体层次（function_definition→block→if→for→call）、节点 ID 确定性、scope 隔离/前缀陷阱、reuse 的 updated_at 硬证明、stale→refresh、alignment resolved/unmatched、零影响——**语义行为与 D2 完全一致**。

## 8. 全量测试结果

**87/87 OK**（5.3s）= 原有 67 + D1 11 + D2 11 + D3 9。D1/D2/D3 三套 analysis 测试在新 contract 上全部通过。

## 9. 停止条件核查

| 条件 | 结果 |
| --- | --- |
| 破坏 D1 freshness / atomic replacement 契约 | 未命中——freshness 键只增维度（analyzer），原子替换原样保留且被预验证强化 |
| payload limit 无法 replace 前完整验证 | 未命中——预验证在事务外，零写入失败 |

## 10. 结论

**READY_FOR_D4** — derived-analysis 底座已具备 `file × analyzer` 稳定契约（`ast:python` / `cfg:python` 身份就位）与字节级 payload 治理。下一阶段按计划进入 **Python intraprocedural CFG V1**：feature=`cfg`、analyzer=`cfg:python`、contract=`python-cfg-v1`，直接落在本次冻结的 store/analyzer/cache contract 上。

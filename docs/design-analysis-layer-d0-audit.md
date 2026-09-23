# PL-DA-D0 — Optional Deep Analysis Architecture Contract（只读审计报告）

状态：**READY_FOR_D1**
分支：`feature/analysis-layer`
日期：2026-09-22
范围：只读审计，未修改任何代码、未建表、未实现 CLI。

---

## 0. 结论先行

1. **方向成立**：AST / CFG 作为"按目录、按 feature 的可选派生分析层"，在不修改基础图语义的前提下可挂载，**无停止条件命中**。
2. **归属判定**：optional analysis 必须使用**独立 derived tables**（主 SQLite 内新增 `analysis_*` 表族），**绝不进入 `ENTITY_TABLES` / overlay 体系**（代码证据见 §6）。
3. **零影响可证明**：`replace_snapshot` 与 `snapshot()` parity gate 均按固定表集合操作，analysis 表天然不在其作用域内；只要 analysis builder 不写基础表，基础图结果逐字节不变（证明思路见 §8）。
4. **ID 设计采纳 D0 纠正**：AST/CFG 节点 ID 只要求同一 source snapshot 内确定性稳定，**不混入文件级 `source_hash`**；`source_hash` 仅用于缓存有效性判断。跨版本节点追踪（`symbol_id + structural_path`）不进入第一版。
5. **CFG 边界采纳 D0 纠正**：V1 严格 intraprocedural；call-site 只是当前函数 CFG 中的节点，可挂 `resolved_symbol_id + target_scope`，**不展开目标函数**。
6. **DFG 不进入 V1**（见 §7.2 分阶段理由）。

---

## 1. 审计范围

按任务书调查五项：

| # | 调查项 | 结果位置 |
| --- | --- | --- |
| Q1 | parser 已产生 / 丢弃的 AST 信息 | §2 |
| Q2 | Node / Edge / RawReference / file_state / shard 的 schema 与写入路径 | §3 |
| Q3 | Full / Incremental parity 覆盖范围 | §4 |
| Q4 | CLI 入口与扩展点 | §5 |
| Q5 | 可挂载 derived-analysis store 的现有机制 + 归属判定 | §6 |

---

## 2. Q1 — Parser 层：已产生与被丢弃的 AST 信息

### 2.1 已产生、但当前用后即弃

完整 tree-sitter CST **每次解析都会构建**，且已被封装在返回值中：

- `ParseOutcome(parsed, tree, changed_ranges)`（`parsing/base.py:10-14`）
- Python：`TreeSitterParser.parse_bytes` 返回完整 `tree` 与 `changed_ranges`（`parsing/tree_sitter.py:25-41`）
- C/C++：`CppTreeSitterParser.parse_bytes` 同样返回（`parsing/cpp.py:237-244`）

三个事实：

1. **CST 在 extractor 运行完毕后被整体丢弃**。所有调用方只取 `outcome.parsed`：`parser.py:29-32`（`parse_file_with_diagnostics` 只返回 `parsed` 与 `has_error`）。
2. **`changed_ranges` 全项目零消费**。`parse_bytes` 的 `old_tree` 参数已实现 old-tree 复用与变更区间计算，但 `parser.py:32` 从不传 `old_tree`——增量复用目前靠 `file_state` 哈希在文件粒度完成，语法粒度复用能力闲置。
3. 因此 **AST 层不需要引入任何新 parser runtime**：同一棵 CST 上挂第二个 extractor 即可（增量场景下重解析文件也可传入 `old_tree`）。

### 2.2 被提取并持久化的（基础层现状）

`ParsedFile = symbols + references + imports`（`models.py:150-154`），并整体 JSON 化持久在 File 节点 metadata（见 §3.3）：

| 提取物 | Python | C/C++ | 位置 |
| --- | --- | --- | --- |
| 符号 | `function/async_function/class/type_alias/annotated assignment` | `namespace/class/struct/function/declaration/field_declaration/type_definition/alias/using` | `parsing/python.py:58-93,183-206`；`parsing/cpp.py:76-94` |
| 引用 | `call` 节点→CALLS；`identifier`→REFERENCES（排除参数/定义位）；class bases→IMPLEMENTS | `call_expression`→CALLS | `parsing/python.py:114-121,160-181,208-229`；`parsing/cpp.py:164-173` |
| 导入 | `import / from-import`（含 alias 表） | `preproc_include` | `parsing/python.py:128-158`；`parsing/cpp.py:156-162` |

引用候选统一进 `RawReference`（语法事实层），resolved 才提升为 `Edge`（`resolver.py:306-319`）。

### 2.3 被丢弃的（正是 AST/CFG 层需要补的）

walker（`parsing/python.py:58-79`、`parsing/cpp.py:76-96`）对以下 CST 结构**直穿不记录**：

- **全部控制流**：`if/elif/else、for/while、try/except/finally、with、match、switch/if/for/while/do` —— CFG 的原始材料
- **语句边界与块结构**：`block/compound_statement` 的语句序列 —— 语句级 CFG 切分依据
- **局部赋值与局部变量**：函数体内 assignment 完全丢弃（Python 仅模块级 annotated assignment 建 Type 符号，`python.py:195-206`；C++ 无 declaration 语句提取）—— DFG 的核心缺口
- **参数结构**：只存活于 `signature` 字符串
- **函数体字节范围**：仅存行号（`start_line/end_line`）

**审计结论**：AST 层 = "对现有 CST 的第二遍、更完整的遍历"，成本增量是第二次树遍历 + 序列化存储；CFG 层在此之上做确定性结构化。两者都不触碰 extractor 现有输出。

---

## 3. Q2 — Schema 与写入路径

### 3.1 基础表族（`storage.py:12-96` SCHEMA）

```text
repositories / files / shards / nodes / edges / shard_edges
raw_references / file_state / graph_generation
（Knowledge 附带）raw_evidence_links / document_state
```

关键列：

- `nodes`：`id, kind, repo_id, shard_id, file_id, name, qualified_name, language, start_line, end_line, signature, source_hash, generation, metadata(JSON)`（storage.py:29-34）
- `raw_references`：含 `owner_symbol_id`、`start_line/end_line`、`status CHECK(resolved/ambiguous/unresolved)`、`candidate_symbols`、`resolved_symbol_id`（storage.py:45-54）；四个查询索引（storage.py:88-91）
- `file_state`：`file_id, path, source_hash, generation`（storage.py:70-73）
- `shard_edges`：boundary 边反查索引（storage.py:40-44）

### 3.2 写入路径（唯一性与双轨制）

**轨道一：`replace_snapshot`（storage.py:200-312）** —— `full_index` 与 `incremental_update` 的**唯一**落库入口：

- 全量 `DELETE` 后重写 `edges/shard_edges/raw_references/nodes/shards/files/file_state`（storage.py:216-225）。**增量是计算意义上的增量，存储意义上的全量重写**。
- 唯一豁免：`metadata.layer='knowledge'` 的 nodes/edges 行在 DELETE 中被保留（storage.py:216-221）。
- 同事务写 `repositories`、`graph_generation`（storage.py:227-231, 309-312）。

**轨道二：Knowledge 原位写（`knowledge.py:172-423`）** —— 第二构建器先例：

- 不走 `replace_snapshot`，直接 `storage.connection.execute` 原位 INSERT/UPDATE/DELETE 后自行 commit（knowledge.py:274-277, 395-399, 422）。
- 靠 `metadata.layer='knowledge'` 标记与 replace_snapshot 的豁免逻辑共存。
- 自带增量状态表 `document_state`（content_hash + content-addressed version，knowledge.py:184-191, 242）。

**对 analysis 层的含义**：analysis 采用轨道二的"独立构建器"形态，但**比 knowledge 更干净**——knowledge 仍要写 nodes/edges 表（因此需要 layer 豁免补丁）；analysis 写完全独立的 `analysis_*` 表，与 `replace_snapshot` 的 DELETE 集合**零交集**，无需任何豁免逻辑。

### 3.3 现成的派生数据缓存先例：parser_cache

- `build_graph` 把整个 `ParsedFile` JSON 化存进 File 节点 `metadata.parsed`（graph.py:121）
- 增量时未变文件**不重新解析**，从该缓存反序列化还原（`incremental.py:76-79` + `storage.parser_cache` storage.py:158-167 + `parsed_from_dict` graph.py:25-46）
- 失效判据 = `file_state.source_hash` 比对（incremental.py:42-45）

**这是"派生数据 + 文件哈希失效 + 未变即复用"语义的现成实现**，analysis cache 的 invalidation 语义与它同构，且 analysis cache 应该做得更好（独立表而非塞 metadata，避免膨胀 nodes 行）。

### 3.4 Identity 现状（analysis 不得破坏的对象）

`identity.py:12-54` 全部 content-addressed、不依赖行号：

- `symbol_id = f(repo_id, path, kind, qualified_name, signature)` —— **不含 source_hash、不含行号**（空行修改不改变 symbol 身份，body-only 修改测试依赖此性质，test_integration.py:99-122）
- `path_id / edge_id / raw_reference_id(+ordinal)`

---

## 4. Q3 — Full / Incremental Parity 覆盖范围

### 4.1 parity 比较的实际表集合

`snapshot()`（`tests/test_integration.py:61-81`）比较固定四表 + 一个投影：

```text
nodes / edges / shards / raw_references   （忽略 generation、boundary_dirty）
+ boundary_edges（edges 中 metadata.scope='boundary' 的投影）
```

### 4.2 parity 测试证据

| 测试 | 位置 | 覆盖 |
| --- | --- | --- |
| `test_full_incremental_parity_for_required_changes` | test_integration.py:124-142 | Python 全量 vs 增量，`snapshot(incremental.db) == snapshot(full.db)` |
| `test_candidate_disappearance_rebuilds_ambiguous_reference` | test_integration.py:155-178 | 候选消失后 ambiguous→resolved 重建 parity |
| `test_cpp_full_incremental_parity_and_cross_shard_edge` | test_cpp.py:76 | C/C++ parity + 跨 shard 边 |
| `test_full_incremental_and_overlay_parity` | test_knowledge.py:121-158 | Knowledge 层也在 parity 内（因 knowledge 行就在 nodes/edges/raw_evidence_links 中）+ overlay materialization 对 RawEvidenceLink 的 parity |

### 4.3 与 overlay 的关系

`ENTITY_TABLES`（overlay.py:19-26）是 overlay 体系的封闭准入清单：`Node/Edge/RawReference/RawEvidenceLink/Shard/BoundaryEdge`。所有 materialize/conflict/version 逻辑遍历该字典（overlay.py:143,196,205,228,361,376,385,395-396,409）。**不在清单内的实体不参与 overlay**——已有判例：`document_state` 是索引状态、明确不是 Overlay 图实体（design-v1.md:96）。

---

## 5. Q4 — CLI 入口与扩展点

- 命令注册：`build_parser`（cli.py:23-96）；`command()` helper 自动附加 `--database/--json`
- 构建类命令：`index / update / knowledge`（cli.py:33-41），查询类命令走 `GraphQuery`
- 分发：`run()`（cli.py:99-171）；`_database()` 统一默认路径 `<repo>/.provenlattice/codegraph.db`（cli.py:16-20）
- **`knowledge` 是新构建命令的完整模板**：
  1. 前置校验基础索引存在（knowledge.py:180-181）
  2. 复用 `repositories` 中的 `repo_id` 与 `current_generation`（knowledge.py:182-183）
  3. 自己的状态表做增量（`document_state`）
  4. 自己的 metrics，返回 dict 交给 CLI 的 `--json` 通道

`analyze` 命令照此模板：新增 `add_parser("analyze")` + `--scope`（append，多个）+ `--features`（`ast,cfg` 组合）+ `--incremental`，分发到 `analysis.index_analysis(...)`。**index/update 命令零改动**。

---

## 6. Q5 — 挂载机制与归属判定

### 6.1 判定：独立 derived tables，不进 overlay

**判定**：在主 SQLite（同一 `codegraph.db` 文件）新增 `analysis_*` 表族；**不**加入 `ENTITY_TABLES`。

**代码证据**：

1. **ENTITY_TABLES 封闭枚举 + 全逻辑遍历**（overlay.py:19-26, 143, 196, 205, 228, 361-409）。加入即自动卷入 conflict（`ENTITY_CONFLICT/DELETE_UPDATE_CONFLICT/ADD_ADD_CONFLICT`）、tombstone、rebase、`capture_snapshot_delta`。而 analysis 产物是**从源码确定性可重建的派生数据**，没有"两个会话并发编辑同一 CFG 节点"的真实语义——强行进 overlay 是把无冲突语义的数据塞进冲突机器。
2. **`replace_snapshot` 的 DELETE 集合与 analysis 表零交集**（storage.py:216-225 只触碰基础表）。analysis 行若混入 nodes/edges，将重演 knowledge 的 layer 豁免补丁（storage.py:216-221）；独立表则**不需要任何对现有函数的修改**。
3. **parity gate 按固定表集合工作**（test_integration.py:61-81）。analysis 表不在 `snapshot()` 内 → 天然排除在 Full/Incremental parity 之外；同时因为 analysis 不写基础表，基础 parity 的**前提**（两路构建在基础四表上产生相同行）不受任何影响。
4. **先例一致**：`document_state` 不进 overlay、不参与 parity（design-v1.md:96 明文）；`OverlayStore` 本身就是"旁挂 store"形态（独立 SQLite，overlay.py:73-103）。derived analysis 挂主 DB 内独立表（利于 join `file_id/owner_symbol_id`），比 sidecar DB 更合适。

### 6.2 可直接复用的组件

| 组件 | 位置 | 复用方式 |
| --- | --- | --- |
| tree-sitter CST 构建与 old-tree 复用 | tree_sitter.py:25-41 / cpp.py:237-244 | analysis builder 调 `parse_bytes` 拿 `tree`（第二次消费同一对象） |
| `scan_repository` + `IGNORED_DIRS/SUPPORTED_EXTENSIONS` | scanner.py:8-64 | scope 内文件枚举直接过滤，保证与基础扫描同一文件集合 |
| `file_state` 哈希机制 | storage.py:70-73, 152-156；incremental.py:42-45 | analysis invalidation 的哈希来源（不另算哈希） |
| `document_state` 增量先例 | storage.py:64-69；knowledge.py:184-191 | `analysis_state` 表的同构蓝本 |
| parser_cache"未变不重解析"先例 | storage.py:158-167；incremental.py:76-79 | analysis 复用同一文件粒度跳过逻辑 |
| `shard/strategy.path_for` 目录语义 | shard.py:20-98 | scope→文件集合判定可对齐 shard 粒度 |
| internal/boundary 边分类 | shard.py:130-136 | CFG call-site `target_scope` 判定的语义模板 |
| knowledge 构建命令模板 | knowledge.py:172-183；cli.py:39-41,111-112 | `analyze` 命令结构 |
| `raw_references` 索引（owner/file/resolved） | storage.py:88-91 | CFG call-site 对齐 `resolved_symbol_id` 的只读来源 |
| SQLite 事务上下文 | storage.py:129-136 | analysis 落库复用 |

### 6.3 需要新增的最小组件

```text
src/provenlattice/analysis/
  __init__.py          index_analysis(repo, database, *, scopes, features, incremental) -> dict
  ast_extract.py       第二遍 CST 遍历：函数体语句/表达式结构序列化（不触碰现有 extractor）
  cfg_build.py         intraprocedural CFG：entry/exit、语句节点、call-site 节点、边
  model.py             AnalysisUnit / CfgNode / CfgEdge dataclass + ANALYSIS_FEATURE_VERSION
（storage 侧仅新增建表 SQL 与三个读写函数，不改任何现有函数）
```

CLI：`analyze` 子命令 + `run()` 分支。基础管线（graph/incremental/resolver/shard/identity/storage 既有函数）**零修改**。

---

## 7. 最小 Contract（PL-DA-D0 冻结草案）

### 7.1 分层与归属

```text
Base Graph（现状，永远构建，永不因 analysis 改变）
  Node / Edge / RawReference / Shard / BoundaryEdge / file_state

Optional Analysis（独立 derived tables，按需按 scope 构建）
  feature = ast        函数体结构化语法树（语句/表达式序列化）
  feature = cfg        intraprocedural 控制流图
  [dfg 不进入 V1]
  实体：不进 ENTITY_TABLES / overlay / snapshot() parity
```

### 7.2 分阶段依据（AST → CFG → DFG）

- **AST**：最便宜，材料（CST）现成，语言相关性最低——只是第二遍遍历 + 确定性序列化。
- **CFG**：从 AST 语句序列做确定性结构化（branch/loop/seq），无别名问题；call-site 仅是节点，不需要过程间分析。
- **DFG**：需要 def-use、作用域、参数/返回值流、alias/指针语义，语言相关性陡增（C++ 尤甚）。V1 不做，契约中仅保留 `feature` 枚举扩展位。

### 7.3 scope 语义

```text
scope ::= repo-relative directory（例：src/net，posix 分隔符）
文件集合 = { f ∈ scan_repository(root) : f.relative_path 以 scope 前缀匹配 }
```

- 复用 scanner 的 IGNORED_DIRS / SUPPORTED_EXTENSIONS（scanner.py:8-33），**与基础扫描共享同一文件判定函数，不允许第二套过滤规则**
- `--scope` 可多次给出，取并集；scope 为空 = 整仓（不推荐，仅测试用）
- scope 是**构建期参数**，记入 `analysis_state`；查询 coverage 时由 state 重算

### 7.4 cache key 与 invalidation

```text
cache key = (file_id, feature, feature_version, source_hash)

analysis_state(file_id, path, feature, feature_version, source_hash, generation)

invalidation（对每个 scope 内文件 × 每个 feature）：
  state 存在 且 source_hash 未变 且 feature_version 未变  → reuse（不重解析、不重写）
  否则                                                    → rebuild 该 (file, feature)
  文件删除 / 移出 scope                                    → DELETE 对应 analysis 行（无 tombstone，直接删）
  ANALYSIS_FEATURE_VERSION 常量升级                        → 该 feature 全量失效
```

- `source_hash` 唯一来源 = `scan_repository` 的 `content_hash`（scanner.py:44-45），与 `file_state` 同源，杜绝双哈希漂移
- **采纳 D0 纠正**：`source_hash` 只出现在 state（有效性判据），**不进入** AST/CFG 内部节点 ID

### 7.5 ID 规则（snapshot 内确定性）

```text
analysis_unit_id  = "aunit:" + sha256(repo_id, file_id, feature, owner_symbol_id 或 "file")
analysis_node_id  = "anode:" + sha256(analysis_unit_id, local_path)
  local_path ::= 同一 source snapshot 内的结构路径
  （例：cfg:b2:stmt3；ast:func:body:stmt[1].cond）
analysis_edge_id  = "aedge:" + sha256(analysis_node_id(src), analysis_node_id(dst), edge_kind, ordinal)
```

- 确定性来源：**结构路径**（对同一份源码字节恒定），而非行号、而非文件哈希
- 不承诺跨版本节点身份稳定；跨版本追踪（`symbol_id + structural_path` 匹配）留待未来独立设计
- 后果诚实声明：源码任何字节变化 → 该文件该 feature 整单元重建，ID 全换。这是派生缓存的正确语义，不是缺陷

### 7.6 CFG 边界语义（V1 = intraprocedural only）

```text
CFG 构造单元 = 一个函数定义（Node kind ∈ {Function, Method} 且有 body）
节点：entry / exit / stmt / call-site / branch 分支端点
边：seq（顺序）/ branch（条件出边）/ loop（回边）；exception 边 V1 不做
```

call-site 节点：

```text
raw_name           = 调用表达式目标文本（同 RawReference.raw_name 口径）
alignment          = 与 raw_references 按 (file_id, owner_symbol_id, 行号包含) 只读对齐
resolved_symbol_id = 对齐成功的 RawReference.resolved_symbol_id（不复制解析逻辑）
alignment ∈ {matched, unmatched}；unmatched 不冒充 resolved
target_scope ∈ {internal, external, unknown}
  = 以 resolved_symbol_id 所属文件的 scope 归属判定；unresolved → unknown
不展开目标函数 CFG；跨函数/过程间（ICFG）为后续独立能力
```

### 7.7 coverage / partial-result 语义

```text
coverage = FULL      scope 覆盖整仓
coverage = PARTIAL   scope 为子集
```

- 每次 analysis 查询结果 envelope 携带 `coverage` 与 `scopes`
- 跨 scope 断点显式暴露：call-site `target_scope=external/unknown` 即边界标记，查询方不得将缺目标解释为"无被调者"（与 resolved/ambiguous/unresolved 诚实边界哲学一致）
- 查询 anchor 落在无 analysis 数据的文件时，结果标注 `analysis_status: not_built`，不得静默返回空
- GraphQuery 新增 analysis 查询方法（如 `get_cfg(symbol)`）只读 `analysis_*` 表，不进 overlay 合成路径

### 7.8 推荐数据模型（主 SQLite 新增，D1 可微调列）

```sql
CREATE TABLE IF NOT EXISTS analysis_state (
  file_id TEXT NOT NULL, feature TEXT NOT NULL,
  feature_version INTEGER NOT NULL, source_hash TEXT NOT NULL,
  path TEXT NOT NULL, generation INTEGER NOT NULL,
  PRIMARY KEY(file_id, feature)
);
CREATE TABLE IF NOT EXISTS analysis_units (
  analysis_unit_id TEXT PRIMARY KEY, repo_id TEXT NOT NULL, file_id TEXT NOT NULL,
  feature TEXT NOT NULL, feature_version INTEGER NOT NULL,
  owner_symbol_id TEXT, kind TEXT NOT NULL,   -- 'function_cfg' | 'file_ast'
  node_count INTEGER NOT NULL, edge_count INTEGER NOT NULL,
  payload TEXT NOT NULL                        -- 该单元的确定性 JSON（节点+边）
);
CREATE INDEX IF NOT EXISTS idx_analysis_units_owner ON analysis_units(owner_symbol_id);
CREATE INDEX IF NOT EXISTS idx_analysis_units_file ON analysis_units(file_id, feature);
```

- **单元粒度 = (文件 × feature) 下的函数级 unit**：一个函数一份 payload JSON，随该函数所在文件整体重建——重建粒度与失效粒度一致，避免百万行碎行
- 需要跨函数图遍历的查询（V1 不含 ICFG，暂无此需求）出现时，再按需加展开表；payload-first 是最小可逆形态
- feature_version 独立列，支持单 feature 失效

---

## 8. 零影响证明思路 + parity 排除论证

### 8.1 "关闭 analysis → 基础构建逐字节不变"的证明路径

1. **静态论证（代码路径隔离）**：`full_index / incremental_update / build_graph / ReferenceResolver / replace_snapshot` 的输入输出签名与控制流均不引用任何 `analysis_*` 符号；`snapshot()`（test_integration.py:61-81）与 `ENTITY_TABLES`（overlay.py:19-26）为封闭集合，不含 analysis 表。D1 实现约束：**不改这些函数的任何一行**（新增仅限：SCHEMA 追加建表语句、storage 三个新读写函数、cli 两个分支）。
2. **动态验证（D1 必做回归）**：
   - `fixture: index + update`（无 analysis）vs `index + update + analyze`（有 analysis）：`snapshot()` 四表 + `repositories + graph_generation`（除 metrics 中的 database_size 外）逐字节相等；
   - `analyze` 之后对同一 DB 再跑 `update`：`replace_snapshot` 后 analysis 表仍存在且未被触碰（验证 DELETE 集合零交集）；
   - `DROP` 全部 analysis 表后 `snapshot()` 与未 analyze 的 DB 相等（验证无残留在基础表）。
3. **性质依据**：`replace_snapshot` 的 DELETE 是白名单式固定表清单（storage.py:216-225），不是通配清理；这是隔离成立的根本机制。

### 8.2 analysis 是否可完全排除在 Full/Incremental parity gate 之外？

**可以，且必须**。论证：

- parity gate 的命题是"增量计算路径与全量计算路径对**同一基础事实**收敛"，对象是 nodes/edges/raw_references/shards 四表（test_integration.py:124-142、test_cpp.py:76、test_knowledge.py:121-158）。analysis 不产生基础事实，不参与该命题。
- analysis 的正确性命题不同：**缓存收敛性**——"从任意历史状态出发，`analyze --incremental` 的结果 == 对当前源码直接全量 `analyze` 的结果"。这是另一组测试（invalidation 语义测试），不是现有 parity gate 的扩展。若把 analysis 塞进现有 gate，反而要求全量 index 也构建 analysis，与"按需"目标矛盾。
- 判例支持：`document_state` 同为"索引状态而非图实体"（design-v1.md:96），不参与 parity。

### 8.3 停止条件核查

| 停止条件 | 核查结果 |
| --- | --- |
| 不可避免改变 Node/Edge/RawReference identity | 未命中——identity 全部沿用；analysis 用独立 `anode:/aedge:/aunit:` namespace |
| 不可避免改变 overlay 语义 | 未命中——ENTITY_TABLES 不动 |
| 不可避免改变 Full/Incremental parity 语义 | 未命中——snapshot() 四表不动；analysis 走独立缓存收敛命题 |
| 现有写入路径被迫修改 | 未命中——replace_snapshot DELETE 白名单天然豁免新表 |

---

## 9. 风险与 D1 注意事项（非阻塞）

1. **payload 体积**：RocksDB 量级（622.8k LOC）下若对超大文件做 AST，payload JSON 可达 MB 级。D1 应在 benchmark 中实测 per-unit 体积，必要时对超大函数设 unit 上限并标注 `truncated`。
2. **C++ 宏**：CST 层面 `#if` 分支合并，CFG 反映的是预处理后的单一视图——契约中明示，不冒充语义保真。
3. **`changed_ranges` 闲置能力**：`parse_bytes(old_tree)` 已实现（tree_sitter.py:28），D1 的 `analyze --incremental` 可选启用语法粒度复用；但 V1 契约以文件粒度为准（与 file_state 对齐），语法粒度作为后续优化，不进契约。
4. **scope 与 shard 不对齐的可能**：scope 是目录前缀，shard 可能被 structural 策略切分（shard.py:63-98）。coverage 语义以 scope 文件集合为准，不以 shard 为准。

---

## 10. 最终结论

**READY_FOR_D1**——按 §7 契约实施。第一步 D1 建议：实现 `analysis/` 骨架 + `ast` feature + `analyze` CLI + §8.1 动态回归三件套，bench 先用 fixture 与 aria2 子目录，不直接上 RocksDB 全量。

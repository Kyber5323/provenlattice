# PL-DA-D5 — Optional Analysis Orchestrator + CLI V1（实现报告）

状态：**READY_FOR_D6**
分支：`feature/analysis-layer`
日期：2026-09-22
前置：PL-DA-D0/D1/D2/D3/D4（均闭合）

---

## 1. 新增 / 修改文件

| 文件 | 类型 | 说明 |
| --- | --- | --- |
| `src/provenlattice/orchestrator.py` | **新增** | 统一入口 `analyze_scope(repo, scope, features, database)`：pre-validation → dependency resolution → 执行 → per-file 结果合并 |
| `src/provenlattice/cli.py` | **修改（唯一既有源码文件）** | 注册 `analyze` 子命令（`--scope`/`--features` required）+ 分发 + `AnalysisRequestError` → 结构化 failed 输出。index/update/knowledge 及全部查询命令零改动 |
| `src/provenlattice/analysis_ast.py` | **修改（仅重构）** | `_normalize_scope` → 公开 `normalize_scope`；新增公开 `scoped_sources(root, scope_path)`（parts 级 scope 过滤唯一实现） |
| `src/provenlattice/analysis_cfg.py` | **修改（仅重构）** | 改用公开 helper，行为不变（D4 全部测试回归通过） |
| `tests/test_analysis_cli.py` | **新增** | G1–G12 十三个测试 |

基础图源码（graph/incremental/storage/resolver/shard/query/overlay/knowledge/identity/scanner/parser）修改数 = 0。禁止事项全部遵守：无 DFG、无 GraphView 混合查询、无 DSL、无 server、无 changed_ranges、无自动后台分析；**index/update 不创建任何 analysis 表/行**（D1 lazy 契约保持，G12/全量回归证明）。

## 2. Orchestration API

```python
analyze_scope(repo, scope, features: str | list[str], database=None) -> dict
```

执行顺序（全部 mutation 前置校验）：

```text
1. _parse_features      → unknown_feature / invalid_features（G8，先于一切）
2. normalize_scope      → invalid_scope（D2 语义原样复用，G9，先于一切）
3. base graph gate      → base_graph_required（storage.repository() 为空即失败，
                           不隐式 build，G10）
4. dependency closure   → plan = requested ∪ deps（cfg → ast）
5. 执行：analyze_ast_scope → analyze_cfg_scope（D2/D4 原函数，零语义修改）
6. 合并：categories → no_functions_built 重分类 → per_file / features / files / status
```

`AnalysisRequestError(code, detail)`：`unknown_feature | invalid_features | invalid_scope | base_graph_required`。CLI 捕获后返回结构化 failed 结果，不产生 traceback。

## 3. Feature Dependency Graph

```text
cfg ──requires──> ast        （FEATURE_DEPENDENCIES 唯一边，V1）
```

- 解析发生在 orchestrator：请求 `cfg` 时显式先执行 `analyze_ast_scope`（AST fresh 则整体 reuse），**`analysis_cfg.py` 内部 fail-closed 语义一字未动**——它看到的 AST 永远已由 orchestrator 保证 fresh；若 AST materialization 对某文件失败（如 payload 超限），CFG 侧仍会以 `ast_missing` fail-closed，诚实级联。
- 结果暴露：`dependencies: {ast: {reason: "required_by_cfg", action: materialized|reused|failed, files: {...}}}`，仅当依赖未被显式请求时出现（G3/G4/G5）。

## 4. Scope 复用证据

- CLI 不含任何路径判断；orchestrator 调用 `normalize_scope`（D2 实现：绝对路径/`..`/空/`.`/空白组件/盘符拒绝）+ 目录存在性检查。
- 文件集合由 `scoped_sources(root, scope_path)` 统一产出（parts 级包含，防 `srcfoo/` 前缀陷阱），ast/cfg sub-analyzer 与 orchestrator 同一实现（本次重构去重，行为不变）。
- 非源码文件（.md/.js）经 `_inventory` 计入 `files.seen` 与 `skipped`（复用 scanner 的 `IGNORED_DIRS`/`SUPPORTED_EXTENSIONS` 常量，无第二套过滤规则）。

## 5. 结果 Schema（机器可读 = 现有 `--json` 通道，零新依赖）

实测 `provenlattice analyze <repo> --scope src/net --features cfg --json`：

```json
{
  "status": "complete",
  "scope": "src/net",
  "requested_features": ["cfg"],
  "executed_features": ["ast", "cfg"],
  "dependencies": {
    "ast": {"reason": "required_by_cfg", "action": "materialized",
            "files": {"materialized": 1, "reused": 0, "skipped": 1}}
  },
  "files": {"seen": 2, "analyzed": 1, "reused": 0, "skipped": 1},
  "features": {
    "ast": {"materialized": ["src/net/a.py"], "reused": [],
            "skipped": {"unsupported_language": ["src/net/b.c"]}, "failures": []},
    "cfg": {"materialized": ["src/net/a.py"], "reused": [],
            "skipped": {"unsupported_language": ["src/net/b.c"]}, "failures": []}
  },
  "per_file": [
    {"path": "src/net/a.py", "language": "python", "bucket": "analyzed",
     "ast_status": "materialized", "cfg_status": "materialized", "reasons": []},
    {"path": "src/net/b.c", "language": "c", "bucket": "skipped",
     "ast_status": "skipped", "cfg_status": "skipped",
     "reasons": ["ast:unsupported_language", "cfg:unsupported_language"]}
  ]
}
```

human 模式沿用现有 pprint。**status 语义**（无自行发明成功率）：

```text
complete  所有 eligible Python 文件在全部 requested features 上 materialized/reused
partial   至少一个 file-feature 完成，但存在明确 skipped/failure（reasons 全保留）
failed    请求非法（unknown_feature/invalid_scope/base_graph_required）或零可用结果
```

非 Python 文件的 `unsupported_language` 不使状态降级（非 eligible），但逐文件 reason 恒记录。函数级 fail-closed（`no_functions_built` / `functions_skipped_*`）经 CFG state metadata 读取后进入 per_file reasons——文件全部函数被跳过时该文件重分类为 `skipped`（对应任务书"1 个 unsupported construct 文件"不得伪装全成功），部分函数被跳过时状态仍 materialized 但 reason 记数保留。

## 6. G1–G12 测试结果（tests/test_analysis_cli.py，13/13 OK）

| 测试 | 结果 |
| --- | --- |
| G1 test_g1_ast_only | 仅 `ast:python` 行；cfg 零行；per_file cfg_status=not_requested ✓ |
| G2 test_g2_ast_and_cfg | 双层构建；files={seen:5, analyzed:2, skipped:3}；state 4 行 ✓ |
| G3 test_g3_cfg_only_materializes_ast_dependency | requested=[cfg]，dependencies.ast.action=materialized，CFG 全部构建（无 skipped_ast_stale）✓ |
| G4 test_g4_cfg_only_reuses_fresh_ast | dependencies.ast.action=reused；AST updated_at 不变；CFG 构建 ✓ |
| G5 test_g5_incremental_change_rebuilds_both | 更新后 orchestrator 重建 AST 依赖 + CFG，两者 read_current fresh ✓ |
| G6 test_g6_scope_isolation | per_file 全部 src/ 下；tests/c.py 零 analysis 行 ✓ |
| G7 test_g7_unsupported_language_accounted | a.py analyzed；b.c/README.md/x.js skipped（reasons 含 ast:unsupported_language）；命令整体 complete 不失败 ✓ |
| G8 test_g8_unknown_feature_fail_closed | `ast,dfg`/`ast,foo` → unknown_feature；CLI failed；**零 analysis 行**（validation 先于 mutation）✓ |
| G9 test_g9_invalid_scope_fail_closed | 4 类非法 scope → invalid_scope；CLI failed；基础 snapshot 不变、零 analysis 行 ✓ |
| G10 test_g10_missing_base_graph | 未 index repo → base_graph_required；CLI failed；零 analysis 行（无隐式 build）✓ |
| G11 test_g11_repeat_execution_cache | 二次执行：reused=2/analyzed=0；两 feature updated_at 逐字节不变 ✓ |
| G12 test_g12_base_graph_zero_impact | 完整 orchestrator 运行前后基础四表 snapshot 逐字段相等 ✓ |
| 附加 test_cli_analyze_command | CLI 全链路（parse→run→结构化结果）✓ |

**全量 ProvenLattice：112/112 OK**（7.8s，连续两次运行确认）= 原有 56 + D1 11 + D2 11 + D3 9 + D4 12 + D5 13。

## 7. 生命周期与停止条件核查

```text
index / update   → Base Graph（行为与 D0 完全一致，默认不做任何 analysis）
analyze          → Derived Analysis（显式 scope + features，Base Graph 先行校验）
```

| 停止条件 | 结果 |
| --- | --- |
| index/update 被迫默认创建/更新 analysis | 未命中——cli.py 仅新增 analyze 分支，index/update 路径零改动 |
| CLI 无法在 mutation 前完成 feature/scope 校验 | 未命中——_parse_features/normalize_scope/base gate 全部先于任何 sub-analyzer 调用（G8/G9/G10 证明零副作用） |
| cfg 依赖 AST 需要修改 D4 fail-closed 语义 | 未命中——analysis_cfg.py 仅重构 import，D4 12/12 回归通过 |

## 8. 结论

**READY_FOR_D6** — 项目最初设想的核心使用方式正式成立：`provenlattice analyze <repo> --scope src/net --features ast,cfg`。基础图永远轻量存在；只有用户点名目录与能力时才建立 AST/CFG；不执行 analyze 时 ProvenLattice 与既有版本行为完全一致。D6 候选方向：partial 状态下的重试/修复工作流、analysis 查询 API（把 CFG/AST 读侧暴露给 Agent）、或更大规模仓库的 payload/性能治理。

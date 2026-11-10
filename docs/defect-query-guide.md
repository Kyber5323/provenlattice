# Defect Query Guide

> 适用版本:V1.4(2026-10-13)
> 适用语言:C/C++(`.c` `.h` `.cc` `.cpp` `.cxx` `.hpp` `.hh` `.hxx` `.inl`)
> 分析范围:`local-only, diagnostic`

## 1. 先决条件

缺陷查询需要上游分析链已物化且新鲜:

```bash
# 1. 构建基础图谱(L1)
provenlattice index /path/to/repo --json

# 2. 物化上游链(events → sparse_cfg → sparse_dfg)
provenlattice analyze /path/to/repo --scope src --features defect --json
```

上游链缺失或 stale 时查询返回 fail-closed(stale/not_built),不产生候选。

## 2. 运行缺陷查询

```bash
# 单类查询
provenlattice defect /path/to/repo --type double_free --json
provenlattice defect /path/to/repo --type use_after_free --json
provenlattice defect /path/to/repo --type null_flow --json

# 全部九类
provenlattice defect /path/to/repo --type all --json

# 带预算限制
provenlattice defect /path/to/repo --type all \
  --max-candidates 50 --max-paths 16 --max-hops 32 --json
```

### 支持的查询类型

| 查询名 | Matrix Key | 说明 |
|---|---|---|
| double_free | 4.2 | 同一对象连续两次释放 |
| use_after_free | 4.3 | 释放后使用悬垂指针 |
| null_flow | 3.1 | NULL 值传播到解引用 |
| resource_lifetime | 4.1 | 资源分配后遗漏释放 |
| lock_order | — | 相反锁顺序 |
| race_condition | — | 共享变量无锁读写 |
| taint_path | taint | source→sink 传播 |
| error_handling | 9.1 | 返回值未检查 |
| mismatched_free | mismatch | 分配器与释放函数不配对（CWE-590/762/763） |

## 3. 结果 JSON 结构

```json
{
  "query": "double_free",
  "defect_type": "double_free",
  "status": "available",
  "candidates": [
    {
      "defect_key": "4.2",
      "query": "double_free",
      "subject": {
        "kind": "release",
        "name": "p",
        "identity": "symbol:...",
        "storage": "local",
        "method": "res01",
        "owner_symbol_id": "symbol:..."
      },
      "anchor": "event:...",
      "resolution_status": "resolved",
      "confidence": 0.5,
      "facts": [
        {"event": "RELEASE", "method": "res01", "matched_name": "free", "source": "t.c:2-2"}
      ],
      "uncertain_facts": [],
      "paths": [{"points": [...], "types": [...], "edges": [...]}],
      "source_evidence": ["res01.c:2-2"],
      "missing_evidence": [
        "the repository's release convention: ...",
        "whether two names really address one object: ...",
        "the noreturn conventions the graph does not hold: ..."
      ],
      "candidate_id": "E-DEFECT-...",
      "metadata": {"flow_class": "...", "via": [...], "control_reachable": "true"}
    }
  ],
  "coverage": {...},
  "truncated": false,
  "grade": "local-only, diagnostic",
  "level": "level0+structural+access+dfg",
  "provenance": {
    "queries": [...],
    "resolved_is_not_verified": "resolution_status is an evidence-resolution state; confidence is the PR heuristic score, not a calibrated probability"
  }
}
```

## 4. 字段含义

| 字段 | 含义 |
|---|---|
| `resolution_status` | 证据解析状态:resolved(声明身份一致)/ ambiguous(部分不确定)/ unresolved(无法归因)。**不是 Verified Finding** |
| `confidence` | Heuristic score(如 resolved=0.5),不是校准的概率 |
| `facts` | 图已证明的事件事实(事件类型、方法、位置、匹配名称) |
| `uncertain_facts` | 图无法确定的事实(带 `decisive` 标志:decisive 的不确定会降低候选状态) |
| `paths` | 事件间的语义边路径(点序列 + 类型 + 边 ID) |
| `source_evidence` | `path:startLine-endLine` 格式的源码位置引用 |
| `missing_evidence` | 更强分析需要补充的证据描述(如释放约定、别名精度、noreturn) |
| `control_reachable` | 三态:`true`(图中有路径)/ `false`(连接端点无路径)/ `unknown`(端点为 edgeless 访问点或 partial parse) |
| `grade` | `local-only, diagnostic` — 方法内分析,精度有限 |
| `truncated` | 候选数达到 max_candidates 被截断 |

## 5. 状态说明

| 状态 | 含义 | 处理 |
|---|---|---|
| `available` | 查询成功,返回候选(可能为 0) | 正常消费 |
| `stale` | 上游分析过期(源码变更或依赖版本变化) | 重新运行 `analyze --features defect` |
| `not_built` | 上游分析未物化 | 运行 `analyze --features defect` |
| `failed` | 查询错误(未知类型、基础图缺失等) | 检查 `error.code` 和 `error.detail` |

## 6. 已知误报和限制

| 类型 | 查询 | 描述 |
|---|---|---|
| FP | null_flow | `if(p){*p=1}` — null 臂仍可达隐式 else |
| FN | use_after_free | 别名 q=p 的 deref 不与 p 的 free 配对(精度缺口) |
| FN | CONSUMES | 仅直接传递形状(`f(malloc(n))`),赋值形状走 DATA_FLOW_TO |
| FN | sparse CFG | goto 目标不建模,短路坍缩为单 BRANCH |
| 边界 | 全部 | 方法内分析,无跨函数边;无 SSA/points-to |
| 边界 | error_handling | 精度受 API contract 知识限制 |
| 边界 | taint_path | 仅 4 source/3 sink,无 sanitizer/跨函数 |

## 7. 故障排查

| 问题 | 原因 | 解决 |
|---|---|---|
| `stale` + `upstream_analysis_stale` | 源码变更后未重新分析 | 运行 `analyze --features defect` |
| `stale` + `files_not_covered` | 新文件未物化上游 | 同上 |
| `stale` + `dependency_changed` | 上游 analyzer 版本升级 | 同上(自动重建) |
| `not_built` | 从未运行过 `analyze --features defect` | 同上 |
| `failed` + `base_graph_required` | 未运行 `index` | 运行 `provenlattice index` |
| `failed` + `unknown_defect_query` | 查询名拼写错误 | 检查支持的查询列表 |
| 候选数达到 max_candidates | 预算截断,实际 population 可能更大 | 增大 `--max-candidates` 或查看 coverage |
| `control_reachable: unknown` | 端点为 edgeless 访问点或 partial parse | 不是确定性不可达;需人工复核 |

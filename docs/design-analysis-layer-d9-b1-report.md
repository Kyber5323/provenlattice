# PL-DA-D9-B1 — `analysis_v1` Single-Target Evidence Wiring

状态：**D9-B1 = PASS / READY_FOR_D9_B2**  
分支：`feature/analysis-layer`  
日期：2026-09-23

## 1. 已接入 surface

本切片只接入 native `find-related-documents`
（`GraphQuery.find_related_documents` / CLI `find-related-documents`）。
它以一个已解析的 code symbol 为 anchor，因而天然满足单目标语义；调用链为
CLI → `find_related_documents` → `_bundle_result`，比 module/document-to-code
multi-target surfaces 更短，且不需要 target selector 或 fan-out。

CLI 新增显式 `--analysis ast,cfg` 参数。默认调用没有该请求时，不会增加
`analysis` 键、不调用 analysis query，也不会触发构建或写入。

## 2. identity 与 payload contract

`find_related_documents` 已先通过 `_resolve(symbol)` 得到基础图的精确 anchor。
仅该 anchor 的 `id` 和 `qualified_name` 进入 `analysis_v1.targets[0]`；不会从
document relation、evidence ID 或 related entities 猜测第二个 symbol。

显式请求时，顶层 payload 为：

```json
{
  "analysis": {
    "version": "analysis-v1",
    "requested_features": ["ast", "cfg"],
    "targets": [{
      "symbol_id": "...",
      "qualified_name": "...",
      "features": {"ast": {}, "cfg": {}}
    }]
  }
}
```

`features` 由新增的最小共享 `_analysis_v1` helper 组装，它只复用现有
`_analysis_sections` → `query_analysis` 路径。因此 AST/CFG 算法、D6 六态、
size guard 和 fail-closed 行为均未复制或改变。请求 feature 排序确定化；无
请求时整个顶层 `analysis` 字段缺席，原有 bundle 字段不变。

## 3. Tests and regression

新增 `tests/test_analysis_tool.py` 中四个 D9-B1 tests：

1. 默认调用在分析层不存在/已构建两种情况下均无 `analysis`，且去除
   `query_time_ms` 后逐键相等。
2. 显式 CFG 返回 `analysis-v1`、唯一精确 target，并与 D7
   `explain-symbol --analysis cfg` 的 authoritative section 完全一致。
3. 显式 AST 返回同一契约；`not_built` 原样透传，未伪装为成功。
4. CLI `find-related-documents --analysis cfg` 端到端返回可用 CFG。

执行：

```text
PYTHONPATH=src python -m unittest \
  tests.test_analysis_tool tests.test_analysis_query \
  tests.test_analysis_contract tests.test_analysis_cli tests.test_evidence
```

结果：**56 tests PASS**（6.206 s）。该组覆盖 D9-B1 focused tests、D6/D7
analysis gates 与 D9-A 的基础 evidence gate。D8 没有独立 unittest module；其
`d8_runner.py` 与 `d8_reattribute.py` 已通过 `py_compile` 语法回归。

## 4. Production diff scope

- `src/provenlattice/cli.py`：仅 `find-related-documents` 的 opt-in parameter
  与 dispatch。
- `src/provenlattice/query.py`：仅 `analysis_v1` shared assembly helper 与
  `find_related_documents(..., analysis=None)` 的显式路径。
- `tests/test_analysis_tool.py`：D9-B1 focused coverage。

未接第二个 evidence surface；未改 `explain-symbol`、SQI-V1.2、bundle/evidence
framework、Agent prompt/discovery、L3/DFG 或 L4/taint。D0–D8 工作树未整理或
提交。

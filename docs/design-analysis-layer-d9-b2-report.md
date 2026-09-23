# PL-DA-D9-B2 — Multi-target Evidence Explicit Selection

状态：**D9-B2 = PASS / READY_FOR_D9_B3**  
分支：`feature/analysis-layer`  
日期：2026-09-23

## 1. Chosen surface and selector

本切片只接入 native `find-related-code`
（`GraphQuery.find_related_code` / CLI `find-related-code`）。它从一个
document/section anchor 返回多个 resolved code entities，是 D9-A matrix 中最小的
真实 multi-target surface。

新增 opt-in 参数：

```text
--analysis ast,cfg --analysis-symbol-id <stable symbol_id>
```

selector 使用基础图的稳定 `symbol_id`。生成基础 bundle 后，实施从该次实际返回的
`bundle.related_entities` 筛出 `SYMBOL_KINDS`；selector 必须精确命中这一集合。
不做 qualified-name/fuzzy lookup，不重查更宽范围，也不默认选择第一项。

## 2. Frozen call semantics

| Condition | Result |
| --- | --- |
| No `--analysis` | 不要求 selector；无 `analysis` 键，基础 bundle 不变。 |
| Analysis + returned `symbol_id` | 仅该 symbol 进入一个 `analysis_v1.targets[0]`；每个 requested feature 只经既有 `query_analysis` 读取该 target。 |
| Analysis + omitted selector | `analysis.error.code = ANALYSIS_SYMBOL_ID_REQUIRED`；targets 为空。 |
| Analysis + non-returned/nonexistent selector | `ANALYSIS_SYMBOL_ID_NOT_RETURNED`；targets 为空。 |
| Analysis + resolved document but zero code symbols | `ANALYSIS_NO_SELECTABLE_SYMBOL`；targets 为空。 |

错误对象是确定性、结构化的 `analysis-v1` payload（保留 version、requested_features
和空 targets），基础 evidence bundle 仍按原规则返回。所有错误分支 fail closed：
不存在自动 target 选择、隐式 fan-out、隐式 `analyze` 或写入。

## 3. No-fan-out proof and authoritative reuse

新增 fixture document 明确关联 `src.main.dispatch`、`src.main.retry`、
`src.main.commit` 三个 code symbols。测试选择其中一个并 monkeypatch
`provenlattice.query.query_analysis`：单 feature CFG 请求**恰好调用一次**，且传入的
`symbol_id` 就是 selected target；响应仅有一个 target，另外两个 id 不在 analysis
结果中。

AST 和 CFG 都继续经 B1 的 `_analysis_v1` → `_analysis_sections` → D6
`query_analysis` 路径。CFG 与 `explain-symbol --analysis cfg` 的 authoritative
section 逐项一致；AST 也与 `explain-symbol --analysis ast` 的 authoritative section
逐项一致。未复制 AST/CFG 实现。

## 4. Regression

新增四个 B2 focused tests，覆盖：

1. 三 target fixture 的默认路径，在分析层已构建/未构建时均无 `analysis` 且基础
   bundle 不变。
2. 有效 selector 的唯一 target、exact identity、一次 CFG query 和 no-fan-out。
3. selector 缺失、非 returned/nonexistent selector、零 selectable symbol 的
   fail-closed response。
4. CLI AST request 与 authoritative AST section 的一致性。

执行：

```text
PYTHONPATH=src python -m unittest \
  tests.test_analysis_tool tests.test_analysis_query \
  tests.test_analysis_contract tests.test_analysis_cli tests.test_evidence
```

结果：**60 tests PASS**（6.229 s）。覆盖 B2 focused、B1、D6/D7、基础 evidence
gate。D8 无独立 unittest module；`experiments/analysis_layer/d8/d8_runner.py` 与
`d8_reattribute.py` 已通过 `py_compile` 回归。

## 5. Production diff scope

- `src/provenlattice/cli.py`：仅 `find-related-code` 的两个 opt-in parameters
  与 dispatch。
- `src/provenlattice/query.py`：仅 multi-target selector validation、fail-closed
  `analysis-v1` error helper 和 selected-target assembly。
- `tests/test_analysis_tool.py`：B2 fixture/coverage。

未改 `find-related-documents`、`explain-symbol`、其他 evidence surface、SQI-V1.2、
Agent discovery/adoption、L3/DFG、L4/taint 或 evidence/bundle framework；未整理或
提交 D0–D8 工作树。

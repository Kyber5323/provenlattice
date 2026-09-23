# PL-DA-D9-B3 — Optional Analysis Discovery Surface

状态：**D9-B3 = PASS / READY_FOR_D9_C_AGENT_ADOPTION**  
分支：`feature/analysis-layer`  
日期：2026-09-23

## 1. Capability discovery source

Agent 从 native CLI 自己的 `--help` 发现 optional analysis capability。新增的
`cli.py::_add_analysis_options` 与 `ANALYSIS_FEATURES_HELP` 是三个已接入 surface
唯一的共同描述来源；没有第二份 command-specific 长文案或外部 prompt。

共同的简洁说明包括：

- `--analysis ast,cfg` 是 optional；不请求时保持 base evidence 不变。
- `ast` 用于函数结构与调用位置。
- `cfg` 用于分支、循环、fallthrough 与 early return 等控制流结构。

三个 surface 的可发现信息为：

| Command | Discovery contract |
| --- | --- |
| `explain-symbol --help` | 显示 `--analysis FEATURES`、AST/CFG 的通用用途，以及 omit 保持 base evidence。其 anchor 已是精确 symbol。 |
| `find-related-documents --help` | 同样显示 `--analysis FEATURES`；其 symbol anchor 是唯一 analysis target，响应使用 `analysis_v1`。 |
| `find-related-code --help` | 除共同说明外显示 `--analysis-symbol-id SYMBOL_ID`：analysis 必须选择本次 returned bundle 内的 exact symbol_id；只分析该 symbol，绝不自动分析所有结果。 |

这只是 capability description，不包含“某类问题必须调用 CFG”的策略，也没有
D8 fixture、case id、函数名或任务特判。

## 2. Behavior boundary

没有修改 `analysis_v1`、selector 语义、AST/CFG、bundle/evidence payload 或任何
execution dispatch。`--help` 是唯一生产行为变化。B1/B2 的默认执行回归继续证明：
无 analysis request 时结果没有 `analysis` 字段，且 optional layer 是否已经构建
不会改变基础 evidence response。

未运行 D8 real-Agent A/B；adoption 实验必须在这版 discovery contract 冻结后由
D9-C 单独执行，才能将 adoption 变化归因于统一描述。

## 3. Tests

新增 B3 focused tests 覆盖：

1. 三个 command 的 help 均显示 `--analysis`, `ast`, `cfg`, optional 语义和
   base-unchanged 说明。
2. `find-related-code` 的 help 显示 exact returned `symbol_id` selector 与
   no-auto-analysis 约束。
3. 三份 help 均不含 D8/case/fixture-specific 字样。
4. B1/B2 default-path、parameter parsing 和 fail-closed tests 继续包含在同一
   regression group。

执行：

```text
PYTHONPATH=src python -m unittest \
  tests.test_analysis_tool tests.test_analysis_query \
  tests.test_analysis_contract tests.test_analysis_cli tests.test_evidence
```

结果：**62 tests PASS**（7.213 s）。包含 B3 focused、B1/B2、D6/D7 和 D9-A
frozen regression gate。D8 没有独立 unittest；`d8_runner.py` 和
`d8_reattribute.py` 已通过 `py_compile` 语法 gate。

## 4. Production diff

- `src/provenlattice/cli.py`：共享 help constant/helper，并应用到恰好三个已接入
  analysis 的 native commands。
- `tests/test_analysis_tool.py`：help discovery 与 case-specific-text guard。

未接新 surface；未改 SQI-V1.2、system prompt、Agent 全局行为、D8 fixture、
L3/DFG、L4/taint 或 D0–D8 工作树。

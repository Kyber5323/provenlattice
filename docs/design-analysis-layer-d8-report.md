# PL-DA-D8 — Real Agent Deep-Analysis Utility Observation（实验报告）

状态：**READY_FOR_D9_PRODUCTIZATION**（附 ADOPTION_GAP 子结论，见 §6）
分支：`feature/analysis-layer`
日期：2026-09-22
前置：PL-DA-D0…D7（均闭合）
性质：观察/证据任务。未修改任何 analysis 实现、Agent routing、tool schema 或 prompt 策略；新增仅 fixture / runner / oracle / report。

---

## 1. 实验设置

- **Fixture**：`experiments/analysis_layer/d8/fixture-src/`——6 模块部署管道仓库，每模块一个目标函数，各含一个基础 CALLS 图无法表达的控制流事实（互斥 / merge fallthrough / return termination / break vs return / 条件+无条件 / 顺序）。
- **两臂**：同一 fixture 的两个副本、同一 commit fingerprint、同一 prompt/system prompt/allowedTools/model。
  - B 臂：`index` only（base graph）
  - D 臂：`index` + `analyze --scope pipeline --features ast,cfg`
- **Agent**：真实 claude CLI（headless stream-json），backend `deepseek-v4-flash`（12/12 runs 一致，无 drift）。工具面两臂完全一致：`Read, Grep, Glob, Bash(python -m provenlattice explain-symbol *)`——analysis 能力仅通过 CLI 自身 `--help` 可发现，任何 prompt 均未提及 AST/CFG/analysis。
- **批次**：两批各 6 case × 2 臂 = 12 runs。
  - 批 1（D8-20260922T090600Z）：遥测缺陷——adapter 只持久化 evidence-bundle 子对象，analysis envelope 未落盘；Agent 行为与答案完整保留。
  - 批 2（D8-20260922T091934Z）：修复 adapter（加性 `raw["envelope"]`，不影响任何既有字段）+ 影子重放归因（见 §4），遥测完整。
- **归因纪律**：先重放后归类；infra 失败（timeout/格式）单独排除，不混入能力结论。

## 2. 结果总表（批 2 为主判定，批 1 为行为复现参照）

| Case | B success | D success | AST/CFG used | Incremental fact | Verdict |
| ---- | --------- | --------- | ------------ | ---------------- | ------- |
| A1   | yes | yes | **yes**（4 次调用，含 1 次 bogus feature 探测） | yes（condition true/false 边互斥、两分支间无边） | **UTILITY_OBSERVED** |
| A2   | yes | yes | no（两批均零采用） | n/a | ADOPTION_GAP |
| B1   | yes | yes | **yes**（3 次调用，10 节点 CFG） | yes（finalize 唯一前驱=loop_exit merge；return 节点直达 exit 绕过 finalize） | **UTILITY_OBSERVED** |
| B2   | yes | yes | no（两批均零采用） | n/a | ADOPTION_GAP |
| C1   | yes | yes | **yes**（3 次调用，6 节点 CFG） | yes（persist 仅经 condition true 边可达；notify 是 branch_merge 后继、两分支必经） | **UTILITY_OBSERVED** |
| C2   | yes | yes | **yes**（2 次调用，7 节点 CFG） | yes（register 条件内、announce/cleanup 无条件且顺序固定） | **UTILITY_OBSERVED** |

```text
UTILITY_OBSERVED   = 4
ADOPTION_GAP       = 2
NO_INCREMENTAL_VALUE = 0
CAPABILITY_GAP     = 0
```

批 1 与批 2 的采用模式**按 case 稳定分化**：A1/B1/C1/C2 两批均发生真实 CFG 消费；A2/B2 两批均零采用——不是随机噪声，而是与任务类型相关的稳定行为。

## 3. 归因证据样例（Agent 最终答案原文摘录）

- **A1-D**："互斥。……CFG 证据进一步确认二者分别经 condition 节点的 `true`/`false` 边到达、之间无边，实际每次调用恰好执行其中一个、不可能都执行。"
- **B1-D**："CFG 中 finalize 调用点（`expression_statement[1]/call[0]`）唯一前驱是 `loop_exit` 汇合节点；`return False` 节点有直达 exit 的 return 边绕过 finalize。"
- **C1-D**："persist … reachable only via the condition's `true` edge … notify is the successor of the CFG `branch_merge` node that both edges lead into."
- **C2-D**：答案含 cnode/true/false/structural_path/resolved_symbol_id 引用及 register→announce→cleanup 顺序结论。

这些结构（true/false 边、merge 后继、cnode id）在基础 CALLS 图与源码字面中均不存在，只能来自 derived CFG。

## 4. 影子重放归因方法（诚实性说明）

Agent 习惯用 `| head -c N` 截断 Bash 输出，导致完整 JSON envelope 被切碎、harness 无法从 tool_result 直接解析 analysis status。由于 `analyze` 后 derived store 静态且查询只读（D6 契约），runner 对每条已记录的 `explain-symbol --analysis` 调用按 (symbol → 基础图精确 id → D6 `query_analysis`) 链路**确定性重放**，得到该调用当时必然返回的 status（重放口径与 D7 工具链一致：get_definition → anchor id → query_analysis；D6 对批 1 中一次 `--analysis bogus_feature` 返回 `unsupported`，顺带验证了未知 feature 的 fail-closed 交互）。命令原文与截断行为均如实保留，未伪造任何遥测。

批 1 的 instrumentation 缺陷（envelope 未持久化）已在批 2 修复并重跑；两批 Agent 行为不一致处（B2-D 零采用、C2-D 出现采用）以各自原始记录如实呈现，未合并抹平。

## 5. 关键观察

1. **CFG 提供了基础图不可表达的增量事实，且 Agent 确实消费**：4/6 case 中 Agent 用 CFG 的 true/false 边、merge 后继、return 直达边直接回答了"分支互斥 / 谁绕过 finalize / 谁无条件"这类问题，并把这些结构写进答案证据。
2. **采用是按任务类型分化的**：互斥（A1）、early-return（B1）、条件依赖（C1）、条件+顺序（C2）稳定触发采用；merge-fallthrough（A2）与 break-vs-return（B2）两批均零采用——Agent 认为读码即可，且确实答对。
3. **B 臂的 fail-closed 交互被观察到**：B1-B 臂 Agent 探索 `--analysis` 后得到 not_built，转回读码完成任务——optional 缺失不破坏任务（与 D6 契约一致）。
4. **B2-D 两臂全对但零采用**说明：这些小 fixture 上"读码推演"足够便宜——CFG 的价值在更大/更复杂函数上才会显著；本轮不据此下效率结论。
5. **零 CAPABILITY_GAP**：所有被消费的 CFG 语义（分支、merge、return、break、顺序）均已足够回答本轮问题——**没有出现指向 DFG 的需求信号**。

## 6. 结论

```text
UTILITY_OBSERVED   = 4   （A1, B1, C1, C2）
ADOPTION_GAP       = 2   （A2, B2——两批一致零采用）
NO_INCREMENTAL_VALUE = 0
CAPABILITY_GAP     = 0
```

**READY_FOR_D9_PRODUCTIZATION** —— 真实 utility 已在 4/6 case 上被观察到且跨批可复现，Agent 消费链路（explain-symbol --analysis）可用、诚实、不破坏基础查询；扩 Agent 消费覆盖面（把 `--analysis` 带入其余 evidence 命令、bundle 内的结构化 analysis evidence）具备依据。同时 A2/B2 的稳定零采用是明确的次级信号：D9 应并行研究轻量 tool guidance（如 bundle 内对 optional capability 的一行提示），并优先观察**更大/更复杂函数**上 CFG 的效率价值——本轮 fixture 偏小，读码推演成本过低。

## 7. 运行清单与产物

- 批 2（判定批）：`experiments/analysis_layer/d8/results/D8-20260922T091934Z/`（runs/*/run.json + agent-output.txt + events.ndjson + batch-summary.json）
- 批 1（参照批）：`results/D8-20260922T090600Z/`
- runner / 重放归因 / 任务定义：`d8_runner.py` / `d8_reattribute.py` / `d8_tasks.json`
- 全量回归：分析层实现零修改，D0–D7 测试未重跑（本轮未触碰任何实现文件；`git status` 仅新增 D8 实验文件与 harness 加性字段 `agent_adapter.py`）。

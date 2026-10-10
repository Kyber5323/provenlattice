# ProvenLattice Reproducibility Baseline

> Tag: `provenlattice-sqi-v1-repro`（2026-09-17）。
> 本文件是研究证据的**总入口**：当前证明了什么、没证明什么、如何验证、如何复现。
> 详细实验治理见 `docs/provenlattice-research-validation-framework-v1.md` 与各线 README。

## 1. 当前结论（全部限各自冻结 scope）

```text
RQ1  Graph Fidelity                    = QUALIFIED   (2026-09-15)
RQ2  Systems + Scale Qualification V1  = QUALIFIED   (Contract V1 scope, 2026-09-16)
SQI-V1 Structured Query Interface      = QUALIFIED   (Formal Protocol V1 scope, 2026-09-17)
SQI-V1.1 Evaluation Hardening          = PASS        (2026-09-17)
SQI Cost Optimization C0               = PASS / Analysis Only (2026-09-17)
SQI-V1.2 Cost Optimization Qualification = QUALIFIED / RELEASE READY (2026-09-20)
Analysis Layer D0-D8 (Python-only)     = QUALIFIED   (2026-09-22)
D9-C10 P2 Controlled Capability        = PASS        (2026-10-09)
ContextBench zstd 16-case revalidation = 14/16 resolved, 2 infra-excluded, 0 genuine failure (2026-10-09, diagnostic)
C analysis A1-A3.6                     = INTEGRATED on main (diagnostic; 16-case Agent revalidation PENDING)
Colleague R1 revalidation              = PENDING (package/manifest/guide frozen; results not pre-written)
```

**尚未做（防止误读）：**

```text
正式 V1.3 qualification + tag                    = NOT YET（待 colleague 16-case rerun 后收敛，未宣称）
Overlay V1.1                                     = NOT STARTED
S3+ scale bands / concurrency                    = NOT STARTED（RQ2 明确排除）
第四个 benchmark 仓库                             = NOT STARTED（明确不做）
```

## 2. 关键数字

### RQ2（3 仓 × A/B/C/D，SYSTEMS_BENCHMARK_CONTRACT_V1）

```text
3 repos（aria2 118.9k / brpc 227.2k / rocksdb 622.8k C/C++ LOC）
21 valid formal records（A/B 各 5 measured reps；C 4 mutations × 3 reps；D 1 record）
36/36 incremental qualification reps exact parity（六状态层逐 rep digest 相等）
A/B scale counts 精确落在冻结 v0.2 anchors（±10% anomaly band 中心）
0 INVALID_EXECUTION · 0 silently dropped · 1 infrastructure-attributed（B3 D 启动环境，已排除）
```

### SQI-V1（Native vs SQI，36 formal cells）

```text
36/36 cells completed · 0 blockers · 0 capability failures · 0 arm contamination
Task success: Native 13/18 vs SQI 18/18（T02 1/3→3/3，T04 0/3→3/3，无 SQI-induced regression）
SQI citation closure 18/18 · unsupported claims 0（59 次调用，~517 KB envelope）
诚实成本：SQI input 963k vs 527k · cache-read 14.7M vs 3.4M · median 73.5s vs 22.7s
```

### SQI-V1.1 Hardening + C0

```text
48/48 implementation + wiring + isolation tests PASS
sandbox 隔离：预防层 live 实证（越界读取 DENIED）+ CHECKOUT_LEAKAGE 检测层
T05 fixture 与真实双库 evidence source 一致（amendment seal）
C0：59 SQI calls · ~517 KB envelope · evidence 字节 80.2% 被实际引用
    cache amplification 15.3×（native 6.5×）为成本主导机制
```

### SQI-V1.2（C4-R4 clean batch，2026-09-20）

```text
SQI 18/18（T01-T06 全部 3/3）· 9/9 frozen floors PASS · verdict QUALIFIED
单 backend deepseek-flash x36 · 逐 cell provenance · 零 sensitive leakage
成本 vs V1.1 baseline：input -36% · output -58% · cache-read -73% ·
    amplification 15.3 → 6.4 · calls 59 → 49 · envelope bytes -26%
T05.sqi 调用数 6-20/rep → 5/2/6（V1.2-NR2 通用修复，无任务特判、
    无硬编码 required id、无 oracle 变更）
```

### Post-V1.2 开发态（2026-09-21 → 2026-10-10，已集成 main；非正式 qualification）

```text
Analysis Layer D0-D8       UTILITY_OBSERVED 4/6 · ADOPTION_GAP 2/6 · CAPABILITY_GAP 0 ·
                           Base Graph 零影响回归 137/137
D8-RV1 (2026-10-09)        历史 ADOPTION_GAP case A2/B2 自然使用重验 →
                           CASE_SOLVED_WITHOUT_PROVENLATTICE ×2（如实在案，不计能力 win）
D9-C10 P2 (2026-10-09)     6 case × 2 arm × r1 = 12/12 cells · 6/6 pairs VALID ·
                           Arm A facts=0 ×6 · Arm B facts>0 ×6 · 0 leakage / 0 hidden retry
ContextBench zstd 16-case  14/16 resolved · 0 真实失败 · 2 infra-excluded（≠16/16）
                           历史 3 失败 case 对账 = harness 苛判产物
C 链 A1-A3.6               全量回归 254/254 · 真实 zstd diagnostic false direct bindings = 0 ·
                           def-use not justified yet（A4_DEFUSE_NOT_YET_JUSTIFIED）
Colleague R1 (16-case)     PENDING（wheel/manifest/guide 已冻结，结果不预写）
```

## 3. Post-V1.2 能力对账（逐项 status / evidence / reproduction / 性质）

| 能力项 | Status | Evidence path | Reproduction entry | 性质 |
| --- | --- | --- | --- | --- |
| Analysis Layer D0–D8（Python AST/CFG） | QUALIFIED（Python-only，2026-09-22） | `docs/design-analysis-layer-d0-audit.md`…`design-analysis-layer-d8-report.md`；`experiments/analysis_layer/d8/results/D8-20260922T091934Z/`、`D8-20260923T061603Z/` | `experiments/analysis_layer/d8/d8_runner.py` | **formal**（限 Python-only scope） |
| D8-RV1（历史 ADOPTION_GAP case 新版本自然使用重验） | 已闭合：CASE_SOLVED_WITHOUT_PROVENLATTICE ×2（A2/B2，如实在案） | `experiments/analysis_layer/d8/d8_rv1_runner.py`；`experiments/analysis_layer/d8/results/D8-RV1-20261009T083159Z/`、`D8-RV1-20261009T083452Z/`（含 `revalidation-summary.json`） | `experiments/analysis_layer/d8/d8_rv1_runner.py` | **diagnostic**（重验观察，非能力 win） |
| Context Bundle / `task-context`（D9-C1/C2） | implemented；task-context-v0 事实包（fact_id + evidence_ids 全量可追溯） | `docs/design-analysis-layer-d9-c1-report.md`、`docs/design-context-bundle-d9-c2-prereg.md`；`experiments/analysis_layer/d8/results/D9-C1-T3-20260923T074912Z/` + `D9-C1-TRANSPORT-*/` | `python -m provenlattice task-context <repo> <symbol> --json` | **diagnostic**（传输/probe 级；formal 稳定性证据见 D9-C10） |
| Native MCP 暴露（D9-C7/C8） | implemented；单工具 `mcp__provenlattice__query` 透明桥接，C8-A 冻结合约 byte-identical；C8-B 正式批漏斗证据 | `experiments/context_bundle_v0/formal_harness/provenlattice_mcp_server.py`；`experiments/context_bundle_v0/formal/`（`dispatch-contract.json` + sha256、`formal-manifest.json` + sha256） | `python experiments/context_bundle_v0/formal_harness/provenlattice_mcp_server.py` | **formal**（C8-A 冻结合约 + C8-B 正式批） |
| normalization-v2（D9-C9） | implemented；Agent trace → 事实级观测，raw≡normalized 校验（C10 内 ×6） | `experiments/context_bundle_v0/formal_harness/formal_c9c1.py`、`formal_c9c2_runner.py`、`test_formal_c9c1.py` | `PYTHONPATH=src python -m unittest discover -s experiments/context_bundle_v0/formal_harness -p "test_formal_*.py"` | **formal** |
| D9-C10 P2 受控能力稳定性 | PASS（2026-10-09；12/12 cells、6/6 pairs VALID、0 leakage / 0 hidden retry） | `experiments/context_bundle_v0/formal_harness/d9-c10/controlled-capability-protocol.json` + `assignment-sealed.json` + `c10-b1-verdict.json`（+ `c10-b2-amendment.json`） | `experiments/context_bundle_v0/formal_harness/formal_c10d_p2.py`（协议见 `d9-c10/`） | **formal** |
| ContextBench zstd 16-case 重验 | 14/16 resolved、0 真实失败、2 infra-excluded（**≠16/16**）；对比历史 13/16 无回归 | 外部批目录 `benchmark-contextbench-zstd/`（`revalidation-summary.json`、`infra-limited-classification.json`、`historical-failure-attribution-audit.json`、`evidence-infra-limited/`） | `benchmark-contextbench-zstd/rv1-harness.py`（按冻结指南协议重建） | **diagnostic**（自然使用重验，非 formal qualification） |
| C 分析链 A1/A2/A2.1/A3/A3.5/A3.6 | 已集成 main；全量回归 254/254；真实 zstd diagnostic 通过（false direct bindings = 0） | `docs/design-analysis-layer-c-a1-report.md`…`c-a36-report.md`（含各 audit）；`docs/design-analysis-layer-c-a35-zstd-diagnostic.md`；`docs/c-analysis-release-r1-integration-audit.md` | `PYTHONPATH=src python -m unittest tests.test_c_ast_analysis tests.test_c_cfg_analysis tests.test_c_scfg_analysis tests.test_c_call_semantics tests.test_c_context_retrieval`；`python -m provenlattice analyze <repo> --scope <dir> --features ast,cfg --json` | **diagnostic**（16-case Agent revalidation PENDING） |
| Colleague R1 revalidation（16-case） | PENDING（wheel/manifest/guide 已冻结；结果不预写） | `revalidation/c-contextbench-zstd-r1-manifest.json`；`revalidation/packages/provenlattice-1.0.0-py3-none-any.whl`；`docs/c-analysis-colleague-revalidation-guide.md` | 按指南执行：`pip install revalidation/packages/provenlattice-1.0.0-py3-none-any.whl` → per-case protocol | **pending** |

## 4. 复现矩阵

| 结论 | 复现入口 | 验证内容 | 需要 Agent/DB |
| --- | --- | --- | --- |
| 实现正确性（SQI） | `python experiments/query_interface_v1/tools/verify_release.py` | 全部 seal + 48 tests | DB（只读） |
| RQ2 系统正确性 | `experiments/systems_v1/results/*summary*.json` + 21 records | 增量/full parity、anomaly band | 记录已含 |
| SQI 冻结任务 | `experiments/query_interface_v1/tasks/SQI-T01..T06.json` | 任务/锚点/ground truth | 记录已含 |
| SQI smoke | `results/implementation-smoke/SQI-SMOKE-20260917T020338Z-15ea13/` | frozen anchors 6/6 | DB（只读） |
| SQI formal | `results/formal/SQI-FORMAL-20260917-1/` | Native vs SQI 36 cells | 需 Agent 环境（见 §5） |
| SQI-V1.2 formal | `results/formal/SQI-FORMAL-C4-20260920T093328Z/`（batch-summary）+ `results/formal/C4-R4-SYNTHESIS-20260920-175458.json` + `reviews/v1.2-final-qualification-review.md` | SQI 18/18 · 9/9 floors · 单 backend 36 cells | 需 Agent 环境（见 §5） |
| C0 成本归因 | `results/formal/.../cost-attribution-v1.json` + `reviews/c0-cost-attribution-review.md` | 成本逐源拆解（不跑 Agent） | 已有记录 |
| 基座能力证据链 | `experiments/foundation_analysis/results/PROVENLATTICE-FOUNDATION-EVIDENCE.json` + `experiments/foundation_analysis/reviews/provenlattice-foundation-evidence-review.md` | RQ1/RQ2/工程基线/架构映射/缺口矩阵综合（只读现有正式 evidence） | 已有记录 |
| C4-R4 Native vs SQI 横向 | `experiments/query_interface_v1/results/analysis/C4-R4-NATIVE-VS-SQI-V1.2.json` + `reviews/c4-r4-native-vs-sqi-v1.2.md` | 同批横向成功率（100% vs 66.7%）与 success-normalized 成本（−28.1%）分析 | 已有记录 |

## 5. 一键验证

```bash
python experiments/query_interface_v1/tools/verify_release.py
```

预期输出（节选）：

```text
Contract seal (SQI V1)                     PASS 6/6 (+1 superseded by amendment)
Implementation seal (SQI)                  PASS 5/5
Protocol seal (Formal Protocol V1)         PASS 2/2
Amendment seal (Protocol V1.1 + T05)       PASS 2/2
Secondary seal (V1.1, 10 files)            PASS 10/10
Systems contract seal (RQ2)                PASS 25/25
Frozen databases                           PASS 4/4
Tests (...)                                PASS 61/61
```

单跑测试：

```bash
PYTHONPATH=src python -m unittest experiments.query_interface_v1.tests.test_sqi_v1 \
    experiments.query_interface_v1.tests.test_sqi_formal_wiring   # 61 tests
PYTHONPATH=src python -m unittest discover -s tests              # 核心 integration tests
```

## 6. 环境与冻结输入

| 项 | 值 |
| --- | --- |
| Python | 3.12.10 |
| OS | Windows 11（单机） |
| 三方依赖 | `requirements.txt`（tree-sitter 0.26.0 + c/cpp/python grammars，其余为 stdlib） |
| Agent runtime | claude CLI 2.1.270 · model `claude-sonnet-4-5-20250929`（SQI formal） |
| Benchmark commits | 见 `repositories.lock.json`（aria2 `9e727358…`、brpc `ae09e960…`、rocksdb `37234200…`） |
| Frozen databases | 见 `repositories.lock.json` `databases`（含 sha256，`graph_generation=1`） |

重建 frozen DB：按 `repositories.lock.json` checkout 三仓至冻结 commit，使用本仓
`src/provenlattice`（V0.2 实现，`python -m provenlattice.cli`）构建索引与知识层；
sha256 必须与本锁文件一致方可复跑 qualification。

## 7. 证据地图（formal / smoke / historical）

物理路径保持 seal 冻结时的原样（移动会破坏 SHA-256 seal），分层语义如下：

| 层 | 位置 | 内容 |
| --- | --- | --- |
| **formal** | `experiments/systems_v1/results/QUALIFICATION-V1-*.json`、`qualification-v1-*-summary.json`、`rq2-cross-system-synthesis-v1.json`；`experiments/query_interface_v1/results/formal/SQI-FORMAL-20260917-1/`（summary/synthesis/cost-attribution/manifest）；`experiments/query_interface_v1/results/formal/SQI-FORMAL-C4-20260920T093328Z/`（batch-summary）+ `C4-R4-SYNTHESIS-20260920-175458.json` | 支撑结论的正式证据 |
| **smoke** | `experiments/systems_v1/results/RESMOKE-*.json`；`experiments/query_interface_v1/results/implementation-smoke/SQI-SMOKE-20260917T020338Z-15ea13/` | 有效 smoke（非正式证据） |
| **historical** | `experiments/systems_v1/results/SUPERSEDED-*.md`、`SYSV1-*`（pre-freeze）、`*.invalid.json`；`experiments/query_interface_v1/results/implementation-smoke/SUPERSEDED-smoke-runs.json`、V1 batch 内每 cell 的 `evaluation.json`(V1) 与 `evaluation-v11.json` | superseded / invalid / excluded，显式标记、永不删除 |

规则：`task failure ≠ capability failure`；任何失败先归因；superseded/invalid/excluded
一律显式标记，不删除。

## 8. 各线入口

| 线 | 入口 |
| --- | --- |
| RQ1 Graph Fidelity | `experiments/fidelity_v1/` + `docs/p0-graph-fidelity-v1-status-v1.md` |
| RQ2 Systems & Scale | `experiments/systems_v1/README.md` |
| SQI-V1 / V1.1 / V1.2 | `experiments/query_interface_v1/README.md` |
| Analysis Layer D0–D8 / C 链 A1–A3.6 | `experiments/analysis_layer/` + `docs/design-analysis-layer-*-report.md` |
| Context Bundle / MCP / normalization / C10 | `experiments/context_bundle_v0/` + `docs/design-analysis-layer-d9-*.md` |
| Post-V1.2 综合报告 | `docs/report-post-v1.2.md` |
| Colleague R1 revalidation | `revalidation/` + `docs/c-analysis-colleague-revalidation-guide.md` |
| 治理框架 | `docs/provenlattice-research-validation-framework-v1.md` |
| 证据盘点 | `docs/provenlattice-evidence-inventory-gap-matrix-v1.md` |

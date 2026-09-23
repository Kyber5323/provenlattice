# D9-C2 Context Bundle Controlled Adoption Experiment — Preregistration

**Status:** frozen before bundle implementation and before formal A/B runs
**Decision:** `READY_FOR_MINIMAL_CONTEXT_BUNDLE_IMPLEMENTATION`

## 1. Question and causal contract

This experiment asks whether a compact, deterministic task-context bundle can
replace part of an Agent's native Grep/Read retrieval without lowering repair
correctness. It does not test new graph capability, Python AST/CFG, an LLM
summary, a C/C++ resolver change, or a knowledge layer.

The only treatment is a new callable compact bundle surface in Arm B. Therefore
the arms intentionally do **not** have identical tool surfaces:

| Arm | Frozen common conditions | Difference |
| --- | --- | --- |
| A — current PL | zstd base commit, PL database/evidence, task prompt, model, machine/runtime, Agent permissions, timeout, evaluator | current PL CLI only |
| B — compact bundle | exactly the same conditions | current PL CLI plus one discoverable `task-context` bundle surface |

The B prompt must not request, recommend, or imply that the bundle be used.
Its natural discovery, invocation, consumption, and reuse are outcomes. The
new command's own normal help/description may describe its contract; no
case-specific help or prompt text is permitted.

## 2. Authoritative input and immutable case universe

The source is the public `Contextbench/ContextBench` dataset, `default/train`,
filtered to `repo == "facebook/zstd"`. The downloaded source is not an Agent
input and must not be committed with the experiment code.

| Source property | Frozen value |
| --- | --- |
| Dataset repository revision | `c2855792b006af41c67202d33883fb9d46362853` |
| File | `data/full.parquet` |
| SHA-256 | `2f56535bdc73eb8a68bf4ebb49789d8e9cd4f219ea60df6290b85278aee61ca8` |
| Filtering rule | `repo == "facebook/zstd"` |
| Candidate universe | 16 rows |
| Selection fields allowed | `instance_id`, `original_inst_id`, `repo`, `base_commit`, `problem_statement`, and the pre-existing ContextBench zstd report |
| Fields forbidden for selection | `gold_context`, `patch`, `test_patch`, `f2p`, `p2p`, and all future A/B observations |

The selected six are fixed below. They were selected before any bundle exists:
their problem statements explicitly span command selection, public API plus
implementation, streaming paths, or component-to-component parameter flow.
This is a task-structure criterion, not a claim that a bundle will help.

| Case | Original ID | Base commit | Frozen problem statement | Why it is cross-component / cross-file |
| --- | --- | --- | --- | --- |
| `Multi-SWE-Bench__c__maintenance__bugfix__5adf8efc` | `facebook__zstd-3942` | `372fddf4e6a6db6776b745f31c02a7c8c8dfc83f` | Fix #3719: when `-c`, `-o`, and `--rm` occur together, `-o` can become the selected option after `-c`, causing the wrong removal rule. | CLI option selection and removal policy must be traced together. |
| `Multi-SWE-Bench__c__maintenance__bugfix__a47dfbbf` | `facebook__zstd-2094` | `6b4a3e019f8eeb3423065f7b24d790358e8cbc59` | Add `ZSTD_d_stableOutBuffer`; fix empty-frame single-pass eligibility; add single-pass/stable-output tests and fuzz coverage. | Public decompression API, streaming implementation, frame handling, and tests interact. |
| `Multi-SWE-Bench__c__maintenance__bugfix__9a7be8ba` | `facebook__zstd-1532` | `eb3a7a38270dc2a1f533c1b347f8b9b56e789f8f` | Rename `ZSTD_CCtxParam_*` to `ZSTD_CCtxParams_*`, preserving `ZSTDMT_` compatibility functions and all call sites. | API declaration, implementation, compatibility boundary, and distributed call sites must agree. |
| `Multi-SWE-Bench__c__maintenance__bugfix__2b5e5e23` | `facebook__zstd-1530` | `54e9412ddd02ca4c6663d09dbf44f5a209a9a7ce` | Centralize ZSTDMT parameter setting in `ZSTD_CCtxParams_*Parameter()`, preserve worker/overlap settings, revise job-context copying, clamp bounds, and add tests. | Explicit transfer of parameter logic between MT and CCtx components. |
| `Multi-SWE-Bench__c__maintenance__bugfix__616889c2` | `facebook__zstd-1416` | `c584e84e68109e6722e32cf0157a2c3706ca8f0d` | Add rsyncable mode by factoring LDM hashing, exposing an advanced API, integrating zstdmt, fixing block-size behavior, and adding stream/play tests. | Hashing, multithreading, public API, CLI behavior, and tests span several components. |
| `Multi-SWE-Bench__c__maintenance__bugfix__21d59e43` | `facebook__zstd-836` | `3128e03be69b9d3db054a3765ae1310c7b3666f6` | Add `ZSTD_decodingBufferSize_min()` for bufferless streaming and use it in `ZSTD_decompressStream()`. | New public helper and existing streaming path must share one sizing rule. |

`gold_context`, reference patch, and test patch remain evaluation-only. They
must never be made visible to either Agent or bundle builder.

## 3. Run matrix and execution freeze

The experiment is exactly **6 cases × 2 arms × 3 independent repeats = 36
real Agent runs**. No case may be added, removed, replaced, or repeated after
looking at an A/B result. A run is an independent fresh checkout reset to the
table's `base_commit`; no state, session, bundle cache, or conversation is
reused across repeats.

Before the first run, the harness must persist a manifest containing the
following byte-level values for every cell: dataset revision/file hash above,
case id, commit, prompt SHA-256, system-prompt SHA-256, model/backend identity,
CLI version, runtime/machine identity, allowed-tools list, timeout, evaluator
version, PL database identity/generation, and Arm A/B command inventories.
The only expected command-inventory delta is `task-context` in B.

Task prompt bytes are the selected row's `problem_statement` without treatment
language. The shared system prompt, permissions, and evaluator are captured
once in that manifest and must be byte-identical between paired A/B runs.

## 4. Compact task-context bundle V0

The not-yet-implemented B surface is a deterministic view over the same
existing C/C++ PL database and evidence available to A. It may contain only:

- primary symbols and stable symbol identities;
- file paths and source line/byte spans;
- direct resolved caller/callee/reference relations;
- unresolved or ambiguous references and their existing candidates;
- minimal source spans; and
- expansion handles (`symbol_id`, `reference_id`, relation selector, existing
  CLI entry point).

It must not contain LLM-written text, Python AST/CFG, any new C/C++ analysis,
function-pointer resolution, knowledge/document results, case-specific rules,
gold-derived ranking, reference-patch information, or test-patch information.

### Hard output limits

The V0 response is bounded before implementation:

| Item | Limit | Ordering / truncation |
| --- | ---: | --- |
| Primary targets | 2 | explicit anchor first; then existing PL relevance order |
| Direct relations | 12 | existing deterministic relevance order; ties by relation kind, source id, destination id |
| Unresolved/ambiguous candidates | 6 | existing candidate order; ties by reference id then symbol id |
| Source spans | 4 | primary-target span first, then relation-backed span order |
| Lines per span | 20 | preserve target/call/reference line; grow symmetrically where possible |
| Aggregate source lines | 80 | omit lowest-ranked complete spans, never partial facts |
| Serialized response | 6,000 UTF-8 characters | omit in the same order; set `truncated: true` plus omitted-counts |

No implementation may raise these limits or alter ordering after observing A/B
outcomes. A fact omitted by a limit is absent, not summarized or inferred.

## 5. Stable fact identity and audit rules

Every emitted atomic fact must carry exactly one stable identity. Identity is
based on the frozen base revision, not display strings:

| Fact type | Identity |
| --- | --- |
| target | `symbol:<symbol_id>` |
| direct relation | `relation:<kind>:<src_symbol_id>:<dst_symbol_id>` |
| source span | `span:<base_commit>:<file_path>:<start_byte>:<end_byte>` |
| unresolved reference | `unresolved:<reference_id>` |
| candidate | `candidate:<reference_id>:<symbol_id>` |

Fact identity—not file identity—drives retrieval-replacement and redundant-read
measurement. Reading a different fact from the same file is never redundant.

An independent fact is **reacquired** when a later Grep/Read result contains
the same relation endpoints, candidate pair, unresolved reference, or a source
range overlapping at least 80% of that fact's span. A fact is **consumed** only
when the trace or final rationale/patch action demonstrably relies on its value;
two arm-blinded reviewers label this from traces, with adjudication on
disagreement. A bundle fact is **independently reacquirable** only if a native
Grep/Read operation could retrieve it from the frozen checkout; pure handles
and transport metadata are excluded from the denominator.

## 6. Metrics and adoption funnel

The following are primary recorded measures for every run: task correctness,
bundle availability/discovery/invocation/consumption, Retrieval Replacement
Rate, Post-KG Redundant Read Rate, input tokens, Grep calls, Read calls, PL
calls, tool-output characters/tokens, unique files read, repeat reads, wall
time, and fact-level audit labels.

The funnel is recorded without treating any drop-off as a transport error:

```text
available → discovered → invoked → fact consumed → fact reused without native re-read
```

Definitions:

```text
Retrieval Replacement Rate
= consumed, independently reacquirable bundle facts not subsequently
  reacquired through Grep/Read
  / consumed, independently reacquirable bundle facts

Post-KG Redundant Read Rate
= subsequent native re-acquisition events for a bundle-provided fact
  / bundle-provided, independently reacquirable facts
```

The denominator is zero/undefined when no qualifying consumed facts exist; it
is never coerced to zero or one. Report that condition separately.

## 7. Correctness gate and result classes

Dynamic ContextBench evaluation remains the frozen oracle. A claimed bundle
benefit requires `Correctness_B >= Correctness_A` over the fixed 18 paired
runs; the preregistration does not require correctness to increase.

Classify each B run, then summarize counts by case and repeat, using this
precedence:

1. `CORRECTNESS_REGRESSION` — B is unsuccessful where its paired A run is
   successful, or aggregate B correctness is lower than A.
2. `ADOPTION_GAP` — bundle was available but was not discovered, invoked, or
   consumed.
3. `CONSUMPTION_GAP` — bundle was invoked and supplied a consumed,
   independently reacquirable fact, but that fact was subsequently reacquired
   natively.
4. `UTILITY_OBSERVED` — bundle was naturally invoked and consumed, at least one
   qualifying fact was not reacquired natively, and the run has no correctness
   regression.
5. `NO_INCREMENTAL_VALUE` — bundle was normally consumed with no correctness
   regression, but it produced no measurable retrieval replacement and no
   consumption-gap re-acquisition event.

## 8. Stop, validity, and analysis rules

There is no result-driven early stopping: once a valid first A/B cell starts,
complete all 36 cells. Abort and mark the affected batch invalid only for a
shared infrastructure failure (transport failure, lost evaluator, wrong base
commit/database, prompt/tool-surface drift, or unavailable model) that prevents
the paired condition from being run. Repair requires a separate task and a new
manifest; invalid cells are not silently replaced.

After the 36 cells, do not add capacity or new bundle content in response to
results. If Agents invoke the bundle but still reacquire the same facts, the
next question is bundle organization/span granularity/tool selection—not new
graph capability. No result from this experiment authorizes L3/DFG, L4 taint,
Python AST/CFG use on C/C++, a resolver change, or a knowledge-layer treatment.

## 9. Freeze boundary

At this commit: bundle implementation is zero and formal A/B runs are zero.
The downloaded public dataset is an uncommitted experiment input; its revision
and SHA-256 above are the reproducibility lock. Any later implementation must
conform to this document rather than revising it for implementation convenience.

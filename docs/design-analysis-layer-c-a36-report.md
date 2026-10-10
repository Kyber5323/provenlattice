# C-ANALYSIS-A3.6 report

## Scope

Retrieval and compactness only — no new analysis semantics. Built on the
A3.5 diagnostic findings (declaration-first symbol anchoring; file-level
AST/CFG verbosity).

## Declaration vs definition retrieval

Before: `GraphQuery._resolve` broke same-qualified-name ties by node id hash,
so a header declaration (zstd.h) could anchor `get_definition`,
`explain-symbol`, `callers`/`callees` and `call_semantics` while the
implementation definition (which owns the CFG/call facts) went unused —
A3.5 observed `cfg: not_built` and empty call semantics for
`ZSTD_CCtxParam_setParameter`.

After: among same-name Function/Method ties the unique
`metadata.definition == true` node is preferred when at least one
declaration exists. The preference is C-only by construction (Python nodes
carry no `definition` flag, so it can never fire for Python). Multiple
definitions keep the deterministic legacy choice and are surfaced honestly:
`get_definition` now adds additive envelope keys —
`related_declarations` (unique definition case) or
`definition_ambiguous` + `definition_candidates` (multiple definitions).
Declarations are never removed; they remain first-class targets when no
definition exists (declaration-only extern functions).

Side effect (intended): `callers`/`callees` on a C function name now resolve
to the definition, so incoming CALLS edges (which attach to the definition)
are found instead of an empty result through the declaration.

## Compact symbol context

New read-only `GraphQuery.get_analysis_summary(symbol)`:

```text
symbol_id / file / lines            definition span
calls.direct | .indirect | .unresolved   sorted unique callee names
calls.ambiguous                     [{callee, candidate_count, sites}]
call_counts                         per-class site counts
ast                                 {status, node_count}   (symbol subtree)
cfg                                 {status, node_count, edge_count,
                                     branches, switch_cases, fallthroughs,
                                     returns, calls, gotos, loop_back_edges}
```

Read-only by contract: it reads existing analyzer state and never builds
(tested — fresh index yields `not_built` and zero created states). Full
AST/CFG remain available through the explicit `analysis` query and
`explain-symbol --analysis`, unchanged.

## zstd query replay (read-only, same A3.5 worktree bases)

### 35ed56f6

| query | before (A3.5) | after (A3.6) |
| --- | --- | --- |
| ZSTD_getFrameHeader_internal | decl/def anchor arbitrary; file AST 11.5 MB; cfg n/a | definition primary (zstd_decompress.c:314-387); summary 1 116 B; AST 462 nodes; CFG 98 nodes / 22 branches / 10 switch cases / 2 fallthroughs / 8 returns / 3 calls; explain analysis 267 KB available with entry/exit |
| ZSTD_getFrameHeader | decl-first anchor risk | definition primary + `related_declarations: [lib/zstd.h]`; summary 838 B; CFG 3 nodes (trivial wrapper) |
| ZSTD_frameHeaderSize_internal | — | definition primary; summary 846 B; CFG 11 nodes / 2 branches |

### 8bffb1b1

| query | before (A3.5) | after (A3.6) |
| --- | --- | --- |
| ZSTD_CCtx_setParameter | decl-anchor → cfg not_built, empty call semantics | `.c` definition primary + zstd.h related declaration; summary 904 B; CFG 58 nodes / 23 switch cases / 14 fallthroughs / 14 branches / 14 returns |
| ZSTD_CCtxParam_setParameter | decl-anchor → cfg not_built | `.c` definition primary; summary 952 B; CFG 123 nodes / 23 switch cases / 4 fallthroughs / 28 branches / 24 returns / 11 calls; explain analysis 313 KB available |

Switch/case/default/fallthrough from A2 is directly visible in the compact
summary for the real 140-line parameter dispatch.

## Validation

- New golden tests RET-C-01..10 + read-only/no-build + callers-through-
  definition: 12 tests (definition preference, ambiguity honest, declaration
  preserved, symbol-scoped AST, CFG summary counts, call groups,
  determinism, Python unchanged, read-only).
- Focused: retrieval 12/12 + call semantics 23/23 PASS.
- Full regression: 254/254 PASS (A3 baseline 242 + 12).

## No new semantic capability

No def-use, no points-to, no Clang, no new CFG semantics, no
function-pointer target inference, no benchmark-specific branching. The
change surface: `query.py` (`_resolve_candidates`, `get_definition`,
`get_analysis_summary`) only.

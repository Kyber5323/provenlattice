# C-ANALYSIS-A3.5 — Real zstd diagnostic smoke

Diagnostic, not a formal benchmark. Read-only: zero production code changes.
ProvenLattice commit `61b5012`; diagnostic scripts run from a temp directory
(worktrees created from the frozen `D:\pl-c4-pilot\zstd` checkout at each
case base commit, removed after use; benchmark sources untouched).

## Environment

- ProvenLattice: `61b5012` (A1+A2+A2.1+A3)
- zstd base commits per case (ContextBench cases, same source as the frozen
  benchmark): 35ed56f6 `99e8063d`, 8bffb1b1 `04212178`, controls f9bb5442
  `df09d431`, 2687f1e4 `517d8c98`
- index mode: `full_index` over the whole worktree + `analyze_c_ast_scope` /
  `analyze_c_cfg_scope` over `lib` (optional, explicit)

Index stats (representative, 35ed56f6):

```text
index wall        ~2.7 s
files             201
symbols (nodes)   14 666
raw references    22 864  (resolved 8 108 / ambiguous 4 161 / unresolved 10 595)
edges             19 314
DB size           64.5 MB
lib AST           62 files, 241 549 nodes, ~9.7 s
lib CFG           39 files, 1 504 functions built, 32 055 nodes / 35 160 edges, ~4.9 s
                  8 functions degraded, 23 skipped (no bodies: headers), 0 unresolved gotos
```

## Case 35ed56f6 — `ZSTD_getFrameHeader_advanced()`

Targets probed at base `99e8063d` (the advanced API does not exist yet; the
patch renames/generalizes static `ZSTD_getFrameHeader_internal`).

- Symbol facts: `ZSTD_getFrameHeader` (zstd.h:883 decl), `ZSTD_getFrameHeader_internal`
  (zstd_decompress.c:314-387, static), `ZSTD_frameHeaderSize_internal`,
  `ZSTD_startingInputLength`, `ZSTD_decodeFrameHeader` all found;
  `ZSTD_getFrameHeader_advanced` correctly NOT_FOUND at base.
- Header/impl linkage: `ZSTD_getFrameHeader` links zstd.h:883 decl ↔
  zstd_decompress.c:395-398 definition ✓.
- Call semantics: `ZSTD_getFrameHeader_internal` has exactly 3 resolved
  direct call sites (zstd_decompress.c:397, 494, 2770) — precisely the three
  sites the reference patch rewires. `ZSTD_decodeFrameHeader`: 4 direct +
  1 ambiguous (`ZSTD_isError`, 6 candidates — macro/helper ambiguity, honest).
- CFG facts: `ZSTD_getFrameHeader_internal` slice = 90 nodes: 10 case nodes,
  8 break, 2 default, 2 fallthrough, 13 conditions, 8 returns, 3 callsites —
  the format-switch structure is fully represented.
- fuzzer.c: `ZSTD_DCtx_setFormat` @ line 1090 (resolved direct) — the test
  patch inserts the `ZSTD_getFrameHeader_advanced` call immediately after
  this line; the insertion point is discoverable.
- Compactness: file-level AST query for zstd_decompress.c = 11.5 MB /
  16 391 nodes (too large for direct consumption); file-level CFG 2.7 MB.
  The symbol-scoped path is bounded (see below).
- Missing facts: none blocking. `MEM_readLE32` ambiguous (7 candidates,
  macro/inline helper), `memset` unresolved (system function) — honest, and
  not needed for this task.
- Classification: `CURRENT_FACTS_APPEAR_SUFFICIENT`
  (plus a `DISCOVERY_OR_ADOPTION_GAP` note below).

## Case 8bffb1b1 — `ZSTD_CCtx(Param)?_getParameter()`

Targets probed at base `04212178` (getParameter does not exist yet).

- Symbol facts: `ZSTD_CCtx_setParameter` zstd_compress.c:276-336 with
  zstd.h:1072 declaration linked ✓; `ZSTD_CCtxParam_setParameter`
  zstd.h:1239 decl + zstd_compress.c:338-478 definition ✓;
  `ZSTD_CCtxParams_setParameter`/`getParameter` correctly NOT_FOUND at base.
- CFG facts (the A2 switch validation on real code):
  - `ZSTD_CCtx_setParameter`: 23 case nodes, 22 `case` edges, 1 `default`,
    14 `fallthrough` edges, 7 if-conditions, 14 returns.
  - `ZSTD_CCtxParam_setParameter` (the 140-line parameter dispatch):
    23 case nodes, 22 `case` edges, 1 default, 4 `fallthrough`, 15
    conditions, 24 returns, 11 callsites, single entry/exit.
  Switch/case/default/fallthrough from A2 demonstrably represent the real
  parameter dispatch.
- Call semantics: `ZSTD_CCtx_setParameter` → direct `ZSTD_isUpdateAuthorized`,
  `ZSTD_CCtxParam_setParameter` ×3, `ERROR` — the delegation structure is
  visible.
- fuzzer.c: `ZSTD_CCtx_setParameter` call sites at lines 228/229/248/249 and
  1033-1035 (all resolved direct) — the test patch inserts getParameter
  calls adjacent to these; insertion points discoverable. (`ZSTD_getParams`/
  `ZSTD_getCParams` in fuzzer.c are ambiguous — multiple repo definitions —
  honest, bounded candidates.)
- Missing facts: none blocking for implementing getParameter by mirroring
  the set-side case→field mapping (each case's store statement is visible
  via CFG statement spans / AST).
- Classification: `CURRENT_FACTS_APPEAR_SUFFICIENT`
  (same `DISCOVERY_OR_ADOPTION_GAP` note).

## PASS controls

- f9bb5442 (local edit, zstd_compress.c @ df09d431): index 2.8 s; same lib
  AST/CFG coverage; `ZSTD_decompressSequences` (the heavy control-flow
  function) exposes 16 direct + 2 unresolved call sites with CFG available
  (1 420 nodes for the file slice probed). Fact quality identical to the
  INFRA cases.
- 2687f1e4 (negative-level estimates, zstd_compress.c @ 517d8c98):
  `ZSTD_estimateCCtxSize`/`ZSTD_estimateCStreamSize` found with CFG
  available; call sites in fuzzer.c (line 619, resolved direct) captured.
  Fact quality identical.

## Comparative findings

No material fact-quality difference between the two INFRA_LIMITED cases and
the PASS controls: same index depth, same AST/CFG coverage, same call
semantics behavior. The INFRA_LIMITED outcomes are environment artifacts
(2016-era CLI benchmark hang under MSYS2 pipes), unrelated to C analysis
capability.

Recorded adoption/discovery gap (the only systematic one): name→symbol
resolution prefers the first match, which is a declaration (e.g. zstd.h) —
`explain-symbol --analysis cfg` on that anchor reports `not_built` and
`call_semantics` is empty because calls belong to the definition symbol.
Using the definition symbol (zstd_compress.c:338) yields the full bounded
view: AST 624 nodes / 161 KB, CFG 123 nodes / 155 edges / 153 KB with
entry/exit, call_semantics 12 direct + 11 unresolved (counts complete, site
list bounded at 16), total explain 335 KB. File-level AST/CFG queries remain
verbose (11-14 MB / 2.7-3.5 MB) — the symbol-scoped surface is the intended
consumption path.

Optional agent diagnostic (Step 7): SKIP — historical runs already recorded
agent metrics (pl_cli_calls 0-1); a new run would add quota cost without
changing this diagnostic's conclusions.

## Def-use decision

Real dependency candidate found (zstd_compress.c:349-362, case
`ZSTD_p_compressionLevel`):

```c
int cLevel = (int)value;                             /* def from parameter */
if (cLevel > ZSTD_maxCLevel()) cLevel = ZSTD_maxCLevel();  /* conditional redef */
if (cLevel) {                                        /* branch on the value */
    CCtxParams->disableLiteralCompression = (cLevel<0);
    CCtxParams->compressionLevel = cLevel;           /* store */
}
if (CCtxParams->compressionLevel >= 0) return CCtxParams->compressionLevel;
```

This is a genuine parameter → local → conditional-redefinition → branch →
struct-store chain. However: every span is inside one function, adjacent,
and already sequenced by the A2/A2.1 CFG (branch edges + statement spans);
the get-side implementation only needs the per-case store field, which is
visible in the set-side case bodies through current facts. Neither INFRA
case requires cross-function value tracking that AST/CFG/call semantics
cannot express.

## Conclusion

- 35ed56f6 / 8bffb1b1 / both controls:
  `CURRENT_FACTS_APPEAR_SUFFICIENT`
- Systematic gap: `DISCOVERY_OR_ADOPTION_GAP` (definition-first symbol
  resolution + compact symbol-scoped consumption), not a CFG/call/type gap.
- Def-use: `A4_DEFUSE_NOT_YET_JUSTIFIED` — the `cLevel` chain above is the
  first genuine candidate if a future task requires cross-function value
  tracking, but no current evidence demands it.

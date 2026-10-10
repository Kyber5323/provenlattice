# C-ANALYSIS-A2 implementation audit

## Scope

A2 adds function-local C CFG V1 on top of the frozen A1 C AST
(commit `235ef4e`). The C CFG must be built from the A1 normalized C AST
boundary, reuse the common optional-analysis storage and query surfaces, and
leave the Python analyzer untouched.

## Current architecture

```text
Python AST (ast:python, analysis_ast.py)
   ↓
Python CFG builder (_CFGBuilder in analysis_cfg.py)
   ↓
common AnalysisStore (analysis_state/nodes/edges)
   ↓
analysis_query.query_analysis / GraphQuery._analysis_sections
```

Target:

```text
C normalized AST (ast:c, analysis_c_frontend/analysis_c_ast, frozen in A1)
   ↓
C CFG builder (new analysis_c_cfg.py)
   ↓
same AnalysisStore / same query surfaces
```

## Audit answers

A. **Python CFG builder location**: `analysis_cfg.py`. Entry
   `analyze_cfg_scope` (file loop, freshness, skip categories) and
   `_CFGBuilder` (per-function graph construction). No public graph model of
   its own; it only emits `AnalysisNode`/`AnalysisEdge` rows.

B. **ENTRY/EXIT representation**: one `entry` node and one `exit` node per
   function (`kind="entry"`/`kind="exit"`, `structural_role="entry"/"exit"`),
   id = `cnode:sha256(file_id, owner_symbol_id, role, structural_path)` where
   the path is the function AST node's structural path. `entry -> first
   statement` (`next`), block ends -> `exit` (`next`), returns -> `exit`
   (`return`). Empty functions get a direct `entry -> exit` edge.

C. **Statement/AST identity sharing**: every CFG statement node carries
   `ast_node_id` = the persisted `ast:python` node id, computed with the same
   `ast_node_id(file_id, structural_path)` algorithm and validated against the
   persisted AST id set (`ast_ids`); if the AST row is missing the link is
   dropped, never guessed. Synthetic nodes (entry/exit/merge/loop_exit) have
   `ast_node_id=None`.

D. **CFG edge schema**: `AnalysisEdge(analysis_edge_id, src_id, dst_id, kind,
   payload)` with `edge_id = cedge:sha256(src, dst, kind, ordinal)`; the
   ordinal disambiguates duplicate edges between the same endpoints. Payload
   currently carries `owner_symbol_id`. The `analysis_edges` table and
   `_check_cfg_integrity` are language-neutral.

E. **Edge semantics storage**: a single lowercase `kind` string per edge —
   Python V1 uses `next`, `true`, `false`, `back`, `continue`, `break`,
   `return`. There is no edge-property table; semantics live in `kind`.

F. **return/break/continue wiring**: `return` nodes are collected during the
   build and connected to EXIT with kind `return` after the body is built;
   statements after `return` in the same block become unreachable (they get no
   successor from the return). `break` nodes are recorded on the innermost
   `_LoopFrame` and connected to that loop's exit merge with kind `break`.
   `continue` connects directly to the loop header with kind `continue`.

G. **CFG digest/determinism testing**: identity is fully derived from
   canonical strings (file/owner/role/path and src/dst/kind/ordinal), so the
   same source yields byte-identical node/edge ids. Existing regression covers
   integrity (`_check_cfg_integrity`, corrupt tests) and query determinism;
   A2 adds explicit C golden fixtures plus a store-level digest re-run test.

H. **Query layer CFG read**: `analysis_query._read_state(file_id, analyzer)`
   loads `cfg:<language>` state, nodes and edges; `_check_cfg_integrity`
   validates owners, single entry/exit per owner and edge endpoints; symbol
   queries filter `owner_symbol_id`; `entry_id`/`exit_id` are surfaced;
   `GraphQuery._analysis_sections` reuses `query_analysis` for
   `explain-symbol`/related surfaces. Nothing is Python-specific except the
   analyzer-id selection, which A1 already parameterized by language.

I. **Directly reusable (unchanged)**: `AnalysisStore` persistence/freshness,
   `AnalysisNode`/`AnalysisEdge`, node/edge id algorithms, skip-category
   result shape, `_merge_feature_results` orchestrator merge, query surfaces,
   integrity checks, response limits, CLI plumbing.

J. **C-specific rules (must be implemented new)**: C statement grammar over
   the normalized vocabulary — `if/else` (with `else_clause` unwrap),
   `switch`/`case`/`default` with fallthrough, `do-while` (body-before-
   condition), `for` (init/condition/update decomposition, including omitted
   parts), `goto`/labels (forward/backward, unresolved-label fail-closed),
   implicit exit without `return`, and loop-context semantics where `break`
   exits the nearest loop **or switch** while `continue` targets the nearest
   loop's condition/update. Python rules (async, loop-else, with/try/match)
   are not shared.

## Contract findings

- The common CFG contract (`AnalysisNode`/`AnalysisEdge`, entry/exit kinds,
  lowercase edge kinds) is language-neutral. No decoupling refactor is
  required; C adds lowercase kinds in the same style (`case`, `default`,
  `fallthrough`, `goto`) plus node roles, without migrating the schema.
- The Python-specific part of `analysis_cfg.py` is confined to
  `_CFGBuilder.build_stmt` and the forbidden-construct scan. The C builder is
  a separate module and does not subclass or reuse those rules.
- The builder consumes the A1 normalized tree (`CFrontend` output: kinds,
  parser_kind, spans, names) and links every statement to the persisted
  `ast:c` node ids. Raw tree-sitter nodes are consulted only to resolve the
  grammar fields of `for_statement` (initializer/condition/update/body),
  the only construct whose normalized child order is ambiguous when parts are
  omitted. Raw kinds never enter the persisted schema.

## V1 boundary

Function-local only. No expression-level short-circuit, no interprocedural
edges, no def-use/data-flow, no computed goto / setjmp / asm modeling, no
Clang. Malformed sources degrade (ERROR regions become opaque straight-line
statements, unresolved goto/break are counted and fail-closed) instead of
crashing.

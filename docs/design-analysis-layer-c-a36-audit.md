# C-ANALYSIS-A3.6 implementation audit

## Scope

Retrieval and compactness only — no new analysis semantics. Make the existing
C facts (AST/CFG/call semantics) reachable through definition-preferring,
symbol-scoped, bounded surfaces.

## Audit answers

1. **Symbol lookup ordering**: `GraphQuery._resolve` matches by exact id,
   then qualified_name, then name; ties resolve by
   `min((match_rank, qualified_name, id))`. For a C function the header
   declaration and the `.c` definition share the same qualified name
   (file-level C functions are qualified by name only), so the tie is
   decided by the node id hash — effectively arbitrary. That is why
   `explain-symbol("ZSTD_CCtxParam_setParameter")` anchored on the zstd.h
   declaration (A3.5 finding).
2. **Why the declaration can win**: same qualified name + arbitrary id
   tiebreak; no notion of `metadata.definition` (which the extractor already
   records: `definition = node.type == "function_definition"`).
3. **task-context primary target**: `task_context()` calls
   `get_definition(symbol)` and uses `data` as the anchor — so fixing
   definition preference in `get_definition` fixes the task-context target
   selection with zero change to the frozen `task-context-v0` envelope
   (byte-identical repeated-call tests must keep passing).
4. **explain-symbol analysis state location**:
   `_analysis_sections(anchor_id)` → `query_analysis(feature, symbol_id=...)`
   → filters the file analyzer state by `owner_symbol_id == anchor_id`. A
   declaration anchor has no owned CFG/call facts, hence `not_built` and an
   empty `call_semantics`.
5. **File-level AST exposure**: the `analysis` CLI/query with
   `--file-path` returns the whole file AST (11-14 MB on real zstd files);
   the symbol path (`symbol_id=`) already returns the bounded subtree.
   Nothing defaults to the file-level path for a symbol query — but the
   declaration-first anchor made the symbol path look unavailable.
6. **Symbol-level AST/CFG extraction**: `_query_symbol` filters
   `owner_symbol_id` (CFG) or `structural_path` prefix (AST subtree rooted at
   the function node); `ANALYSIS_RESPONSE_NODE_LIMIT` (400) bounds responses.
7. **Envelope budget**: explain-symbol analysis sections apply
   `_prune_tree` + node limits deterministically; `get_call_semantics`
   bounds sites (16) and candidates (8).
8. **Declaration/definition knowledge**: the foundation already stores
   `metadata.definition` on C Function/Method nodes (`true` only for
   `function_definition`); prototypes/variables are `false`. Python nodes
   carry no `definition` flag at all.
9. **Existing preference mechanism to reuse**: none for retrieval — only the
   A3 call resolver has definition-aware `_select_candidate`. The retrieval
   fix reuses the stored `metadata.definition` flag.

## Plan

- `_resolve`: among same-name ties, prefer the unique `definition=True`
  Function/Method node when at least one declaration exists (C-only by
  construction — Python nodes have no `definition` flag, so the preference
  can never fire for Python). Multiple definitions keep the deterministic
  legacy choice (no arbitrary pick).
- `get_definition`: additive envelope keys — `related_declarations`
  (unique-definition case) or `definition_ambiguous` +
  `definition_candidates` (multiple definitions). Declarations are preserved,
  never removed.
- New read-only `get_analysis_summary(symbol)`: definition span, compact
  call groups (direct/indirect/unresolved names, ambiguous with candidate
  counts), AST summary (status + node count) and CFG summary (node/edge
  counts, branches, switch cases, defaults, fallthroughs, returns, calls,
  gotos, loop back-edges) — only from existing analyzer state, never builds.
- No changes to `task_context()` (frozen `task-context-v0` envelope),
  Python analyzers, or any analysis semantics.

# C-ANALYSIS-A1 implementation audit

## Scope and conclusion

The current optional-analysis storage contract is already language-neutral. C AST
support does not require a foundation-graph migration or a rewrite of
`analysis_v1`. The minimum safe change is to add a C frontend/normalization
adapter and dispatch the existing materialization and query surfaces by the
base file language.

The Python AST and CFG implementations remain unchanged. C CFG is deliberately
not registered and must fail closed as `not_implemented`.

## Current architecture

```text
CLI/query
  -> orchestrator.analyze_scope / analysis_query.query_analysis
  -> fixed Python analyzer selection
       -> analysis_ast.analyze_ast_scope
       -> analysis_cfg.analyze_cfg_scope
  -> AnalysisStore (analysis_state/nodes/edges)
  -> analysis_v1 response assembled by GraphQuery
```

### Audit answers

1. **Python AST/CFG entry points**
   - Materialization: `analysis_ast.analyze_ast_scope` and
     `analysis_cfg.analyze_cfg_scope`.
   - CLI orchestration: `orchestrator.analyze_scope`, reached by
     `provenlattice analyze --features ast,cfg`.
   - Read-only access: `analysis_query.query_analysis`, reached by the
     `analysis` command and the evidence-query `--analysis` option.

2. **`analysis_v1` public model**
   - Persistence is defined in `analysis.py`: `AnalysisNode`, `AnalysisEdge`,
     `AnalysisStore`, analyzer IDs and versioned contracts.
   - Agent-facing envelopes are assembled in `query.GraphQuery._analysis_v1`.
   - The store is keyed by `file_id x analyzer`; it does not assume Python.

3. **Language dispatch**
   - Foundation parsing already dispatches through
     `parser.parser_for_language` and has Tree-sitter C/C++ adapters.
   - Optional analysis is not dispatched: the orchestrator calls only the
     Python materializers and `analysis_query.py` fixes analyzers to
     `ast:python`/`cfg:python`.

4. **Optional-analysis requests**
   - `cli.py` accepts `--analysis` on `explain-symbol`,
     `find-related-documents` and `find-related-code`.
   - `GraphQuery._analysis_sections` calls `query_analysis`; reads never build
     or refresh analysis implicitly.

5. **Identity, spans and evidence**
   - AST IDs are SHA-256 IDs over `file_id`, unit and structural path via
     `analysis_ast.ast_node_id`.
   - AST payloads carry byte and row/column spans. Foundation symbol IDs come
     from `identity.symbol_id`; file IDs come from `identity.path_id`.
   - Optional AST does not create foundation evidence IDs. Query responses
     retain source locators through the persisted spans and owner-symbol link.

6. **Python-specific leakage**
   - `analysis_ast.py` persists raw Python Tree-sitter node kinds.
   - `orchestrator.py` assumes AST/CFG eligibility is Python-only.
   - `analysis_query.py` fixes analyzer selection and function-root kinds to
     Python.
   - The store schema, freshness logic, CLI envelope and `analysis_v1`
     envelope are language-neutral.

7. **Reuse versus adapters**
   - Reuse unchanged: store tables, cache/freshness, scope validation, stable
     AST-ID algorithm, bounded payloads, query envelopes and response limits.
   - Add adapters: C parsing/diagnostics, C normalized kinds, C symbol
     alignment and analyzer dispatch.

## Target architecture

```text
CLI/query
  -> analysis request
  -> language dispatch
       |-- Python analyzer (existing behavior)
       `-- C AST analyzer
            -> tree-sitter-c
            -> C normalized representation
            -> AnalysisStore / analysis_v1
  -> query response
```

For `.h`, A1 uses the C source view for optional AST even though the foundation
scanner conservatively labels ambiguous headers as `cpp`. `.hpp` and other C++
extensions remain unsupported by deep analysis.

## Minimal refactor decision

- Register `ast:c` independently from `ast:python`.
- Keep the Python materializer intact.
- Add a C frontend module that is the only layer aware of Tree-sitter C kinds.
- Persist normalized C kinds plus `parser_kind` metadata.
- Merge per-language AST materialization results only in the orchestrator.
- Resolve the analyzer from file language/path at query time.
- Do not register `cfg:c`; a C CFG request returns explicit
  `not_implemented` and never returns AST as a substitute.

No foundation schema, frozen evidence/oracle, historical experiment, prompt or
benchmark behavior is changed by this plan.

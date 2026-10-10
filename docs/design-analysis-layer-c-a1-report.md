# C-ANALYSIS-A1 report

## Implemented

- Optional `ast:c` analyzer with contract `c-ast-v1`.
- Tree-sitter C frontend isolated behind `analysis_c_frontend.py`.
- Normalized C AST persisted through the existing `AnalysisStore`.
- `.c` and `.h` source-view analysis. Ambiguous `.h` foundation files are
  intentionally parsed as C only for this optional analyzer.
- Shared `analyze`, `analysis`, `explain-symbol`, `find-related-documents` and
  `find-related-code` query paths through language-aware analyzer dispatch.
- Error/MISSING diagnostics, bounded parse coverage and explicit
  `complete`/`degraded` metadata. Partial trees remain queryable.
- Stable IDs from file identity plus normalized structural path.

## Normalized C AST schema

Stored node `kind` values are normalized and never contain a Tree-sitter C
grammar name. The initial vocabulary is:

```text
FILE FUNCTION PARAMETER LOCAL DECLARATION TYPE_DECL TYPE FIELD BLOCK
CONTROL_STRUCTURE CALL IDENTIFIER LITERAL RETURN ASSIGNMENT OPERATOR
FIELD_ACCESS SUBSCRIPT CAST CONDITIONAL LABEL PREPROCESSOR COMMENT
EXPRESSION SYNTAX UNKNOWN
```

Every C node payload contains:

```text
parent_id, structural_path, language, parser_kind,
start_byte, end_byte, start_row, start_col, end_row, end_col,
child_order, name, owner_symbol_id, alignment, partial
```

`parser_kind` is diagnostic/frontend metadata; callers consume normalized
`kind`. The AST remains a derived cache and does not mutate the foundation
graph or frozen evidence contracts.

## Supported constructs

- Functions and prototypes; parameters and local/global declarations.
- Struct, union, enum, typedef and fields.
- Blocks, expression statements and return.
- If/else, switch/case/default, while, do-while, for, break, continue,
  goto and labels as AST structure only.
- Identifier, literal, call, assignment, unary/binary operator, field/pointer
  field access, subscript, cast and conditional expression structure.
- Function-pointer declarations and indirect-looking calls are preserved
  without target inference.
- Preprocessor nodes are retained structurally without macro semantics.

## CLI/query usage

```text
provenlattice analyze <repo> --scope <dir> --features ast
provenlattice analysis <repo> --feature ast --file-path src/example.c
provenlattice analysis <repo> --feature ast --symbol-id <exact-symbol-id>
provenlattice explain-symbol <symbol> --analysis ast
```

Analysis stays opt-in. Indexing and queries without an analysis option keep
their existing behavior. A C `--feature cfg` request returns
`unsupported/not_implemented` and creates no `cfg:c` state.

## Not implemented

- C CFG.
- C++ deep AST.
- Precise C type binding.
- `compile_commands.json`.
- Clang.
- Macro-expanded semantic view.
- Indirect-call target resolution.
- Def-use.
- Points-to.
- Taint.
- PDG.

## Validation

The A1 fixture suite (14 tests) covers the ten required construct groups plus
identity, span, determinism, function-pointer syntax without target inference,
malformed-source degradation and fail-closed C CFG behavior.

Focused tests: 38/38 PASS (C AST suite + analysis CLI + analysis query).
Full regression: 178/178 PASS
(`$env:PYTHONPATH='src'; python -m unittest discover -s tests -p 'test*.py'`).

Real C smoke (repo fixture `tests/fixture_cpp/src/plain.c`, committed `5ef47d9`,
no compilation, no `compile_commands.json`, no include resolution):

```text
file                     src/plain.c (132 bytes)
target symbol            add (Function)
parse success            true
AST node count           31
ERROR node count         0
MISSING node count       0
parse coverage           1.0
analyze wall time        ~42 ms (materialize+persist)
query wall time          ~2 ms
returned analysis bytes  20250 (query envelope)
determinism              identical stored-node digest across re-runs
C CFG request            unsupported / not_implemented
```

## Capability matrix

| Language | AST | CFG |
| --- | --- | --- |
| Python | supported | supported |
| C (`.c`, `.h`) | supported, optional normalized AST | not implemented |
| C++ | current foundation behavior only | not implemented |

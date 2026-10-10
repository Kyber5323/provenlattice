# C-ANALYSIS-A2.1 report

## Implemented

### Normalized CFG closure (no raw parser in the builder)

- `CCfgBuilder` no longer imports or consults any parser API. The A2
  `for_index` (raw `for_statement` span map + `child_by_field_name`) is
  removed; `_build_file_cfg` parses each source exactly once through
  `CFrontend`.
- The pipeline is now strictly:
  `tree-sitter-c -> CFrontend -> normalized AST/IR -> CCfgBuilder`.
- Architecture test: `test_builder_source_has_no_parser_api` (static source
  scan of `analysis_c_cfg` for `tree_sitter`, `child_by_field_name`,
  `field_name_for_child`, `parser_for_language`, `parse_bytes`) and
  `test_builder_runs_on_handbuilt_normalized_tree` (the builder produces a
  correct short-circuit CFG from a hand-constructed normalized tree with no
  parser involved).

### Normalized IR extension (A1 contract v1 -> v2)

- `NormalizedCNode.part_role` (persisted as the `ast:c` payload key
  `part_role`), populated by the frontend — the only parser-aware layer —
  from raw field names: `init`, `condition`, `update`, `body`, `consequence`,
  `alternative`, `value`. Missing for-parts simply have no annotated child,
  so `for (;;)`, `for (; c;)`, `for (i;; u)` and body-only loops are
  representable without positional guessing.
- Normalized kinds `LOGICAL_AND` / `LOGICAL_OR` for `binary_expression` whose
  operator token is `&&` / `||` (the token is anonymous and would otherwise
  be lost by normalization). Ternary keeps its normalized kind
  `CONDITIONAL` with `condition`/`consequence`/`alternative` roles.
- Analyzer versions: `ast:c` 1 -> 2 (`c-ast-v2`), `cfg:c` 1 -> 2
  (`c-cfg-v2`); stale states rebuild via the existing freshness rules.
- The builder now derives if/while/do/switch/for/case parts from `part_role`
  (positional fallbacks remain only for malformed ERROR-recovered parses).

### Expression short-circuit CFG

- `_condition_flow(expr, true, false, role)` lowers boolean expressions:
  - `a && b`: `a -true-> b`, `a -false-> F`; `b` decides.
  - `a || b`: `a -true-> T`, `a -false-> b`; `b` decides.
  - `c ? t : f` in boolean position: `c -true-> t-flow`,
    `c -false-> f-flow`; both operand flows decide.
  - leaf: one `condition` node with `true`/`false` edges (context role kept,
    e.g. `if_condition`/`loop_header`; plain parenthesized conditions keep
    their A2 spans).
- `_value_flow(expr, continuation, role)` lowers value positions with a
  common continuation node: assignment right-hand sides and `return`
  expressions decompose; both evaluation paths reach the statement/return
  node, so evaluation order is exact while produced values stay unmodeled
  (data-flow remains out of scope).
- Loop integration: `while (a && b())` and `for (; a || b(); )` compose with
  existing `back`/`continue` edges; `continue` targets the condition-flow
  entry (or the for-update when present), `break` still exits the nearest
  loop/switch.
- Synthetic nodes remain exactly the A2 set (`entry`, `exit`, `merge`,
  `loop_repeat` for degenerate body-less loops) with the existing
  `ast_node_id = None` mechanism marking them non-source; every short-circuit
  operand node is a real AST-linked node with source spans.

## Golden validation

SCFG-01..10 (`&&`, `||`, chained `&&`, chained `||`, `a && (b || c)`,
`(a || b) && c`, ternary in condition, `while` + `&&`, `for` + `||` with
skipped operand, `return` short-circuit, assignment ternary) assert exact
edge sets, TRUE/FALSE semantics, skipped operand paths and merge behavior;
plus normalized-for-role tests (full / no-init / no-condition / no-update /
body-only) asserting identical A2 CFG edges and persisted `part_role` values,
and a determinism digest across re-runs.

## Validation

- Focused: 79/79 PASS (SCFG 20 + C CFG 21 + C AST 14 + analysis CLI + query).
- Full regression: 219/219 PASS
  (`$env:PYTHONPATH='src'; python -m unittest discover -s tests -p 'test*.py'`),
  A2 baseline 199 + 20 new.

## Still not implemented

- Comma-operator special flow.
- GCC statement-expression advanced flow.
- Computed goto, `setjmp`/`longjmp`, noreturn inference, inline-asm CFG.
- Interprocedural CFG.
- Function-pointer target inference, def-use, SSA, points-to, taint, PDG.
- Clang / `compile_commands.json` / macro-expanded semantic views.
- C++ deep analysis (unchanged).
- Short-circuit decomposition inside `switch` conditions (multi-way dispatch
  stays a single condition node) and inside non-top-level arithmetic
  expressions (e.g. `(a && b()) + c` stays opaque).

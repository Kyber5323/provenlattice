# C-ANALYSIS-A2.1 implementation audit

## Scope

Two closures on top of A2 (`d0297dc`):

1. Remove the last raw tree-sitter dependency from the C CFG builder so the
   pipeline is strictly `tree-sitter-c -> CFrontend -> normalized AST/IR ->
   CCfgBuilder`.
2. Add expression-level short-circuit CFG for `&&`, `||` and `?:`.

## Audit answers

1. **Where the CFG builder reads raw parser information**:
   - `analysis_c_cfg._build_file_cfg` parses each source twice: once through
     `CFrontend` (normalized) and once through `parser_for_language("c")`
     (raw), building `for_index`, a map of raw `for_statement` nodes keyed by
     span.
   - `_build_for` resolves the four for-parts with
     `raw.child_by_field_name("initializer" / "condition" / "update" /
     "body")`, then matches the raw field nodes to normalized children by
     span.
   This is the only raw-parser dependency in the CFG layer.

2. **Why the normalized AST was insufficient for `for` roles**: the A1
   `NormalizedCNode` records kind, parser_kind, spans and a name, but not the
   grammar role of a child. For `for (init; cond; update)` the roles live in
   raw field names, which normalization dropped; child position alone is
   ambiguous when parts are omitted (`for (;;)` vs `for (; c;)` vs
   `for (i;;u)`).

3. **Minimal normalized contract extension** (frontend-owned, parser API
   never leaves the frontend):
   - `NormalizedCNode.part_role` (and persisted `ast:c` payload key
     `part_role`), populated by `CFrontend` from raw field names with a fixed
     normalized role vocabulary: `init`, `condition`, `update`, `body`,
     `consequence`, `alternative`, `value`. Missing parts simply have no
     annotated child, so `for (;;)`, `for (; c;)`, `for (i;; u)` and
     body-only loops are all representable without positional guessing.
   - Normalized kinds `LOGICAL_AND` / `LOGICAL_OR` for `binary_expression`
     whose operator token is `&&` / `||`. The operator is an anonymous token
     and would otherwise be invisible after normalization, making `&&`
     indistinguishable from `+`. Ternary is already a distinct normalized
     kind (`CONDITIONAL`) and its parts get `condition` / `consequence` /
     `alternative` roles.
   - Analyzer versions `ast:c` and `cfg:c` bump 1 -> 2 (contracts
     `c-ast-v2`, `c-cfg-v2`); old states rebuild through the existing
     freshness rules. No tree-sitter-specific concept enters the public
     contract.

4. **Other potential parser leakage**: none found. `analysis_c_frontend.py`
   keeps the only raw access (it is the parser boundary by design);
   `analysis_c_ast.py` only persists the normalized tree; after this change
   `analysis_c_cfg.py` imports no parser module, holds no raw node and calls
   no parser API. A dedicated architecture test asserts this statically and
   by construction (builder runs on a hand-built normalized tree).

## Short-circuit design

`_condition_flow(expr, true_target, false_target, role)` lowers a condition
expression to CFG flow and returns its entry node:

- `a && b`: `a.true -> b-flow`, `a.false -> false_target`; `b.true ->
  true_target`, `b.false -> false_target`.
- `a || b`: `a.true -> true_target`, `a.false -> b-flow`; `b` decides.
- `cond ? t : f` in boolean position: `cond.true -> t-flow`,
  `cond.false -> f-flow`; both operand flows decide.
- leaf: one `condition` node (context role kept, e.g. `if_condition` /
  `loop_header`), `true`/`false` edges to the targets. Parentheses are only
  unwrapped when they group a boolean/ternary expression, so plain
  conditions keep their A2 spans.

Value positions decompose the same way with a common continuation node:
`x = a || b();` and `return a && b();` evaluate operands in order and both
paths reach the statement/return node; `x = c ? f() : g();` wires
`c.true -> f`, `c.false -> g`, both branches continue into the assignment
statement node. Evaluation order is exact; result values are not modeled
(data-flow remains out of scope). Loop headers use the same lowering, so
`while (a && b())` and `for (; a || b(); )` compose with existing
back/continue edges (continue targets the condition-flow entry / update).
`switch` conditions stay single dispatch nodes (multi-way, not boolean).

# C-ANALYSIS-A3 report

## Implemented

### Call-site semantic classification (foundation-level)

- The C extractor (`parsing/cpp.py`) now classifies every `call_expression`
  by callee shape and records it in the existing `raw_references` metadata
  JSON column — no schema change:
  - `call_kind`: `direct` (plain identifier / qualified identifier callee) or
    `indirect` (field access, pointer dereference `(*fp)()`, subscript
    `table[i]()`, nested call `get()()`, complex/missing — fail-closed).
  - `callee_shape`: `identifier` / `qualified` / `field_access` / `deref` /
    `subscript` / `nested_call` / `complex`.
  - Indirect detail: `callee_symbol` (pointer variable, receiver) and/or
    `field` (struct callback field) and/or `callee_expression`.
  - `argument_identifiers` plus post-resolved `function_reference_args`
    (bounded 4): syntax-level address-taken facts for callback registration
    (`register_cb(foo)`) — never converted into call edges.
- Callable-shape tracking (file-local, syntax-only): function-pointer
  typedefs, function-pointer parameters and locals (declarator chains with
  `parenthesized_declarator` + `pointer_declarator` under
  `function_declarator`, or a callable typedef). `h(x)` for a callable
  parameter/variable is `indirect` even when a same-named function exists.
- Classification is C-only; C++ retains its exact legacy extraction behavior
  (`callee_shape: "legacy"`).

### Resolver

- Indirect calls are never name-bound (`indirect_call_shape` strategy):
  no `CALLS` edge, no candidates, `target_id = null`. This removes the two
  pre-existing false-binding holes (`o.read(x)` -> global `read`;
  `h(x)` -> unrelated function `h`).
- Direct-looking calls keep the existing precedence (same-file unique ->
  qualified with unique definition -> unique repo-wide); ambiguity yields
  `status=ambiguous` with bounded candidate display (query layer caps at 8)
  and `resolved_target = null`.
- Every fresh reference records `call_kind_version: 1`; reference cache reuse
  is gated on that version, so full and incremental runs converge on
  identical classification after the upgrade (one-time re-resolution).
  Migration note: previously misbound indirect calls intentionally lose
  their erroneous CALLS edge; legitimate direct edges are unchanged.

### Foundation completeness fix (discovered by the real smoke)

- Calls inside variable initializers (`int x = f();`) were previously never
  walked and therefore invisible to the graph. The extractor now walks
  initializer values only (no duplicate type symbols), owned by the
  enclosing function rather than the declared variable.

### Query exposure

- `GraphQuery.get_call_semantics(symbol)` — bounded per-symbol view:
  `counts` by semantic class and compact `sites`
  (`file`, `line`, `call_kind`, `class`, `callee`, `target`, `candidates`≤8;
  sites≤16). `DIRECT_CALL = kind direct + resolved`; `UNRESOLVED_CALL =
  kind direct + unresolved`; `AMBIGUOUS = kind direct + ambiguous`;
  `INDIRECT_CALL = kind indirect` (always unresolved).
- `explain-symbol` includes the `call_semantics` section for the anchor.
- `symbol.callers` / `symbol.callees` keep their existing direct-edge
  contract; indirect/unresolved sites are visible through the new section
  and `raw_references` rows.

### AST / CFG alignment

- AST CALL node, CFG callsite node and the CALL semantic record share the
  same source span (byte-identical for AST/CFG; line-level for the
  reference), asserted by golden test on the same file/line/callee.

## Golden validation

CALL-01..15 (same-file direct, cross-file declaration direct, ambiguous
never bound, function-pointer parameter, local function pointer, `(*fp)()`,
`p->field()`, `o.field()`, unknown, callback registration/address-taken,
nested call, table subscript, macro-shaped unresolved, same-file static,
cross-file unique) with explicit negative precision tests:
function-pointer ≠ direct function (no edge to same-named global),
ambiguous name ≠ resolved (0 edges, 2 bounded candidates),
unknown call ≠ arbitrary target (0 candidates),
struct callback ≠ same-name global function (no edge, no candidates).

## Real C smoke (`tests/fixture_cpp/src/callbacks.c`)

General-purpose fixture (not case-specific):

```text
run_all          direct=2 (apply_callback, run_pipeline), unresolved=1 (unknown_helper)
apply_callback   indirect=1 (cb — function-pointer parameter)
run_pipeline     indirect=1 (pipe->step — struct callback)
ambiguous        0
false direct     0 (no indirect site has a CALLS edge or resolved target)
```

## Still not implemented

- Points-to, whole-program pointer analysis, indirect concrete target
  inference, assignment propagation (`h = foo` stays a non-binding
  syntax fact only at call arguments).
- Def-use, SSA, taint, data-flow, interprocedural CFG.
- Clang / `compile_commands.json` / macro expansion (macro-shaped calls stay
  `unresolved` unless a same-named function exists).
- Comma/GCC-statement-expression flow; C++ classification (legacy shape).
- Candidate metadata beyond bounded display; candidate_targets are
  "possibly related" and never equivalent to a resolved target.

## Capability matrix

| Language | AST | CFG | Call semantics |
| --- | --- | --- | --- |
| Python | supported | supported | existing resolver behavior |
| C (`.c`, `.h`) | supported | supported | direct/indirect/unresolved classification; high-confidence direct binding; precise indirect target resolution NOT supported |
| C++ | foundation behavior only | not implemented | legacy behavior unchanged |

`INDIRECT_CALL recognition ≠ points-to analysis`.

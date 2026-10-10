# C-ANALYSIS-A2 report

## Implemented

- Optional `cfg:c` analyzer with contract `c-cfg-v1`, built on the frozen A1
  normalized C AST boundary (`analysis_c_frontend.py` vocabulary, spans,
  structural paths, `ast:c` node identity).
- New `analysis_c_cfg.py`: per-function `CCfgBuilder` over the A1 normalized
  tree. Tree-sitter raw nodes are consulted only to resolve `for_statement`
  grammar fields (initializer/condition/update/body); raw kinds never enter
  the persisted schema.
- Reused unchanged: `AnalysisStore` persistence/freshness, `AnalysisNode`/
  `AnalysisEdge`, node/edge id algorithms, orchestrator merge, query surfaces,
  integrity checks, response limits, CLI plumbing.
- Language dispatch updated: `analysis_query.QUERY_ANALYZERS` adds
  `("cfg", "c") -> "cfg:c"`; the A1 `not_implemented` special case is removed.
- Orchestrator: C files are eligible for `cfg`; a C CFG request materializes
  its declared `ast:c` dependency (mirroring Python `cfg -> ast`); AST-only
  requests still never build CFG; default index/query never builds analysis.
- Fail-closed accounting: unresolved `goto`/`break`/`continue`, opaque ERROR
  regions and stray cases are counted in state metadata
  (`unresolved_gotos`, `unresolved_breaks`, `unresolved_continues`,
  `error_statements`, `stray_cases`) with `analysis_status: degraded`; files
  with zero CFG-able functions are skipped as `no_functions_built` (an honest
  N/A, e.g. prototype-only headers) and never persist an integrity-violating
  empty graph.

## C CFG V1 supported control structures

- Straight-line sequences; implicit exit (fallthrough to EXIT without
  `return`).
- `if` / `else` (incl. `else if` chains via nesting) with `true`/`false`
  branches and a branch merge.
- `while` (condition header, `true` -> body, body `back` -> condition,
  `false` -> exit), `do-while` (body runs first; body -> condition; condition
  `true` -> body), `for` (init -> condition -> body -> update -> condition;
  omitted init/condition/update handled, missing condition becomes a synthetic
  header; `continue` targets the update when present, else the condition).
- `break` -> nearest enclosing loop **or switch** exit; `continue` -> nearest
  enclosing loop target only.
- `return` -> EXIT (`return` edge); statements after `return` stay unreachable
  (no fabricated successors).
- `switch` / `case` / `default`: condition -> each case (`case`) /
  default (`default`); empty case bodies fall through from the case node;
  natural case-body ends fall through to the next case (`fallthrough`); the
  last case's open ends reach the switch exit; `break` exits the switch.
- `goto` / labels: label table per function; forward and backward gotos wired
  by kind `goto`; unresolved labels degrade the function instead of guessing a
  target.
- Every function gets exactly one ENTRY and one EXIT; `entry -> first` /
  open ends -> EXIT (`next`), returns -> EXIT (`return`).

## Edge kinds

Reuses the lowercase single-token kinds of the Python contract and adds the
same-style C kinds: `next`, `true`, `false`, `back`, `continue`, `break`,
`return` (existing) + `case`, `default`, `fallthrough`, `goto` (new). No
schema migration; semantics live in the existing `kind` column.

## Not implemented (explicit)

- Expression-level short-circuit CFG (`&&`, `||`, `?:` — a condition is one
  CFG condition node).
- Computed goto, `setjmp`/`longjmp`, inline-asm control flow, noreturn
  inference.
- Interprocedural CFG, call-target resolution (direct or indirect).
- Def-use, SSA, points-to, taint, PDG.
- Clang / `compile_commands.json` / macro-expanded semantic views.
- C++ deep analysis (unchanged foundation behavior only).

## Validation

- Golden fixtures CCFG-01..15 (sequence, if, if/else, while, do-while,
  for+continue-to-update, nested break/continue scoping, switch+break,
  fallthrough, default, forward goto, backward goto, multiple returns,
  implicit exit, unresolved label) with node/edge/kind/span assertions,
  plus determinism digest, AST/CFG coexistence with shared identity, ast-only
  builds no CFG, explain-symbol cfg, header function CFG, malformed fail-closed.
- Focused: 59/59 PASS (C CFG 21 + C AST 14 + analysis CLI + analysis query).
- Full regression: 199/199 PASS
  (`$env:PYTHONPATH='src'; python -m unittest discover -s tests -p 'test*.py'`),
  A1 baseline 178 + 21 new.

## Real C smoke (repo fixture `tests/fixture_cpp/src/plain.c`)

```text
file                     src/plain.c (committed 5ef47d9, pre-existing)
target function          add (1 function built)
AST nodes                31
CFG nodes                3 (entry, return, exit)
CFG edges                2 (entry -next-> return, return -return-> exit)
ENTRY/EXIT               present, single per function
analysis time            ~53 ms (ast,cfg scope run incl. dependencies)
query time               ~1.4 ms
returned bytes           2864 (cfg query envelope)
determinism              identical query digest across re-runs
explain-symbol cfg       available (entry_id/exit_id present)
```

`plain.c` is straight-line (its real shape: sequence + return); the full
control-flow matrix is covered by the golden fixtures. No compilation, no
`compile_commands.json`, no include resolution.

## Performance sanity

Single small function: C AST + C CFG build well under 100 ms including the
scope run; CFG query ~1.4 ms. No anomaly (>1 s) observed; no optimization
performed.

## Capability matrix

| Language | AST | CFG |
| --- | --- | --- |
| Python | supported | supported |
| C (`.c`, `.h`) | supported, optional normalized AST | supported (function-local V1) |
| C++ | current foundation behavior only | not implemented |

## Supersedes

The C CFG `not_implemented` boundary recorded in
`design-analysis-layer-c-a1-report.md` is superseded by this report. The A1
normalized AST contract itself is unchanged.

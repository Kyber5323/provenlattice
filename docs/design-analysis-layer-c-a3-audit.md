# C-ANALYSIS-A3 implementation audit

## Scope

Stable call-site semantic classification for C — `direct` / `indirect` /
`unresolved` — carried by the foundation graph so that no optional analysis
request is needed to know a call's kind. Precision-first: knowing the call
type beats guessing a target.

## Audit answers

1. **Where C CALLS come from**: `CppExtractor._call` (`parsing/cpp.py`) emits
   one `CALLS` `ParsedReference` per `call_expression` with
   `target = normalize(text(function_field))` and metadata
   `{"syntax": "call_expression"}`.
2. **Name matching**: yes. `ReferenceResolver._resolve_reference` tries
   same-file unique name (0.98), qualified name (0.99), import-based, then
   unique repo-wide name (0.75); multiple candidates -> `ambiguous`, none ->
   `unresolved`. `_select_candidate` only picks among identical signatures
   with exactly one definition — it never arbitrarily picks one of several
   distinct functions.
3. **Fields saved today**: `raw_name` (callee text), `start_line`/`end_line`,
   `target`, plus status/candidates/resolved id/strategy/provenance/
   confidence on the `RawReference`. No callee-shape information.
4. **Direct lookup**: same-file -> qualified -> imports -> unique repo name,
   as above.
5. **Same-name static/global functions**: same-file unique wins; otherwise
   multiple repo candidates -> `ambiguous` (no arbitrary binding). Same-file
   static and global with the same name in one file deduplicate through the
   extractor's declaration/definition merge.
6. **Function-pointer calls today**: `h(x)` where `h` is a callable parameter
   is indistinguishable from a function call — if any function named `h`
   exists it false-binds (same-file 0.98 or repo-unique 0.75). `(*fp)(x)`
   survives only because its text `(*fp)` never matches a name; `table[i](x)`
   and `get()()` likewise stay unresolved by accident of text shape.
7. **Struct callbacks today**: `o->read(x)` -> `ops->read` never name-matches
   (accidentally safe), but `o.read(x)` normalizes to `o.read` whose short
   name `read` can false-bind a global function `read`.
8. **Unresolved calls**: kept — every call emits a `raw_references` row
   (`status=unresolved`, provenance `tree_sitter_syntax`, raw string kept);
   none are dropped, none misbound except the two false-binding holes above.
9. **Normalized AST callee shape**: yes — the A1 `CALL` node keeps its callee
   subtree (first child `parser_kind`: `identifier`, `field_expression`,
   `parenthesized_expression`+`pointer_expression`, `subscript_expression`,
   nested `call_expression`), and CFG callsite nodes link to the same spans.
   But requiring AST/CFG to classify calls would violate the A3 architecture
   requirement, so classification belongs in the foundation extractor.
10. **Best carrier**: the foundation extractor writes `call_kind` +
    shape metadata into the existing `RawReference.metadata` JSON column —
    no schema change; the resolver honors the kind (never name-binds an
    indirect call); queries expose kind through `raw_references` and a new
    bounded `call_semantics` section on `explain-symbol`.

## Chosen compatibility strategy

- `raw_references` schema unchanged; classification rides in the existing
  JSON `metadata` column (`call_kind`, `callee_shape`, optional
  `callee_symbol`/`field`/`callee_expression`, `function_reference_args`).
- Every freshly written reference records `call_kind_version: 1`; reference
  cache reuse is gated on that version, so incremental and full index runs
  converge on identical classification after the upgrade (one-time
  re-resolution; explicit migration note in the report).
- Indirect calls are never resolved: no `CALLS` edge is created for them.
  Previously misbound indirect calls (`o.read(x)` -> global `read`,
  `h(x)` -> unrelated function `h`) intentionally lose their erroneous edge;
  every legitimate direct edge is unchanged.

## Call model

```text
call_kind (extractor, syntax shape)   x   status (resolver)
  direct                                  resolved    -> DIRECT_CALL
  direct                                  ambiguous   -> AMBIGUOUS (candidates, bounded)
  direct                                  unresolved  -> UNRESOLVED_CALL
  indirect                                unresolved  -> INDIRECT_CALL (never resolved)
```

Extractor callable-shape tracking (all syntax-level, file-local):
function-pointer typedefs (`typedef int (*handler_t)(int)`), function-pointer
parameters and locals (declarator chain contains `parenthesized_declarator`
+ `pointer_declarator` below `function_declarator`, or the declared type is a
callable typedef), plus callee shapes: identifier (direct-shaped unless the
name is a known callable variable), field access, dereference, subscript,
nested call, complex (fail-closed to indirect).

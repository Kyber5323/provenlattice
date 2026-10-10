# C-ANALYSIS-RELEASE-R1 integration audit

## Commit chain

Development branch `codex/c-analysis-a1` (base `7ed211f` = `main` /
`origin/main`) contains the complete, unsquashed capability chain:

```text
235ef4e  A1    optional C AST frontend + normalized analysis support
d0297dc  A2    optional C control-flow analysis
90db29f  A2.1  C short-circuit CFG semantics
61b5012  A3    C call-site semantic classification
ebecea0  A3.5  real zstd diagnostic (evidence only)
af6d66d  A3.6  C symbol targeting + compact analysis context
```

`git merge-base --is-ancestor` confirms every commit is in HEAD; the chain is
linear and continuous on top of `main` (`git log main..HEAD` = exactly these
six commits).

## Diff classification (`git diff --stat main...HEAD`, 30 files, +4917/−66)

1. **C AST**: `src/provenlattice/analysis_c_frontend.py`,
   `analysis_c_ast.py` (new), `analysis.py` (analyzer registry `ast:c`),
   `orchestrator.py` (dispatch/merge), `analysis_query.py` (language-aware
   analyzers).
2. **C CFG**: `analysis_c_cfg.py` (new).
3. **Short-circuit CFG**: inside `analysis_c_frontend.py` (`part_role`,
   `LOGICAL_AND`/`LOGICAL_OR`) + `analysis_c_cfg.py` (`_condition_flow` /
   `_value_flow`).
4. **Call semantics**: `parsing/cpp.py` (`call_kind`/`callee_shape`
   metadata, callable-shape tracking), `resolver.py` (indirect never
   name-bound, `call_kind_version` cache gate), `query.py`
   (`get_call_semantics`).
5. **Real-zstd diagnostic evidence**: `docs/design-analysis-layer-c-a35-zstd-diagnostic.md`
   (report only; no benchmark source, no case-specific code).
6. **Retrieval / compact context**: `query.py` (definition preference,
   `get_analysis_summary`), A3.6 audit/report docs.
7. **Tests**: `tests/test_c_ast_analysis.py`, `test_c_cfg_analysis.py`,
   `test_c_scfg_analysis.py`, `test_c_call_semantics.py`,
   `test_c_context_retrieval.py` (new), `test_analysis_cli.py` /
   `test_analysis_query.py` (updated expectations), fixture
   `tests/fixture_cpp/src/callbacks.c`.
8. **Docs**: A1/A2/A2.1/A3/A3.5/A3.6 audit+report docs, vision capability
   matrix, README updates.

## Forbidden-content check

- No benchmark-specific implementation (case ids appear only in the A3.5
  diagnostic report and release docs).
- No temporary DB, no raw zstd checkout, no Agent home in the tree.
- No secrets, no local absolute paths, no cache dirs, no `__pycache__`,
  no `.pyc` in the committed diff.
- Foundation graph schema, V1.2 frozen oracle, D9/C10 historical evidence
  untouched (`graph.py`, `identity.py`, storage schema unchanged).

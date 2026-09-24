# D9-C5 formal harness

Artifact namespace after authorization: `artifacts/formal/<cell_id>/` holds
immutable `raw/stream.jsonl`, `normalized-trace.json`, `review-packets/`,
independent reviewer submissions, evaluator output, and a cell record. The
root `formal-manifest.json` contains the 36 planned provenance records.

`formal_harness.py` schemas are: `d9-c5-formal-manifest-v1`,
`d9-c5-normalized-trace-v1`, `d9-c5-blinded-packet-v1`, and reviewer schema
`d9-c5-review-v1`. Qualification tests use synthetic strings only and never
open, checkout, or execute a frozen ContextBench case.

`formal_runner.py --config config.json --output artifacts/formal` first creates
the immutable plan. Its `--execute --cell-id <id>` path loads that plan, calls
the single `dispatch_cell` lifecycle, and accepts no caller overrides of cell
identity or runtime. Qualification uses a fake executor; the production adapter
must be explicitly installed before a formal run can start.

Each dispatch materializes `worktrees/<stable-cell-hash>/` as a detached, clean
checkout at the manifest base commit. Existing workspaces and either completed
or incomplete artifacts for the selected cell fail closed; completed artifacts
for other cells do not block a later cell.

`--materialize-only --manifest <frozen-manifest> --cell-id <id>` invokes the
same `prepare_cell_execution()` lifecycle used by dispatch, then writes a
readiness artifact under `readiness/materialization/`. It does not enter the
executor branch and never writes `cells/*/cell-record.json` or `incomplete.json`.

Every formal Git operation uses command-scoped `git -c safe.directory=<exact
canonical approved path>`. Source and worktree paths are independently
canonicalized and validated; the harness never writes global/system Git config
and never uses wildcard trust.

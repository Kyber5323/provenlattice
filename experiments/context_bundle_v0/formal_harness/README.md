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

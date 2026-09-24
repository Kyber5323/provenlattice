# Defect Candidate Review V1 — Stage 4

Stage 4 asks whether the sparse graph (Level-0 events + the stage 3 sparse CFG) is
enough to *propose* defects at all. The only honest way to answer that is to look
at the candidates: this package turns a graph database into a deterministic,
stratified sample of candidates with their evidence bundles and source text, and
leaves the verdicts blank for a human to fill in.

The number it produces — candidate precision — is V2's first quality baseline.
It is a measurement of the *candidate generator*, not of the graph's resolution
decisions, which are already reported by the fidelity packages.

## Scope

- Queries: `resource_lifetime` (4.1 leaks, 4.6 unreleased locks on the error
  path), `lock_order` (1.3 inversions, 1.4 re-entrant acquisition),
  `race_condition` (1.1)
- Strata: `repository x defect_key x resolution_status`, sampled round-robin so
  one family cannot eat the quota
- One sample across every repository: the target is the size of one
  adjudication, so passing a second `--database` does not double the sheet
- Population: every candidate the requested queries produce, capped at
  `population_limit_per_query`; `population_N` is recorded per stratum

Three states are the graph's: `resolved` means every relation the decisive
predicate needed was resolved in the graph, `ambiguous` means the predicate holds
but at least one input is a name-level proxy, `unresolved` means the predicate
holds with no identity to attach it to. They are never silently upgraded, and
they are not the annotator's conclusion.

## Build the review sheet

```bash
PYTHONPATH=src python experiments/defect_v1/build_review.py \
  --database redis50=D:/代码理解/测试代码仓/redis-50/.provenlattice/codegraph.db \
  --database llamacpp100=D:/代码理解/测试代码仓/llama.cpp-69/.provenlattice/codegraph.db \
  --root llamacpp100=D:/代码理解/测试代码仓/llama.cpp-69 \
  --type 4.1 --type 4.6 --type 1.3 --type 1.4 --type 1.1 \
  --sample 20 --seed provenlattice-defect-v1 \
  --output experiments/defect_v1/results/stage4
```

`--root` is there because the llama.cpp checkout was renamed after it was
indexed, and a database records the root it indexed: without the override every
snippet from that repository reads as `text: null` and the sheet silently stops
carrying the material a verdict needs. The repository *label* stays
`llamacpp100` so case ids do not move when a directory does; the recorded commit
is the identity. `sources[]` keeps the database's own `root_path` and adds
`root_path_used` and `root_override`, so a reader can see where the snippets
came from.

Two repositories, because 1.3's population is zero on both: the lock family is
represented in this sheet by 1.4 alone. `1.4` is requested as a type of its own
for the same reason — it is a key inside `lock_order`, so `--type lock_order`
would also work, but the recorded command names the key that has material.

`4.6` is named for the same reason: it is a key inside `resource_lifetime`, and
it is the key where the RAII exclusion does its work, so a sheet that omitted it
would leave the one filter designed to suppress false positives unmeasured.

The five keys are not equally represented in the population — 1.1 alone is
1,463,722 candidates on llama.cpp-100 against 4.1's 713 — which is why the
sample is stratified and round-robin rather than uniform. A uniform sample of
this pool would be a sheet about 1.1.

The database is opened through a `mode=ro` URI and the semantic tables are read
as raw rows fed straight to `defect.py`. It deliberately does not go through
`GraphQuery`: opening a `SQLiteStorage` runs `executescript(SCHEMA)` and a
backfill `INSERT`, which is right for a build and wrong for a review artifact.
Same database and same seed give a byte-identical `defect_review.json` and
`defect_review.md` — no timestamps, no durations, no machine-specific paths.

Each case records the candidate id (`E-DEFECT-...`, citable), its subject,
anchor, facts, uncertain facts, the paths the CFG walk confirmed, the source
spans, the source text padded by two lines, and the query's `missing_evidence`.
A missing repository root is recorded as `<no source: ...>` rather than skipped:
the spans and the bundle are still usable and the reader can see what is absent.

The JSON is the record and the Markdown is the reading copy, so building over a
sheet that already carries verdicts is **refused** rather than merely
discouraged: adjudication is the only thing in this package that costs human
hours, and re-sampling is cheap. `--overwrite` says to replace it on purpose.

After filling in verdicts, refresh the reading copy from the record — no
database, no re-sampling:

```bash
PYTHONPATH=src python experiments/defect_v1/build_review.py --render \
  experiments/defect_v1/results/stage4/defect_review.json
```

That writes `defect_review.md` beside the JSON, with each case's recorded
verdict rendered where the blank prompt was: `REVIEWED` with
`is_true_positive: null` reads as **uncertain**, and a case that has not been
looked at still reads as the blank prompt.

## Annotation protocol

Fill in the `annotation` block of each case in `defect_review.json`:

| field | meaning |
|---|---|
| `verdict` | the review *state*: `PENDING` → `REVIEWED` |
| `is_true_positive` | the judgment: `true` / `false` — is this a defect an engineer would act on? |
| `failure_reason` | why it is a false positive, from the closed list below |
| `reason` | one line of prose; `failure_reason` is the countable version |
| `needs_dfg` | `true` when a **data-flow** edge is the missing piece — the value's propagation, or two names that may be one object |
| `missing_capability` | what was missing (`declaration identity / scope`, `READ/WRITE events`, `RAII ownership`, `acquire-release contract`, `MAY_ALIAS`, ...) |
| `confidence` | the annotator's own confidence, not the graph's |
| `notes` | anything else |

`verdict` is a state and `is_true_positive` is the finding, so there are three
outcomes and not two. `REVIEWED` with `is_true_positive` left null means the
annotator looked and the sheet did not carry what the decision needed: that is
`uncertain`, it is counted in the denominator, and it is not `pending` — the
adjudication is finished and the case is a measurement of the instrument.
Precision is `tp / (tp + fp)`, so an `uncertain` case is excluded from the
ratio and visible in the table beside it.

`failure_reason` is a closed list because its whole value is being countable —
"eleven of the twenty were the same extractor mistake" is what turns a
precision number into a work item. A free-text `reason` reads well and counts
for nothing.

| `failure_reason` | meaning |
|---|---|
| `identity` | the name is not the object: scope, storage duration, declaration |
| `extractor` | the event itself is wrong or missing |
| `missing_read_write` | the access kind is a syntactic proxy |
| `alias` | two names may be one object |
| `ownership` | RAII, transfer, or a release in another frame |
| `contract` | the API's meaning for the resource, not the value flow |
| `call_argument_loss` | the resource was handed over as a later argument |
| `insufficient_context` | the sheet does not carry what the decision needs |
| `other` | |

`needs_dfg` and `missing_capability` are what answer stage 4's acceptance
question ③. They are per-case on purpose: "the graph needed a data-flow edge
here" is a fact about one candidate, and aggregating it into a yes/no would
throw away which capability was missing.

`needs_dfg` asks the narrow question — is a data-flow edge the piece that is
missing? — and not "were the events and the CFG enough on their own?". Every
false positive in this sheet fails the second reading, so that reading would
make the field a restatement of the verdict: the stage's central question,
whether the gap is data flow or something else, would come back as one number
that always says yes. Under the narrow reading the two fields split the false
positives into a DFG-shaped pile (`alias`, value propagation) and the piles a
DFG would not fix (`identity`, `extractor`, `ownership`, `contract`), and that
split is the argument against reading stage 4 as a single "we need DFG".

## Summarize

```bash
PYTHONPATH=src python experiments/defect_v1/build_review.py --summarize \
  experiments/defect_v1/results/stage4/defect_review.json
```

Prints `n`, `tp`, `fp`, `uncertain`, `pending`, `precision` and `needs_dfg` per
stratum and overall, followed by a count for every `failure_reason` in the
taxonomy — including the zeroes, because a reason with no cases is a finding
about which of the five gaps stage 4 actually hit. Precision is
`tp / (tp + fp)` over the judged cases only, and `n/a` — not `0.0` — when a
stratum has no verdicts yet: "no verdicts" and "every verdict was wrong" are
different findings. `uncertain` cases sit in the denominator and outside the
ratio. A stratum whose population is smaller than the sample target is reported
as it is; it is never backfilled from another family or repository.

The stage 4 baseline this produced — 20/20 false positives, precision `0.000`,
`needs_dfg` 0, `ownership` 10 / `identity` 6 / `contract` 2 / `extractor` 1 /
`call_argument_loss` 1 — is recorded with its caveats in `AGENTS.md`, under
"裁定结果（2026-09-22）". **Read it there before quoting the number**: with
twelve strata sharing twenty cases, only the overall figure means anything, and
it is a measurement of the candidate generator rather than of the queries.

## Known limits of the instrument

These are properties of the queries being measured, and they belong in the
report rather than in a footnote:

- **No READ/WRITE events.** Level 1 does not exist yet, so 1.1's access kind is a
  syntactic proxy (`subject_from == "assignment"` or ALLOC/RELEASE reads as a
  write) and `MAY_PARALLEL` is never resolvable. Every 1.1 candidate is
  `ambiguous` by construction, and a shared variable that produces no event at
  all (`counter++`) is invisible to the query. 1.1's recall within "names that
  appear in an event expression" is a real number; outside it, it is zero.
- **Lock identity is a name.** `m.lock()` on two objects named the same, a copy
  of a mutex, an aliased pointer — all invisible. The graph records how much of
  the designation the name is (`subject_exact`): `&g_index_lock` is the lock,
  `&index->slot_locks[i]` is only the struct that holds an array of them. A
  candidate whose subject is not exact is capped at `ambiguous` with a
  `SUBJECT_PATH` fact, because two elements of that array would otherwise read
  as one lock acquired twice. A lock recovered from a receiver or a declaration
  is capped the same way — unless the name it recovered is the whole thing.
- **A call names one subject, and it is the first one.** `CALL` carries the
  assignment target if there is one, else the first argument's root, else the
  receiver. So `p = f(q)` transfers `p` and `f(q)` transfers `q`, but
  `f(x, q)` transfers `x` and says nothing about `q` — a resource handed over
  as a later argument reads as still owned, and its leak candidate stays
  `resolved`. The bundle still names the ownership contract as missing.
- **Nothing knows a name's storage duration.** A local, a parameter, a
  function-scope `static`, a struct field and a file-scope global are all just
  names, so two methods that both have a local called `i` are two methods
  sharing `i`. This is 1.1's dominant false-positive shape and it is not a
  data-flow question: the declaration is in the syntax tree the parser already
  reads.
- **An anonymous `new` has no identity at all.** `sink.emplace_back(new X(k));`
  and `return new X(k);` allocate an object nothing binds, so the candidate is
  `unresolved`: the graph cannot attribute it, and whether it leaks depends on
  what consumes it. They are reported rather than suppressed — "an unowned
  allocation reaches the exit" is the honest reading of the graph — but telling
  them apart from real leaks needs the ownership edge, not a better name
  heuristic. Over half of llama.cpp-100's leak population is this shape.
- **Acquisition order is lexical.** Lock points are ordered by their per-method
  ordinal; the CFG only confirms that the first lock is still held at the second
  acquisition. Under branches and loops the lexical order is an approximation.
- **Vocabulary recall is not graph capability.** `zmalloc`/`zfree` are not in the
  shared vocabulary, so a repository's leak candidates are as limited by the
  keyword tables as by the graph. `points_by_type` in each query's coverage
  distinguishes the two; per-repository alias files are a later stage's decision.
- **`max_paths` truncation.** A leak whose first N paths are all clean is missed.
  It is reported as `recall_truncated` per query and per stratum, separately from
  `population_truncated`: the first says the walk behind the candidates stopped
  early, the second says this script's own cap made `population_N` a floor and
  turned the sample into a sorted prefix. Only the second is a statement about
  the sampling.
- **The semantic layer is base-snapshot only.** An active overlay's changes are
  not in these events or edges; the query layer says so per bundle and in
  coverage when a layer is active.

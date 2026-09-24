# D9-C5-B Context Bundle Reviewer Protocol Amendment

**Status:** frozen before formal harness implementation and before formal A/B runs  
**Decision:** `REVIEW_PROTOCOL_FROZEN`

## 1. Scope and provenance

This prospective amendment was created when formal runs were **0** and none of
the six frozen formal cases had been touched. It is not based on a formal
outcome. Its sole purpose is to operationalize the double-blind fact-consumption
review and adjudication already required by D9-C2 §5; every other D9-C2
experimental definition remains unchanged.

In particular, this amendment does not alter the frozen cases, A/B runtimes,
prompt, Bundle V0 schema or limits, fact identities, correctness gate, machine
retrieval-replacement or redundant-read semantics, 6 × 2 × 3 matrix, or result
classes.

## 2. Roles and blindness

Each returned fact in a B-arm run is reviewed independently by two
arm-blinded reviewers, **R1** and **R2**. Neither may see the other’s verdict
or record. **R3** is an adjudicator who acts only on an R1/R2 disagreement;
R3 cannot override an agreement.

Review packets must not expose an arm label, `A`/`B` label, runtime SHA,
directory or filename that identifies an arm, correctness/evaluator result,
formal aggregate, another-arm trace, gold/reference/test patch, or another
reviewer’s decision. A packet may include a blinded review ID, the task problem
statement, ordered relevant agent trace, task-context invocation and returned
facts, stable fact IDs, and later tool/reasoning context necessary to evaluate
use. The internal run ID is retained only outside the packet.

## 3. Unit, verdict, and meaning

The unit of review is **one returned fact × one B-arm run**. A bundle or a run
does not receive one substituted consumption verdict.

The only formal verdicts are `YES` and `NO`.

- `YES`: after task-context returned the fact, observable subsequent reasoning,
  tool selection, code navigation, patch action, or final explanation supplies
  sufficient trace evidence that this specific fact affected later behavior.
- `NO`: the trace lacks sufficient evidence that this specific fact was used.

This operationalizes D9-C2’s requirement that a trace or final
rationale/patch action demonstrably rely on the fact. A returned fact is not
therefore consumed; lack of a repeated Read is not consumption; a later
corresponding Read is not automatically `NO`; correctness success or failure
does not determine consumption; and machine replacement/redundant-read metrics
never replace the human verdict.

## 4. Audit reasons

Reasons document an already independent binary verdict and do not determine it.
One record may list more than one applicable reason.

| Verdict | Allowed reasons |
| --- | --- |
| `YES` | `EXPLICIT_REASONING_USE`, `TOOL_SELECTION_USE`, `CODE_NAVIGATION_USE`, `PATCH_ACTION_USE` |
| `NO` | `NO_OBSERVABLE_USE`, `FACT_DUPLICATED_BY_INDEPENDENT_RETRIEVAL`, `FACT_NOT_ACTIONABLE_IN_TRACE` |
| R3 disagreement | `EVIDENCE_INTERPRETATION_DISAGREEMENT` |

## 5. Independent records and finalization

Each R1/R2 submission is immutable and contains at least:

```text
blinded_review_id
fact_id
reviewer_role              # R1 or R2
verdict                    # YES or NO
reasons                    # zero or more allowed reasons
schema_version
submitted_at
evidence_note
trace_references
```

It must not contain an arm label. Required records cannot be backfilled from
machine metrics or another reviewer.

Finalization is mechanical:

| R1 | R2 | State / final verdict |
| --- | --- | --- |
| `YES` | `YES` | final `YES` |
| `NO` | `NO` | final `NO` |
| `YES` | `NO` (or reverse) | `ADJUDICATION_REQUIRED` |
| missing | any | `INCOMPLETE` |

For `ADJUDICATION_REQUIRED`, R3 receives the same blinded fact evidence and
records the two binary verdicts plus an independent final `YES` or `NO`. R3’s
record additionally contains `r1_verdict`, `r2_verdict`, `final_verdict`, and
the reason `EVIDENCE_INTERPRETATION_DISAGREEMENT`. R3 must not access
correctness, aggregate outcomes, or another arm’s performance. Missing R3 for
a disagreement remains `INCOMPLETE`. R3 cannot submit for an agreement.

## 6. Fact-level aggregation and completeness

For a run, `returned_fact_count` is the number of facts returned to the agent;
`consumed_fact_count` is the number with a finalized `YES`; and
`consumption_rate = consumed_fact_count / reviewed_returned_facts`. If no facts
were returned, consumption rate is `N/A`, never zero percent.

Human consumption, machine retrieval replacement, and machine redundant-read
remain distinct fields. The latter two may be mechanically calculated from the
normalized trace, but must not mutate a review record or its final verdict.

Formal aggregation is prohibited unless every returned fact has complete R1/R2
coverage and every disagreement has a valid R3 record. Any missing record,
unexpected reviewer role, unpermitted reason, duplicate submission, or review
record that reveals an arm produces `FORMAL_DATA_INCOMPLETE`.

## 7. Protocol version

The formal review schema is `d9-c5-review-v1`. Any later change requires a new
prospective amendment before formal runs begin; it cannot be introduced while
the 36-cell formal matrix is in progress.

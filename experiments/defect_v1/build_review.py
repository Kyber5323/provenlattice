"""Build the defect-candidate review sheet.

Stage 4's acceptance question was whether Event + sparse CFG is enough to
propose defects at all, and the only way to answer it is to look at the
candidates. This script turns a graph database into a deterministic, sampled
sheet of candidates with their evidence bundles and source text, and leaves the
verdicts blank. It never decides anything: precision comes from the human
annotations, and `--summarize` only does the arithmetic on them. Stage 6 runs
the same instrument over the eight-query pipeline and the same acceptance
protocol (人工核对 + precision 基线).

Two properties are load-bearing.

**Read-only, provably.** The database is opened through a `mode=ro` URI and the
semantic tables are read straight from storage, adapted to the form the
delivery path feeds `defect.py`. In particular this does not go through
`GraphQuery`: opening a `SQLiteStorage` runs `executescript(SCHEMA)` and a
backfill `INSERT`, which is correct for a build but is a write, and a review
package produced by a writer is a package whose numbers cannot be reproduced
from a frozen artifact.

**Deterministic.** Same database and same seed give a byte-identical JSON and
Markdown. Nothing here records a timestamp, a duration, or a path that varies
between machines; the sampling is a seeded shuffle over candidate ids, and the
strata are visited in sorted order.

**Delivery state, or its labelled control.** Stage 6's sheet must describe the
pipeline as delivered, so the assembly mirrors `query.get_defect_candidates`:
the ownership contracts are built next to the candidates and the parameter
join rides with them for `race_condition`, exactly as the query layer arms
them. It is re-implemented here rather than called, because `GraphQuery` is
the one writer in the path (see above). `--no-contracts` turns both off in one
move -- the same single dial the query layer keeps -- and the sheet records
which column it is in its `sampling` block.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

from provenlattice import contracts, defect
from provenlattice.sparsecfg import materialize_points
from provenlattice.sparsedfg import bind_parameters

REVIEW_ID = "provenlattice-defect-v1-review"
REVIEW_STATUS = "DEFECT_REVIEW_READY_FOR_ANNOTATION"
CLAIM = "NO_PRECISION_VERDICT_YET"
# The strata denominators have to be the real counts, so this is a memory
# guard and not a sampling device: it sits far above any population measured
# so far. A query that hits it is reported as truncated on the source and on
# every stratum, because the sample would then be drawn from a sorted prefix
# of the population rather than from the population.
POPULATION_LIMIT = 1_000_000
SNIPPET_PADDING = 2
# Why a candidate was wrong, as a closed list. Stage 4's answer to "is the
# sparse graph enough" is a list of missing capabilities, and the only way that
# list is worth anything is if the twenty verdicts roll up into it: a free-text
# `reason` reads well and counts for nothing.
FAILURE_REASONS = (
    "identity",            # the name is not the object: scope, storage, declaration
    "extractor",           # the event itself is wrong or missing
    "missing_read_write",  # the access kind is a syntactic proxy
    "alias",               # two names may be one object
    "ownership",           # RAII, transfer, or a release in another frame
    "contract",            # the API's meaning for the resource, not the value flow
    "call_argument_loss",  # the resource was handed over as a later argument
    "insufficient_context",  # the sheet does not carry what the decision needs
    "other",
)


def _open_read_only(database: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{database.resolve().as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _events(connection: sqlite3.Connection, repo_id: str) -> list[dict]:
    """The persisted event rows, in the form the delivery path feeds `defect.py`.

    The adapter mirrors the storage readback (`storage.semantic_events`): the
    same columns, with `flags` decoded to a tuple and `metadata` to a dict.
    That decoding is not an implementation detail -- the delivered query layer
    hands `defect.py` decoded rows, and a sheet whose input form differs from
    the delivery path's would not be describing the delivery path. Renaming
    `id` to `event_id` is the rest of the adapter.
    """
    return [
        {
            "event_id": row["id"], "event_type": row["event_type"],
            "owner_symbol_id": row["owner_symbol_id"], "file_id": row["file_id"],
            "ordinal": row["ordinal"], "start_line": row["start_line"],
            "end_line": row["end_line"], "matched_name": row["matched_name"],
            "matched_via": row["matched_via"],
            "flags": tuple(json.loads(row["flags"])),
            "metadata": json.loads(row["metadata"]),
        }
        for row in connection.execute(
            "SELECT * FROM semantic_events WHERE repo_id = ? ORDER BY id", (repo_id,)
        )
    ]


def _edges(connection: sqlite3.Connection, repo_id: str) -> list[dict]:
    return [
        {
            "edge_id": row["id"], "src_event_id": row["src_event_id"],
            "dst_event_id": row["dst_event_id"], "relation": row["relation"],
            "owner_symbol_id": row["owner_symbol_id"], "file_id": row["file_id"],
            "flags": tuple(json.loads(row["flags"])),
            "metadata": json.loads(row["metadata"]),
        }
        for row in connection.execute(
            "SELECT * FROM semantic_edges WHERE repo_id = ? ORDER BY id", (repo_id,)
        )
    ]


def _names(connection: sqlite3.Connection, repo_id: str) -> dict[str, str]:
    return {
        row["id"]: row["qualified_name"]
        for row in connection.execute(
            "SELECT id, qualified_name FROM nodes WHERE repo_id = ?", (repo_id,)
        )
    }


def _references(connection: sqlite3.Connection, repo_id: str) -> list[dict]:
    """The CALLS rows the contract table and the taint query resolve through.

    The query layer reads this table twice -- `_contracts` without
    `target_module`, `_taint_references` with it -- but the column is extra
    data on the same rows, not a different table, and one read keeps this
    script's shape flat.
    """
    return [
        {
            "file_id": row["file_id"], "owner_symbol_id": row["owner_symbol_id"],
            "start_line": row["start_line"], "raw_name": row["raw_name"],
            "target_module": row["target_module"],
        }
        for row in connection.execute(
            "SELECT file_id, owner_symbol_id, start_line, raw_name, target_module "
            "FROM raw_references WHERE repo_id = ? AND reference_type = 'CALLS'",
            (repo_id,),
        )
    ]


def _signatures(connection: sqlite3.Connection, repo_id: str) -> dict[str, str]:
    """Symbol id to signature, for the layer that splits parameter lists."""
    return {
        row["id"]: row["signature"]
        for row in connection.execute(
            "SELECT id, signature FROM nodes WHERE repo_id = ? "
            "AND signature IS NOT NULL AND signature != ''",
            (repo_id,),
        )
    }


def _run(events: list[dict], edges: list[dict], names: dict[str, str], types: list[str],
         identity: bool = True, *, table=None, references=None, signatures=None):
    """Every requested query, as `(candidates, coverage, limits_by_query)`.

    The key-to-query mapping is `defect.query_for`, so the matrix file stays
    the single source of truth for which query serves both keys. `all` is
    every query; a query name is itself; a matrix key filters its query's
    output, because one query can serve two keys (`resource_lifetime` is both
    4.1 and 4.6).

    Filtering *here* is safe because this script never truncates: it asks for
    `POPULATION_LIMIT` and filters what comes back. The query layer has to push
    the same filter inside itself, where truncation happens -- see
    `query.get_defect_candidates`.

    `identity=False` joins on the subject's spelling, which is stage 4's
    behaviour. It is what makes the four-column comparison readable: the same
    database read both ways separates the extraction changes from the identity
    changes, and on an old database it must reproduce the old sheet exactly.

    `limits` keeps two truncations apart, because they mean different things
    about the same stratum and one of them is not a population fact at all:

    - `recall_truncated` is the query's own flag, verbatim. Whatever caused it,
      candidates may be missing, so the population is a floor in that weaker
      sense. On a real repository the usual cause is `max_paths`: a leak whose
      first eight paths are all clean is not reported.
    - `population_capped` is this script's own cap, checked independently --
      the query returned already-capped output, so `population_N` is a floor
      *and* the sample is drawn from a sorted prefix of the population rather
      than from the population. That is a statement about the sheet's own
      sampling, and only it belongs on a stratum.
    """
    selected: dict[tuple[str, str, str], defect.Candidate] = {}
    coverage: dict[str, dict] = {}
    limits: dict[str, dict] = {}
    for defect_type in types:
        if defect_type == "all":
            queries, keep = defect.QUERY_NAMES, None
        elif defect_type in defect.QUERY_NAMES:
            queries, keep = (defect_type,), None
        else:
            queries, keep = (defect.query_for(defect_type),), {defect_type}
        for query in queries:
            # The delivery assembly, query by query: the parameter join rides
            # the contracts switch and only the race query consumes it; the
            # taint query's two ends are callee names a CALL event does not
            # carry. `run` hands each query only the parameters its signature
            # accepts -- it passes on everything, filtered per query.
            bindings = None
            if table is not None and query == defect.RACE_CONDITION:
                bindings = bind_parameters(
                    materialize_points(events, edges), callees=table,
                    signatures=signatures, names=names,
                )
            taint_references: list = []
            taint = None
            if query == defect.TAINT_PATH:
                taint_references = references or []
                taint = defect.load_taint()
            result = defect.run(
                query, events, edges, names=names, max_hops=64, max_paths=8,
                max_candidates=POPULATION_LIMIT, identity=identity,
                contracts=table, bindings=bindings,
                references=taint_references, taint=taint,
            )
            coverage[query] = result.coverage
            limits[query] = {
                "recall_truncated": result.truncated,
                "population_capped": len(result.candidates) >= POPULATION_LIMIT,
            }
            for candidate in result.candidates:
                if keep is None or candidate.defect_key in keep:
                    key = (candidate.defect_key, candidate.anchor, candidate.discriminator)
                    selected[key] = candidate
    return sorted(selected.values(), key=defect.Candidate.sort_key), coverage, limits


def _stratified(cases: list[tuple[str, defect.Candidate]], sample: int, seed: str) -> list:
    """Round-robin across `repository x defect_key x resolution_status`.

    The target is the size of one adjudication, not one per repository, so a
    repository where one family has hundreds of candidates and another has two
    does not produce a sheet about the first family only: every stratum with
    material contributes before any stratum contributes twice. Within a
    stratum the order is a seeded shuffle, so the sheet is reproducible without
    being ordered by anything meaningful.
    """
    strata: dict[tuple[str, str, str], list] = {}
    for repository, candidate in cases:
        key = (repository, candidate.defect_key, candidate.resolution_status)
        strata.setdefault(key, []).append(candidate)
    if not strata:
        return []
    for (repository, defect_key, status), rows in strata.items():
        rows.sort(key=lambda candidate: (candidate.anchor, candidate.discriminator))
        random.Random(f"{seed}:{repository}:{defect_key}:{status}").shuffle(rows)

    # Ordered so that every repository contributes its first stratum before any
    # repository contributes its second. Plain sorted order is repository-major,
    # so a target smaller than the stratum count would spend the whole quota on
    # whichever repository sorts first, and the sheet would be about one
    # repository while claiming to be about all of them.
    by_repository = {
        repository: sorted(key for key in strata if key[0] == repository)
        for repository in sorted({key[0] for key in strata})
    }
    order = [
        keys[rank]
        for rank in range(max(len(keys) for keys in by_repository.values()))
        for keys in by_repository.values()
        if rank < len(keys)
    ]
    selected: list[tuple[str, defect.Candidate]] = []
    while len(selected) < sample and any(strata[key] for key in order):
        for key in order:
            if strata[key] and len(selected) < sample:
                selected.append((key[0], strata[key].pop()))
    return selected


def _git(where: Path, *argv: str) -> str:
    try:
        completed = subprocess.run(
            ["git", *argv], cwd=where, capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return completed.stdout.strip() if completed.returncode == 0 else ""


def _code_version(workspace: Path) -> dict:
    """What produced these candidates, including whether the tree was dirty.

    Stage 4's baseline is measured from a working tree, not a release, and a
    precision number whose code version is unknown cannot be compared with
    anything later.
    """
    return {
        "commit": _git(workspace, "rev-parse", "HEAD") or "unknown",
        "dirty": bool(_git(workspace, "status", "--porcelain")),
    }


def _repository_state(root: Path | None, recorded: str,
                      database: Path | None = None) -> tuple[str, bool | None]:
    """Which revision of the *reviewed* code produced these candidates.

    `repositories.metadata` is where the pipeline would record it, and it is
    empty in practice, so the repository's own HEAD is asked for instead: a
    precision baseline that cannot be attributed to a revision of the code it
    measured is not a baseline. Dirtiness is `None` when the question could not
    be asked at all (no root, no git), which is not the same answer as `False`.

    The index directory is excluded from the dirtiness question when it sits
    under the root, because the tool wrote it and the reviewed code did not.
    Without that, every run reports `dirty` for its own artifact and the column
    stops distinguishing anything -- which is the one thing it is for.
    """
    if recorded:
        return recorded, None
    if root is None:
        return "unknown", None
    commit = _git(root, "rev-parse", "HEAD")
    if not commit:
        return "unknown", None
    pathspec: list[str] = []
    if database is not None:
        try:
            artifact = database.resolve().relative_to(root.resolve()).parts[0]
        except (ValueError, OSError):
            pass
        else:
            pathspec = ["--", ".", f":(exclude){artifact}"]
    return commit, bool(_git(root, "status", "--porcelain", *pathspec))


def _span_bounds(citation: str) -> tuple[str, int, int] | None:
    path, _, span = citation.rpartition(":")
    start, _, end = span.partition("-")
    if not (path and start.isdigit() and end.isdigit()):
        return None
    return path, int(start), int(end)


def _snippets(candidate: defect.Candidate, root: Path | None) -> list[dict]:
    """The source text behind each citation, padded by two lines.

    A missing root is recorded rather than skipped: the sheet stays usable
    (the spans and the evidence bundle are still there) and the reader can see
    exactly which part is absent.
    """
    snippets = []
    for citation in candidate.source_evidence:
        bounds = _span_bounds(citation)
        if bounds is None:
            snippets.append({"span": citation, "text": None, "note": "unparsable span"})
            continue
        path, start, end = bounds
        if root is None:
            snippets.append({"span": citation, "text": None, "note": "repository root unknown"})
            continue
        source = root / path
        try:
            lines = source.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as error:
            snippets.append({"span": citation, "text": None, "note": f"unreadable: {error.strerror}"})
            continue
        first = max(1, start - SNIPPET_PADDING)
        last = min(len(lines), end + SNIPPET_PADDING)
        snippets.append({
            "span": citation, "first_line": first, "last_line": last,
            "text": "\n".join(lines[first - 1 : last]),
        })
    return snippets


def _case(repository: str, candidate: defect.Candidate, evidence_id: str,
          root: Path | None) -> dict:
    digest = hashlib.sha256(evidence_id.encode()).hexdigest()[:16]
    return {
        "case_id": f"DFV1-{repository}-{digest}",
        "repository": repository,
        "defect_key": candidate.defect_key,
        "query": candidate.query,
        "resolution_status": candidate.resolution_status,
        "confidence": candidate.confidence,
        "subject": dict(candidate.subject),
        "candidate_id": evidence_id,
        "anchor": candidate.anchor,
        "discriminator": candidate.discriminator,
        "facts": [dict(fact) for fact in candidate.facts],
        "uncertain_facts": [dict(fact) for fact in candidate.uncertain_facts],
        "paths": [dict(path) for path in candidate.paths],
        "source_spans": list(candidate.source_evidence),
        "source_snippets": _snippets(candidate, root),
        "missing_evidence": list(candidate.missing_evidence),
        # Blank on purpose, and with the two fields that answer stage 4's
        # acceptance question ③ spelled out: whether the annotator would have
        # needed a data-flow graph to decide, and which capability was missing.
        #
        # `failure_reason` is a closed list, because its whole value is being
        # countable: "eleven of the twenty were the same extractor mistake" is
        # what turns a precision number into a work item. `verdict` stays the
        # review *state*; the judgment is `is_true_positive`, and leaving it
        # null with `verdict: REVIEWED` means the annotator looked and could
        # not tell -- which is a finding, not a missing value.
        #
        # `needs_dfg` asks whether a *data-flow* edge is the missing piece, not
        # whether the events and the CFG were enough on their own: every false
        # positive here fails the second reading, so counting that would make
        # the field a restatement of the verdict and the stage's central
        # question -- whether the gap is data flow or something else -- would
        # come back as one number that always says yes.
        "annotation": {
            "verdict": "PENDING",
            "is_true_positive": None,
            "failure_reason": None,
            "reason": None,
            "needs_dfg": None,
            "missing_capability": None,
            "confidence": None,
            "annotator": None,
            "notes": None,
        },
    }


def build_review(databases: dict[str, Path], types: list[str], sample: int, seed: str,
                 workspace: Path, roots: dict[str, Path] | None = None,
                 identity: bool = True, use_contracts: bool = True) -> dict:
    """One sheet, sampled once across every repository.

    The sample target is the size of one adjudication, so it is spent across
    the whole pool rather than per repository: sampling per repository makes
    the sheet's size a function of how many repositories were passed in, which
    is a property of the command line and not of the code being reviewed.
    """
    availability: list[dict] = []
    coverage: dict[str, dict] = {}
    sources: list[dict] = []
    pool: list[tuple[str, defect.Candidate]] = []
    context: dict[str, dict] = {}
    for repository, database in sorted(databases.items()):
        # `sqlite3.Connection` as a context manager commits a transaction; it
        # does not close, and a connection left open holds a Windows lock on
        # the file. `closing` is what actually releases it.
        with closing(_open_read_only(database)) as connection:
            repository_row = connection.execute("SELECT * FROM repositories LIMIT 1").fetchone()
            if repository_row is None:
                raise ValueError(f"{database} holds no repository row")
            repo_id = repository_row["repo_id"]
            generation = int(repository_row["current_generation"])
            metadata = json.loads(repository_row["metadata"] or "{}")
            # The database records where the tree stood when it was indexed.
            # That path is the right default and the wrong authority: a tree
            # that has been moved since indexing is still the tree the graph
            # describes, and reading its source out of the recorded path would
            # silently turn every snippet into a missing one. `--root` says
            # where the sources are now, and the row records both.
            recorded_root = repository_row["root_path"]
            override = (roots or {}).get(repository)
            root_path = str(override) if override is not None else recorded_root
            root = Path(root_path) if root_path and Path(root_path).is_dir() else None
            events, edges = _events(connection, repo_id), _edges(connection, repo_id)
            names = _names(connection, repo_id)
            references = _references(connection, repo_id)
            signatures = _signatures(connection, repo_id)

        # The ownership contracts, built the way the query layer builds them:
        # the CALLS rows for the callee names, the signatures for the parameter
        # ordinals, and the declared table on disk -- a missing or unreadable
        # declared table is an empty one rather than an error, there as here.
        table = None
        if use_contracts:
            table = contracts.build(
                events, edges, references=references, names=names,
                signatures=signatures, declared=contracts.load_declared(),
            )
        candidates, query_coverage, limits_by_query = _run(
            events, edges, names, types, identity=identity,
            table=table, references=references, signatures=signatures,
        )
        coverage[repository] = query_coverage
        commit, dirty = _repository_state(root, str(metadata.get("commit") or ""), database)
        context[repository] = {
            "repo_id": repo_id, "generation": generation, "commit": commit, "root": root,
        }
        sources.append({
            "repository": repository, "database": str(database), "repo_id": repo_id,
            "generation": generation, "root_path": recorded_root,
            "root_path_used": root_path, "root_override": override is not None,
            "root_available": root is not None,
            "repository_commit": commit, "repository_dirty": dirty,
            "events": len(events), "edges": len(edges),
            "candidates": len(candidates), "limits": limits_by_query,
        })
        pool.extend((repository, candidate) for candidate in candidates)

        for query in sorted(limits_by_query):
            if limits_by_query[query]["population_capped"]:
                print(
                    f"warning: {repository}: {query} hit the {POPULATION_LIMIT} candidate "
                    "population limit, so population_N is a floor and the sample is drawn "
                    "from a sorted prefix of the population rather than from the population",
                    file=sys.stderr,
                )

        strata: dict[tuple[str, str], int] = {}
        for candidate in candidates:
            key = (candidate.defect_key, candidate.resolution_status)
            strata[key] = strata.get(key, 0) + 1
        for (defect_key, status), population in sorted(strata.items()):
            limits = limits_by_query.get(defect.query_for(defect_key), {})
            availability.append({
                "repository": repository, "defect_key": defect_key,
                "resolution_status": status, "population_N": population, "sample_n": 0,
                # Per stratum, not per repository: a truncated 1.1 says nothing
                # about whether 4.1's population is complete, and flagging both
                # would make the caveat unreadable.
                "population_truncated": bool(limits.get("population_capped", False)),
                # The query's own flag, on the stratum too, because it is a
                # different caveat: the population is exact and the candidates
                # in it are the ones the walk found.
                "recall_truncated": bool(limits.get("recall_truncated", False)),
            })

    selected = _stratified(pool, sample, seed)
    picked: dict[tuple[str, str, str], int] = {}
    for repository, candidate in selected:
        key = (repository, candidate.defect_key, candidate.resolution_status)
        picked[key] = picked.get(key, 0) + 1
    for item in availability:
        item["sample_n"] = picked.get(
            (item["repository"], item["defect_key"], item["resolution_status"]), 0
        )

    cases: list[dict] = []
    for repository, candidate in selected:
        source = context[repository]
        evidence = defect.candidate_evidence(
            candidate, repository=source["repo_id"], commit=source["commit"],
            generation=source["generation"],
        )
        cases.append(_case(repository, candidate, evidence.evidence_id, source["root"]))

    return {
        "review_id": REVIEW_ID,
        "schema_version": 1,
        "status": REVIEW_STATUS,
        "claims": CLAIM,
        "seed": seed,
        "code_version": _code_version(workspace),
        "sampling": {
            "types": list(types),
            "sample_target": sample,
            "sample_selected": len(selected),
            "strata": "repository x defect_key x resolution_status, round-robin, "
                      "one sample across all repositories",
            "population_limit_per_query": POPULATION_LIMIT,
            "snippet_padding_lines": SNIPPET_PADDING,
            # Which column of the stage 4.5 comparison this sheet is. Two
            # sheets built from the same database and different settings differ
            # in their populations, and a record that cannot say which
            # question it answered is a record that will be compared wrongly.
            "subject_join": "identity" if identity else "spelling",
            # Which column of the contract comparison: the delivered pipeline,
            # or the same code with the ownership rulings and the parameter
            # join off in one move -- the dial stage 5C froze as a control.
            "contracts": "on" if use_contracts else "off (--no-contracts control)",
        },
        "sources": sources,
        "availability": availability,
        "coverage": coverage,
        "cases": cases,
    }


def _recorded(value) -> str:
    """How a recorded field reads inside backticks: JSON's words, not Python's."""
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    return str(value)


def _quoted(value: str) -> str:
    """A code span that survives a value containing backticks.

    An annotation's `reason` cites code, so it contains backticks, and a single
    delimiter would be closed by the first one. The delimiter has to be longer
    than any run in the value and the value has to be padded, or the content's
    own backticks merge into the delimiter and no span forms at all.
    """
    if "`" not in value:
        return f"`{value}`"
    ticks = "`"
    while ticks in value:
        ticks += "`"
    return f"{ticks} {value} {ticks}"


def _verdict_lines(annotation: dict) -> list[str]:
    """The recorded verdict, or the blank prompt while it is still `PENDING`.

    The JSON is the record and the Markdown is the reading copy, so the copy
    renders what was written rather than asking the same question again. A
    `REVIEWED` case with no judgment reads as *uncertain*, which is a verdict of
    its own and the one state the reading copy would otherwise lose.
    """
    if str(annotation.get("verdict") or "").upper() != "REVIEWED":
        return [
            "Verdict (fill in `defect_review.json`): `verdict` = `REVIEWED`, "
            "`is_true_positive` = ____ (`true` / `false` / `null`), "
            "`failure_reason` = ____ (closed list), "
            "`needs_dfg` = ____ (is a data-flow edge the missing piece?), "
            "`missing_capability` = ____, `reason` = ____",
        ]
    judgment = {
        True: "**true positive**",
        False: "**false positive**",
        None: "**uncertain** — looked, and the sheet did not carry what the "
              "decision needed",
    }[annotation.get("is_true_positive")]
    lines = ["Verdict (recorded in `defect_review.json`):", ""]
    for field in ("verdict", "is_true_positive", "failure_reason", "needs_dfg",
                  "missing_capability", "confidence", "reason", "annotator", "notes"):
        value = _quoted(_recorded(annotation.get(field)))
        if field == "is_true_positive":
            value += f" — {judgment}"
        lines.append(f"- `{field}`: {value}")
    return lines


def _render(package: dict) -> str:
    """The same sheet as Markdown, for reading rather than for parsing."""
    lines = [
        "# Defect candidate review",
        "",
        f"- review id: `{package['review_id']}`",
        f"- status: `{package['status']}` / claims `{package['claims']}`",
        f"- seed: `{package['seed']}`",
        f"- code: `{package['code_version']['commit']}`"
        f"{' (dirty working tree)' if package['code_version']['dirty'] else ''}",
        f"- subject join: `{package['sampling']['subject_join']}`",
        f"- contracts: `{package['sampling']['contracts']}`",
        f"- sampling: {package['sampling']['strata']}, target "
        f"{package['sampling']['sample_target']} "
        f"(selected {package['sampling']['sample_selected']})",
        "",
        "Verdicts are recorded in the JSON file's `annotation` block:",
        "",
        "| field | what it is |",
        "|---|---|",
        "| `verdict` | the review *state*: `PENDING` → `REVIEWED` |",
        "| `is_true_positive` | the judgment: `true` / `false`, or `null` with "
        "`verdict: REVIEWED` for **uncertain** — you looked and the sheet did not "
        "carry what the decision needed. Counted separately, not as pending |",
        "| `failure_reason` | why it is a false positive, from the closed list "
        "(`identity` / `extractor` / `missing_read_write` / `alias` / `ownership` / "
        "`contract` / `call_argument_loss` / `insufficient_context` / `other`) |",
        "| `needs_dfg` | is a **data-flow** edge the piece that is missing — the "
        "value's propagation, or two names that may be one object? If what is "
        "missing is identity, an event, ownership or an API contract, this is "
        "`false` and `missing_capability` says which |",
        "| `missing_capability` | what was missing, in words |",
        "| `reason` / `confidence` / `annotator` / `notes` | free text, optional |",
        "",
        "`needs_dfg` and `missing_capability` are what answer stage 4's "
        "acceptance question ③, and they are two different questions: a false "
        "positive caused by a name that is not the object is not fixed by a "
        "data-flow edge, and counting it as one would be the mistake this stage "
        "exists to avoid.",
        "",
        "## Sources",
        "",
        "| repository | database | generation | reviewed code | candidates | limits | root |",
        "|---|---|---|---|---|---|---|",
    ]
    for source in package["sources"]:
        # `recall` is the query's own flag (usually `max_paths` on the path
        # walk); `population` is this script's cap, which is the one that makes
        # the sample a prefix rather than a sample.
        limits = ", ".join(
            f"{query}={'population' if item['population_capped'] else 'recall' if item['recall_truncated'] else 'ok'}"
            for query, item in sorted(source["limits"].items())
        )
        dirty = source["repository_dirty"]
        note = " (dirtiness unknown)" if dirty is None else " (dirty)" if dirty else ""
        reviewed = source["repository_commit"][:12] + note
        lines.append(
            f"| {source['repository']} | `{source['database']}` | {source['generation']} "
            f"| `{reviewed}` | {source['candidates']} | {limits} "
            f"| {'yes' if source['root_available'] else '**missing**'}"
            f"{' (override)' if source.get('root_override') else ''} |"
        )
    lines += ["", "## Availability", "",
              "| repository | key | status | population_N | sample_n | population capped | recall truncated |",
              "|---|---|---|---|---|---|---|"]
    for item in package["availability"]:
        lines.append(
            f"| {item['repository']} | {item['defect_key']} | {item['resolution_status']} "
            f"| {item['population_N']} | {item['sample_n']} "
            f"| {'**yes**' if item['population_truncated'] else 'no'} "
            f"| {'yes' if item['recall_truncated'] else 'no'} |"
        )
    lines += ["", "## Cases", ""]
    for case in package["cases"]:
        lines += [
            f"### {case['case_id']}",
            "",
            f"- type: `{case['defect_key']}` / `{case['query']}`",
            f"- status: `{case['resolution_status']}` (confidence {case['confidence']})",
            f"- subject: `{case['subject'].get('name', '')}` "
            f"in `{case['subject'].get('method', '')}`",
            f"- candidate: `{case['candidate_id']}`",
            f"- anchor: `{case['anchor']}`",
            f"- source: {', '.join(f'`{span}`' for span in case['source_spans'])}",
            "",
            "Facts:",
            "",
        ]
        for fact in case["facts"]:
            lines.append("- " + ", ".join(f"`{key}`={fact[key]!r}" for key in sorted(fact)))
        lines += ["", "Uncertain facts:", ""]
        for fact in case["uncertain_facts"]:
            lines.append("- " + ", ".join(f"`{key}`={fact[key]!r}" for key in sorted(fact)))
        lines += ["", "Missing evidence:", ""]
        lines += [f"- {item}" for item in case["missing_evidence"]]
        for snippet in case["source_snippets"]:
            # The fence language comes from the file itself: the sheet covers
            # whatever the repository is written in, not C specifically.
            suffix = Path(snippet["span"].rpartition(":")[0]).suffix.lstrip(".")
            lines += ["", f"```{suffix}", snippet["text"] if snippet["text"] is not None
                      else f"<no source: {snippet.get('note', 'unavailable')}>", "```"]
        lines += ["", *_verdict_lines(case["annotation"]), ""]
    return "\n".join(lines).rstrip() + "\n"


def summarize(path: Path) -> dict:
    """Precision from the recorded verdicts, by stratum and overall.

    Nothing here is hand-written. `precision` is `None` rather than `0.0` when
    no case in a stratum has been judged yet: "no verdicts" and "every verdict
    was wrong" are different findings.

    Three states, not two. `pending` is "not looked at yet"; `uncertain` is
    "looked at, and the sheet does not carry what the decision needs" -- a
    verdict of its own, and one that belongs in the baseline rather than in the
    denominator. Both are outside `precision`, and neither is reported as the
    other.
    """
    package = json.loads(path.read_text(encoding="utf-8"))
    strata: dict[tuple[str, str], dict] = {}
    for case in package["cases"]:
        key = (case["defect_key"], case["resolution_status"])
        row = strata.setdefault(
            key,
            {"n": 0, "tp": 0, "fp": 0, "pending": 0, "uncertain": 0,
             "needs_dfg": 0, "reasons": [], "failure_reasons": {}},
        )
        row["n"] += 1
        annotation = case["annotation"]
        judged = annotation.get("is_true_positive")
        if judged is True:
            row["tp"] += 1
        elif judged is False:
            row["fp"] += 1
        elif str(annotation.get("verdict") or "").upper() == "REVIEWED":
            row["uncertain"] += 1
        else:
            row["pending"] += 1
        if annotation.get("needs_dfg") is True:
            row["needs_dfg"] += 1
        if annotation.get("reason"):
            row["reasons"].append({"case_id": case["case_id"], "reason": annotation["reason"]})
        failure = annotation.get("failure_reason")
        if failure:
            # Unknown values are kept and counted rather than dropped: a typo
            # in the taxonomy would otherwise vanish from the tally that the
            # whole exercise exists to produce.
            row["failure_reasons"][failure] = row["failure_reasons"].get(failure, 0) + 1

    def ratio(row: dict) -> float | None:
        judged = row["tp"] + row["fp"]
        return (row["tp"] / judged) if judged else None

    overall = {
        "n": 0, "tp": 0, "fp": 0, "pending": 0, "uncertain": 0,
        "needs_dfg": 0, "reasons": [], "failure_reasons": {},
    }
    for row in strata.values():
        for key in ("n", "tp", "fp", "pending", "uncertain", "needs_dfg"):
            overall[key] += row[key]
        overall["reasons"].extend(row["reasons"])
        for failure, count in row["failure_reasons"].items():
            overall["failure_reasons"][failure] = (
                overall["failure_reasons"].get(failure, 0) + count
            )
    overall["precision"] = ratio(overall)
    return {
        "review_id": package["review_id"],
        "seed": package["seed"],
        "code_version": package["code_version"],
        "strata": [
            {"defect_key": key[0], "resolution_status": key[1], "precision": ratio(row), **row}
            for key, row in sorted(strata.items())
        ],
        "overall": overall,
    }


def _format(summary: dict) -> str:
    lines = [
        f"review: {summary['review_id']}  seed: {summary['seed']}  "
        f"code: {summary['code_version']['commit']}",
        "",
        "| defect_key | status | n | tp | fp | uncertain | pending | precision | needs_dfg |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for row in summary["strata"]:
        precision = "n/a" if row["precision"] is None else f"{row['precision']:.3f}"
        lines.append(
            f"| {row['defect_key']} | {row['resolution_status']} | {row['n']} | {row['tp']} "
            f"| {row['fp']} | {row['uncertain']} | {row['pending']} | {precision} "
            f"| {row['needs_dfg']} |"
        )
    overall = summary["overall"]
    precision = "n/a" if overall["precision"] is None else f"{overall['precision']:.3f}"
    lines.append(
        f"| **overall** | | {overall['n']} | {overall['tp']} | {overall['fp']} "
        f"| {overall['uncertain']} | {overall['pending']} | **{precision}** "
        f"| {overall['needs_dfg']} |"
    )
    # The tally is the part that becomes work items, so it is printed even when
    # empty -- an all-zero table says the verdicts did not use the taxonomy,
    # which is itself worth seeing before the number is quoted.
    lines += ["", "| failure_reason | count |", "|---|---|"]
    for failure in FAILURE_REASONS:
        lines.append(f"| {failure} | {overall['failure_reasons'].get(failure, 0)} |")
    unknown = sorted(set(overall["failure_reasons"]) - set(FAILURE_REASONS))
    for failure in unknown:
        lines.append(f"| {failure} *(not in the taxonomy)* | {overall['failure_reasons'][failure]} |")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build (or summarize) the stage 4 defect candidate review sheet."
    )
    parser.add_argument("--database", action="append", default=[], metavar="NAME=PATH",
                        help="Graph database for one repository; repeat per repository.")
    parser.add_argument("--root", action="append", default=[], metavar="NAME=PATH",
                        help="Where a repository's sources live now, when the tree has "
                             "moved since it was indexed; repeat per repository.")
    parser.add_argument("--type", action="append", default=[], metavar="TYPE",
                        help="Matrix key, query name, or `all`; repeatable.")
    parser.add_argument("--sample", type=int, default=20)
    parser.add_argument("--seed", default="provenlattice-defect-v1")
    parser.add_argument("--output", type=Path, help="Directory for defect_review.{json,md}.")
    parser.add_argument("--summarize", type=Path,
                        help="Print precision from a filled-in review sheet and exit.")
    parser.add_argument("--render", type=Path, metavar="JSON",
                        help="Re-render the Markdown reading copy of a recorded review "
                             "sheet, beside it. Takes no database and no other input.")
    parser.add_argument("--no-identity", dest="identity", action="store_false",
                        help="Join subjects by spelling, as stage 4 did. Reproduces the "
                             "stage 4 numbers on a stage 4 database; on a new one it "
                             "isolates the extraction changes from the identity changes.")
    parser.add_argument("--no-contracts", dest="use_contracts", action="store_false",
                        help="Turn the ownership contracts and the parameter join off "
                             "in one move -- the control column, the same single dial "
                             "the query layer keeps. The default sheet is the "
                             "delivered pipeline.")
    parser.add_argument("--overwrite", action="store_true",
                        help="Replace a review sheet that already carries verdicts. "
                             "Without it, a rebuild refuses to discard recorded work.")
    args = parser.parse_args()

    if args.summarize:
        print(_format(summarize(args.summarize)))
        return 0
    if args.render:
        # The verdicts are written into the JSON and the Markdown is derived
        # from it, so refreshing the reading copy must not need a database: the
        # only input is the record itself.
        if args.database or args.output:
            parser.error("--render reads one JSON and writes its Markdown; "
                         "--database and --output do not apply")
        if not args.render.is_file():
            parser.error(f"review sheet does not exist: {args.render}")
        package = json.loads(args.render.read_text(encoding="utf-8"))
        target = args.render.with_suffix(".md")
        target.write_text(_render(package), encoding="utf-8")
        print(json.dumps({"rendered": str(target), "cases": len(package["cases"])},
                         indent=2, ensure_ascii=False))
        return 0
    if not args.database:
        parser.error("--database is required unless --summarize is given")
    if not args.output:
        parser.error("--output is required unless --summarize is given")
    if args.sample < 1:
        parser.error("--sample must be positive")

    databases: dict[str, Path] = {}
    for value in args.database:
        name, separator, raw_path = value.partition("=")
        if not separator or not name or not raw_path:
            parser.error(f"invalid --database value: {value!r}; expected NAME=PATH")
        path = Path(raw_path)
        if not path.is_file():
            parser.error(f"database does not exist: {path}")
        databases[name] = path

    roots: dict[str, Path] = {}
    for value in args.root:
        name, separator, raw_path = value.partition("=")
        if not separator or not name or not raw_path:
            parser.error(f"invalid --root value: {value!r}; expected NAME=PATH")
        if name not in databases:
            parser.error(f"--root {name} names no --database")
        path = Path(raw_path)
        if not path.is_dir():
            parser.error(f"root does not exist: {path}")
        roots[name] = path

    types = args.type or ["all"]
    workspace = Path(__file__).resolve().parent.parent.parent
    package = build_review(databases, types, args.sample, args.seed, workspace, roots,
                           identity=args.identity, use_contracts=args.use_contracts)

    args.output.mkdir(parents=True, exist_ok=True)
    # An adjudicated sheet is the most expensive thing this package produces:
    # it is the only artifact here that costs human hours. Rebuilding over one
    # is therefore refused rather than merely discouraged, and the escape hatch
    # is explicit. An unreadable file is not recorded work, so it is replaced.
    sheet = args.output / "defect_review.json"
    if sheet.is_file() and not args.overwrite:
        try:
            recorded = json.loads(sheet.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            recorded = {}
        reviewed = sum(
            1 for case in recorded.get("cases", [])
            if str(case.get("annotation", {}).get("verdict") or "").upper() == "REVIEWED"
        )
        if reviewed:
            parser.error(
                f"{sheet} already carries {reviewed} recorded verdict(s) and a "
                "rebuild would discard them. Pass --overwrite to replace the "
                "sheet, or --render to refresh only its Markdown."
            )
    (args.output / "defect_review.json").write_text(
        json.dumps(package, indent=2, ensure_ascii=False) + "\n", encoding="utf-8",
    )
    (args.output / "defect_review.md").write_text(_render(package), encoding="utf-8")
    print(json.dumps({
        "review_id": package["review_id"],
        "cases": len(package["cases"]),
        "sources": package["sources"],
        "availability": package["availability"],
    }, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

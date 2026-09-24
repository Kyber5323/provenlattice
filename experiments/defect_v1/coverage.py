"""Population coverage of the three defect queries, for a before/after table.

Stage 4.5's acceptance is a two-repository before/after comparison, and the
numbers it compares are *populations*, not samples: `candidate_count`, the
three-state split, and the extractor's own coverage counters. This script
produces exactly those numbers for a database and writes them as JSON, so the
before column can be frozen before any code changes and the after column
recomputed with the same instrument.

It reads a database and never writes one: `mode=ro`, like `build_review.py`.
The whole point of freezing `before.json` first is that the before column
cannot move once the code does, so the instrument must not be able to.

`max_candidates` is left at `POPULATION_LIMIT` (not the CLI's default 20)
because coverage is computed before truncation inside each query -- a truncated
run reports the same populations, but the returned list is then a sorted prefix
and the `population_capped` flag is the honest way to say so.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from contextlib import closing
from pathlib import Path

if __package__ in (None, ""):  # `python experiments/defect_v1/coverage.py`
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from experiments.defect_v1.build_review import (  # noqa: E402
    POPULATION_LIMIT,
    _edges,
    _events,
    _names,
    _open_read_only,
    _run,
)


def measure(database: Path, *, identity: bool = True) -> dict:
    """Coverage for one database, with the cost of producing it.

    `identity=False` joins subjects by spelling, which is stage 4's behaviour.
    On a stage 4 database it must reproduce `before.json` exactly -- that is
    what makes the four columns of the comparison readable, because a column
    that cannot reproduce the baseline is not a control.
    """
    with closing(_open_read_only(database)) as connection:
        row = connection.execute("SELECT repo_id FROM repositories LIMIT 1").fetchone()
        if row is None:
            raise SystemExit(f"{database}: no repository row; is it an index?")
        repo_id = row["repo_id"]
        events, edges = _events(connection, repo_id), _edges(connection, repo_id)
        names = _names(connection, repo_id)

    started = time.perf_counter()
    candidates, coverage, limits = _run(events, edges, names, ["all"], identity=identity)
    elapsed_ms = (time.perf_counter() - started) * 1000.0

    # The three-state split, per query, over the candidates that came back --
    # so it counts what `candidates_returned` counts and not a second
    # population. `_run` dedupes on `(defect_key, anchor, discriminator)`, and
    # the queries' own `coverage["candidates"]` is counted before that, which
    # is why the two can differ by a handful on a large repository.
    by_status: dict[str, Counter] = {}
    for candidate in candidates:
        by_status.setdefault(candidate.query, Counter())[candidate.resolution_status] += 1

    return {
        "database": database.resolve().as_posix(),
        "database_size": database.stat().st_size,
        "repo_id": repo_id,
        "events": len(events),
        "edges": len(edges),
        "query_time_ms": elapsed_ms,
        "candidates_returned": len(candidates),
        "candidates_by_status": {
            query: dict(sorted(counts.items())) for query, counts in sorted(by_status.items())
        },
        "coverage": coverage,
        "limits": limits,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
    )
    parser.add_argument("--database", action="append", default=[], metavar="NAME=PATH",
                        help="repeatable; the name is the key in the output")
    parser.add_argument("--output", type=Path, required=True, help="JSON destination")
    parser.add_argument("--label", default="", help="free text carried into the output")
    parser.add_argument("--no-identity", dest="identity", action="store_false",
                        help="Join subjects by spelling, as stage 4 did. This is the "
                             "control column: on a stage 4 database it must reproduce "
                             "before.json byte for byte.")
    args = parser.parse_args()

    measured: dict[str, dict] = {}
    for item in args.database:
        name, _, path = item.partition("=")
        if not path:
            raise SystemExit(f"--database wants NAME=PATH, got {item!r}")
        database = Path(path)
        if not database.exists():
            raise SystemExit(f"{database}: no such database")
        print(f"measuring {name} ({database}) ...", file=sys.stderr, flush=True)
        measured[name] = measure(database, identity=args.identity)
        print(
            f"  {measured[name]['query_time_ms'] / 1000.0:.1f}s, "
            f"{measured[name]['candidates_returned']} candidates",
            file=sys.stderr, flush=True,
        )

    payload = {
        "instrument": "experiments/defect_v1/coverage.py",
        "label": args.label,
        "population_limit": POPULATION_LIMIT,
        # Which column this is. `before.json` was written before the switch
        # existed, so it carries no key at all -- and it is a spelling column,
        # which is exactly what `--no-identity` reproduces.
        "subject_join": "identity" if args.identity else "spelling",
        "repositories": measured,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, indent=1, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {args.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

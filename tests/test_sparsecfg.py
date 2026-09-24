"""Stage 3 acceptance: the sparse CFG over semantic events.

The three query shapes AGENTS.md B.4 stage 3 names -- `ALLOC → RETURN without
RELEASE`, `CHECK → DEREFERENCE`, `RELEASE → USE` -- each get a test here, and
every one of them runs against artifacts the *publisher* produced
(`graph.build_graph`), not against a private reimplementation of it. If the
publisher and the tests ever disagree, the tests are the ones that must be
wrong, because the publisher is what a user's database contains.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

try:
    from tests.test_semantics_events import LEAK_AFTER, LEAK_BEFORE
except ImportError:  # `unittest discover -s tests` puts the directory on sys.path.
    from test_semantics_events import LEAK_AFTER, LEAK_BEFORE

from provenlattice.graph import build_graph, full_index, parsed_to_dict, parsed_from_dict
from provenlattice.incremental import incremental_update
from provenlattice.parsing.cpp import CppTreeSitterParser
from provenlattice.query import GraphQuery
from provenlattice.scanner import SourceFile, content_hash
from provenlattice.semantics import KNOWN_EVENTS, LEVEL_1_EVENTS
from provenlattice.semantics.events import STRUCTURAL_EVENTS
from provenlattice.sparsecfg import (
    CONTROL_REACHES,
    build_adjacency,
    leaked_allocations,
    materialize_points,
    unchecked_dereferences,
    use_after_release,
)
from provenlattice.sparsedfg import CONSUMES, DATA_FLOW_TO

REPO = "cfg-repo"


def publish(root: Path, relative_path: str, source: str):
    """Run one in-memory C file through the real publisher.

    Returns the `(semantic_events, semantic_edges)` lists `build_graph` would
    hand to storage -- the same objects `full_index` persists, minus SQLite.
    """
    data = source.encode("utf-8")
    parsed = CppTreeSitterParser().parse_bytes(data, relative_path).parsed
    source_file = SourceFile(
        path=root / relative_path, relative_path=relative_path,
        language="cpp", source_hash=content_hash(data),
    )
    *_, events, edges = build_graph(
        root, [source_file], {relative_path: parsed}, 1, repository_id_override=REPO,
    )
    return events, edges


def by_type(events) -> dict:
    table: dict[str, list[str]] = {}
    for event in events:
        table.setdefault(event.event_type, []).append(event.event_id)
    return table


def edge_facts(events, edges) -> set:
    """`(src_type, dst_type_or_None, flags)` per edge; `None` is the exit.

    Types collapse distinct points of the same kind, which is exactly the
    resolution these tests need: they assert on shapes, not on ordinals.
    """
    types = {event.event_id: event.event_type for event in events}
    return {
        (types[edge.src_event_id], types.get(edge.dst_event_id), frozenset(edge.flags))
        for edge in edges
    }


def out_edges(edges, src_id: str) -> list:
    return [edge for edge in edges if edge.src_event_id == src_id]


def is_exit(edge) -> bool:
    """The synthetic exit: published as a `cfg-exit:` id, not as None."""
    return edge.dst_event_id.startswith("cfg-exit:")


def pattern_paths(events, edges, pattern) -> list:
    points = materialize_points(events, edges)
    paths, truncated = pattern(edges, points)
    assert not truncated
    return paths


def semantic_snapshot(database: Path) -> dict:
    """Both semantic tables, generation-normalised, in id order."""
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    result = {}
    for table in ("semantic_events", "semantic_edges"):
        rows = []
        for row in connection.execute(f"SELECT * FROM {table} ORDER BY id"):
            item = dict(row)
            item.pop("generation", None)
            rows.append(item)
        result[table] = rows
    connection.close()
    return result


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


NULLCHECK = """#include <stdlib.h>

void unchecked(struct S *p) {
  if (p == NULL) { log_it(); }
  p->f = 1;
}

void guarded(struct S *p) {
  if (p == NULL) { return; }
  p->f = 1;
}

void guarded_positive(struct S *p) {
  if (p != NULL) { p->f = 1; }
}
"""

RELEASE_USE = """#include <stdlib.h>

void freed_use(int *p) {
  free(p);
  p[0] = 1;
}

void freed_clean(int *p) {
  free(p);
}
"""


class LeakPatternTests(unittest.TestCase):
    def test_alloc_reaches_exit_without_release(self) -> None:
        """Acceptance shape 1, on the exact source the event layer pins.

        The guarded early return is not itself a leak path: the null arm of the
        check on the allocated subject is excluded, so exactly one candidate
        survives -- the strlen branch, which reaches the exit holding `buffer`.
        """
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        events, edges = publish(root, "alpha/leak.cpp", LEAK_BEFORE)
        paths = pattern_paths(events, edges, leaked_allocations)
        self.assertEqual(len(paths), 1)
        types = list(paths[0].types)
        self.assertEqual(types[0], "ALLOC")
        self.assertEqual(types[-1], "EXIT")
        self.assertIn("CHECK", types)
        self.assertNotIn("RELEASE", types)

    def test_adding_the_release_removes_the_candidate(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        events, edges = publish(root, "alpha/leak.cpp", LEAK_AFTER)
        self.assertEqual(pattern_paths(events, edges, leaked_allocations), [])

    def test_a_subject_less_allocation_still_produces_a_candidate(self) -> None:
        """`release(*p)`-style subjects the parser cannot root leave the block
        set empty: over-reporting is the intended bias for a candidate
        generator, and a silent zero would be the worse failure."""
        source = """#include <stdlib.h>

void odd(void) {
  malloc(8);
}
"""
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        events, edges = publish(root, "alpha/odd.cpp", source)
        self.assertTrue(pattern_paths(events, edges, leaked_allocations))


class CheckThenDereferenceTests(unittest.TestCase):
    def test_check_then_dereference(self) -> None:
        """Acceptance shape 2: the null arm leads to a dereference of the same
        subject. Both guarded spellings stay silent."""
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        events, edges = publish(root, "alpha/check.cpp", NULLCHECK)
        paths = pattern_paths(events, edges, unchecked_dereferences)
        self.assertEqual(len(paths), 1)
        # The path runs from the null arm's *target* (here the log call) to the
        # dereference -- the plan's "从代表主体为 null 的那条出边出发".
        self.assertEqual(list(paths[0].types), ["CALL", "DEREFERENCE"])

    def test_the_check_point_carries_subject_and_polarity(self) -> None:
        """`subject`/`polarity` are what let the pattern pair two operations
        whose matched names are the keywords; they must survive publication."""
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        events, _ = publish(root, "alpha/check.cpp", NULLCHECK)
        check = [event for event in events if event.event_type == "CHECK"]
        self.assertEqual(len(check), 3)
        by_polarity = {event.metadata["polarity"] for event in check}
        self.assertEqual(by_polarity, {"true_when_null", "true_when_non_null"})
        for event in check:
            self.assertEqual(event.metadata["subject"], "p")
            self.assertEqual(event.matched_via, "structural")


class ReleaseThenUseTests(unittest.TestCase):
    def test_release_then_use(self) -> None:
        """Acceptance shape 3: DEREFERENCE is USE for v1 (口径 2); argument-type
        use is a registered gap, not a supported silence."""
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        events, edges = publish(root, "beta/use.c", RELEASE_USE)
        paths = pattern_paths(events, edges, use_after_release)
        self.assertEqual(len(paths), 1)
        types = list(paths[0].types)
        self.assertEqual(types[0], "RELEASE")
        self.assertIn("DEREFERENCE", types)

    def test_release_alone_reports_nothing(self) -> None:
        source = "#include <stdlib.h>\n\nvoid ok(int *p) {\n  free(p);\n}\n"
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        events, edges = publish(root, "beta/ok.c", source)
        self.assertEqual(pattern_paths(events, edges, use_after_release), [])


class RebuildDeterminismTests(unittest.TestCase):
    def test_rebuild_is_byte_identical(self) -> None:
        """The acceptance clause "路径可复现": two independent full indexes of
        the same tree produce byte-identical rows in both semantic tables."""
        root = Path(tempfile.mkdtemp()) / "repo"
        write(root / "alpha" / "leak.cpp", LEAK_BEFORE)
        write(root / "beta" / "use.c", RELEASE_USE)
        first = Path(tempfile.mkdtemp()) / "one.db"
        second = Path(tempfile.mkdtemp()) / "two.db"
        full_index(root, first, repository_id_override=REPO)
        full_index(root, second, repository_id_override=REPO)
        self.assertEqual(
            json.dumps(semantic_snapshot(first), sort_keys=True),
            json.dumps(semantic_snapshot(second), sort_keys=True),
        )


class IncrementalParityTests(unittest.TestCase):
    def test_full_incremental_parity_for_semantic_tables(self) -> None:
        """A body-only edit followed by an incremental update lands in the same
        place as a from-scratch full index: same events, same edges, same ids."""
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        base = Path(temp.name)
        root = base / "repo"
        write(root / "alpha" / "leak.cpp", LEAK_BEFORE)
        write(root / "beta" / "use.c", RELEASE_USE)
        database = base / "incremental.db"
        full_index(root, database, repository_id_override=REPO)
        write(root / "alpha" / "leak.cpp", LEAK_AFTER)
        incremental_update(root, database, repository_id_override=REPO)

        fresh_root = base / "fresh"
        write(fresh_root / "alpha" / "leak.cpp", LEAK_AFTER)
        write(fresh_root / "beta" / "use.c", RELEASE_USE)
        fresh = base / "fresh.db"
        full_index(fresh_root, fresh, repository_id_override=REPO)

        self.assertEqual(
            json.dumps(semantic_snapshot(database), sort_keys=True),
            json.dumps(semantic_snapshot(fresh), sort_keys=True),
        )


class CarryForwardTests(unittest.TestCase):
    def test_unchanged_file_edges_are_carried_forward(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        base = Path(temp.name)
        root = base / "repo"
        write(root / "alpha" / "leak.cpp", LEAK_BEFORE)
        write(root / "beta" / "use.c", RELEASE_USE)
        database = base / "repo.db"
        full_index(root, database, repository_id_override=REPO)

        def edges_by_file(db: Path) -> dict:
            connection = sqlite3.connect(db)
            connection.row_factory = sqlite3.Row
            groups: dict[str, set] = {}
            for row in connection.execute(
                "SELECT e.id, ev.file_id FROM semantic_edges e "
                "JOIN semantic_events ev ON e.src_event_id = ev.id"
            ):
                groups.setdefault(row["file_id"], set()).add(row["id"])
            connection.close()
            return groups

        before = edges_by_file(database)
        self.assertEqual(len(before), 2)
        write(root / "alpha" / "leak.cpp", LEAK_AFTER)
        incremental_update(root, database, repository_id_override=REPO)
        after = edges_by_file(database)

        use_file_id = next(
            file_id for file_id in before
            if "use.c" in self._file_path(database, file_id)
        )
        self.assertEqual(before[use_file_id], after[use_file_id])
        leak_file_id = next(file_id for file_id in before if file_id != use_file_id)
        self.assertNotEqual(before[leak_file_id], after[leak_file_id])

    @staticmethod
    def _file_path(database: Path, file_id: str) -> str:
        connection = sqlite3.connect(database)
        row = connection.execute(
            "SELECT path FROM files WHERE file_id=?", (file_id,)
        ).fetchone()
        connection.close()
        return row[0]

    def test_every_edge_is_intra_file_and_intra_owner(self) -> None:
        """The invariant the carry-forward rests on: a method lives in exactly
        one file, so a file's edge set changes if and only if its events do.
        Pinned here so a future cross-method edge breaks this test loudly
        instead of silently corrupting incremental updates."""
        root = Path(tempfile.mkdtemp()) / "repo"
        write(root / "alpha" / "leak.cpp", LEAK_BEFORE)
        write(root / "beta" / "use.c", RELEASE_USE)
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        database = Path(holder.name) / "repo.db"
        full_index(root, database, repository_id_override=REPO)
        connection = sqlite3.connect(database)
        connection.row_factory = sqlite3.Row
        events = {
            row["id"]: row
            for row in connection.execute("SELECT * FROM semantic_events")
        }
        exits = 0
        for edge in connection.execute("SELECT * FROM semantic_edges"):
            source = events[edge["src_event_id"]]
            self.assertEqual(source["file_id"], edge["file_id"])
            self.assertEqual(source["owner_symbol_id"], edge["owner_symbol_id"])
            target = events.get(edge["dst_event_id"])
            if target is None:
                exits += 1
                self.assertTrue(edge["dst_event_id"].startswith("cfg-exit:"))
            else:
                self.assertEqual(target["file_id"], edge["file_id"])
                self.assertEqual(target["owner_symbol_id"], edge["owner_symbol_id"])
        connection.close()
        self.assertGreater(exits, 0)


class BuilderShapeTests(unittest.TestCase):
    def test_statements_compress_to_the_operations(self) -> None:
        """`malloc(8); free(0);` between two semicolons adds no CFG machinery:
        the only points are the operations, and the chain runs through them to
        the exit. `malloc` is both an ALLOC and a CALL (the CALL vocabulary's
        syntax axis claims every call_expression), hence the interleaving."""
        source = """void f(void) {
  malloc(8);
  free(0);
}
"""
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        events, edges = publish(root, "src/compress.c", source)
        self.assertGreater(len(events), 0)
        # Own points at one node are ordered by (event_type, ordinal): ALLOC
        # before CALL at the malloc node, CALL before RELEASE at the free node.
        self.assertEqual(
            edge_facts(events, edges),
            {
                ("ALLOC", "CALL", frozenset()),
                ("CALL", "CALL", frozenset()),
                ("CALL", "RELEASE", frozenset()),
                ("RELEASE", None, frozenset()),
            },
        )
        for edge in edges:
            self.assertEqual(edge.relation, CONTROL_REACHES)

    def test_if_else_carries_branch_flags(self) -> None:
        source = """extern void g(void);
extern void h(void);

void pick(struct S *p) {
  if (!p) { g(); } else { h(); }
}
"""
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        events, edges = publish(root, "src/branches.c", source)
        check = by_type(events)["CHECK"][0]
        arms = out_edges(edges, check)
        self.assertEqual(
            {frozenset(edge.flags) for edge in arms},
            {frozenset({"branch=true"}), frozenset({"branch=false"})},
        )
        self.assertEqual({edge.dst_event_id for edge in arms}, set(by_type(events)["CALL"]))
        for call in by_type(events)["CALL"]:
            exits = [edge for edge in out_edges(edges, call) if is_exit(edge)]
            self.assertEqual(len(exits), 1)
            self.assertEqual(exits[0].flags, ())

    def test_condition_inner_points_precede_the_condition_point(self) -> None:
        """`if (p->f)` dereferences inside the condition: the DEREFERENCE is a
        point of its own, ordered before the condition's BRANCH, and the branch
        consumes it. Post-order own-point emission is what guarantees this."""
        source = """extern void g(void);

void probe(struct S *p) {
  if (p->f) { g(); }
}
"""
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        events, edges = publish(root, "src/cond.c", source)
        deref = by_type(events)["DEREFERENCE"][0]
        branch = by_type(events)["BRANCH"][0]
        # Ordinals count per event type (both are the first of their kind);
        # the *edge direction* is what proves the inner point precedes the
        # condition point: the branch consumes the dereference.
        deref_event = next(event for event in events if event.event_id == deref)
        branch_event = next(event for event in events if event.event_id == branch)
        self.assertEqual(deref_event.ordinal, 0)
        self.assertEqual(branch_event.ordinal, 0)
        self.assertEqual(len(out_edges(edges, deref)), 1)
        self.assertEqual(out_edges(edges, deref)[0].dst_event_id, branch)

    def test_while_loop_back_and_break(self) -> None:
        source = """extern void tick(void);

void spin(int n) {
  while (n > 0) {
    tick();
    if (n == 0) { break; }
  }
}
"""
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        events, edges = publish(root, "src/while.c", source)
        facts = edge_facts(events, edges)
        # Exactly five edges, each carrying the arm it was reached on: the
        # break keeps `branch=true` all the way to the exit, the loop's false
        # arm reaches the exit as `branch=false`, and the body's normal
        # completion loops back flagged with the arm it arrived on.
        self.assertEqual(
            facts,
            {
                ("BRANCH", "CALL", frozenset({"branch=true"})),
                ("CALL", "BRANCH", frozenset()),
                ("BRANCH", "BRANCH", frozenset({"branch=false", "loop_back"})),
                ("BRANCH", None, frozenset({"branch=true"})),
                ("BRANCH", None, frozenset({"branch=false"})),
            },
        )
        self.assertEqual(len([item for item in facts if "loop_back" in item[2]]), 1)

    def test_do_while_condition_comes_after_the_body(self) -> None:
        source = """extern void tick(void);

void once(int n) {
  do {
    tick();
  } while (n > 0);
}
"""
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        events, edges = publish(root, "src/dowhile.c", source)
        call = by_type(events)["CALL"][0]
        branch = by_type(events)["BRANCH"][0]
        call_event = next(event for event in events if event.event_id == call)
        branch_event = next(event for event in events if event.event_id == branch)
        # Ordinals count per event type (both are first of their kind); source
        # position proves the body came before the condition.
        self.assertEqual((call_event.ordinal, branch_event.ordinal), (0, 0))
        self.assertLess(call_event.start_line, branch_event.start_line)
        back = [edge for edge in out_edges(edges, branch)
                if edge.dst_event_id == call]
        self.assertEqual(len(back), 1)
        self.assertEqual(frozenset(back[0].flags), frozenset({"branch=true", "loop_back"}))

    def test_for_continue_targets_the_update(self) -> None:
        source = """extern int step(int value);
extern void tick(void);

void loop(int n) {
  for (int i = 0; i < n; i = step(i)) {
    if (i == 1) { continue; }
    tick();
  }
}
"""
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        events, edges = publish(root, "src/for.c", source)
        facts = edge_facts(events, edges)
        # `continue` jumps to the update expression, not to the condition --
        # C semantics: the third clause still runs on the next iteration. It
        # keeps the arm it was reached on (branch=true) and gains loop_back.
        self.assertIn(("BRANCH", "CALL", frozenset({"branch=true", "loop_back"})), facts)
        # The normal body exit threads into the update unflagged...
        self.assertIn(("CALL", "CALL", frozenset()), facts)
        # ...and the update flows back into the condition as the back edge.
        self.assertIn(("CALL", "BRANCH", frozenset({"loop_back"})), facts)
        self.assertIn(("BRANCH", None, frozenset({"branch=false"})), facts)

    def test_switch_labels_are_entries_and_fallthrough_is_flagged(self) -> None:
        source = """extern void a(void);
extern void b(void);

void pick(int x) {
  switch (x) {
    case 1:
      a();
    default:
      b();
      break;
  }
}
"""
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        events, edges = publish(root, "src/switch.c", source)
        facts = edge_facts(events, edges)
        branch = by_type(events)["BRANCH"][0]
        condition_arms = out_edges(edges, branch)
        self.assertEqual(
            {frozenset(edge.flags) for edge in condition_arms},
            {frozenset({"branch=true"}), frozenset({"branch=false"})},
        )
        self.assertTrue(all(is_exit(edge) is False for edge in condition_arms))
        # `case 1` has no break, so control falls into `default` -- a real
        # reachability fact, flagged rather than hidden.
        self.assertIn(("CALL", "CALL", frozenset({"fallthrough"})), facts)
        # And the default body's break reaches the exit.
        self.assertEqual(len([item for item in facts if item[1] is None and item[2] == frozenset()]), 1)

    def test_throw_reaches_each_handler_and_the_exit(self) -> None:
        source = """int divide(int a, int b) {
  try {
    if (a < 0) { throw a; }
    return a / b;
  } catch (int e) {
    return -e;
  }
}
"""
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        events, edges = publish(root, "src/throw.c", source)
        throw = by_type(events)["THROW"][0]
        outs = out_edges(edges, throw)
        exits = [edge for edge in outs if is_exit(edge)]
        self.assertEqual(len(exits), 1)
        self.assertIn("exception", exits[0].flags)
        handlers = [edge for edge in outs if not is_exit(edge)]
        self.assertEqual(len(handlers), 1)
        self.assertIn("exception", handlers[0].flags)
        self.assertEqual(
            next(event for event in events if event.event_id == handlers[0].dst_event_id).event_type,
            "RETURN",
        )

    def test_exit_call_gets_no_handler_edges(self) -> None:
        """`exit(1)` is a THROW by vocabulary but not a syntactic `throw`, so
        it must not fabricate an edge into the catch block."""
        source = """extern void g(void);

void hard_exit(void) {
  try {
    exit(1);
  } catch (int e) {
    g();
  }
}
"""
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        events, edges = publish(root, "src/exitcall.c", source)
        throw = by_type(events)["THROW"][0]
        outs = out_edges(edges, throw)
        # One edge, to the exit, unflagged: the walker sees a plain call and
        # honest flags mean only a syntactic `throw` earns `exception`.
        self.assertEqual(len(outs), 1)
        self.assertTrue(is_exit(outs[0]))
        self.assertEqual(outs[0].flags, ())

    def test_exit_is_created_only_for_methods_with_points(self) -> None:
        source = """int proto(int x);

void empty(void) {}

void full(void) { malloc(1); }
"""
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        events, edges = publish(root, "src/exits.c", source)
        self.assertEqual({event.event_type for event in events}, {"ALLOC", "CALL"})
        exits = [edge for edge in edges if is_exit(edge)]
        self.assertEqual(len(exits), 1)
        owner = next(event for event in events if event.event_type == "ALLOC").owner_symbol_id
        self.assertEqual(exits[0].owner_symbol_id, owner)

    def test_reformatting_does_not_change_any_edge_id(self) -> None:
        tidy = """#include <stdlib.h>

void shape(struct S *p, int n) {
  char *buffer = (char *)malloc(n);
  if (!buffer) { return; }
  free(buffer);
}
"""
        messy = tidy.replace("if (!buffer) { return; }", "if ( ! buffer )\n  {\n    return ;\n  }")
        messy = messy.replace("  free(buffer);", "\n\n  free( buffer ) ;")
        first = tempfile.TemporaryDirectory()
        self.addCleanup(first.cleanup)
        second = tempfile.TemporaryDirectory()
        self.addCleanup(second.cleanup)
        tidy_events, tidy_edges = publish(Path(first.name), "src/shape.c", tidy)
        messy_events, messy_edges = publish(Path(second.name), "src/shape.c", messy)
        self.assertEqual(
            sorted(edge.edge_id for edge in tidy_edges),
            sorted(edge.edge_id for edge in messy_edges),
        )
        self.assertEqual(
            sorted(event.event_id for event in tidy_events),
            sorted(event.event_id for event in messy_events),
        )

    def test_editing_one_method_leaves_another_methods_edges_untouched(self) -> None:
        before = """#include <stdlib.h>

void keeper(struct S *p) {
  if (!p) { return; }
  p->f = 1;
}

void churn(int n) {
  char *buffer = (char *)malloc(n);
  free(buffer);
}
"""
        # The edit adds a guard inside `churn` only: new points, new edges --
        # but all of them belong to churn, so keeper's edge ids must survive
        # byte for byte (identity is method-anchored, appendix B.2.1).
        after = before.replace("  free(buffer);",
                               "  if (buffer == 0) { return; }\n  free(buffer);")
        first = tempfile.TemporaryDirectory()
        self.addCleanup(first.cleanup)
        second = tempfile.TemporaryDirectory()
        self.addCleanup(second.cleanup)
        _, before_edges = publish(Path(first.name), "src/pair.c", before)
        _, after_edges = publish(Path(second.name), "src/pair.c", after)

        def by_owner(edges) -> dict:
            groups: dict[str, set] = {}
            for edge in edges:
                groups.setdefault(edge.owner_symbol_id, set()).add(edge.edge_id)
            return groups

        before_groups, after_groups = by_owner(before_edges), by_owner(after_edges)
        self.assertEqual(set(before_groups), set(after_groups))
        changed = [owner for owner in before_groups
                   if before_groups[owner] != after_groups.get(owner, set())]
        self.assertEqual(len(changed), 1)

    def test_publication_represents_each_operation_once(self) -> None:
        """No operation appears twice: vocabulary rows come from the matcher,
        structural rows say `structural`, and nothing else is published."""
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        events, edges = publish(root, "alpha/leak.cpp", LEAK_BEFORE)
        structural = {event.event_id for event in events if event.matched_via == "structural"}
        vocabulary = {event.event_id for event in events} - structural
        for event in events:
            if event.event_id in structural:
                self.assertIn(event.event_type, STRUCTURAL_EVENTS)
            else:
                self.assertNotIn(event.event_type, STRUCTURAL_EVENTS)
        self.assertEqual(len(structural | vocabulary), len(events))
        # Every edge endpoint names a published event, or is the exit.
        self.assertEqual(
            {edge.src_event_id for edge in edges},
            {event.event_id for event in events} & {edge.src_event_id for edge in edges},
        )
        self.assertTrue(
            all(edge.dst_event_id is None or edge.dst_event_id.startswith("cfg-exit:")
                or any(event.event_id == edge.dst_event_id for event in events)
                for edge in edges),
        )
        # The subject annotations that make pattern pairing work travel in the
        # published metadata.
        release = next(event for event in events if event.event_type == "RELEASE")
        self.assertEqual(release.metadata["subject"], "buffer")

    def test_access_events_publish_outside_the_structural_channel(self) -> None:
        """Stage 5A's READ/WRITE rows are a third publication channel: they are
        not vocabulary hits and not structural points, so the
        structural-iff-STRUCTURAL_EVENTS split still holds exactly, and their
        ids appear on no CONTROL_REACHES edge -- the access layer publishes
        rows and nothing else. Stage 5B's DATA_FLOW_TO edges are the
        registered exception, and they say so in their relation and their
        metadata; a flow edge without a flow_class is a bug, not a relation
        this test knows nothing about."""
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        events, edges = publish(
            Path(temp.name), "alpha/access.cpp",
            "static int g_v;\n"
            "int bump(void) {\n"
            "  if (g_v > 0) { g_v = g_v - 1; }\n"
            "  return g_v;\n"
            "}\n",
        )
        access = [event for event in events if event.event_type in ("READ", "WRITE")]
        self.assertTrue(access)
        for event in access:
            self.assertEqual(event.matched_via, "access")
            self.assertNotIn(event.event_type, STRUCTURAL_EVENTS)
        structural = [event for event in events if event.matched_via == "structural"]
        for event in structural:
            self.assertIn(event.event_type, STRUCTURAL_EVENTS)
        access_ids = {event.event_id for event in access}
        flow_classes = {"input_to_sink", "alloc_to_use", "free_to_use", "null_to_deref", "value_flow"}
        for edge in edges:
            if edge.relation == DATA_FLOW_TO or edge.relation == CONSUMES:
                self.assertIn(edge.metadata["flow_class"], flow_classes)
                continue
            self.assertEqual(edge.relation, CONTROL_REACHES)
            self.assertNotIn(edge.src_event_id, access_ids)
            self.assertNotIn(edge.dst_event_id, access_ids)
        # And the positive half: this fixture really does mint a flow edge
        # whose endpoints are access rows -- `g_v = g_v - 1` defining what
        # `return g_v` reads -- so the exception above is not vacuous.
        touching = [edge for edge in edges
                    if edge.src_event_id in access_ids or edge.dst_event_id in access_ids]
        self.assertTrue(touching)
        for edge in touching:
            self.assertEqual(edge.relation, DATA_FLOW_TO)

    def test_a_file_with_a_syntax_error_still_builds_a_cfg(self) -> None:
        source = """#include <stdlib.h>

void broken(struct S *p) {
  if (!p { return; }
  free(p);
}
"""
        parsed = CppTreeSitterParser().parse_bytes(source.encode("utf-8"), "src/broken.c").parsed
        self.assertIsNotNone(parsed.cfg)
        self.assertTrue(parsed.cfg.edges)

    def test_file_scope_events_produce_no_points_or_edges(self) -> None:
        source = """#include <stdlib.h>

static int *g = (int *)malloc(64);

void reset(void) { free(g); }
"""
        parsed = CppTreeSitterParser().parse_bytes(source.encode("utf-8"), "src/global.c").parsed
        # The file-scope malloc matches twice -- ALLOC (keyword) and CALL (the
        # syntax axis claims every call_expression) -- and both are counted as
        # skipped rather than dropped quietly.
        self.assertEqual(parsed.unowned_events, 2)
        self.assertIsNotNone(parsed.cfg)
        self.assertNotIn("ALLOC", {point.event_type for point in parsed.cfg.points})


class StorageContractTests(unittest.TestCase):
    def test_cfg_is_not_written_into_the_parse_cache(self) -> None:
        """The parse cache is 94.8% of the nodes table; the CFG is cheap to
        rebuild, so it must never ride along."""
        source = "#include <stdlib.h>\n\nvoid f(void) { malloc(1); }\n"
        parsed = CppTreeSitterParser().parse_bytes(source.encode("utf-8"), "src/f.c").parsed
        self.assertIsNotNone(parsed.cfg)
        restored = parsed_from_dict(parsed_to_dict(parsed))
        self.assertIsNone(restored.cfg)
        self.assertNotIn("cfg", parsed_to_dict(parsed))

    def test_check_and_dereference_are_not_vocabulary_events(self) -> None:
        """The vocabulary TOML is untouched by stage 3: CHECK/BRANCH/DEREFERENCE
        are structural products of the CFG walker, and no keyword could name
        them anyway."""
        from provenlattice.semantics import load_vocabulary
        for language in ("cpp", "python"):
            declared = set(load_vocabulary(language).events)
            self.assertEqual(declared & STRUCTURAL_EVENTS, set())
        self.assertTrue(STRUCTURAL_EVENTS <= LEVEL_1_EVENTS)
        self.assertTrue(STRUCTURAL_EVENTS <= KNOWN_EVENTS)

    def test_semantic_edge_indexes_exist(self) -> None:
        root = Path(tempfile.mkdtemp()) / "repo"
        write(root / "alpha" / "leak.cpp", LEAK_BEFORE)
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        database = Path(holder.name) / "repo.db"
        full_index(root, database, repository_id_override=REPO)
        with closing(sqlite3.connect(database)) as connection:
            indexes = {
                row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='semantic_edges'"
                )
            }
        self.assertEqual(
            indexes,
            {"sqlite_autoindex_semantic_edges_1", "idx_semantic_edges_src",
             "idx_semantic_edges_dst", "idx_semantic_edges_owner",
             "idx_semantic_edges_file", "idx_semantic_edges_relation"},
        )


class GraphQueryPatternTests(unittest.TestCase):
    def test_graph_query_exposes_the_three_patterns(self) -> None:
        """End to end through `GraphQuery`: index a written repo, then read the
        three acceptance shapes back through the public query surface."""
        root = Path(tempfile.mkdtemp()) / "repo"
        write(root / "alpha" / "check.c", NULLCHECK + """
void positive_only(struct S *q) {
  if (q != NULL) { q->f = 1; }
}
""")
        write(root / "alpha" / "mem.c", """#include <stdlib.h>

char *make_leak(void) {
  char *buffer = (char *)malloc(256);
  if (!buffer) { return 0; }
  return buffer;
}
""")
        write(root / "beta" / "use.c", RELEASE_USE)
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        database = Path(holder.name) / "repo.db"
        full_index(root, database, repository_id_override=REPO)
        with GraphQuery(database) as query:
            leaks = query.get_leaked_allocations()
            self.assertEqual(len(leaks["data"]["paths"]), 1)
            self.assertNotIn("RELEASE", leaks["data"]["paths"][0]["types"])

            unchecked = query.get_unchecked_dereferences()
            self.assertEqual(len(unchecked["data"]["paths"]), 1)
            # The path runs from the null arm's target (the log call) to the
            # dereference, per the pattern's definition.
            self.assertEqual(list(unchecked["data"]["paths"][0]["types"]),
                             ["CALL", "DEREFERENCE"])

            uar = query.get_use_after_release()
            self.assertEqual(len(uar["data"]["paths"]), 1)
            self.assertEqual(uar["data"]["paths"][0]["types"][0], "RELEASE")

            # The generic control-path query is raw reachability: subject-paired
            # starts, every arm explored. `positive_only` is the one function
            # whose CHECK has subject q, and exactly one of its arms reaches the
            # dereference (the null arm falls out of the method).
            paired = query.get_control_paths(
                source_type="CHECK", target_type="DEREFERENCE", subject="q",
            )
            self.assertEqual(len(paired["data"]["paths"]), 1)
            self.assertEqual(list(paired["data"]["paths"][0]["types"]),
                             ["CHECK", "DEREFERENCE"])

            miss = query.get_control_paths(
                source_type="THREAD_SPAWN", target_type="THREAD_JOIN",
            )
            self.assertEqual(miss["data"], {"paths": [], "truncated": False})

    def test_fixture_knowledge_leak_is_reported(self) -> None:
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        database = Path(holder.name) / "fixture.db"
        full_index("tests/fixture_knowledge", database, repository_id_override="fixture")
        with GraphQuery(database) as query:
            leaks = query.get_leaked_allocations()
            paths = leaks["data"]["paths"]
            self.assertEqual(len(paths), 1)
            self.assertEqual(paths[0]["types"][0], "ALLOC")
            rows = {
                row["id"]: row
                for row in query.storage.rows("SELECT id, metadata FROM semantic_events")
            }
            metadata = json.loads(rows[paths[0]["points"][0]]["metadata"])
            self.assertEqual(metadata["relative_path"], "src/resource.cpp")
            self.assertEqual(metadata["subject"], "buffer")
            # `subject_from` is stage 4's addition, and it has to arrive
            # through the real publishing path -- it travels as JSON metadata
            # in a column the graph layer writes, so a key that exists on the
            # parsed event but not here would be invisible to every query.
            self.assertEqual(metadata["subject_from"], "assignment")


class SparseDFGTests(unittest.TestCase):
    """Stage 5B: the method-local data-flow edges, read from the publisher.

    Every fixture here goes through `publish`, because the minted rows are
    only real once they survived the id map, the dedup and the storage
    round-trip the same way a user's database sees them.
    """

    def publish_source(self, source: str):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        return publish(Path(holder.name), "alpha/dfg.cpp", source)

    @staticmethod
    def _points(events, edges):
        return materialize_points(events, edges)

    @staticmethod
    def _flow(edges):
        return [edge for edge in edges if edge.relation == DATA_FLOW_TO]

    @staticmethod
    def _typed(points, event_type):
        """(point_id, info) pairs of one event type, in (line, ordinal) order.

        The point id is the dict key in `materialize_points`, not a field of
        the record -- every assertion here names endpoints by id, so the pairs
        are what the assertions want.
        """
        return sorted(
            ((point_id, info) for point_id, info in points.items() if info["event_type"] == event_type),
            key=lambda pair: (pair[1]["start_line"], pair[1]["ordinal"]),
        )

    def test_alloc_to_use_carries_the_intermediate_chain(self) -> None:
        """The edge is def-to-use, and the chain between them rides in `via`.

        `p[0] = 0` after `if (!p)` binds the allocation through the CHECK:
        one edge, allocation to use, with the CHECK's id in `via` so the
        propagation stays recoverable evidence instead of being collapsed
        away. `via_copies` stays empty -- nothing here is an alias.
        """
        events, edges = self.publish_source(
            "#include <stdlib.h>\n"
            "void f(void) {\n"
            "  char *p = (char *)malloc(8);\n"
            "  if (!p) { return; }\n"
            "  p[0] = 0;\n"
            "}\n"
        )
        points = self._points(events, edges)
        allocs = self._typed(points, "ALLOC")
        self.assertEqual(len(allocs), 1)
        flow = self._flow(edges)
        self.assertTrue(flow)
        for edge in flow:
            self.assertEqual(edge.metadata["flow_class"], "alloc_to_use")
            self.assertEqual(edge.metadata["via_copies"], [])
        check_ids = {point_id for point_id, info in self._typed(points, "CHECK")}
        through_check = [edge for edge in flow if check_ids & set(edge.metadata["via"])]
        self.assertTrue(through_check)

    def test_an_intervening_definition_kills_the_chain(self) -> None:
        """A use binds the most recent live definition, and the older one is
        gone: the kill rule in value form. The first allocation mints no flow
        edge at all -- an O(#uses) shape, not O(defs x uses)."""
        events, edges = self.publish_source(
            "#include <stdlib.h>\n"
            "static char *g_p;\n"
            "void f(void) {\n"
            "  g_p = (char *)malloc(8);\n"
            "  g_p = (char *)malloc(16);\n"
            "  char *q = g_p;\n"
            "}\n"
        )
        points = self._points(events, edges)
        reads = self._typed(points, "READ")
        self.assertEqual(len(reads), 1)
        read_id = reads[0][0]
        into_read = [edge for edge in self._flow(edges) if edge.dst_event_id == read_id]
        self.assertEqual(len(into_read), 1)
        allocs = self._typed(points, "ALLOC")
        self.assertEqual(len(allocs), 2)
        # The surviving def is the second allocation; the first minted no
        # flow edge. (It still owns the CFG's CONTROL_REACHES through-edges
        # -- every event point does -- which is exactly why the assertion
        # filters on the flow relation.)
        self.assertEqual(into_read[0].src_event_id, allocs[1][0])
        first_alloc_into = [edge for edge in self._flow(edges) if edge.src_event_id == allocs[0][0]]
        self.assertEqual(first_alloc_into, [])

    def test_release_feeds_the_uses_that_follow_it(self) -> None:
        """FREE -> USE is the same chain with the RELEASE as the definition:
        the free-state is what the later dereference reads."""
        events, edges = self.publish_source(
            "#include <stdlib.h>\n"
            "static char *g_p;\n"
            "void f(void) {\n"
            "  g_p = (char *)malloc(8);\n"
            "  free(g_p);\n"
            "  *g_p = 1;\n"
            "}\n"
        )
        points = self._points(events, edges)
        derefs = self._typed(points, "DEREFERENCE")
        releases = self._typed(points, "RELEASE")
        self.assertEqual(len(derefs), 1)
        self.assertEqual(len(releases), 1)
        into_deref = [edge for edge in self._flow(edges) if edge.dst_event_id == derefs[0][0]]
        self.assertEqual(len(into_deref), 1)
        self.assertEqual(into_deref[0].src_event_id, releases[0][0])
        self.assertEqual(into_deref[0].metadata["flow_class"], "free_to_use")

    def test_a_null_write_feeds_a_dereference_until_redefined(self) -> None:
        """NULL_SOURCE -> DEREFERENCE: `g_p = NULL` defines the null state,
        and the dereference binds it. A branch-arm redefinition is a later
        definition, so after one the dereference binds that instead -- the
        null class disappears, which is the registered branch kill."""
        events, edges = self.publish_source(
            "static int *g_p;\n"
            "void f(void) {\n"
            "  g_p = NULL;\n"
            "  *g_p = 1;\n"
            "}\n"
        )
        points = self._points(events, edges)
        derefs = self._typed(points, "DEREFERENCE")
        self.assertEqual(len(derefs), 1)
        into_deref = [edge for edge in self._flow(edges) if edge.dst_event_id == derefs[0][0]]
        self.assertEqual(len(into_deref), 1)
        self.assertEqual(into_deref[0].metadata["flow_class"], "null_to_deref")
        self.assertEqual(into_deref[0].metadata["via"], [])

        events, edges = self.publish_source(
            "static int *g_p;\n"
            "static int *g_q;\n"
            "void f(int c) {\n"
            "  g_p = NULL;\n"
            "  if (c) { g_p = g_q; }\n"
            "  *g_p = 1;\n"
            "}\n"
        )
        points = self._points(events, edges)
        derefs = self._typed(points, "DEREFERENCE")
        self.assertEqual(len(derefs), 1)
        into_deref = [edge for edge in self._flow(edges) if edge.dst_event_id == derefs[0][0]]
        self.assertEqual(len(into_deref), 1)
        self.assertNotEqual(into_deref[0].metadata["flow_class"], "null_to_deref")

    def test_a_write_flows_into_the_calls_that_take_it(self) -> None:
        """INPUT -> SINK is the argument-root binding: the live definition
        whose subject root matches a call argument feeds the CALL, tagged
        `input_to_sink` with the root in the metadata. `malloc`'s own CALL
        row is not the sink -- the consuming call is."""
        events, edges = self.publish_source(
            "#include <stdlib.h>\n"
            "static char *g_buf;\n"
            "void f(void) {\n"
            "  g_buf = (char *)malloc(16);\n"
            "  consume(g_buf);\n"
            "}\n"
        )
        points = self._points(events, edges)
        # A CALL row names no callee (`matched_name` is the node type), so
        # the sink is pinned from the edge side: the input_to_sink edge whose
        # arg root is `g_buf`, landing on the CALL that takes `g_buf` as an
        # argument -- `malloc`'s own CALL row (its argument is a literal)
        # cannot be it.
        calls = self._typed(points, "CALL")
        self.assertEqual(len(calls), 2)
        into_call = [
            edge for edge in self._flow(edges)
            if edge.metadata["flow_class"] == "input_to_sink"
            and edge.metadata["arg_root"] == "g_buf"
        ]
        self.assertEqual(len(into_call), 1)
        sink = points[into_call[0].dst_event_id]
        self.assertEqual(sink["event_type"], "CALL")
        self.assertEqual(sink["subject"], "g_buf")

    def test_every_flow_edge_is_intra_method(self) -> None:
        """Both endpoints of a flow edge answer to the edge's owner symbol --
        the construction guarantee that keeps the incremental invariants
        true without a special case."""
        events, edges = self.publish_source(
            "#include <stdlib.h>\n"
            "static char *g_a;\n"
            "static char *g_b;\n"
            "void f(void) {\n"
            "  g_a = (char *)malloc(8);\n"
            "  g_b = g_a;\n"
            "  *g_b = 1;\n"
            "}\n"
            "void g(void) {\n"
            "  *g_a = 2;\n"
            "}\n"
        )
        owners = {event.event_id: event.owner_symbol_id for event in events}
        flow = self._flow(edges)
        self.assertTrue(flow)
        for edge in flow:
            self.assertEqual(edge.owner_symbol_id, owners[edge.src_event_id])
            self.assertEqual(edge.owner_symbol_id, owners[edge.dst_event_id])

    def test_the_default_adjacency_never_sees_a_flow_edge(self) -> None:
        """`build_adjacency`'s relation filter is the load-bearing half of
        the same-table design: the default keeps every path walk exactly as
        it was, and only a consumer that names the flow relations gets them."""
        events, edges = self.publish_source(
            "#include <stdlib.h>\n"
            "static char *g_a;\n"
            "void f(void) {\n"
            "  g_a = (char *)malloc(8);\n"
            "  *g_a = 1;\n"
            "}\n"
        )
        flow_ids = {edge.edge_id for edge in self._flow(edges)}
        self.assertTrue(flow_ids)
        default = build_adjacency(edges)
        default_ids = {entry[0] for neighbours in default.values() for entry in neighbours}
        self.assertEqual(default_ids & flow_ids, set())
        widened = build_adjacency(edges, relations=frozenset({CONTROL_REACHES, DATA_FLOW_TO}))
        widened_ids = {entry[0] for neighbours in widened.values() for entry in neighbours}
        self.assertTrue(flow_ids <= widened_ids)

    def test_alias_is_metadata_and_never_a_relation(self) -> None:
        """MAY_ALIAS stays candidate-grade: the bare copy stamps `alias` on
        the ends that have events, and no edge in the table says ALIAS."""
        events, edges = self.publish_source(
            "static char *g_a;\n"
            "static char *g_b;\n"
            "void f(void) {\n"
            "  g_b = g_a;\n"
            "  *g_b = 1;\n"
            "  *g_a = 2;\n"
            "}\n"
        )
        points = self._points(events, edges)
        # The copy's own WRITE row carries the candidate; `write_source` and
        # `alias` name the same opposite root from the write and read sides.
        copy = next(info for info in points.values()
                    if info["event_type"] == "WRITE" and info.get("alias"))
        self.assertEqual(copy["subject"], "g_b")
        self.assertEqual(copy["write_source"], "g_a")
        self.assertEqual(copy["alias"], "g_a")
        # The dereference of `g_b` -- a later point of the copied name --
        # carries the candidate too.
        derefs = [info for info in points.values() if info["event_type"] == "DEREFERENCE"]
        self.assertEqual(len(derefs), 2)
        by_subject = {info["subject"]: info for info in derefs}
        self.assertEqual(by_subject["g_b"]["alias"], "g_a")
        self.assertEqual(by_subject["g_a"]["alias"], "g_b")
        for edge in edges:
            self.assertNotIn("ALIAS", edge.relation)

    def test_parameter_writes_publish_without_moving_an_edge(self) -> None:
        """The 5B parameter fact: the write side is a row, and a row only --
        no edge of any relation touches it, in this fixture with no in-frame
        consumer. The cross-method composition happens at query time."""
        events, edges = self.publish_source(
            "void f(int n) {\n"
            "  n = 1;\n"
            "}\n"
        )
        points = self._points(events, edges)
        param_writes = [(pid, info) for pid, info in self._typed(points, "WRITE")
                        if info.get("subject_storage") == "parameter"]
        self.assertEqual(len(param_writes), 1)
        param_id = param_writes[0][0]
        for edge in edges:
            self.assertNotEqual(edge.src_event_id, param_id)
            self.assertNotEqual(edge.dst_event_id, param_id)


if __name__ == "__main__":
    unittest.main()

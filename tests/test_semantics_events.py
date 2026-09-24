from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from provenlattice.graph import full_index
from provenlattice.identity import event_id, path_id, repository_id, symbol_id
from provenlattice.incremental import incremental_update
from provenlattice.parsing.cpp import CppTreeSitterParser
from provenlattice.parsing.events import OwnerIndex, OwnerSpan
from provenlattice.parsing.tree_sitter import TreeSitterParser
from provenlattice.storage import SQLiteStorage

REPO = repository_id(Path("."))


def ids_by_owner(parsed, relative_path: str) -> list[tuple[str, str, str]]:
    """(owner, event_type, event_id) for every event, using the real identity
    functions rather than a reimplementation of them."""
    result = []
    for event in parsed.events:
        owner = symbol_id(
            REPO, relative_path, event.owner_kind, event.owner_qualified_name, event.owner_signature
        )
        result.append(
            (event.owner_qualified_name, event.event_type, event_id(REPO, owner, event.event_type, event.ordinal))
        )
    return result


def cpp_events(source: bytes, path: str = "src/sample.cpp"):
    return CppTreeSitterParser().parse_bytes(source, path).parsed


def py_events(source: bytes, path: str = "pkg/sample.py"):
    return TreeSitterParser().parse_bytes(source, path).parsed


class CppEventTests(unittest.TestCase):
    def test_allocation_and_release_are_found(self) -> None:
        parsed = cpp_events(b"void f() {\n  char *p = (char *)malloc(8);\n  free(p);\n}\n")
        kinds = [(event.event_type, event.matched_name) for event in parsed.events]
        self.assertIn(("ALLOC", "malloc"), kinds)
        self.assertIn(("RELEASE", "free"), kinds)

    def test_owner_is_the_enclosing_function_not_a_local_variable(self) -> None:
        """`char *p = malloc(8);` is a declaration, and a declaration's
        declarator is easy to mistake for a function. If it were, this event
        would be owned by a symbol named `f.p` that should not exist."""
        parsed = cpp_events(b"void f() {\n  char *p = (char *)malloc(8);\n}\n")
        self.assertEqual([event.owner_qualified_name for event in parsed.events], ["f"] * len(parsed.events))
        self.assertEqual([symbol.qualified_name for symbol in parsed.symbols], ["f"])

    def test_calls_inside_a_declaration_still_produce_a_call_edge(self) -> None:
        """The same confusion used to swallow the CALLS edge entirely, because
        treating the declaration as a function stopped the tree walk before the
        initialiser was visited."""
        parsed = cpp_events(b"void f() {\n  char *p = (char *)create(8);\n}\n")
        self.assertIn("create", [reference.target for reference in parsed.references])

    def test_raii_lock_is_read_from_the_declared_type(self) -> None:
        """`std::lock_guard` acquires in its constructor, so no call in the body
        mentions it. Brace initialisation is used because the parenthesised
        form is the most vexing parse -- C++ really does read
        `guard(m)` as a function declaration, and so does tree-sitter."""
        parsed = cpp_events(b"void f() {\n  std::lock_guard<std::mutex> guard{m};\n}\n")
        locks = [event for event in parsed.events if event.event_type == "LOCK"]
        self.assertEqual(len(locks), 1)
        self.assertEqual(locks[0].matched_name, "std.lock_guard")
        self.assertEqual(locks[0].matched_via, "qualified")
        self.assertEqual(locks[0].owner_qualified_name, "f")

    def test_one_declaration_starting_two_threads_reports_two_events(self) -> None:
        parsed = cpp_events(b"void f() {\n  std::thread a{w}, b{w};\n}\n")
        spawns = [event for event in parsed.events if event.event_type == "THREAD_SPAWN"]
        self.assertEqual(len(spawns), 2)
        self.assertEqual(sorted(event.ordinal for event in spawns), [0, 1])

    def test_an_ambiguous_keyword_yields_both_events_flagged(self) -> None:
        parsed = cpp_events(b"void f() {\n  WaitForSingleObject(h, 10);\n}\n")
        flagged = [event for event in parsed.events if "ambiguous_keyword" in event.flags]
        self.assertEqual(
            sorted(event.event_type for event in flagged), ["LOCK", "THREAD_JOIN"]
        )
        # Both carry the same evidence, so a consumer can see why it is unsure.
        self.assertEqual({event.matched_name for event in flagged}, {"WaitForSingleObject"})

    def test_ordinals_are_source_ordered_not_walk_ordered(self) -> None:
        """A pre-order walk visits the outer call of `f(g(x))` first, which
        would number `g` after `f` even though it starts earlier. An ordinal a
        reader cannot reproduce from the file is worse than no ordinal."""
        parsed = cpp_events(b"void f() {\n  outer(inner());\n  later();\n}\n")
        calls = [event for event in parsed.events if event.event_type == "CALL"]
        by_line = [(event.start_line, event.ordinal) for event in calls]
        self.assertEqual(by_line, sorted(by_line))

    def test_ordinals_are_counted_per_event_type(self) -> None:
        parsed = cpp_events(b"void f() {\n  malloc(1);\n  free(0);\n  malloc(2);\n}\n")
        allocs = sorted(e.ordinal for e in parsed.events if e.event_type == "ALLOC")
        releases = sorted(e.ordinal for e in parsed.events if e.event_type == "RELEASE")
        self.assertEqual(allocs, [0, 1])
        self.assertEqual(releases, [0])


class EventIdentityTests(unittest.TestCase):
    def test_editing_one_method_leaves_another_methods_events_untouched(self) -> None:
        """This is the whole point of method-anchored identity (appendix B.2.1).

        A statement-level id -- hash(file, line, ...) -- would renumber every
        event below the edit and shatter the incremental delta. Anchoring to
        the owning method plus an ordinal means an edit to `a` cannot reach
        `b`, however much it moves `b` down the file.
        """
        before = cpp_events(
            b"void a() {\n  malloc(1);\n}\n\nvoid b() {\n  malloc(2);\n  free(0);\n}\n"
        )
        after = cpp_events(
            b"void a() {\n  malloc(9);\n  malloc(8);\n  malloc(7);\n}\n\nvoid b() {\n  malloc(2);\n  free(0);\n}\n"
        )
        b_before = {row for row in ids_by_owner(before, "src/sample.cpp") if row[0] == "b"}
        b_after = {row for row in ids_by_owner(after, "src/sample.cpp") if row[0] == "b"}
        self.assertTrue(b_before)
        self.assertEqual(b_before, b_after)

    def test_reformatting_does_not_change_any_event_id(self) -> None:
        compact = cpp_events(b"void f() {\n  malloc(1);\n  free(0);\n}\n")
        spread = cpp_events(b"void f() {\n\n\n      malloc(1);\n\n      free(0);\n\n}\n")
        self.assertEqual(
            sorted(row[2] for row in ids_by_owner(compact, "src/sample.cpp")),
            sorted(row[2] for row in ids_by_owner(spread, "src/sample.cpp")),
        )

    def test_extraction_is_deterministic(self) -> None:
        source = b"void f() {\n  pthread_mutex_lock(&m);\n  pthread_mutex_unlock(&m);\n}\n"
        first = cpp_events(source)
        second = cpp_events(source)
        self.assertEqual(
            [event.to_dict() if hasattr(event, "to_dict") else event for event in first.events],
            [event for event in second.events],
        )
        self.assertEqual(first.events, second.events)


class PythonEventTests(unittest.TestCase):
    def test_locking_and_threads_are_found(self) -> None:
        parsed = py_events(
            b"import threading\n"
            b"def f(lock, t):\n"
            b"    lock.acquire()\n"
            b"    lock.release()\n"
            b"    t.start()\n"
            b"    t.join()\n"
        )
        kinds = {event.event_type for event in parsed.events}
        self.assertLessEqual({"LOCK", "UNLOCK", "THREAD_SPAWN", "THREAD_JOIN"}, kinds)
        self.assertTrue(all(event.owner_qualified_name == "pkg.sample.f" for event in parsed.events))

    def test_a_qualified_and_a_bare_match_are_one_event(self) -> None:
        """`sys.exit(1)` matches `exit` on the bare axis and `sys.exit` on the
        qualified axis. Recording both would report two throws for one throw."""
        parsed = py_events(b"import sys\ndef f():\n    sys.exit(1)\n")
        throws = [event for event in parsed.events if event.event_type == "THROW"]
        self.assertEqual(len(throws), 1)
        self.assertEqual(throws[0].matched_name, "sys.exit")
        self.assertEqual(throws[0].matched_via, "qualified")

    def test_a_nested_function_owns_its_own_events(self) -> None:
        parsed = py_events(
            b"def outer():\n"
            b"    def inner():\n"
            b"        malloc(1)\n"
            b"    inner()\n"
        )
        owners = {event.owner_qualified_name for event in parsed.events}
        self.assertIn("pkg.sample.outer.inner", owners)
        self.assertIn("pkg.sample.outer", owners)


class OwnerIndexTests(unittest.TestCase):
    def test_innermost_span_wins(self) -> None:
        index = OwnerIndex([
            OwnerSpan(0, 100, "Function", "outer", "()"),
            OwnerSpan(10, 40, "Function", "inner", "()"),
        ])
        self.assertEqual(index.owner_of(20, 30).qualified_name, "inner")
        self.assertEqual(index.owner_of(50, 60).qualified_name, "outer")

    def test_a_position_outside_every_span_has_no_owner(self) -> None:
        index = OwnerIndex([OwnerSpan(10, 20, "Function", "f", "()")])
        self.assertIsNone(index.owner_of(0, 5))
        self.assertIsNone(index.owner_of(30, 35))

    def test_events_outside_any_symbol_are_counted_not_dropped(self) -> None:
        """A file-scope `static Foo *g = malloc(8);` is a real leak candidate.
        Stage 2 cannot attribute it to a method, and saying so is better than
        reporting a clean file."""
        parsed = cpp_events(b"static char *g = (char *)malloc(8);\nvoid f() {\n  free(0);\n}\n")
        # The file-scope malloc is gone from the event list...
        self.assertNotIn("ALLOC", [event.event_type for event in parsed.events])
        self.assertEqual(
            [event.event_type for event in parsed.events], ["CALL", "RELEASE"]
        )
        # ...but it is not gone from the record. Two, not one: that malloc is
        # both a CALL and an ALLOC, and neither is attributable to a method.
        self.assertEqual(parsed.unowned_events, 2)

    def test_a_file_with_nothing_unowned_says_so(self) -> None:
        parsed = cpp_events(b"void f() {\n  malloc(8);\n}\n")
        self.assertEqual(parsed.unowned_events, 0)


class ParseCacheTests(unittest.TestCase):
    def test_events_are_not_written_into_the_parse_cache(self) -> None:
        """Appendix B.2.3. `parsed` is already persisted twice -- once in
        `nodes.metadata.parsed` and once in `parser_cache` -- and is 94.8% of
        the nodes table. Caching the event list there as well would keep three
        copies of every event, and `semantic_events` is the only one read."""
        from provenlattice.graph import parsed_from_dict, parsed_to_dict

        parsed = cpp_events(b"void f() {\n  malloc(8);\n}\n")
        self.assertTrue(parsed.events)
        cached = parsed_to_dict(parsed)
        self.assertNotIn("events", cached)
        self.assertNotIn("unowned_events", cached)
        # A file restored from the cache carries no events rather than stale
        # ones; its events come from the table.
        self.assertEqual(parsed_from_dict(cached).events, [])
        self.assertEqual(parsed_from_dict(cached).symbols, parsed.symbols)


LEAK_BEFORE = """#include <stdlib.h>
#include <string.h>

namespace storage {

// 4.1 memory leak: the early return skips the free.
int load_index(const char *name) {
  char *buffer = (char *)malloc(256);
  if (!buffer) {
    return -1;
  }
  if (strlen(name) > 255) {
    return -1;  // buffer is never released
  }
  free(buffer);
  return 0;
}

}
"""

# The same file with the leak fixed: one `free` added inside the strlen branch,
# signature untouched. This is the smallest edit that changes what the event
# layer knows and nothing that the code graph knows.
LEAK_AFTER = LEAK_BEFORE.replace(
    "  if (strlen(name) > 255) {\n    return -1;  // buffer is never released\n  }",
    "  if (strlen(name) > 255) {\n    free(buffer);\n    return -1;\n  }",
)

BETA_SOURCE = """#include <stdlib.h>
#include <string.h>

namespace util {

int copy_name(const char *text) {
  char *copy = (char *)malloc(64);
  strcpy(copy, text);
  free(copy);
  return 0;
}

}
"""


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class SemanticDirtyTriggerTests(unittest.TestCase):
    """Acceptance criterion 3: appendix B.2.2.

    The plan is explicit that no test written before this one can catch this.
    The parity tests compare full against incremental and ask whether the two
    *results agree*; what fails here is the *trigger* -- whether the update
    notices it has work to do at all. A body-only edit leaves the code graph
    byte-identical and `api_fingerprint` unmoved, so an update consulting only
    that fingerprint concludes there is nothing to recompute, and the event
    table goes on describing code that no longer exists. Nothing downstream
    can detect that: the stale rows are well-formed, their ids are still
    correct, and the symbol they point at is still there.
    """

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "repo"
        self.database = Path(self.temp.name) / "graph.db"
        self.repo_id = "repo-semantic-dirty"
        write(self.root / "alpha" / "leak.cpp", LEAK_BEFORE)
        write(self.root / "beta" / "util.cpp", BETA_SOURCE)
        full_index(self.root, self.database, repository_id_override=self.repo_id)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def update(self) -> dict:
        return incremental_update(
            self.root, self.database, repository_id_override=self.repo_id
        )

    def shard_of(self, path: str) -> str:
        with closing(sqlite3.connect(self.database)) as connection:
            row = connection.execute(
                "SELECT shard_id FROM shards WHERE path = ?", (path,)
            ).fetchone()
        self.assertIsNotNone(row, f"no shard for {path}")
        return row[0]

    def events_in(self, relative_path: str) -> list:
        file_id = path_id(self.repo_id, "File", relative_path)
        with SQLiteStorage(self.database) as storage:
            return [
                event for event in storage.semantic_events(self.repo_id)
                if event.file_id == file_id
            ]

    def test_a_body_only_edit_is_semantically_dirty_even_though_the_api_is_not(self) -> None:
        alpha = self.shard_of("alpha")
        before = self.events_in("alpha/leak.cpp")
        self.assertEqual(
            sorted(event.event_type for event in before).count("RELEASE"), 1
        )

        write(self.root / "alpha" / "leak.cpp", LEAK_AFTER)
        delta = self.update()["delta"]

        # The code graph genuinely cannot see this change, which is the whole
        # reason the second fingerprint exists...
        self.assertEqual(delta["boundary_dirty"], [])
        self.assertEqual(
            delta["old_api_fingerprint"][alpha], delta["new_api_fingerprint"][alpha]
        )
        # ...so the semantic fingerprint is the only thing that moves.
        self.assertEqual(delta["semantic_dirty"], [alpha])

        # And the signal has a consequence rather than merely being set: the
        # table now holds the fixed function's events, not the leaked one's.
        after = self.events_in("alpha/leak.cpp")
        self.assertEqual(
            sorted(event.event_type for event in after).count("RELEASE"), 2
        )

    def test_a_shard_whose_files_did_not_change_is_not_semantically_dirty(self) -> None:
        """The flag is per-shard, not "something somewhere changed"."""
        alpha = self.shard_of("alpha")
        beta = self.shard_of("beta")
        self.assertNotEqual(alpha, beta)

        write(
            self.root / "beta" / "util.cpp",
            BETA_SOURCE.replace("malloc(64)", "malloc(128)"),
        )
        delta = self.update()["delta"]

        self.assertEqual(delta["semantic_dirty"], [beta])
        self.assertNotIn(alpha, delta["semantic_dirty"])

    def test_an_edit_that_changes_no_event_still_marks_the_shard_dirty(self) -> None:
        """The fingerprint is deliberately coarse: it says "a source in here
        changed", not "an event changed". Reformatting this file adds no event,
        drops none, and moves no id -- and the shard is dirty anyway.

        That is the intended trade (appendix B.2.2). Recomputing a shard that
        did not need it costs time; missing one that did costs correctness, and
        only one of those is recoverable. This test exists to stop the coarse
        signal being "optimized" into a false negative later.
        """
        alpha = self.shard_of("alpha")
        before = sorted(event.event_id for event in self.events_in("alpha/leak.cpp"))

        write(
            self.root / "alpha" / "leak.cpp",
            LEAK_BEFORE.replace(
                "  char *buffer = (char *)malloc(256);",
                "  char  *buffer  =  (char *)malloc(256);",
            ),
        )
        delta = self.update()["delta"]

        self.assertEqual(delta["semantic_dirty"], [alpha])
        self.assertEqual(delta["boundary_dirty"], [])
        self.assertEqual(
            sorted(event.event_id for event in self.events_in("alpha/leak.cpp")),
            before,
        )


if __name__ == "__main__":
    unittest.main()

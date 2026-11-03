"""SUB-04 (R4) per-access lockset tests (issue-pilot #40).

Freezes the per-access ``held_locks`` projection and the lock identity relations
(SAME_LOCK / DIFFERENT_LOCK / PROTECTS / UNLOCK_OMITTED_ON_PATH) laid out in the
frozen G0 lock contract (``cwe1123_v2_query_contract.md`` §3):

* ``held_locks`` is attributed by provable object identity, never display name;
* only a provably-identified **blocking** acquisition is held -- a try/timed
  acquire may fail, a RAII acquisition is released at scope exit, and an
  inexact/empty identity is never promoted to a held lock;
* every record and relation is ``candidate_state: "ambiguous"`` -- the analysis
  never asserts a decisive "definitely held";
* every relation (PROTECTS/SAME_LOCK/DIFFERENT_LOCK/UNLOCK_OMITTED_ON_PATH) is
  derived from the access projection, never from a new points-to/underlying
  relation.
"""

import json
import tempfile
import unittest
from pathlib import Path

from provenlattice.analysis_concurrency_context import query_access_lockset
from provenlattice.graph import full_index
from provenlattice.orchestrator import analyze_scope


def write(root: Path, relative: str, value: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value.encode("utf-8"))


class AccessLocksetBase(unittest.TestCase):
    FIXTURE = ""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "repo"
        write(self.root, "src/a.c", self.FIXTURE)
        self.database = Path(self.temp.name) / "graph.db"
        full_index(self.root, self.database)
        analyze_scope(self.root, "src", ("events", "sparse_cfg"),
                      database=self.database)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def lockset(self) -> dict:
        result = query_access_lockset(
            self.root, file_path="src/a.c", database=self.database)
        self.assertEqual(result["status"], "available", result.get("reason"))
        return result

    def accesses(self, subject: str) -> list[dict]:
        return [
            record for record in self.lockset()["access_locksets"]
            if record["subject"] == subject
        ]

    def relations(self, relation: str) -> list[dict]:
        return [
            rel for rel in self.lockset()["relations"]
            if rel["relation"] == relation
        ]


class SameLockProtectedTests(AccessLocksetBase):
    FIXTURE = (
        "#include <pthread.h>\n"
        "pthread_mutex_t g_lock;\n"
        "int g_count;\n"
        "void inc(void) {\n"
        "    pthread_mutex_lock(&g_lock);\n"
        "    g_count++;\n"
        "    pthread_mutex_unlock(&g_lock);\n"
        "}\n"
        "void dec(void) {\n"
        "    pthread_mutex_lock(&g_lock);\n"
        "    g_count++;\n"
        "    pthread_mutex_unlock(&g_lock);\n"
        "}\n"
    )

    def test_shared_subject_is_held_under_the_same_lock(self) -> None:
        for record in self.accesses("g_count"):
            self.assertEqual(record["held_locks"], ["g_lock"])
            self.assertEqual(record["candidate_state"], "ambiguous")

    def test_no_different_lock_on_shared_subject(self) -> None:
        different = [
            rel for rel in self.relations("DIFFERENT_LOCK")
            if rel["subject"] == "global:g_count"
        ]
        self.assertEqual(different, [])

    def test_same_lock_relation_is_by_identity_not_name(self) -> None:
        same = [
            rel for rel in self.relations("SAME_LOCK")
            if rel["subject"] == "global:g_count"
        ]
        self.assertTrue(same)
        for rel in same:
            self.assertEqual(rel["locks"], ["g_lock"])

    def test_every_record_is_ambiguous(self) -> None:
        bundle = self.lockset()
        for record in bundle["access_locksets"]:
            self.assertEqual(record["candidate_state"], "ambiguous")


class DifferentLockTests(AccessLocksetBase):
    FIXTURE = (
        "#include <pthread.h>\n"
        "pthread_mutex_t a, b;\n"
        "int g_count;\n"
        "void f(void) {\n"
        "    pthread_mutex_lock(&a);\n"
        "    g_count++;\n"
        "    pthread_mutex_unlock(&a);\n"
        "}\n"
        "void g(void) {\n"
        "    pthread_mutex_lock(&b);\n"
        "    g_count++;\n"
        "    pthread_mutex_unlock(&b);\n"
        "}\n"
    )

    def test_cross_function_accesses_see_different_locks(self) -> None:
        held = {}
        for record in self.accesses("g_count"):
            held.setdefault(record["owner"], set()).update(record["held_locks"])
        self.assertEqual(held.get("f"), {"a"})
        self.assertEqual(held.get("g"), {"b"})

    def test_different_lock_relation_is_both_protected(self) -> None:
        different = [
            rel for rel in self.relations("DIFFERENT_LOCK")
            if rel["subject"] == "global:g_count"
        ]
        self.assertTrue(different)
        for rel in different:
            # Both halves hold *some* lock, just not the same one.
            self.assertFalse(rel["unprotected"])


class UnprotectedTests(AccessLocksetBase):
    FIXTURE = (
        "int g_count;\n"
        "void f(void) { g_count++; }\n"
        "void g(void) { g_count++; }\n"
    )

    def test_unlocked_access_is_unprotected(self) -> None:
        for record in self.accesses("g_count"):
            self.assertEqual(record["held_locks"], [])
        self.assertEqual(self.lockset()["coverage"]["protected_accesses"], 0)

    def test_no_lock_relations_without_a_lock(self) -> None:
        self.assertEqual(self.relations("PROTECTS"), [])
        self.assertEqual(self.relations("SAME_LOCK"), [])
        self.assertEqual(self.relations("UNLOCK_OMITTED_ON_PATH"), [])


class UnlockOmittedTests(AccessLocksetBase):
    FIXTURE = (
        "#include <pthread.h>\n"
        "pthread_mutex_t g_lock;\n"
        "int g_count;\n"
        "void f(void) {\n"
        "    pthread_mutex_lock(&g_lock);\n"
        "    g_count++;\n"
        "}\n"
    )

    def test_omitted_unlock_is_flagged_not_fabricated(self) -> None:
        for record in self.accesses("g_count"):
            self.assertEqual(record["held_locks"], ["g_lock"])
        omitted = self.relations("UNLOCK_OMITTED_ON_PATH")
        self.assertTrue(omitted)
        for rel in omitted:
            self.assertEqual(rel["lock"], "g_lock")
            self.assertEqual(rel["candidate_state"], "ambiguous")


class TryTimedNotHeldTests(AccessLocksetBase):
    FIXTURE = (
        "#include <pthread.h>\n"
        "pthread_mutex_t g_lock;\n"
        "int g_count;\n"
        "void f(void) {\n"
        "    pthread_mutex_trylock(&g_lock);\n"
        "    g_count++;\n"
        "    pthread_mutex_unlock(&g_lock);\n"
        "}\n"
    )

    def test_try_acquire_is_never_promoted_to_held(self) -> None:
        for record in self.accesses("g_count"):
            self.assertEqual(record["held_locks"], [])
        self.assertEqual(self.relations("PROTECTS"), [])


class LoopProtectedTests(AccessLocksetBase):
    """Regression: a held lock must survive a for/while loop back-edge.

    The must-hold analysis must start non-entry nodes from TOP (the function's
    full lock universe) and shrink with meet = intersection; a loop back-edge
    whose first-round value is empty must not under-approximate the header to
    "not held".
    """

    FIXTURE = (
        "#include <pthread.h>\n"
        "pthread_mutex_t g_lock;\n"
        "int g_count;\n"
        "void f(void) {\n"
        "    pthread_mutex_lock(&g_lock);\n"
        "    for (int i = 0; i < 10; i++) {\n"
        "        g_count++;\n"
        "    }\n"
        "    pthread_mutex_unlock(&g_lock);\n"
        "}\n"
    )

    def test_loop_body_access_is_held(self) -> None:
        for record in self.accesses("g_count"):
            self.assertEqual(record["held_locks"], ["g_lock"])
        self.assertTrue(self.relations("PROTECTS"))


class TwoLockLoopTests(AccessLocksetBase):
    """Regression: the flow-successor attribution must follow source line
    order, so a loop body under the first lock is not attributed to a later
    lock's release."""

    FIXTURE = (
        "#include <pthread.h>\n"
        "pthread_mutex_t a, b;\n"
        "int g_count;\n"
        "void f(void) {\n"
        "    pthread_mutex_lock(&a);\n"
        "    for (int i = 0; i < 3; i++) {\n"
        "        g_count++;\n"
        "    }\n"
        "    pthread_mutex_unlock(&a);\n"
        "    pthread_mutex_lock(&b);\n"
        "    g_count++;\n"
        "    pthread_mutex_unlock(&b);\n"
        "}\n"
    )

    def test_loop_body_holds_first_lock_only(self) -> None:
        held_by_line = {}
        for record in self.accesses("g_count"):
            held_by_line[record["start_line"]] = record["held_locks"]
        self.assertEqual(held_by_line[7], ["a"])
        self.assertEqual(held_by_line[11], ["b"])


class RaiiNotHeldNotLeakTests(AccessLocksetBase):
    FIXTURE = (
        "#include <mutex>\n"
        "std::mutex m;\n"
        "int g_count;\n"
        "void f(void) {\n"
        "    std::lock_guard<std::mutex> guard(m);\n"
        "    g_count++;\n"
        "}\n"
    )

    def test_raii_is_released_at_scope_exit_not_held(self) -> None:
        for record in self.accesses("g_count"):
            self.assertEqual(record["held_locks"], [])
        # Not held means there can be no unpaired release to flag as a leak.
        self.assertEqual(self.relations("UNLOCK_OMITTED_ON_PATH"), [])


class InexactIdentityTests(AccessLocksetBase):
    FIXTURE = (
        "struct pool { struct mutex_holder *slots; };\n"
        "struct mutex_holder { void lock(); void unlock(); };\n"
        "int g_count;\n"
        "void touch(struct pool *p, int i) {\n"
        "    p->slots[i].lock();\n"
        "    g_count++;\n"
        "    p->slots[i].unlock();\n"
        "}\n"
    )

    def test_inexact_identity_is_not_promoted_and_flagged(self) -> None:
        for record in self.accesses("g_count"):
            self.assertEqual(record["held_locks"], [])
            self.assertIn("lock_identity_inexact", record["limitation"])


class DeterminismTests(AccessLocksetBase):
    FIXTURE = (
        "#include <pthread.h>\n"
        "pthread_mutex_t g_lock;\n"
        "int g_count;\n"
        "void f(void) {\n"
        "    pthread_mutex_lock(&g_lock);\n"
        "    g_count++;\n"
        "    pthread_mutex_unlock(&g_lock);\n"
        "}\n"
    )

    def test_deterministic_output(self) -> None:
        first = self.lockset()
        second = query_access_lockset(
            self.root, file_path="src/a.c", database=self.database)
        self.assertEqual(first, second)
        self.assertEqual(
            json.dumps(first, sort_keys=True),
            json.dumps(second, sort_keys=True),
        )


class StatusPassthroughTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "repo"
        self.database = Path(self.temp.name) / "graph.db"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_not_built_preserved(self) -> None:
        write(self.root, "src/a.c", "int g; void f(void) { g++; }\n")
        full_index(self.root, self.database)
        result = query_access_lockset(
            self.root, file_path="src/a.c", database=self.database)
        self.assertEqual(result["status"], "not_built")

    def test_python_reports_unsupported(self) -> None:
        write(self.root, "src/a.py", "def a():\n    return 1\n")
        full_index(self.root, self.database)
        result = query_access_lockset(
            self.root, file_path="src/a.py", database=self.database)
        self.assertEqual(result["status"], "unsupported")


if __name__ == "__main__":
    unittest.main()
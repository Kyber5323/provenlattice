"""S3 Lock Semantics V1 tests (issue-pilot #29).

Covers the lock facts the events layer must now carry on LOCK/UNLOCK events:
``lock_identity`` (subject root + exactness), ``acquire_kind``
(blocking/try/timed/wait) and ``raii``. The facts are read back through the
read-only events query surface, the same way the S5 lock scenarios will consume
them.
"""

import tempfile
import unittest
from pathlib import Path

from provenlattice.analysis_query import query_analysis
from provenlattice.graph import full_index
from provenlattice.orchestrator import analyze_scope


def write(root: Path, relative: str, value: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value.encode("utf-8"))


class LockSemanticsBase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "repo"
        self.database = Path(self.temp.name) / "graph.db"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def analyze_events(self) -> None:
        full_index(self.root, self.database)
        analyze_scope(self.root, "src", ("events",), database=self.database)

    def events(self, relative: str) -> list[dict]:
        env = query_analysis(self.root, "events", file_path=relative,
                             database=self.database)
        self.assertEqual(env["status"], "available", env.get("reason"))
        return env["events"]

    def events_of_type(self, relative: str, event_type: str) -> list[dict]:
        return [e for e in self.events(relative) if e["event_type"] == event_type]


class BlockingLockTests(LockSemanticsBase):
    def setUp(self) -> None:
        super().setUp()
        write(self.root, "src/lock.c",
              "#include <pthread.h>\n"
              "pthread_mutex_t g_lock;\n"
              "void guarded(void) {\n"
              "    pthread_mutex_lock(&g_lock);\n"
              "    pthread_mutex_unlock(&g_lock);\n"
              "}\n")
        self.analyze_events()

    def test_pthread_mutex_lock_is_blocking_with_identity(self) -> None:
        locks = self.events_of_type("src/lock.c", "LOCK")
        self.assertEqual(len(locks), 1)
        lock = locks[0]
        self.assertEqual(lock["matched_name"], "pthread_mutex_lock")
        self.assertEqual(lock["acquire_kind"], "blocking")
        self.assertEqual(lock["lock_identity"], "g_lock")
        self.assertTrue(lock["subject_exact"])
        self.assertFalse(lock["raii"])

    def test_pthread_mutex_unlock_carries_same_identity(self) -> None:
        unlocks = self.events_of_type("src/lock.c", "UNLOCK")
        self.assertEqual(len(unlocks), 1)
        unlock = unlocks[0]
        self.assertEqual(unlock["matched_name"], "pthread_mutex_unlock")
        self.assertEqual(unlock["lock_identity"], "g_lock")
        self.assertTrue(unlock["subject_exact"])
        self.assertFalse(unlock["raii"])


class TryTimedLockTests(LockSemanticsBase):
    def setUp(self) -> None:
        super().setUp()
        write(self.root, "src/trylock.cc",
              "#include <mutex>\n"
              "std::mutex m;\n"
              "bool maybe(void) {\n"
              "    if (m.try_lock()) {\n"
              "        m.unlock();\n"
              "        return true;\n"
              "    }\n"
              "    return false;\n"
              "}\n")
        self.analyze_events()

    def test_member_try_lock_is_try_not_blocking(self) -> None:
        locks = self.events_of_type("src/trylock.cc", "LOCK")
        self.assertEqual(len(locks), 1)
        lock = locks[0]
        self.assertEqual(lock["matched_name"], "try_lock")
        self.assertEqual(lock["acquire_kind"], "try")
        self.assertEqual(lock["lock_identity"], "m")
        self.assertTrue(lock["subject_exact"])
        self.assertFalse(lock["raii"])

    def test_member_try_lock_failure_path_is_not_promoted(self) -> None:
        # A try-lock that fails leaves the lock unheld; the fact layer marks it
        # `try` (can fail), never a deterministic blocking acquisition.
        lock = self.events_of_type("src/trylock.cc", "LOCK")[0]
        self.assertNotEqual(lock["acquire_kind"], "blocking")


class WaitKindTests(LockSemanticsBase):
    def setUp(self) -> None:
        super().setUp()
        write(self.root, "src/wait.c",
              "#include <pthread.h>\n"
              "pthread_cond_t cond;\n"
              "pthread_mutex_t mtx;\n"
              "void waiter(void) {\n"
              "    pthread_mutex_lock(&mtx);\n"
              "    pthread_cond_wait(&cond, &mtx);\n"
              "    pthread_mutex_unlock(&mtx);\n"
              "}\n")
        self.analyze_events()

    def test_pthread_cond_wait_is_wait(self) -> None:
        waits = [e for e in self.events("src/wait.c")
                 if e["matched_name"] == "pthread_cond_wait"]
        self.assertEqual(len(waits), 1)
        wait = waits[0]
        self.assertEqual(wait["event_type"], "LOCK")
        self.assertEqual(wait["acquire_kind"], "wait")
        self.assertEqual(wait["lock_identity"], "cond")

    def test_blocking_lock_is_not_wait(self) -> None:
        locks = [e for e in self.events("src/wait.c")
                 if e["matched_name"] == "pthread_mutex_lock"]
        self.assertEqual(len(locks), 1)
        self.assertEqual(locks[0]["acquire_kind"], "blocking")


class C11WaitKindTests(LockSemanticsBase):
    def setUp(self) -> None:
        super().setUp()
        write(self.root, "src/c11_wait.c",
              "#include <threads.h>\n"
              "cnd_t cond;\n"
              "mtx_t mtx;\n"
              "int waiter(void) {\n"
              "    return cnd_wait(&cond, &mtx);\n"
              "}\n")
        self.analyze_events()

    def test_cnd_wait_is_wait(self) -> None:
        waits = [e for e in self.events("src/c11_wait.c")
                 if e["matched_name"] == "cnd_wait"]
        self.assertEqual(len(waits), 1)
        self.assertEqual(waits[0]["acquire_kind"], "wait")


class TimedAcquireKindTests(LockSemanticsBase):
    def setUp(self) -> None:
        super().setUp()
        write(self.root, "src/timed_lock.c",
              "#include <pthread.h>\n"
              "#include <time.h>\n"
              "pthread_mutex_t mtx;\n"
              "int timed(void) {\n"
              "    struct timespec deadline;\n"
              "    return pthread_mutex_timedlock(&mtx, &deadline);\n"
              "}\n")
        self.analyze_events()

    def test_pthread_mutex_timedlock_is_timed(self) -> None:
        locks = [e for e in self.events("src/timed_lock.c")
                 if e["matched_name"] == "pthread_mutex_timedlock"]
        self.assertEqual(len(locks), 1)
        self.assertEqual(locks[0]["acquire_kind"], "timed")


class RaiiLockTests(LockSemanticsBase):
    def setUp(self) -> None:
        super().setUp()
        write(self.root, "src/raii.cc",
              "#include <mutex>\n"
              "std::mutex m;\n"
              "void guarded(void) {\n"
              "    std::lock_guard<std::mutex> guard(m);\n"
              "}\n")
        self.analyze_events()

    def test_lock_guard_is_raii_with_argument_identity(self) -> None:
        locks = self.events_of_type("src/raii.cc", "LOCK")
        self.assertEqual(len(locks), 1)
        lock = locks[0]
        self.assertEqual(lock["matched_name"], "std.lock_guard")
        self.assertTrue(lock["raii"])
        # The lock identity is the constructor argument (`m`), not the handle
        # (`guard`), which a lock query cannot use.
        self.assertEqual(lock["lock_identity"], "m")
        self.assertTrue(lock["subject_exact"])
        self.assertEqual(lock["acquire_kind"], "blocking")

    def test_lock_guard_carries_lexical_scope_end(self) -> None:
        # Issue #69: a RAII acquisition carries its lexical scope end byte, so
        # a consumer can hold it from the declaration to that byte and not past.
        lock = self.events_of_type("src/raii.cc", "LOCK")[0]
        self.assertIn("raii_scope_end", lock)
        scope_end = lock["raii_scope_end"]
        self.assertIsInstance(scope_end, int)
        # The guard is declared in `guarded`'s body, so its scope end is the
        # function body's closing brace -- after the declaration's own end.
        self.assertGreater(scope_end, lock["end_byte"])


class InexactIdentityTests(LockSemanticsBase):
    def setUp(self) -> None:
        super().setUp()
        write(self.root, "src/member.cc",
              "struct pool { struct mutex_holder *slots; };\n"
              "struct mutex_holder { void lock(); void unlock(); };\n"
              "void touch(struct pool *p, int i) {\n"
              "    p->slots[i].lock();\n"
              "    p->slots[i].unlock();\n"
              "}\n")
        self.analyze_events()

    def test_member_path_names_root_and_marks_inexact(self) -> None:
        locks = self.events_of_type("src/member.cc", "LOCK")
        self.assertEqual(len(locks), 1)
        lock = locks[0]
        # `p->slots[i]` names the root `p`; the member path is left behind, so
        # the identity is honest about not being the whole designation.
        self.assertEqual(lock["lock_identity"], "p")
        self.assertFalse(lock["subject_exact"])


class BaselineStabilityTests(LockSemanticsBase):
    def test_non_lock_events_carry_no_lock_fields(self) -> None:
        write(self.root, "src/alloc.c",
              "#include <stdlib.h>\n"
              "void f(void) { char *p = malloc(8); free(p); }\n")
        self.analyze_events()
        for event in self.events("src/alloc.c"):
            self.assertNotIn("lock_identity", event)
            self.assertNotIn("acquire_kind", event)
            self.assertNotIn("raii", event)
            self.assertNotIn("raii_scope_end", event)
            self.assertNotIn("subject_exact", event)

    def test_non_raii_lock_carries_no_scope_end(self) -> None:
        # `raii_scope_end` is meaningful only on a RAII acquisition; a blocking
        # pthread lock has no lexical scope and must not carry the field.
        write(self.root, "src/lock.c",
              "#include <pthread.h>\n"
              "pthread_mutex_t g_lock;\n"
              "void guarded(void) {\n"
              "    pthread_mutex_lock(&g_lock);\n"
              "    pthread_mutex_unlock(&g_lock);\n"
              "}\n")
        self.analyze_events()
        locks = self.events_of_type("src/lock.c", "LOCK")
        self.assertEqual(len(locks), 1)
        self.assertNotIn("raii_scope_end", locks[0])


if __name__ == "__main__":
    unittest.main()

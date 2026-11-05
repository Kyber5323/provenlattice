"""S2 Thread / Shared State (Parent #28): concurrency_context facts.

Freezes the fail-closed fact contract for S4/S5:

1. a spawn→join pair is emitted only when the thread-handle identity is
   provably the same object -- the same ``subject_decl`` in the same owner with
   the spawn preceding the join. FE-5 stamps a handle on THREAD_JOIN, so a
   provable pair is asserted; a join with no recoverable handle (e.g.
   ``WaitForMultipleObjects``) stays unknown and ``thread_handle_identity`` is
   reported rather than pairing by owner + source order;
2. MAY_PARALLEL is a candidate (status ambiguous), keyed by S1's stable
   `subject_decl` identity, carrying its access events, spans and exactness;
3. an unresolved identity (unknown storage, no subject_decl) is marked
   `unresolved` and never merged with a different object by display name;
4. the capability is C/C++ only and preserves the sparse_cfg statuses.
"""

import json
import tempfile
import unittest
from pathlib import Path

from provenlattice.analysis import CAPABILITY_CONTRACT
from provenlattice.analysis_concurrency_context import (
    build_concurrency_context,
    query_concurrency_context,
)
from provenlattice.analysis_query import query_analysis
from provenlattice.graph import full_index
from provenlattice.orchestrator import analyze_scope


def write(root: Path, relative: str, value: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value.encode("utf-8"))


class ConcurrencyContextTestBase(unittest.TestCase):
    FIXTURE = ""
    FILE = "src/a.c"

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "repo"
        write(self.root, self.FILE, self.FIXTURE)
        self.database = Path(self.temp.name) / "graph.db"
        full_index(self.root, self.database)
        analyze_scope(self.root, "src", ("sparse_cfg",), database=self.database)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def events(self) -> list[dict]:
        env = query_analysis(
            self.root, "sparse_cfg", file_path=self.FILE, database=self.database)
        self.assertEqual(env["status"], "available", env.get("reason"))
        return env["events"]

    def context(self) -> dict:
        return build_concurrency_context(self.events())


class SpawnJoinIdentityTests(ConcurrencyContextTestBase):
    FIXTURE = (
        "#include <pthread.h>\n"
        "int g_shared;\n"
        "void *worker(void *arg) { g_shared++; return 0; }\n"
        "void two_threads(void) {\n"
        "    pthread_t a, b;\n"
        "    pthread_create(&a, NULL, worker, NULL);\n"
        "    pthread_create(&b, NULL, worker, NULL);\n"
        "    pthread_join(a, NULL);\n"
        "    pthread_join(b, NULL);\n"
        "}\n"
        "void touch(void) { g_shared = 1; }\n"
    )

    def test_spawns_carry_handle_and_entry(self):
        ctx = self.context()
        self.assertEqual(ctx["thread_entry_points"], ["worker"])
        handles = sorted(s["handle"] for s in ctx["spawns"])
        self.assertEqual(handles, ["a", "b"])

    def test_join_handle_identity_pairs_spawn_with_join(self):
        # Two threads are spawned and joined in the same method. FE-5 stamps a
        # handle on each THREAD_JOIN, so each join shares its spawn's
        # `handle_decl` and the pair is asserted -- never fabricated by
        # owner + source order alone.
        ctx = self.context()
        pairs = ctx["spawn_join_pairs"]
        self.assertEqual(len(pairs), 2)
        handles = sorted(p["handle_decl"] for p in pairs)
        self.assertEqual(handles, ["local:a", "local:b"])
        self.assertTrue(all(p["thread_function"] == "worker" for p in pairs))
        self.assertTrue(all(p["owner"] == "two_threads" for p in pairs))
        # the join now carries the handle, so no missing evidence / gap remains
        self.assertNotIn("thread_handle_identity", ctx["missing_evidence"])
        self.assertFalse(ctx["capability_gaps"])

    def test_join_carries_handle_and_identity(self):
        # Each join recovers the same handle as its spawn: first-argument
        # `pthread_join(a, NULL)` names `a`, and the declaration index turns it
        # into the same `subject_decl` token the spawn recorded.
        ctx = self.context()
        joins = sorted((j["handle"], j["handle_decl"]) for j in ctx["joins"])
        self.assertEqual(joins, [("a", "local:a"), ("b", "local:b")])

    def test_may_parallel_is_a_candidate_keyed_by_identity(self):
        ctx = self.context()
        shared = next(
            subject for subject in ctx["may_parallel"]
            if subject["subject"] == "g_shared"
        )
        self.assertEqual(shared["identity"], "global:g_shared")
        self.assertEqual(shared["status"], "ambiguous")
        self.assertFalse(shared["unresolved"])
        self.assertEqual(shared["owners"], ["touch", "worker"])
        self.assertEqual(shared["thread_entry_owners"], ["worker"])
        # accesses carry event ids, spans and exactness
        for access in shared["accesses"]:
            self.assertIn("event_id", access)
            self.assertIn("start_byte", access)
            self.assertIn("exact", access)

    def test_deterministic_output(self):
        events = self.events()
        first = build_concurrency_context(events)
        second = build_concurrency_context(list(events))
        self.assertEqual(first, second)
        self.assertEqual(
            json.dumps(first, sort_keys=True),
            json.dumps(second, sort_keys=True),
        )


class WaitForMultipleObjectsTests(ConcurrencyContextTestBase):
    FIXTURE = (
        "#include <pthread.h>\n"
        "void *worker(void *arg) { return 0; }\n"
        "void wait_all(unsigned n) {\n"
        "    unsigned hs[2];\n"
        "    WaitForMultipleObjects(n, hs, 1, 0);\n"
        "}\n"
    )

    def test_multi_handle_wait_is_unknown(self):
        # The first argument of WaitForMultipleObjects is the handle *count*,
        # not a handle, so no subject is recovered. Pairing a join against a
        # specific thread cannot be proved -- it stays unknown rather than
        # naming `n` as a handle.
        ctx = self.context()
        self.assertEqual(len(ctx["joins"]), 1)
        self.assertEqual(ctx["joins"][0]["handle"], "")
        self.assertEqual(ctx["joins"][0]["handle_decl"], "")
        self.assertEqual(ctx["spawn_join_pairs"], [])
        self.assertIn("thread_handle_identity", ctx["missing_evidence"])
        self.assertTrue(ctx["capability_gaps"])


class CppJoinReceiverTests(ConcurrencyContextTestBase):
    FILE = "src/a.cc"
    FIXTURE = (
        "#include <thread>\n"
        "void body01(void) {}\n"
        "void spawner01(void) {\n"
        "    std::thread t(body01);\n"
        "    t.join();\n"
        "}\n"
    )

    def test_member_join_recovers_receiver_handle(self):
        # `t.join()` has no argument; the handle is the member-call receiver,
        # recovered by the same rule that names a mutex for `m.lock()`. It
        # shares the spawn's `subject_decl`, so the pair is asserted.
        ctx = self.context()
        joins = [(j["handle"], j["handle_decl"]) for j in ctx["joins"]]
        self.assertEqual(joins, [("t", "local:t")])
        self.assertEqual(len(ctx["spawn_join_pairs"]), 1)
        self.assertEqual(ctx["spawn_join_pairs"][0]["handle_decl"], "local:t")
        self.assertNotIn("thread_handle_identity", ctx["missing_evidence"])


class UnrelatedAccessTests(ConcurrencyContextTestBase):
    FIXTURE = (
        "#include <pthread.h>\n"
        "int g_shared;\n"
        "int g_single;\n"
        "void *worker(void *arg) { return 0; }\n"
        "void spawner(void) { pthread_t t; pthread_create(&t, NULL, worker, NULL); }\n"
        "void touch_shared(void) { g_shared++; }\n"
        "void touch_both(void) { g_shared = 2; g_single = 3; }\n"
    )

    def test_single_owner_subject_is_not_a_candidate(self):
        # g_single is written by exactly one method; the presence of a spawn in
        # the file must not promote it to a MAY_PARALLEL candidate.
        ctx = self.context()
        subjects = {subject["subject"] for subject in ctx["may_parallel"]}
        self.assertIn("g_shared", subjects)
        self.assertNotIn("g_single", subjects)


class UnresolvedIdentityTests(ConcurrencyContextTestBase):
    FIXTURE = (
        "void a(void) { g_undeclared++; }\n"
        "void b(void) { g_undeclared = 2; }\n"
    )

    def test_unknown_identity_is_marked_unresolved(self):
        ctx = self.context()
        candidates = [
            subject for subject in ctx["may_parallel"]
            if subject["subject"] == "g_undeclared"
        ]
        self.assertEqual(len(candidates), 1)
        self.assertTrue(candidates[0]["unresolved"])
        self.assertIsNone(candidates[0]["identity"])
        self.assertIn("unresolved_identity", ctx["missing_evidence"])


class ResourceLifetimeOrderingTests(ConcurrencyContextTestBase):
    # R7 (SUB-08): a local mutex whose address is handed to a worker thread.
    # `m` dies when `f` returns; a join inside `f` proves the worker finished
    # before that destroy. Without one the worker may still be running against
    # a dead object: join_count == 0 and is_alive_at_access == false.
    FIXTURE = (
        "#include <pthread.h>\n"
        "void *worker(void *arg) {\n"
        "    pthread_mutex_t *pm = (pthread_mutex_t *)arg;\n"
        "    pthread_mutex_lock(pm);\n"
        "    pthread_mutex_unlock(pm);\n"
        "    return 0;\n"
        "}\n"
        "void f(void) {\n"
        "    pthread_mutex_t m;\n"
        "    pthread_mutex_init(&m, 0);\n"
        "    pthread_t t;\n"
        "    pthread_create(&t, 0, worker, &m);\n"
        "}\n"
    )

    def test_unjoined_worker_is_a_lifetime_violation(self):
        ctx = self.context()
        records = ctx["resource_lifetime"]
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertTrue(record["object"].startswith("local:src/a.c:f:m:"))
        self.assertEqual(record["object_decl"], "local:m")
        self.assertEqual(record["object_name"], "m")
        self.assertEqual(record["thread_function"], "worker")
        self.assertEqual(record["handle"], "t")
        self.assertEqual(record["handle_decl"], "local:t")
        self.assertIsNotNone(record["spawn_event_id"])
        self.assertEqual(record["join_count"], 0)
        self.assertFalse(record["is_alive_at_access"])
        self.assertEqual(record["lifecycle_sequence"], ["SPAWN", "DESTROY"])

    def test_deterministic_lifetime_output(self):
        events = self.events()
        first = build_concurrency_context(events)
        second = build_concurrency_context(list(events))
        self.assertEqual(first["resource_lifetime"], second["resource_lifetime"])


class JoinedLocalLifetimeTests(ConcurrencyContextTestBase):
    FIXTURE = (
        "#include <pthread.h>\n"
        "void *worker(void *arg) {\n"
        "    pthread_mutex_t *pm = (pthread_mutex_t *)arg;\n"
        "    pthread_mutex_lock(pm);\n"
        "    pthread_mutex_unlock(pm);\n"
        "    return 0;\n"
        "}\n"
        "void f(void) {\n"
        "    pthread_mutex_t m;\n"
        "    pthread_mutex_init(&m, 0);\n"
        "    pthread_t t;\n"
        "    pthread_create(&t, 0, worker, &m);\n"
        "    pthread_join(t, 0);\n"
        "}\n"
    )

    def test_joined_worker_is_not_a_lifetime_violation(self):
        # The good control example joins before scope exit, so no lifetime
        # violation is reported, and the spawn/join pair is still asserted by
        # handle identity.
        ctx = self.context()
        self.assertEqual(ctx["resource_lifetime"], [])
        self.assertEqual(len(ctx["spawn_join_pairs"]), 1)
        self.assertEqual(ctx["spawn_join_pairs"][0]["handle_decl"], "local:t")


class NonLocalEscapeLifetimeTests(ConcurrencyContextTestBase):
    # A global passed by address is not a frame-local object, so its lifetime is
    # not owned by `f` and no violation is claimed -- the STOP condition keeps a
    # non-local storage unknown rather than guessing.
    FIXTURE = (
        "#include <pthread.h>\n"
        "pthread_mutex_t g;\n"
        "void *worker(void *arg) {\n"
        "    pthread_mutex_t *pm = (pthread_mutex_t *)arg;\n"
        "    pthread_mutex_lock(pm);\n"
        "    pthread_mutex_unlock(pm);\n"
        "    return 0;\n"
        "}\n"
        "void f(void) {\n"
        "    pthread_t t;\n"
        "    pthread_create(&t, 0, worker, &g);\n"
        "}\n"
    )

    def test_non_local_escape_is_not_a_lifetime_violation(self):
        ctx = self.context()
        self.assertEqual(ctx["resource_lifetime"], [])


class DetachedLocalLifetimeTests(ConcurrencyContextTestBase):
    # pthread_detach is classified as a THREAD_JOIN by the vocabulary, so the
    # handle-identity pairing treats it as a synchronisation point and the STOP
    # condition keeps detach out of the lifetime-violation set.
    FIXTURE = (
        "#include <pthread.h>\n"
        "void *worker(void *arg) { return 0; }\n"
        "void f(void) {\n"
        "    pthread_mutex_t m;\n"
        "    pthread_mutex_init(&m, 0);\n"
        "    pthread_t t;\n"
        "    pthread_create(&t, 0, worker, &m);\n"
        "    pthread_detach(t);\n"
        "}\n"
    )

    def test_detached_worker_is_not_a_lifetime_violation(self):
        ctx = self.context()
        self.assertEqual(ctx["resource_lifetime"], [])
        self.assertEqual(len(ctx["spawn_join_pairs"]), 1)


class CapabilityRegistrationTests(unittest.TestCase):
    def test_concurrency_context_is_c_cpp_query_time(self):
        record = CAPABILITY_CONTRACT["concurrency_context"]
        self.assertEqual(record["languages"], ("c", "cpp"))
        self.assertEqual(record["depends_on"], ("events", "sparse_cfg"))
        self.assertTrue(record.get("query_time"))
        self.assertEqual(record["status"], "partially_implemented")


class UnsupportedLanguageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "repo"
        write(self.root, "src/a.py", "def a():\n    return 1\n")
        self.database = Path(self.temp.name) / "graph.db"
        full_index(self.root, self.database)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_python_reports_unsupported(self):
        result = query_concurrency_context(
            self.root, file_path="src/a.py", database=self.database)
        self.assertEqual(result["status"], "unsupported")


class MissingStatusTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "repo"
        write(self.root, "src/a.c", "int g; void f(void) { g++; }\n")
        self.database = Path(self.temp.name) / "graph.db"
        full_index(self.root, self.database)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_not_built_preserved(self):
        # sparse_cfg was never materialized for this file, so the context query
        # must surface not_built rather than fabricating facts.
        result = query_concurrency_context(
            self.root, file_path="src/a.c", database=self.database)
        self.assertEqual(result["status"], "not_built")


if __name__ == "__main__":
    unittest.main()
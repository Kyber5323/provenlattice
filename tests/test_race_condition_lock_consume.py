"""defect --type race_condition consumes shared concurrency lock facts (P0+P1).

The race verdict now consumes one bundle produced from the events layer's
``lock_identity`` / ``raii`` / ``acquire_kind`` facts instead of maintaining a
second lock analysis, and the candidate's ``evidence_v2`` joins that producer's
per-access held-lockset, lock relations, and competing-access pairs on the same
access event ids the concurrency-profile path uses.

These tests pin the acceptance matrix that was previously unverifiable:
wrong_lock, std::lock_guard (RAII), non_atomic_rmw, the control case (a single
deterministic lock that discharges the pair), the acquire_kind try/timed/wait
filter, and the evidence_v2 access_locksets / lock_relations / related_accesses
join.
"""

import tempfile
import unittest
from pathlib import Path

from provenlattice.defect_query import query_defect_candidates
from provenlattice.graph import full_index
from provenlattice.orchestrator import analyze_scope


def write(root: Path, relative: str, value: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value.encode("utf-8"))


def _races(body: str, filename: str = "src/t.c") -> list[dict]:
    """Build the upstream chain for one fixture and return its race candidates."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "repo"
        database = Path(tmp) / "graph.db"
        write(root, filename, body)
        full_index(root, database)
        analyze_scope(root, "src", ("defect",), database=database)
        env = query_defect_candidates(
            root, defect_type="race_condition", database=database)
        assert env["status"] == "available", env.get("reason")
        return [c for c in env["candidates"] if c["query"] == "race_condition"]


# control: one deterministic blocking lock around both touches -> SAME_LOCK
# discharge, no candidate.
CONTROL = r"""
#include <pthread.h>
pthread_mutex_t m = PTHREAD_MUTEX_INITIALIZER;
int shared = 0;
void *writer(void *arg) {
    pthread_mutex_lock(&m);
    shared = 1;
    pthread_mutex_unlock(&m);
    return NULL;
}
void *reader(void *arg) {
    pthread_mutex_lock(&m);
    int v = shared;
    pthread_mutex_unlock(&m);
    return (void *)(long)v;
}
"""

# wrong_lock: two methods hold *different* locks around the shared write/read.
# Precise lock identity must NOT merge them into a SAME_LOCK discharge.
WRONG_LOCK = r"""
#include <pthread.h>
pthread_mutex_t lock_a = PTHREAD_MUTEX_INITIALIZER;
pthread_mutex_t lock_b = PTHREAD_MUTEX_INITIALIZER;
int shared = 0;
void *writer(void *arg) {
    pthread_mutex_lock(&lock_a);
    shared = 42;
    pthread_mutex_unlock(&lock_a);
    return NULL;
}
void *reader(void *arg) {
    pthread_mutex_lock(&lock_b);
    int v = shared;
    pthread_mutex_unlock(&lock_b);
    return (void *)(long)v;
}
"""

# std::lock_guard: the two accesses sit inside their guards' lexical scope, so
# they are held (``raii_scope_end``, issue #69) and a same-mutex guard pair now
# discharges as SAME_LOCK-protected.
RAII_SAME_LOCK = r"""
#include <mutex>
std::mutex m;
int shared = 0;
void writer() {
    std::lock_guard<std::mutex> g(m);
    shared = 1;
}
int reader() {
    std::lock_guard<std::mutex> g(m);
    return shared;
}
"""

# non_atomic_rmw: `shared++` is a read-modify-write that is *not* atomic, so it
# is still a write access that races.
NON_ATOMIC_RMW = r"""
int shared = 0;
void *incr(void *arg) { shared++; return NULL; }
void *read(void *arg) { return (void *)(long)shared; }
"""

# acquire_kind filter: try / timed / wait acquisitions may fail or are not
# mutex holds, so a same-facility pair must NOT be discharged.
TRYLOCK = r"""
#include <pthread.h>
pthread_mutex_t m = PTHREAD_MUTEX_INITIALIZER;
int shared = 0;
void *writer(void *arg) {
    pthread_mutex_trylock(&m);
    shared = 1;
    pthread_mutex_unlock(&m);
    return NULL;
}
void *reader(void *arg) {
    pthread_mutex_trylock(&m);
    int v = shared;
    pthread_mutex_unlock(&m);
    return (void *)(long)v;
}
"""

TIMEDLOCK = r"""
#include <pthread.h>
#include <time.h>
pthread_mutex_t m = PTHREAD_MUTEX_INITIALIZER;
int shared = 0;
void *writer(void *arg) {
    struct timespec ts = {0, 0};
    pthread_mutex_timedlock(&m, &ts);
    shared = 1;
    pthread_mutex_unlock(&m);
    return NULL;
}
void *reader(void *arg) {
    struct timespec ts = {0, 0};
    pthread_mutex_timedlock(&m, &ts);
    int v = shared;
    pthread_mutex_unlock(&m);
    return (void *)(long)v;
}
"""

SEM_WAIT = r"""
#include <semaphore.h>
sem_t sem;
int shared = 0;
void *writer(void *arg) {
    sem_wait(&sem);
    shared = 1;
    sem_post(&sem);
    return NULL;
}
void *reader(void *arg) {
    sem_wait(&sem);
    int v = shared;
    sem_post(&sem);
    return (void *)(long)v;
}
"""

# A syntactically nearby mutex is not protection after its explicit release.
EXPLICIT_UNLOCK_BEFORE_ACCESS = r"""
#include <pthread.h>
pthread_mutex_t m = PTHREAD_MUTEX_INITIALIZER;
int shared = 0;
void *writer(void *arg) {
    pthread_mutex_lock(&m);
    pthread_mutex_unlock(&m);
    shared = 1;
    return NULL;
}
void *reader(void *arg) {
    pthread_mutex_lock(&m);
    pthread_mutex_unlock(&m);
    int v = shared;
    return (void *)(long)v;
}
"""

# RAII scope end IS materialized (#69): the guard is held only within its inner
# block, so the access after the closing brace must stay ambiguous rather than
# inheriting the guard to method exit.
RAII_SCOPE_ENDED_BEFORE_ACCESS = r"""
#include <mutex>
std::mutex m;
int shared = 0;
void writer() {
    { std::lock_guard<std::mutex> g(m); }
    shared = 1;
}
int reader() {
    { std::lock_guard<std::mutex> g(m); }
    return shared;
}
"""


class RaceConditionConsumeTests(unittest.TestCase):
    """The race verdict consumes the shared producer's bundle."""

    def test_control_same_deterministic_lock_discharges(self):
        self.assertEqual(_races(CONTROL), [])

    def test_wrong_lock_not_merged_into_same_lock(self):
        races = _races(WRONG_LOCK)
        self.assertEqual(len(races), 1)
        relations = {r["relation"] for r in races[0]["evidence_v2"]["lock"]["relations"]}
        self.assertIn("DIFFERENT_LOCK", relations)
        self.assertNotIn("SAME_LOCK", relations)

    def test_std_lock_guard_raii_same_lock_discharges(self):
        # Pre-#69 this pair was not discharged (``raii=True`` was never held);
        # with ``raii_scope_end`` the accesses are held within their guards, so
        # the same-mutex pair discharges as protected.
        self.assertEqual(_races(RAII_SAME_LOCK, "src/t.cpp"), [])

    def test_non_atomic_rmw_still_flags(self):
        self.assertEqual(len(_races(NON_ATOMIC_RMW)), 1)

    def test_try_timed_wait_acquires_not_discharged(self):
        self.assertEqual(len(_races(TRYLOCK)), 1)
        self.assertEqual(len(_races(TIMEDLOCK)), 1)
        self.assertEqual(len(_races(SEM_WAIT)), 1)

    def test_explicit_unlock_before_access_is_not_protection(self):
        races = _races(EXPLICIT_UNLOCK_BEFORE_ACCESS)
        self.assertEqual(len(races), 1)
        locksets = races[0]["evidence_v2"]["lock"]["access_locksets"]
        self.assertTrue(locksets)
        self.assertTrue(all(not record["held_locks"] for record in locksets))

    def test_raii_inner_scope_end_stays_ambiguous(self):
        races = _races(RAII_SCOPE_ENDED_BEFORE_ACCESS, "src/t.cpp")
        self.assertEqual(len(races), 1)
        self.assertEqual(races[0]["resolution_status"], "ambiguous")


class RaceConditionEvidenceTests(unittest.TestCase):
    """evidence_v2 joins the producer's held-lockset / relations on access ids."""

    def test_access_locksets_and_relations_join_related_accesses(self):
        races = _races(WRONG_LOCK)
        self.assertEqual(len(races), 1)
        candidate = races[0]

        related = candidate.get("related_accesses") or []
        self.assertEqual(len(related), 2)
        related_ids = {item["event_id"] for item in related}

        lock = candidate["evidence_v2"]["lock"]
        access_locksets = lock["access_locksets"]
        self.assertEqual(len(access_locksets), 2)
        lockset_ids = {record["access_event_id"] for record in access_locksets}
        self.assertEqual(related_ids, lockset_ids)

        relations = lock["relations"]
        self.assertGreaterEqual(len(relations), 1)
        relation_kinds = {relation["relation"] for relation in relations}
        self.assertIn("DIFFERENT_LOCK", relation_kinds)

    def test_each_access_reports_its_own_held_lock(self):
        # wrong_lock: writer holds lock_a, reader holds lock_b, so the two
        # access_locksets records carry distinct held locks and PROTECTS both.
        races = _races(WRONG_LOCK)
        self.assertEqual(len(races), 1)
        lock = races[0]["evidence_v2"]["lock"]
        held = {frozenset(record.get("held_locks") or ())
                for record in lock["access_locksets"]}
        self.assertEqual(held, {frozenset({"lock_a"}), frozenset({"lock_b"})})
        relation_kinds = {r["relation"] for r in lock["relations"]}
        self.assertIn("PROTECTS", relation_kinds)

    def test_competing_access_comes_from_the_same_bundle(self):
        races = _races(WRONG_LOCK)
        self.assertEqual(len(races), 1)
        candidate = races[0]
        related_ids = {
            item["event_id"] for item in candidate.get("related_accesses") or ()
        }
        competing = candidate["evidence_v2"]["competing_access"]
        self.assertTrue(competing)
        for pair in competing:
            endpoints = {
                side["access_event_id"] for side in pair.get("accesses") or ()
            }
            self.assertEqual(endpoints, related_ids)


if __name__ == "__main__":
    unittest.main()

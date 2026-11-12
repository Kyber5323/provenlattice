"""PL171-CWD-P0 detector rules (PAIR-01 / PAIR-02 / PAIR-03) — GATE-1 unit.

Freezes the three P0 detector behaviour changes against synthetic fixtures, one
per gate scenario:

1. pre-spawn pair        -> filtered  (PAIR-01)
2. post-join pair        -> filtered  (PAIR-03)
3. mutex/mutex same lock -> filtered  (PAIR-02, identity was already enough)
4. read-lock vs write-lock on one rwlock -> kept (PAIR-02 restores the TP)
5. incomplete lock scope (UNLOCK_OMITTED_ON_PATH) -> kept
6. different lock object -> kept
7. no-lock race          -> kept

Scenarios 1/2/3 must return no candidate; 4/5/6/7 must return one. The contract
is `pl171_p0_detector_contract.md` §5 GATE-1.
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
    """Build the upstream chain for one fixture and return race candidates."""
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


# 1. PAIR-01: an access before a THREAD_SPAWN that starts the other end cannot
# race a not-yet-started worker.
PRE_SPAWN = r"""
#include <pthread.h>
int shared = 0;
void *worker(void *arg) {
    shared = 1;
    return NULL;
}
void starter(void) {
    pthread_t t;
    shared = 2;
    pthread_create(&t, NULL, worker, NULL);
    pthread_join(t, NULL);
}
"""

# 2. PAIR-03: an access after the last THREAD_JOIN cannot race a worker that has
# already finished.
POST_JOIN = r"""
#include <pthread.h>
int shared = 0;
void *worker(void *arg) {
    shared = 1;
    return NULL;
}
void starter(void) {
    pthread_t t;
    pthread_create(&t, NULL, worker, NULL);
    pthread_join(t, NULL);
    shared = 2;
}
"""

# 3. PAIR-02: a plain mutex held around both touches discharges the pair.
MUTEX_MUTEX = r"""
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

# 4. PAIR-02: a read lock and a write lock on one rwlock do not exclude each
# other, and a read lock does not cover a write -- the pair must stay.
RD_VS_WR = r"""
#include <pthread.h>
pthread_rwlock_t g_lck = PTHREAD_RWLOCK_INITIALIZER;
int shared = 0;
void *bad(void *arg) {
    pthread_rwlock_rdlock(&g_lck);
    shared = 1;
    pthread_rwlock_unlock(&g_lck);
    return NULL;
}
void *good(void *arg) {
    pthread_rwlock_wrlock(&g_lck);
    shared = 2;
    pthread_rwlock_unlock(&g_lck);
    return (void *)(long)shared;
}
"""

# 5. PAIR-02: an end whose lock is not released on every path (the writer leaks
# the lock) is not fully covered, so the pair must stay.
INCOMPLETE_SCOPE = r"""
#include <pthread.h>
pthread_mutex_t m = PTHREAD_MUTEX_INITIALIZER;
int shared = 0;
void *writer(void *arg) {
    pthread_mutex_lock(&m);
    shared = 1;
    return NULL;
}
void *reader(void *arg) {
    pthread_mutex_lock(&m);
    int v = shared;
    pthread_mutex_unlock(&m);
    return (void *)(long)v;
}
"""

# 6. Different lock objects must not merge into one SAME_LOCK discharge.
DIFFERENT_LOCK = r"""
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

# 7. An unguarded write vs an unguarded read is still a race.
NO_LOCK = r"""
int shared = 0;
void *incr(void *arg) { shared++; return NULL; }
void *read(void *arg) { return (void *)(long)shared; }
"""


class PL171P0DetectorTests(unittest.TestCase):
    """GATE-1 unit matrix for the three P0 detector rules."""

    def test_01_pre_spawn_pair_filtered(self):
        self.assertEqual(_races(PRE_SPAWN), [])

    def test_02_post_join_pair_filtered(self):
        self.assertEqual(_races(POST_JOIN), [])

    def test_03_mutex_mutex_same_lock_filtered(self):
        self.assertEqual(_races(MUTEX_MUTEX), [])

    def test_04_read_lock_vs_write_lock_kept(self):
        races = _races(RD_VS_WR)
        self.assertEqual(len(races), 1)

    def test_05_incomplete_lock_scope_kept(self):
        races = _races(INCOMPLETE_SCOPE)
        self.assertEqual(len(races), 1)

    def test_06_different_lock_object_kept(self):
        races = _races(DIFFERENT_LOCK)
        self.assertEqual(len(races), 1)

    def test_07_no_lock_race_kept(self):
        races = _races(NO_LOCK)
        self.assertEqual(len(races), 1)


if __name__ == "__main__":
    unittest.main()
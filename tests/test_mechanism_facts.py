"""SUB-06 / R10: additive ``mechanism_facts`` block (non-race mechanism exposure).

The frozen contract (cwe1123_v2_query_contract.md §4) requires ``defect_query``
to expose already-computed lock_order / resource_lifetime / race_condition
results as their own additive ``mechanism_facts`` block, keyed by mechanism
name, without changing the flattened ``candidates`` / ``evidence_bundles``
lists and without computing anything new. ``unlock_discipline`` has no existing
query state, so it must stay absent (not filled, not diverged).
"""

import tempfile
import unittest
from pathlib import Path

from provenlattice.defect_query import query_defect_candidates
from provenlattice.graph import full_index
from provenlattice.orchestrator import analyze_scope

MECHANISM_BLOCKS = {"race_condition", "lock_order", "resource_lifetime"}

# race=0: a single-threaded malloc leak. No shared state, no threads.
LIFETIME = """#include <stdlib.h>
int leak_pos(int n) {
    char *p = malloc(64);
    if (!p) return -1;
    if (n > 0) { return n; }
    free(p);
    return 0;
}
"""

# race=0: a lock acquired and released only on the fall-through path.
UNLOCK_OMISSION = """#include <pthread.h>
pthread_mutex_t m;
void f(int n) {
    pthread_mutex_lock(&m);
    if (n > 0) { return; }
    pthread_mutex_unlock(&m);
}
"""

# race_condition positive: a shared variable written and read by two threads.
RACE = """#include <pthread.h>
int shared = 0;
void *writer(void *arg) { shared = 42; return NULL; }
void *reader(void *arg) { return (void *)(long)shared; }
"""

# lock_order positive: the same lock taken twice (self-deadlock).
SELF_DEADLOCK = """#include <pthread.h>
pthread_mutex_t m;
void f(void) {
    pthread_mutex_lock(&m);
    pthread_mutex_lock(&m);
    pthread_mutex_unlock(&m);
    pthread_mutex_unlock(&m);
}
"""


def write(root: Path, relative: str, value: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value.encode("utf-8"))


class MechanismFactsBase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "repo"
        self.database = Path(self.temp.name) / "graph.db"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def query(self, body: str) -> dict:
        write(self.root, "src/t.c", body)
        full_index(self.root, self.database)
        analyze_scope(self.root, "src", ("defect",), database=self.database)
        env = query_defect_candidates(
            self.root, defect_type="all", database=self.database)
        self.assertEqual(env["status"], "available", env.get("reason"))
        return env

    def block(self, env: dict, name: str) -> list[dict]:
        return env["mechanism_facts"].get(name, [])


class MechanismFactsBlockTests(MechanismFactsBase):
    def test_blocks_are_exactly_the_three_mechanisms(self) -> None:
        env = self.query(LIFETIME)
        self.assertEqual(set(env["mechanism_facts"]), MECHANISM_BLOCKS)
        self.assertNotIn("unlock_discipline", env["mechanism_facts"])

    def test_race_zero_mechanisms_still_visible(self) -> None:
        # race=0 (no shared state) but resource_lifetime has a candidate; the
        # mechanism block must still expose it.
        env = self.query(LIFETIME)
        self.assertEqual(self.block(env, "race_condition"), [])
        self.assertEqual(self.block(env, "lock_order"), [])
        self.assertGreaterEqual(len(self.block(env, "resource_lifetime")), 1)

    def test_unlock_omission_visible(self) -> None:
        env = self.query(UNLOCK_OMISSION)
        self.assertEqual(self.block(env, "race_condition"), [])
        self.assertGreaterEqual(len(self.block(env, "resource_lifetime")), 1)

    def test_race_block_populated(self) -> None:
        env = self.query(RACE)
        self.assertGreaterEqual(len(self.block(env, "race_condition")), 1)

    def test_lock_order_block_populated(self) -> None:
        env = self.query(SELF_DEADLOCK)
        self.assertGreaterEqual(len(self.block(env, "lock_order")), 1)


class MechanismFactsRegressionTests(MechanismFactsBase):
    def test_flattened_candidates_unchanged(self) -> None:
        # Adding the block must not disturb the flattened candidate list: the
        # mechanism candidate still appears once, with its full serialization.
        env = self.query(LIFETIME)
        self.assertEqual(len(env["candidates"]), 1)
        self.assertEqual(len(env["evidence_bundles"]), 1)
        candidate = env["candidates"][0]
        self.assertEqual(candidate["query"], "resource_lifetime")
        # The mechanism block carries the same candidate (facts/paths included),
        # minus the evidence_id and the SUB-03/R0 additive evidence_v2 the
        # flattened list appends (contract §5.8: mechanism_facts stays an
        # independent block and its per-mechanism content is unchanged).
        block_entry = self.block(env, "resource_lifetime")[0]
        self.assertEqual(
            block_entry,
            {key: value for key, value in candidate.items()
             if key not in ("evidence_id", "evidence_v2")},
        )

    def test_mechanism_entry_has_full_serialization(self) -> None:
        env = self.query(LIFETIME)
        entry = self.block(env, "resource_lifetime")[0]
        for key in ("defect_key", "query", "subject", "anchor", "discriminator",
                    "resolution_status", "confidence", "facts", "uncertain_facts",
                    "paths", "source_evidence", "missing_evidence", "metadata"):
            self.assertIn(key, entry)

    def test_mechanism_bound_to_identity(self) -> None:
        # Each mechanism entry is attributable to the same subject/discriminator
        # as its flattened candidate (identity binding consistency).
        env = self.query(LIFETIME)
        ids = {(c["query"], c["discriminator"]) for c in env["candidates"]}
        for name in ("race_condition", "lock_order", "resource_lifetime"):
            for entry in self.block(env, name):
                self.assertIn((entry["query"], entry["discriminator"]), ids)


if __name__ == "__main__":
    unittest.main()

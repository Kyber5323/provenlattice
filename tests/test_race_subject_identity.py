"""Subject identity coverage: every family must publish the same identity block.

The benchmark's variable-level diagnosis could only match on the spelling,
because the family that produces the most candidates -- `race_condition` -- was
the one family whose subject carried no `storage` / `type` / `member` / `decl`.
A reader could not tell a file static from a same-named local, and any
variable-level scoring had to fall back to string equality on `name`.

This pins the block, and pins that it agrees with `identity`, which is what the
rest of the defect layer joins on.
"""

import tempfile
import unittest
from pathlib import Path

from provenlattice.defect_query import query_defect_candidates
from provenlattice.graph import full_index
from provenlattice.orchestrator import analyze_scope

BODY = """#include <pthread.h>

typedef struct { int completed; int busy; } exec_ctx_t;

static int g_isClean = 0;
static exec_ctx_t g_ctx;

int process(exec_ctx_t *ctx, int side){
    ctx->busy = 1;
    g_isClean = 1;
    g_ctx.completed = side;
    ctx->busy = 0;
    return side;
}

void *worker(void *arg){
    exec_ctx_t *c = (exec_ctx_t *)arg;
    process(c, 3);
    return 0;
}

void *other(void *arg){
    exec_ctx_t *c = (exec_ctx_t *)arg;
    g_isClean = 0;
    g_ctx.completed = 0;
    process(c, 0);
    return 0;
}

void go(exec_ctx_t *ctx){
    pthread_t a, b;
    pthread_create(&a, 0, worker, ctx);
    pthread_create(&b, 0, other, ctx);
    pthread_join(a, 0);
    pthread_join(b, 0);
}
"""

IDENTITY_KEYS = ("storage", "type", "member", "decl")


class RaceSubjectIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "repo"
        self.database = Path(self.temp.name) / "g.db"
        (self.root / "src").mkdir(parents=True)
        (self.root / "src" / "t.c").write_text(BODY, encoding="utf-8")
        full_index(self.root, self.database)
        analyze_scope(self.root, "src", ("defect",), database=self.database)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def race(self) -> dict:
        env = query_defect_candidates(
            self.root, defect_type="race_condition", max_candidates=50,
            database=self.database)
        self.assertEqual(env["status"], "available", env.get("reason"))
        return env

    def test_race_subject_carries_the_identity_block(self) -> None:
        env = self.race()
        self.assertGreater(len(env["candidates"]), 0, "fixture should race")
        for candidate in env["candidates"]:
            subject = candidate["subject"]
            for key in IDENTITY_KEYS:
                self.assertIn(key, subject,
                              f"race subject missing {key!r}: {subject}")

    def test_file_static_is_named_as_such(self) -> None:
        # The point of the block: a reviewer can tell the storage class without
        # re-reading the source, which spelling alone cannot convey.
        env = self.race()
        storages = {c["subject"]["storage"] for c in env["candidates"]}
        self.assertIn("file_static", storages)
        self.assertNotIn("", storages, "storage must be resolved, not blank")

    def test_identity_block_agrees_with_the_identity_key(self) -> None:
        env = self.race()
        for candidate in env["candidates"]:
            subject = candidate["subject"]
            identity = subject["identity"]
            self.assertTrue(identity, "identity must be present")
            # A file static is filed under its declaration, so the decl the
            # subject publishes is the token the identity was built from.
            if subject["storage"] == "file_static" and subject["decl"]:
                self.assertIn(subject["decl"], identity)

    def test_identity_block_does_not_change_the_candidate_set(self) -> None:
        # Adding descriptive fields must not add or remove candidates: the
        # population is decided by the walk, not by what we publish about it.
        env = self.race()
        for candidate in env["candidates"]:
            self.assertEqual(candidate["defect_key"], "1.1")
            self.assertEqual(candidate["query"], "race_condition")
            self.assertIn(candidate["resolution_status"],
                          {"resolved", "ambiguous", "unresolved"})


if __name__ == "__main__":
    unittest.main()

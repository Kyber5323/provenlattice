"""S7-F2 tests: restricted DEREF_RESOLVES_TO + must-only same_storage (#48).

These pin the formal contract for SUB-07 S7-F2:
* FIX-05's two thread dereferences both must-resolve to the same scoped storage
  identity, producing a ``same_storage=true`` pair (candidate non-empty).
* Different storage objects (field members, two functions' distinct parameters)
  must NOT be paired (no false ``same_storage=true``).
* An unresolved/undeclared declaration degrades to ``unknown``; a non-address
  pass degrades to ``may``; neither is upgraded to ``must`` nor treated as false.
* Default OFF: ``enable=False`` yields an empty derivation, so FIX-05 stays a
  0-candidate envelope (contract §1.8).
"""

import tempfile
import unittest
from pathlib import Path

from provenlattice.analysis_query import query_analysis
from provenlattice.deref_resolution import build_deref_resolution
from provenlattice.graph import full_index
from provenlattice.orchestrator import analyze_scope

FIX05 = """\
#include <pthread.h>
int main(void) {
    int fd; pthread_t t1, t2;
    pthread_create(&t1, 0, writer, &fd);
    pthread_create(&t2, 0, closer, &fd);
}
void *writer(void *p) { int *f = p; write(*f, "x", 1); return 0; }
void *closer(void *p) { int *f = p; close(*f); return 0; }
"""

FIELD_MEMBER = """\
#include <pthread.h>
struct S { int a; int b; };
struct S s;
int main(void) {
    pthread_t t1, t2;
    pthread_create(&t1, 0, writer, &s.a);
    pthread_create(&t2, 0, closer, &s.b);
}
void *writer(void *p) { int *f = p; write(*f, "x", 1); return 0; }
void *closer(void *p) { int *f = p; close(*f); return 0; }
"""

UNDECLARED = """\
#include <pthread.h>
int main(void) {
    pthread_t t;
    pthread_create(&t, 0, writer, &undeclared_var);
}
void *writer(void *p) { int *f = p; write(*f, "x", 1); return 0; }
"""

POINTER_VAR = """\
#include <pthread.h>
int main(void) {
    int fd; int *ptr = &fd;
    pthread_t t1, t2;
    pthread_create(&t1, 0, writer, ptr);
    pthread_create(&t2, 0, closer, ptr);
}
void *writer(void *p) { int *f = p; write(*f, "x", 1); return 0; }
void *closer(void *p) { int *f = p; close(*f); return 0; }
"""

PARAMETER_TWO_FUNCTIONS = """\
#include <pthread.h>
void f(int fd) { pthread_t t1; pthread_create(&t1, 0, writer, &fd); }
void g(int fd) { pthread_t t2; pthread_create(&t2, 0, closer, &fd); }
void *writer(void *p) { int *f = p; write(*f, "x", 1); return 0; }
void *closer(void *p) { int *f = p; close(*f); return 0; }
"""


def _events(source: str) -> list[dict]:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "repo"
        path = root / "src" / "a.c"
        path.parent.mkdir(parents=True)
        path.write_bytes("\n".join(source.strip().splitlines()).encode())
        database = Path(td) / "graph.db"
        full_index(root, database)
        analyze_scope(root, "src", ("sparse_cfg",), database=database)
        env = query_analysis(root, "sparse_cfg", file_path="src/a.c",
                             database=database)
        return env.get("events", [])


def _derive(source: str, *, enable: bool = True) -> dict:
    return build_deref_resolution(_events(source), enable=enable)


def _must_targets(result: dict) -> list[str]:
    return sorted(
        record["deref_resolves_to"]
        for record in result["deref_resolves_to"]
        if record["confidence"] == "must"
    )


class DerefResolutionTests(unittest.TestCase):
    """The three-state DEREF_RESOLVES_TO derivation and must-only same_storage."""

    def test_fix05_same_storage_must_pair(self) -> None:
        result = _derive(FIX05)
        # Both dereferences resolve, with must, to the very same scoped identity.
        self.assertEqual(
            _must_targets(result),
            ["local:src/a.c:main:fd:42", "local:src/a.c:main:fd:42"])
        # same_storage=true is emitted for the cross-owner pair (candidate non-empty).
        self.assertEqual(len(result["same_storage"]), 1)
        pair = result["same_storage"][0]
        self.assertEqual(pair["same_storage"], "true")
        self.assertEqual(pair["storage_object_id"], "local:src/a.c:main:fd:42")
        self.assertEqual({side["owner"] for side in pair["accesses"]},
                         {"writer", "closer"})

    def test_default_off_is_empty(self) -> None:
        result = _derive(FIX05, enable=False)
        self.assertEqual(result["deref_resolves_to"], [])
        self.assertEqual(result["same_storage"], [])
        self.assertEqual(result["coverage"]["same_storage_pairs"], 0)

    def test_field_members_do_not_match(self) -> None:
        result = _derive(FIELD_MEMBER)
        # Each dereference resolves to its own member; they must NOT be paired.
        self.assertEqual(
            _must_targets(result), ["global:s.a", "global:s.b"])
        self.assertEqual(result["same_storage"], [])

    def test_two_functions_distinct_parameters_do_not_match(self) -> None:
        result = _derive(PARAMETER_TWO_FUNCTIONS)
        self.assertEqual(
            _must_targets(result),
            ["parameter:src/a.c:f:fd:28", "parameter:src/a.c:g:fd:98"])
        self.assertEqual(result["same_storage"], [])

    def test_undeclared_argument_is_unknown(self) -> None:
        result = _derive(UNDECLARED)
        self.assertTrue(result["deref_resolves_to"])
        for record in result["deref_resolves_to"]:
            self.assertEqual(record["confidence"], "unknown")
            self.assertEqual(record["deref_resolves_to"], "")
        self.assertEqual(result["same_storage"], [])

    def test_pointer_var_is_not_must(self) -> None:
        result = _derive(POINTER_VAR)
        # A pointer variable (not an address-of) is never provable must.
        self.assertTrue(result["deref_resolves_to"])
        for record in result["deref_resolves_to"]:
            self.assertNotEqual(record["confidence"], "must")
        self.assertEqual(result["same_storage"], [])


if __name__ == "__main__":
    unittest.main()
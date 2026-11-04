"""S7-F1 tests: additive `thread_argument_decl_scoped` on THREAD_SPAWN.

These tests pin the *formal* contract for SUB-07 S7-F1 (Issue #49): the
`pthread_create` 4th argument produces an additive, scope-qualified declaration
identity alongside the existing storage-scope `thread_argument_decl`, and the
older field stays byte-identical to the A1-era token.

The fixtures are the A2 collision cases (sub07_gate_a2_prep_probe.py), one file
`src/a.c` per case, with the same `src.strip().splitlines()` normalization the
probe applied, so the declaration byte offsets are stable and asserted exactly.

The two guarantees under test:

* `thread_argument_decl` is the storage-class token (`local:fd`,
  `global:fd`, ...) and is **not** widened by the scoped field -- two same-named
  locals in different functions still read `local:fd` (the pre-existing,
  function-blind spelling).
* `thread_argument_decl_scoped` adds the declaring file, the enclosing function
  and the declaration byte offset for the frame-local storage classes, and is
  byte-identical to `thread_argument_decl` for the storage classes whose public
  `scope_token` is already unambiguous (global, field, file_static).
"""

import tempfile
import unittest
from pathlib import Path

from provenlattice.analysis_query import query_analysis
from provenlattice.graph import full_index
from provenlattice.orchestrator import analyze_scope


# The 4th argument is an address-of a frame-local: two same-named `fd`s in one
# function are one object, so both spawns share one scoped identity.
FIX05_MAIN_FD = """\
#include <pthread.h>
int main(void) {
    int fd; pthread_t t1, t2;
    pthread_create(&t1, 0, writer, &fd);
    pthread_create(&t2, 0, closer, &fd);
}
void *writer(void *p) { int *f = p; write(*f, "x", 1); return 0; }
void *closer(void *p) { int *f = p; close(*f); return 0; }
"""

LOCAL_TWO_FUNCTIONS = """\
#include <pthread.h>
int main(void) {
    int fd; pthread_t t1;
    pthread_create(&t1, 0, writer, &fd);
}
void helper(void) {
    int fd; pthread_t t2;
    pthread_create(&t2, 0, closer, &fd);
}
void *writer(void *p) { int *f = p; write(*f, "x", 1); return 0; }
void *closer(void *p) { int *f = p; close(*f); return 0; }
"""

SHADOW_SAME_FUNCTION = """\
#include <pthread.h>
void f(void) {
    pthread_t t1, t2;
    { int fd; pthread_create(&t1, 0, writer, &fd); }
    { int fd; pthread_create(&t2, 0, closer, &fd); }
}
void *writer(void *p) { int *f = p; write(*f, "x", 1); return 0; }
void *closer(void *p) { int *f = p; close(*f); return 0; }
"""

GLOBAL_LOCAL_SAME_NAME = """\
#include <pthread.h>
int fd;
void spawn_global(void) {
    pthread_t t2; pthread_create(&t2, 0, closer, &fd);
}
int main(void) {
    int fd; pthread_t t1; pthread_create(&t1, 0, writer, &fd);
    spawn_global();
}
void *writer(void *p) { int *f = p; write(*f, "x", 1); return 0; }
void *closer(void *p) { int *f = p; close(*f); return 0; }
"""

FUNCTION_STATIC_TWO_FUNCTIONS = """\
#include <pthread.h>
void spawn_w(void) { static int cache; pthread_t t; pthread_create(&t, 0, writer, &cache); }
void spawn_c(void) { static int cache; pthread_t t; pthread_create(&t, 0, closer, &cache); }
int main(void) { spawn_w(); spawn_c(); }
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

UNDECLARED = """\
#include <pthread.h>
int main(void) {
    pthread_t t;
    pthread_create(&t, 0, writer, &undeclared_var);
}
void *writer(void *p) { int *f = p; write(*f, "x", 1); return 0; }
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


def _spawns_by_function(source: str) -> dict[str, dict[str, str]]:
    """Run the real pipeline and return each THREAD_SPAWN's annotations keyed by
    the function the new thread runs (`thread_function`)."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "repo"
        path = root / "src" / "a.c"
        path.parent.mkdir(parents=True)
        path.write_bytes(("\n".join(source.strip().splitlines())).encode())
        database = Path(td) / "graph.db"
        full_index(root, database)
        analyze_scope(root, "src", ("sparse_cfg",), database=database)
        env = query_analysis(root, "sparse_cfg", file_path="src/a.c",
                             database=database)
    result: dict[str, dict[str, str]] = {}
    for event in env.get("events", []):
        if event.get("event_type") != "THREAD_SPAWN":
            continue
        annotations = event.get("annotations", {})
        function = annotations.get("thread_function", "")
        result[function] = annotations
    return result


class ThreadArgumentScopedTests(unittest.TestCase):
    """The additive scoped identity on `pthread_create`'s 4th argument."""

    def test_fix05_two_spawns_share_one_scoped_identity(self) -> None:
        spawns = _spawns_by_function(FIX05_MAIN_FD)
        self.assertEqual(set(spawns), {"writer", "closer"})
        for function in ("writer", "closer"):
            annotations = spawns[function]
            self.assertEqual(annotations["thread_argument"], "fd")
            self.assertEqual(annotations["thread_argument_form"], "address_of")
            # The old, function-blind token is unchanged.
            self.assertEqual(annotations["thread_argument_decl"], "local:fd")
            self.assertEqual(
                annotations["thread_argument_decl_scoped"],
                "local:src/a.c:main:fd:42")
        self.assertEqual(
            spawns["writer"]["thread_argument_decl_scoped"],
            spawns["closer"]["thread_argument_decl_scoped"])

    def test_local_two_functions_are_distinguished(self) -> None:
        spawns = _spawns_by_function(LOCAL_TWO_FUNCTIONS)
        self.assertEqual(set(spawns), {"writer", "closer"})
        # Both read the same pre-existing token...
        self.assertEqual(spawns["writer"]["thread_argument_decl"], "local:fd")
        self.assertEqual(spawns["closer"]["thread_argument_decl"], "local:fd")
        # ...but the scoped token names each declaration's function.
        self.assertEqual(
            spawns["writer"]["thread_argument_decl_scoped"],
            "local:src/a.c:main:fd:42")
        self.assertEqual(
            spawns["closer"]["thread_argument_decl_scoped"],
            "local:src/a.c:helper:fd:131")

    def test_shadow_same_function_distinguished_by_byte(self) -> None:
        spawns = _spawns_by_function(SHADOW_SAME_FUNCTION)
        self.assertEqual(
            spawns["writer"]["thread_argument_decl_scoped"],
            "local:src/a.c:f:fd:64")
        self.assertEqual(
            spawns["closer"]["thread_argument_decl_scoped"],
            "local:src/a.c:f:fd:117")
        self.assertEqual(spawns["writer"]["thread_argument_decl"], "local:fd")
        self.assertEqual(spawns["closer"]["thread_argument_decl"], "local:fd")

    def test_parameter_two_functions_distinguished(self) -> None:
        spawns = _spawns_by_function(PARAMETER_TWO_FUNCTIONS)
        self.assertEqual(spawns["writer"]["thread_argument_decl"], "parameter:fd")
        self.assertEqual(spawns["closer"]["thread_argument_decl"], "parameter:fd")
        self.assertEqual(
            spawns["writer"]["thread_argument_decl_scoped"],
            "parameter:src/a.c:f:fd:28")
        self.assertEqual(
            spawns["closer"]["thread_argument_decl_scoped"],
            "parameter:src/a.c:g:fd:98")

    def test_function_static_two_functions_distinguished(self) -> None:
        spawns = _spawns_by_function(FUNCTION_STATIC_TWO_FUNCTIONS)
        self.assertEqual(
            spawns["writer"]["thread_argument_decl"], "function_static:cache")
        self.assertEqual(
            spawns["closer"]["thread_argument_decl"], "function_static:cache")
        self.assertEqual(
            spawns["writer"]["thread_argument_decl_scoped"],
            "function_static:src/a.c:spawn_w:cache:42")
        self.assertEqual(
            spawns["closer"]["thread_argument_decl_scoped"],
            "function_static:src/a.c:spawn_c:cache:135")

    def test_global_and_local_same_name(self) -> None:
        spawns = _spawns_by_function(GLOBAL_LOCAL_SAME_NAME)
        # The global keeps external-linkage identity: scoped == decl.
        self.assertEqual(spawns["closer"]["thread_argument_decl"], "global:fd")
        self.assertEqual(
            spawns["closer"]["thread_argument_decl_scoped"], "global:fd")
        # The local gets the frame-qualified identity.
        self.assertEqual(spawns["writer"]["thread_argument_decl"], "local:fd")
        self.assertEqual(
            spawns["writer"]["thread_argument_decl_scoped"],
            "local:src/a.c:main:fd:133")

    def test_field_members_keep_global_member_identity(self) -> None:
        spawns = _spawns_by_function(FIELD_MEMBER)
        self.assertEqual(spawns["writer"]["thread_argument"], "s")
        self.assertEqual(spawns["writer"]["thread_argument_decl"], "global:s.a")
        self.assertEqual(
            spawns["writer"]["thread_argument_decl_scoped"], "global:s.a")
        self.assertEqual(spawns["closer"]["thread_argument_decl"], "global:s.b")
        self.assertEqual(
            spawns["closer"]["thread_argument_decl_scoped"], "global:s.b")

    def test_undeclared_argument_stays_unknown(self) -> None:
        spawns = _spawns_by_function(UNDECLARED)
        self.assertEqual(set(spawns), {"writer"})
        annotations = spawns["writer"]
        self.assertEqual(annotations["thread_argument"], "undeclared_var")
        # A name the file does not resolve is never guessed into a token.
        self.assertEqual(annotations["thread_argument_decl"], "")
        self.assertEqual(annotations["thread_argument_decl_scoped"], "")
        self.assertEqual(annotations["thread_argument_form"], "address_of")


if __name__ == "__main__":
    unittest.main()

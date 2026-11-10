"""Category selection for `provenlattice defect --category`.

Two layers are exercised:

* ``_expand_categories`` -- the pure mapping from a comma-separated category
  list to an ordered ``(query, kwargs)`` spec list (unit, no database).
* ``query_defect_candidates(category=...)`` -- the end-to-end selection: a
  ``memory`` run must surface no lock-lifetime (4.6) candidate, a
  ``concurrency`` run no memory-lifetime (4.1) candidate, and the no-argument
  default must stay byte-for-byte equivalent to the previous ``--type all``.
"""

import tempfile
import unittest
from pathlib import Path

from provenlattice.analysis_query import AnalysisRequestError
from provenlattice.defect_query import (
    CATEGORY_QUERIES,
    KNOWN_CATEGORIES,
    _expand_categories,
    query_defect_candidates,
)
from provenlattice.graph import full_index
from provenlattice.orchestrator import analyze_scope


def write(root: Path, relative: str, value: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value.encode("utf-8"))


class ExpandCategoriesTests(unittest.TestCase):
    def _query_names(self, category: str) -> list[str]:
        return list(dict.fromkeys(q for q, _ in _expand_categories(category)))

    def test_memory(self) -> None:
        self.assertEqual(
            self._query_names("memory"),
            ["resource_lifetime", "null_flow", "use_after_free",
             "double_free", "mismatched_free"],
        )

    def test_concurrency(self) -> None:
        self.assertEqual(
            self._query_names("concurrency"),
            ["race_condition", "lock_order", "resource_lifetime"],
        )

    def test_dataflow(self) -> None:
        self.assertEqual(self._query_names("dataflow"), ["taint_path"])

    def test_reliability(self) -> None:
        self.assertEqual(self._query_names("reliability"), ["error_handling"])

    def test_all(self) -> None:
        self.assertEqual(len(_expand_categories("all")), 9)

    def test_multi_select_splits_resource_lifetime(self) -> None:
        specs = _expand_categories("memory,concurrency")
        resource = [extra for q, extra in specs if q == "resource_lifetime"]
        self.assertEqual(len(resource), 2)
        keys = {extra.get("defect_keys") for extra in resource}
        self.assertEqual(keys, {("4.1",), ("4.6",)})

    def test_unknown_fails_closed(self) -> None:
        with self.assertRaises(AnalysisRequestError) as ctx:
            _expand_categories("bogus")
        self.assertEqual(ctx.exception.code, "invalid_request")

    def test_empty_fails_closed(self) -> None:
        with self.assertRaises(AnalysisRequestError):
            _expand_categories("")

    def test_whitespace_tolerated(self) -> None:
        self.assertEqual(
            self._query_names(" memory , concurrency "),
            self._query_names("memory,concurrency"),
        )


class CategorySelectionTests(unittest.TestCase):
    # A function that leaks *both* a malloc (matrix 4.1) and a mutex (matrix
    # 4.6) on the same early-return path, so a single fixture can prove the
    # category split keeps the two halves apart.
    BOTH_LEAKS = (
        "#include <stdlib.h>\n"
        "#include <pthread.h>\n"
        "pthread_mutex_t g_mtx;\n"
        "int f01(int n) {\n"
        "    char *p = malloc(64);\n"
        "    if (!p) return -1;\n"
        "    pthread_mutex_lock(&g_mtx);\n"
        "    if (n > 0) { return n; }  // leaks p (4.1) and g_mtx (4.6)\n"
        "    free(p);\n"
        "    pthread_mutex_unlock(&g_mtx);\n"
        "    return 0;\n"
        "}\n"
    )

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "repo"
        self.database = Path(self.temp.name) / "graph.db"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def build(self, body: str) -> None:
        write(self.root, "src/t.c", body)
        full_index(self.root, self.database)
        analyze_scope(self.root, "src", ("defect",), database=self.database)

    def query(self, **kwargs) -> dict:
        return query_defect_candidates(self.root, database=self.database, **kwargs)

    def lifetime_keys(self, env: dict) -> list[str]:
        return [c["defect_key"] for c in env["candidates"]
                if c["query"] == "resource_lifetime"]

    def test_memory_has_no_lock_lifetime(self) -> None:
        self.build(self.BOTH_LEAKS)
        env = self.query(category="memory")
        self.assertEqual(env["status"], "available")
        keys = self.lifetime_keys(env)
        self.assertTrue(keys, "fixture should produce a 4.1 leak")
        self.assertNotIn("4.6", keys)
        # no concurrency query should have run at all
        self.assertFalse(any(c["query"] in ("race_condition", "lock_order")
                             for c in env["candidates"]))

    def test_concurrency_has_no_memory_lifetime(self) -> None:
        self.build(self.BOTH_LEAKS)
        env = self.query(category="concurrency")
        self.assertEqual(env["status"], "available")
        keys = self.lifetime_keys(env)
        self.assertTrue(keys, "fixture should produce a 4.6 lock leak")
        self.assertNotIn("4.1", keys)
        self.assertFalse(any(c["query"] in ("null_flow", "use_after_free",
                                            "double_free", "mismatched_free")
                             for c in env["candidates"]))

    def test_default_all_includes_both_halves(self) -> None:
        self.build(self.BOTH_LEAKS)
        env = self.query()  # no defect_type, no category -> all
        keys = self.lifetime_keys(env)
        self.assertIn("4.1", keys)
        self.assertIn("4.6", keys)

    def test_category_all_equals_default(self) -> None:
        self.build(self.BOTH_LEAKS)
        default = self.query()
        explicit = self.query(category="all")
        self.assertEqual(default["candidates"], explicit["candidates"])

    def test_query_field_names_the_category(self) -> None:
        self.build(self.BOTH_LEAKS)
        env = self.query(category="memory")
        self.assertTrue(env["query"].startswith("category memory"))
        self.assertEqual(env["category"], "memory")

    def test_type_and_category_conflict_fails_closed(self) -> None:
        # The guard runs before any database access, so a bogus path is fine.
        with self.assertRaises(AnalysisRequestError) as ctx:
            query_defect_candidates(
                self.root, defect_type="double_free", category="memory")
        self.assertEqual(ctx.exception.code, "invalid_request")


if __name__ == "__main__":
    unittest.main()

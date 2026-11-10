"""I0 capability-contract tests (PL-PR1-SELECTIVE-INTEGRATION-I0).

Freezes the isolation contract between the L1 default path and the future
Code Inspection capabilities registered in analysis.CAPABILITY_CONTRACT:

1. default index never creates analysis/inspection artifacts;
2. registered-but-not-implemented capabilities fail closed on analyze
   (capability_not_available, before any mutation) and report honestly
   unavailable on the read-only query surface (capability_not_implemented);
3. unknown features keep the legacy unknown_feature / unsupported answers;
4. analyzer version mismatch fails closed as stale (freshness contract);
5. CLI keeps its structured error shape for the new codes.
"""

import contextlib
import sqlite3
import tempfile
import unittest
from pathlib import Path

from provenlattice.analysis import (
    ANALYZER_VERSIONS,
    CAPABILITY_CONTRACT,
    implemented_features,
    selectable_features,
)
from provenlattice.analysis_query import query_analysis
from provenlattice.defect_query import query_defect_candidates
from provenlattice.graph import full_index
from provenlattice.orchestrator import AnalysisRequestError, analyze_scope

BASE_TABLES = {
    "document_state", "edges", "file_state", "files", "graph_generation",
    "nodes", "raw_evidence_links", "raw_references", "repositories",
    "shard_edges", "shards",
}


def write(root: Path, relative: str, value: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value.encode("utf-8"))


class CapabilityContractTests(unittest.TestCase):
    def test_implemented_features_match_contract(self) -> None:
        self.assertEqual(
            implemented_features(),
            {"ast", "cfg", "sparse_cfg", "sparse_dfg", "defect", "semantic_java"})
        # semantic_java (P2-D) is implemented as a query-time OPTIONAL
        # capability: it never runs in the default index, needs an external
        # JDK/JDT classpath, and its grade stays "local-only, diagnostic"
        # (no formal claim).
        record = CAPABILITY_CONTRACT["semantic_java"]
        self.assertEqual(record["query_time"], True)
        self.assertEqual(record["optional_runtime"], "jdk+jdt")
        self.assertEqual(record["grade"], "local-only, diagnostic")
        self.assertEqual(record["languages"], ("java",))
        # I2A: defect was partially_implemented (two of eight queries);
        # I2B1+overnight: all eight queries are ported (implemented), and
        # R1-C (issue #65) adds mismatched_free as the ninth.
        self.assertEqual(
            {feature for feature, record in CAPABILITY_CONTRACT.items()
             if record["status"] == "registered"},
            set(),
        )

    def test_defect_capability_is_fully_implemented(self) -> None:
        record = CAPABILITY_CONTRACT["defect"]
        self.assertEqual(record["status"], "implemented")
        self.assertEqual(len(record["supported_queries"]), 9)
        self.assertEqual(record["analyzer_ids"], ())
        self.assertTrue(record.get("query_time"))
        self.assertEqual(record["grade"], "local-only, diagnostic")

    def test_events_capability_is_partially_implemented(self) -> None:
        # I1-A: events runs on explicit request but only delivers the level-0
        # vocabulary subset; the contract must say so, never claim the whole
        # capability.
        record = CAPABILITY_CONTRACT["events"]
        self.assertEqual(record["status"], "partially_implemented")
        self.assertEqual(record["level"], "level0")
        self.assertEqual(record["analyzer_ids"], ("events:cpp",))

    def test_implemented_analyzer_ids_match_version_registry(self) -> None:
        for feature, record in CAPABILITY_CONTRACT.items():
            if record["status"] == "implemented":
                for analyzer in record["analyzer_ids"]:
                    self.assertIn(analyzer, ANALYZER_VERSIONS)
        for analyzer in ANALYZER_VERSIONS:
            self.assertIn(analyzer.split(":", 1)[0], selectable_features())

    def test_registered_dependencies_close_over_the_contract(self) -> None:
        for feature, record in CAPABILITY_CONTRACT.items():
            for dependency in record["depends_on"]:
                self.assertIn(dependency, CAPABILITY_CONTRACT,
                              f"{feature} depends on unknown {dependency}")
        self.assertEqual(
            CAPABILITY_CONTRACT["defect"]["depends_on"],
            ("events", "sparse_cfg", "sparse_dfg"),
        )

    def test_full_and_sparse_cfg_are_separate_capabilities(self) -> None:
        # I1-B delivered the sparse walker, but the two stay separate
        # capabilities: no unified CFG implementation exists, and the contract
        # must not pretend otherwise.
        self.assertEqual(CAPABILITY_CONTRACT["cfg"]["status"], "implemented")
        self.assertEqual(CAPABILITY_CONTRACT["sparse_cfg"]["status"], "implemented")
        self.assertEqual(CAPABILITY_CONTRACT["cfg"]["analyzer_ids"], ("cfg:python", "cfg:c", "cfg:java"))
        self.assertEqual(CAPABILITY_CONTRACT["sparse_cfg"]["analyzer_ids"],
                         ("sparse_cfg:cpp", "sparse_cfg:java"))


class DefaultIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "repo"
        write(self.root, "src/a.c", "int a01(void) { return 1; }\n")
        write(self.root, "src/b.py", "def b01():\n    return 2\n")
        self.database = Path(self.temp.name) / "graph.db"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def tables(self) -> set[str]:
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            return {
                row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")
            }

    def test_default_index_creates_no_analysis_or_inspection_artifacts(self) -> None:
        full_index(self.root, self.database)
        self.assertEqual(self.tables(), BASE_TABLES)

    def test_defect_request_materializes_upstream_only(self) -> None:
        # I2A: defect is query-time -- requesting it materializes the
        # upstream chain and nothing else; candidates come from the query.
        full_index(self.root, self.database)
        analyze_scope(self.root, "src", "defect", database=self.database)
        self.assertEqual(self.tables(), BASE_TABLES | {"analysis_state", "analysis_nodes", "analysis_edges"})
        env = query_defect_candidates(self.root, defect_type="double_free",
                                      database=self.database)
        self.assertEqual(env["status"], "available")

    def test_analyze_unknown_feature_keeps_legacy_code(self) -> None:
        with self.assertRaises(AnalysisRequestError) as caught:
            analyze_scope(self.root, "src", "bogus", database=self.database)
        self.assertEqual(caught.exception.code, "unknown_feature")

    def test_query_unknown_feature_still_unsupported(self) -> None:
        full_index(self.root, self.database)
        env = query_analysis(self.root, "bogus",
                             file_path="src/a.c", database=self.database)
        self.assertEqual(env["status"], "unsupported")
        self.assertEqual(env["reason"], "unsupported_feature")


class FreshnessFailClosedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "repo"
        write(self.root, "src/a.py", "def a01(x):\n    if x:\n        return 1\n    return 0\n")
        self.database = Path(self.temp.name) / "graph.db"
        full_index(self.root, self.database)
        analyze_scope(self.root, "src", ("ast", "cfg"), database=self.database)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_version_mismatch_reads_back_as_stale(self) -> None:
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                "UPDATE analysis_state SET feature_version = feature_version + 1")
            connection.commit()
        env = query_analysis(self.root, "cfg", file_path="src/a.py",
                             database=self.database)
        self.assertEqual(env["status"], "stale")
        self.assertIn("feature_version_changed", env["reason"])
        self.assertEqual(env["nodes"], [])
        self.assertEqual(env["edges"], [])

    def test_fresh_untampered_state_stays_available(self) -> None:
        env = query_analysis(self.root, "cfg", file_path="src/a.py",
                             database=self.database)
        self.assertEqual(env["status"], "available")


class CliCompatTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "repo"
        write(self.root, "src/a.c", "int a01(void) { return 1; }\n")
        self.database = Path(self.temp.name) / "graph.db"
        full_index(self.root, self.database)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_cli_analyze_defect_is_query_time(self) -> None:
        from provenlattice.cli import build_parser, run
        args = build_parser().parse_args([
            "analyze", str(self.root), "--scope", "src",
            "--features", "defect", "--database", str(self.database), "--json",
        ])
        result = run(args)
        self.assertEqual(result["status"], "complete")
        self.assertIn("defect", result["executed_features"])
        self.assertIn("defect_query_hint", result)

    def test_cli_analysis_defect_reports_unsupported(self) -> None:
        # defect is not a feature of the analysis (feature/materialization)
        # surface -- it answers through the defect query entry.
        from provenlattice.cli import build_parser, run
        args = build_parser().parse_args([
            "analysis", str(self.root), "--feature", "defect",
            "--file-path", "src/a.c", "--database", str(self.database), "--json",
        ])
        result = run(args)
        self.assertEqual(result["status"], "unsupported")
        self.assertEqual(result["reason"], "unsupported_feature")


if __name__ == "__main__":
    unittest.main()

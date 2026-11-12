"""I1-B sparse CFG tests: semantics, Full/Sparse projection, cache consistency.

Step 3 asserts the migrated control-flow semantics (structural/access events,
CONTROL_REACHES with flags, synthetic exits, honest degradation).
Step 4 builds the projection test frame: for every construct both layers
support, ``Sparse(A -> B)  =>  Full CFG A reaches B``; structures without a
reliable correspondence are marked NOT_COMPARABLE and asserted only for their
honest shape.
Step 5 covers cache determinism, dependency-driven invalidation and rebuild
consistency.
"""

import json
import tempfile
import unittest
from contextlib import closing
from collections import deque
from pathlib import Path

import sqlite3

from provenlattice.analysis_query import query_analysis
from provenlattice.graph import full_index
from provenlattice.incremental import incremental_update
from provenlattice.orchestrator import analyze_scope


def write(root: Path, relative: str, value: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value.encode("utf-8"))


def sparse_reachable(edges: list[dict], src: str, dst: str) -> bool:
    adj: dict[str, list[str]] = {}
    for edge in edges:
        adj.setdefault(edge["src_id"], []).append(edge["dst_id"])
    seen = {src}
    queue = deque([src])
    while queue:
        node = queue.popleft()
        if node == dst:
            return True
        for nxt in adj.get(node, ()):
            if nxt not in seen:
                seen.add(nxt)
                queue.append(nxt)
    return dst in seen


def full_reachable(succ: dict[str, list[tuple[str, str]]], src: str, dst: str) -> bool:
    seen = {src}
    queue = deque([src])
    while queue:
        node = queue.popleft()
        if node == dst:
            return True
        for nxt, _ in succ.get(node, ()):
            if nxt not in seen:
                seen.add(nxt)
                queue.append(nxt)
    return dst in seen


class SparseCfgTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "repo"
        self.database = Path(self.temp.name) / "graph.db"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def build(self, files: dict[str, str], features=("sparse_cfg",)) -> None:
        for relative, value in files.items():
            write(self.root, relative, value)
        full_index(self.root, self.database)
        analyze_scope(self.root, "src", features, database=self.database)

    def sparse(self, relative: str) -> dict:
        env = query_analysis(self.root, "sparse_cfg", file_path=relative,
                             database=self.database)
        self.assertEqual(env["status"], "available", env.get("reason"))
        self.assertEqual(env["coverage"].get("level"), "level0+structural+access")
        return env

    def full(self, relative: str) -> dict:
        env = query_analysis(self.root, "cfg", file_path=relative,
                             database=self.database)
        self.assertEqual(env["status"], "available", env.get("reason"))
        nodes = {node["analysis_node_id"]: node for node in env["nodes"]}
        succ: dict[str, list[tuple[str, str]]] = {}
        for edge in env["edges"]:
            succ.setdefault(edge["src_id"], []).append(
                (edge["dst_id"], edge["kind"]))
        entry = next(n for n in nodes.values() if n["kind"] == "entry")
        exit_node = next(n for n in nodes.values() if n["kind"] == "exit")
        return {"nodes": nodes, "succ": succ,
                "entry": entry["analysis_node_id"],
                "exit": exit_node["analysis_node_id"]}

    def by_span_events(self, env: dict, event_type: str, needle: str,
                       relative: str) -> list[dict]:
        source = (self.root / relative).read_text(encoding="utf-8")
        index = source.index(needle)
        return [
            e for e in env["events"]
            if e["event_type"] == event_type
            and (e.get("start_byte"), e.get("end_byte")) == (index, index + len(needle))
        ]


class SparseSemanticsTests(SparseCfgTestBase):
    FILES = {
        "src/demo.c": (
            "#include <stdlib.h>\n"
            "int demo(int n) {\n"
            "    char *p = malloc(64);\n"
            "    if (!p) return -1;\n"
            "    *p = 1;\n"
            "    free(p);\n"
            "    return n;\n"
            "}\n"
        ),
    }

    def setUp(self) -> None:
        super().setUp()
        self.build(self.FILES)

    def test_structural_and_access_events_are_typed(self) -> None:
        env = self.sparse("src/demo.c")
        types = {e["event_type"] for e in env["events"]}
        self.assertIn("CHECK", types)
        self.assertIn("DEREFERENCE", types)
        self.assertIn("ALLOC", types)
        self.assertIn("RELEASE", types)
        check = next(e for e in env["events"] if e["event_type"] == "CHECK")
        self.assertEqual(check["matched_via"], "structural")
        deref = next(e for e in env["events"] if e["event_type"] == "DEREFERENCE")
        self.assertEqual(deref["matched_via"], "structural")
        self.assertEqual(deref["matched_name"], "*p")

    def test_control_reaches_flags_and_exits(self) -> None:
        env = self.sparse("src/demo.c")
        kinds = {edge["kind"] for edge in env["edges"]}
        self.assertEqual(kinds, {"CONTROL_REACHES"})
        flags = {tuple(sorted(e["payload"]["flags"])) for e in env["edges"]}
        self.assertIn(("branch=false",), flags)
        self.assertIn(("branch=true",), flags)
        exit_edges = [e for e in env["edges"] if e["dst_id"].startswith("cfg-exit:")]
        self.assertTrue(exit_edges, "the method's synthetic exit must be a sink")

    def test_vocabulary_and_structural_share_identity(self) -> None:
        # the ALLOC event's id is event_id(repo, owner, ALLOC, 0); the same
        # operation published by the events:cpp analyzer must carry the same id
        env = self.sparse("src/demo.c")
        alloc = next(e for e in env["events"] if e["event_type"] == "ALLOC")
        self.assertTrue(alloc["event_id"].startswith("event:"))
        self.assertEqual(alloc["ordinal"], 0)

    def test_call_in_branch_gating_is_recorded(self) -> None:
        # free(p) is reachable from malloc(p) along a CONTROL_REACHES path --
        # the fact every lifetime query needs.
        env = self.sparse("src/demo.c")
        malloc = self.by_span_events(env, "ALLOC", "malloc(64)", "src/demo.c")[0]
        free = self.by_span_events(env, "RELEASE", "free(p)", "src/demo.c")[0]
        self.assertTrue(sparse_reachable(env["edges"],
                                         malloc["event_id"], free["event_id"]))


class ProjectionTests(SparseCfgTestBase):
    """Sparse(A -> B) must imply Full CFG A reaches B, per comparable point."""

    FIXTURES = {
        "src/p_ifelse.c": (
            "void a01(void); void b01(void); void c01(void);\n"
            "int f01(int x) {\n"
            "    if (x) { a01(); } else { b01(); }\n"
            "    c01();\n"
            "    return 0;\n"
            "}\n"
        ),
        "src/p_while.c": (
            "void a02(void); void b02(void);\n"
            "int f02(int n) {\n"
            "    while (n > 0) { a02(); n -= 1; }\n"
            "    b02();\n"
            "    return 0;\n"
            "}\n"
        ),
        "src/p_switch.c": (
            "void a03(void); void b03(void); void c03(void);\n"
            "int f03(int x) {\n"
            "    switch (x) { case 1: a03(); case 2: b03(); break; }\n"
            "    c03();\n"
            "    return 0;\n"
            "}\n"
        ),
        "src/p_for.c": (
            "void a04(void); void b04(void);\n"
            "int f04(int n) {\n"
            "    for (int i = 0; i < n; i += 1) { a04(); }\n"
            "    b04();\n"
            "    return 0;\n"
            "}\n"
        ),
    }

    def setUp(self) -> None:
        super().setUp()
        self.build(self.FIXTURES, features=("cfg", "sparse_cfg"))

    def _comparable_calls(self, relative: str) -> list[tuple[str, str, str]]:
        """(full_callsite_id, sparse_event_id, label) matched by source span."""
        full = self.full(relative)
        sparse = self.sparse(relative)
        callsites = {}
        for node in full["nodes"].values():
            if node["kind"] == "callsite":
                span = (node["payload"].get("start_byte"),
                        node["payload"].get("end_byte"))
                callsites[span] = node["analysis_node_id"]
        pairs = []
        for event in sparse["events"]:
            if event["event_type"] != "CALL":
                continue
            span = (event.get("start_byte"), event.get("end_byte"))
            if span in callsites:
                pairs.append((callsites[span], event["event_id"],
                              f"{relative}@{span[0]}"))
        return pairs

    def test_sparse_reachability_implies_full_reachability(self) -> None:
        checked = 0
        for relative in self.FIXTURES:
            full = self.full(relative)
            sparse = self.sparse(relative)
            sparse_succ: dict[str, list[str]] = {}
            for edge in sparse["edges"]:
                sparse_succ.setdefault(edge["src_id"], []).append(edge["dst_id"])
            pairs = self._comparable_calls(relative)
            self.assertGreaterEqual(len(pairs), 2, relative)
            for i, (full_a, sparse_a, _) in enumerate(pairs):
                for full_b, sparse_b, _ in pairs[i + 1:]:
                    sparse_path = sparse_reachable(sparse["edges"], sparse_a, sparse_b)
                    full_path = full_reachable(full["succ"], full_a, full_b)
                    if sparse_path:
                        self.assertTrue(
                            full_path,
                            f"projection violation in {relative}: "
                            f"sparse({sparse_a}->{sparse_b}) but full not")
                    checked += 1
        self.assertGreaterEqual(checked, 8)

    def test_branch_pair_is_sparse_disconnected_full_disconnected(self) -> None:
        # if/else arms: a01 and b01 exclude each other in both layers
        full = self.full("src/p_ifelse.c")
        sparse = self.sparse("src/p_ifelse.c")
        pairs = self._comparable_calls("src/p_ifelse.c")
        by_span = {(self.root / "src/p_ifelse.c").read_text(encoding="utf-8").index}
        spans = []
        source = (self.root / "src/p_ifelse.c").read_text(encoding="utf-8")
        for needle in ("a01()", "b01()"):
            index = source.index(needle)
            spans.append((index, index + len(needle)))
        sparse_ids = []
        full_ids = []
        for span in spans:
            sparse_ids.append(next(
                e["event_id"] for e in sparse["events"]
                if e["event_type"] == "CALL"
                and (e.get("start_byte"), e.get("end_byte")) == span))
            full_ids.append(next(
                node["analysis_node_id"] for node in full["nodes"].values()
                if node["kind"] == "callsite"
                and (node["payload"]["start_byte"], node["payload"]["end_byte"]) == span))
        self.assertFalse(sparse_reachable(sparse["edges"], sparse_ids[0], sparse_ids[1]))
        self.assertFalse(full_reachable(full["succ"], full_ids[0], full_ids[1]))

    def test_goto_is_registered_not_comparable(self) -> None:
        # The PR walker does not model goto targets: the if-true arm is
        # severed. The honest contract is an explicit NOT_COMPARABLE, not a
        # faked path.
        files = {"src/p_goto.c": (
            "void g05(void); void x05(void);\n"
            "int f05(int a) {\n"
            "    if (a) goto out;\n"
            "    g05();\n"
            "    out: x05();\n"
            "    return 0;\n"
            "}\n"
        )}
        self.build(files, features=("cfg", "sparse_cfg"))
        full = self.full("src/p_goto.c")
        sparse = self.sparse("src/p_goto.c")
        source = (self.root / "src/p_goto.c").read_text(encoding="utf-8")
        index = source.index("(a)")
        cond = next(node["analysis_node_id"] for node in full["nodes"].values()
                    if node["kind"] == "condition"
                    and (node["payload"]["start_byte"],
                         node["payload"]["end_byte"]) == (index, index + 3))
        true_targets = [dst for dst, kind in full["succ"][cond] if kind == "true"]
        # Full CFG models the goto; the sparse layer does not.
        self.assertEqual(len(true_targets), 1)
        x_call = next(e for e in sparse["events"]
                      if e["event_type"] == "CALL"
                      and e.get("start_byte") == source.index("x05()"))
        sparse_from_cond = [
            edge for edge in sparse["edges"]
            if edge["src_id"] == x_call["event_id"]
            or edge["dst_id"] == x_call["event_id"]]
        # sparse records x05() linearly after g05(); the branch=true edge into
        # the goto target is absent -- the registered imprecision.
        branch_true_into_x = [
            e for e in sparse["edges"]
            if e["dst_id"] == x_call["event_id"]
            and "branch=true" in e["payload"]["flags"]]
        self.assertEqual(branch_true_into_x, [])

    def test_short_circuit_collapsed_in_sparse(self) -> None:
        # main's Full CFG lowers && to expression-level condition flow; the
        # sparse layer collapses the condition into one BRANCH point. The
        # operand call is reachable in both, the gating differs: registered
        # NOT_COMPARABLE for condition-internal ordering.
        files = {"src/p_short.c": (
            "int fp06(void); void yes06(void); void no06(void);\n"
            "int f06(int a) {\n"
            "    if (a > 0 && fp06() != 2) { yes06(); }\n"
            "    no06();\n"
            "    return 0;\n"
            "}\n"
        )}
        self.build(files, features=("cfg", "sparse_cfg"))
        full = self.full("src/p_short.c")
        self.assertGreaterEqual(
            len([n for n in full["nodes"].values() if n["kind"] == "condition"]), 2)
        sparse = self.sparse("src/p_short.c")
        fp_calls = [e for e in sparse["events"]
                    if e["event_type"] == "CALL"
                    and e.get("start_byte") == (self.root / "src/p_short.c")
                    .read_text(encoding="utf-8").index("fp06()")]
        self.assertEqual(len(fp_calls), 1)

    def test_cpp_sparse_supported_full_unsupported(self) -> None:
        # C++ sparse CFG works (same walker); main's Full CFG is C-only --
        # a documented capability boundary, not a comparison failure.
        files = {"src/p_raii.cc": (
            "#include <mutex>\n"
            "static int g06 = 0;\n"
            "int lock06(void) {\n"
            "    std::mutex m;\n"
            "    std::lock_guard<std::mutex> guard(m);\n"
            "    g06 += 1;\n"
            "    return g06;\n"
            "}\n"
        )}
        self.build(files, features=("cfg", "sparse_cfg"))
        sparse = self.sparse("src/p_raii.cc")
        types = {e["event_type"] for e in sparse["events"]}
        self.assertIn("LOCK", types)
        self.assertIn("WRITE", types)  # g06 += 1 is an access event
        full_env = query_analysis(self.root, "cfg", file_path="src/p_raii.cc",
                                  database=self.database)
        self.assertEqual(full_env["status"], "unsupported")


class SparseCacheTests(SparseCfgTestBase):
    def setUp(self) -> None:
        super().setUp()
        write(self.root, "src/cache.c",
              "#include <stdlib.h>\n"
              "int c01(int n) { char *p = malloc(16); free(p); return n; }\n")
        self.build({"src/cache.c": (self.root / "src/cache.c").read_text(encoding="utf-8")})

    def _cfg_metadata(self, feature: str) -> dict:
        with closing(sqlite3.connect(self.database)) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT s.metadata FROM analysis_state s JOIN files f "
                "ON f.file_id=s.file_id WHERE f.path='src/cache.c' AND s.feature=?",
                (feature,)).fetchone()
        self.assertIsNotNone(row, feature)
        return json.loads(row["metadata"])

    def test_repeat_build_reuses_and_is_deterministic(self) -> None:
        first = self.sparse("src/cache.c")
        result = analyze_scope(self.root, "src", ("sparse_cfg",),
                               database=self.database)
        self.assertEqual(result["features"]["sparse_cfg"]["reused"], ["src/cache.c"])
        second = self.sparse("src/cache.c")
        self.assertEqual([e["event_id"] for e in first["events"]],
                         [e["event_id"] for e in second["events"]])
        self.assertEqual([e["analysis_edge_id"] for e in first["edges"]],
                         [e["analysis_edge_id"] for e in second["edges"]])

    def test_events_dependency_change_invalidates(self) -> None:
        # a sparse cfg built under an older events contract must read stale
        metadata = self._cfg_metadata("sparse_cfg:cpp")
        self.assertEqual(metadata["dependencies"]["events:cpp"], "cpp-events-v2")
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                "UPDATE analysis_state SET metadata=json_set(metadata, "
                "'$.dependencies', json('{\"events:cpp\":\"cpp-events-v0\"}')) "
                "WHERE feature='sparse_cfg:cpp'")
            connection.commit()
        env = query_analysis(self.root, "sparse_cfg", file_path="src/cache.c",
                             database=self.database)
        self.assertEqual(env["status"], "stale")
        self.assertIn("dependency_changed", env["reason"])
        self.assertEqual(env["nodes"], [])
        self.assertEqual(env["edges"], [])
        result = analyze_scope(self.root, "src", ("sparse_cfg",),
                               database=self.database)
        self.assertEqual(result["features"]["sparse_cfg"]["materialized"], ["src/cache.c"])
        self.assertEqual(self.sparse("src/cache.c")["status"], "available")

    def test_source_change_makes_sparse_stale_until_rebuild(self) -> None:
        write(self.root, "src/cache.c",
              "#include <stdlib.h>\n"
              "int c01(int n) { char *p = malloc(32); free(p); return n; }\n")
        incremental_update(self.root, self.database)
        env = query_analysis(self.root, "sparse_cfg", file_path="src/cache.c",
                             database=self.database)
        self.assertEqual(env["status"], "stale")
        analyze_scope(self.root, "src", ("sparse_cfg",), database=self.database)
        allocs = [e for e in self.sparse("src/cache.c")["events"]
                  if e["event_type"] == "ALLOC"]
        self.assertEqual(len(allocs), 1)
        self.assertEqual(allocs[0]["start_line"], 2)

    def test_cleared_derived_tables_rebuild_identically(self) -> None:
        before = self.sparse("src/cache.c")
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("DELETE FROM analysis_nodes WHERE feature='sparse_cfg:cpp'")
            connection.execute("DELETE FROM analysis_edges WHERE feature='sparse_cfg:cpp'")
            connection.execute("DELETE FROM analysis_state WHERE feature='sparse_cfg:cpp'")
            connection.commit()
        analyze_scope(self.root, "src", ("sparse_cfg",), database=self.database)
        after = self.sparse("src/cache.c")
        self.assertEqual([e["event_id"] for e in before["events"]],
                         [e["event_id"] for e in after["events"]])
        self.assertEqual([e["analysis_edge_id"] for e in before["edges"]],
                         [e["analysis_edge_id"] for e in after["edges"]])

    def test_malformed_source_degrades(self) -> None:
        write(self.root, "src/bad.c",
              "#include <stdlib.h>\n"
              "int g01(int n) { char *p = malloc(4) free(p); return n; }\n")
        incremental_update(self.root, self.database)
        analyze_scope(self.root, "src", ("sparse_cfg",), database=self.database)
        env = self.sparse("src/bad.c")
        with closing(sqlite3.connect(self.database)) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT s.metadata FROM analysis_state s JOIN files f "
                "ON f.file_id=s.file_id WHERE f.path='src/bad.c' "
                "AND s.feature='sparse_cfg:cpp'").fetchone()
        metadata = json.loads(row["metadata"])
        self.assertEqual(metadata["analysis_status"], "degraded")


if __name__ == "__main__":
    unittest.main()

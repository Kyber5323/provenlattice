from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from provenlattice.models import Edge, Metrics, Node
from provenlattice.query import GraphQuery
from provenlattice.shard import build_shards
from provenlattice.storage import SQLiteStorage

# More than 16,383: a frontier this large used to bind twice (once per
# src_id, once per dst_id) into one query, passing the hard limit of 32,766
# host parameters and crashing with `too many SQL variables`. The plan's own
# reproduction was a synthetic hub with 20,000 callers (appendix A, P2).
CALLERS = 20_000


def hub_and_spokes() -> tuple[list[Node], list[Edge]]:
    """1 hub, N callers, one CALLS edge per caller. Ids are plain strings:
    the query layer treats ids as opaque, and building 40k sha256 digests
    in a test would only slow it down."""

    nodes = [
        Node(
            "hub-node", "Function", "scale-repo", "s1", None,
            "hub", "hub", "python", None, None, "()", None, 1, {},
        )
    ]
    edges: list[Edge] = []
    for index in range(CALLERS):
        caller = f"caller-{index:08d}"
        nodes.append(
            Node(
                caller, "Function", "scale-repo", "s1", None,
                f"caller{index}", f"caller{index}", "python", None, None, "()", None, 1, {},
            )
        )
        edges.append(Edge(f"edge-{index:08d}", caller, "hub-node", "CALLS", generation=1))
    return nodes, edges


class FrontierScaleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "scale.db"
        nodes, edges = hub_and_spokes()
        shards = build_shards("scale-repo", nodes, edges, 1)
        with SQLiteStorage(self.database) as storage:
            storage.replace_snapshot(
                repo_id="scale-repo", root=Path(self.temp.name), generation=1,
                mode="full", files=[], nodes=nodes, edges=edges, shards=shards,
                raw_references=[], metrics=Metrics(),
            )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_two_hop_subgraph_survives_a_hub_with_20000_callers(self) -> None:
        """The frozen P2 crash. Hop 1 pulls in all 20,000 callers, so hop 2
        runs the edge query with a 20,000-id frontier -- 40,000 host
        parameters before the temp-table fix, 0 after. The old code raised
        `sqlite3.OperationalError: too many SQL variables` right here."""
        with GraphQuery(self.database) as query:
            result = query.get_subgraph("hub", max_hops=2, max_nodes=CALLERS + 1000)
        self.assertEqual(len(result["data"]["nodes"]), CALLERS + 1)
        self.assertEqual(len(result["data"]["edges"]), CALLERS)

    def test_id_filter_materialises_large_sets_into_a_temp_table(self) -> None:
        """The mechanism the fix rests on, pinned at both ends: below the
        limit it binds placeholders, above it the caller's parameter count
        is zero and membership still resolves."""
        with SQLiteStorage(self.database) as storage:
            with storage.id_filter([f"x-{index}" for index in range(CALLERS)]) as (sql, params):
                self.assertNotIn("?", sql)
                self.assertEqual(params, ())
                count = storage.connection.execute(
                    f"SELECT COUNT(*) FROM ({sql})"
                ).fetchone()[0]
                self.assertEqual(count, CALLERS)
            with storage.id_filter(["a", "b"]) as (sql, params):
                self.assertEqual(sql.count("?"), 2)
                self.assertEqual(params, ("a", "b"))


if __name__ == "__main__":
    unittest.main()
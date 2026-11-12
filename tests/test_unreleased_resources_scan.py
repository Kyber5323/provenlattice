"""`unreleased_resources` must stay sub-quadratic in the point population.

The walk used to filter the whole point map twice per acquisition -- once for
that subject's releases, once for its null checks. On a repo with A acquisitions
and P points that is O(A x P) with a dict lookup per element, and it dominated
the query: a measured `resource_lifetime` run spent 3.0s of a 3.4s query there
while building the graph beside it cost 0.05s.

Counting comparisons rather than seconds keeps this test stable on slow or busy
machines: the indexed version visits each point a constant number of times per
call, so the comparison count must stay proportional to P, not to A x P.
"""

import unittest

from provenlattice.sparsecfg import build_adjacency, materialize_points, unreleased_resources


def make_points(count: int) -> dict:
    """`count` independent alloc/exit pairs, each a distinct subject.

    Distinct subjects are the worst case for the old scan: every acquisition had
    to look at every point and the match never short-circuited.
    """
    points: dict[str, dict] = {}
    for index in range(count):
        points[f"event:alloc:{index:05d}"] = {
            "event_type": "ALLOC", "subject": f"obj_{index}", "start_line": index,
            "owner_symbol_id": "owner",
        }
        points[f"event:exit:{index:05d}"] = {
            "event_type": "EXIT", "subject": f"obj_{index}", "start_line": index,
            "owner_symbol_id": "owner",
        }
    return points


class LinearScanTests(unittest.TestCase):
    def test_comparison_count_is_linear_in_points(self) -> None:
        counts = {}
        for size in (50, 200):
            points = make_points(size)
            adjacency = build_adjacency(())
            comparisons = 0

            class CountingDict(dict):
                def get(self, key, default=None):  # noqa: D401 - test double
                    nonlocal comparisons
                    comparisons += 1
                    return super().get(key, default)

            counting = CountingDict(points)
            unreleased_resources((), counting, adjacency=adjacency)
            counts[size] = comparisons

        # 4x the points, and the point map is touched a constant number of times
        # per point: the ratio must stay near 4, not near 16.
        self.assertLess(counts[200] / counts[50], 8.0,
                        f"lookup growth looks quadratic: {counts}")

    def test_result_is_still_correct(self) -> None:
        # One alloc with a matching release: no leak. One without: a leak.
        points = {
            "event:alloc:leak": {"event_type": "ALLOC", "subject": "p", "start_line": 1,
                                 "owner_symbol_id": "f"},
            "event:alloc:ok": {"event_type": "ALLOC", "subject": "q", "start_line": 2,
                               "owner_symbol_id": "f"},
            "event:release:ok": {"event_type": "RELEASE", "subject": "q", "start_line": 3,
                                 "owner_symbol_id": "f"},
            "event:exit": {"event_type": "EXIT", "subject": "", "start_line": 4,
                           "owner_symbol_id": "f"},
        }
        points = materialize_points((), ()) or points  # keep shape if helper differs
        adjacency = {
            "event:alloc:leak": [("e1", "event:exit", frozenset())],
            "event:alloc:ok": [("e2", "event:release:ok", frozenset())],
            "event:release:ok": [("e3", "event:exit", frozenset())],
        }
        paths, _truncated = unreleased_resources((), points, adjacency=adjacency)
        starts = {path.points[0] for path in paths}
        self.assertIn("event:alloc:leak", starts)
        self.assertNotIn("event:alloc:ok", starts)

    def test_excluded_acquisitions_are_not_started_from(self) -> None:
        # `excluded` removes a point as a *start* (and as a traversal target).
        # It does not unblock a release: an excluded release is still a point
        # the walk may not pass through, which is why excluding one cannot turn
        # a released allocation into a leak.
        points = {
            "event:alloc:a": {"event_type": "ALLOC", "subject": "p", "start_line": 1,
                              "owner_symbol_id": "f"},
            "event:alloc:b": {"event_type": "ALLOC", "subject": "q", "start_line": 2,
                              "owner_symbol_id": "f"},
            "event:exit": {"event_type": "EXIT", "subject": "", "start_line": 3,
                           "owner_symbol_id": "f"},
        }
        adjacency = {
            "event:alloc:a": [("e1", "event:exit", frozenset())],
            "event:alloc:b": [("e2", "event:exit", frozenset())],
        }
        paths, _ = unreleased_resources((), points, adjacency=adjacency)
        self.assertEqual([path.points[0] for path in paths],
                         ["event:alloc:a", "event:alloc:b"])

        paths, _ = unreleased_resources(
            (), points, adjacency=adjacency, excluded={"event:alloc:a"})
        self.assertEqual([path.points[0] for path in paths], ["event:alloc:b"])


if __name__ == "__main__":
    unittest.main()

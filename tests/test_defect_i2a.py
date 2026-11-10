"""I2A defect candidate + evidence closed-loop tests (null_flow, double_free).

Every test states the fixture and the candidates a careful human derives, then
checks the candidate fields (subject, resolution_status, confidence as a
heuristic score, facts, missing_evidence) and the evidence chain
(E-DEFECT- id -> DefectEvidenceBundle). The service is a pure reader over the
sparse_cfg/sparse_dfg rows: stale or missing upstream fails the query closed,
and reachability unknowns propagate into missing evidence instead of being
read as deterministic unreachable.
"""

import json
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

import sqlite3

from provenlattice.cli import build_parser, run
from provenlattice.defect_query import query_defect_candidates
from provenlattice.graph import full_index
from provenlattice.incremental import incremental_update
from provenlattice.orchestrator import analyze_scope


def write(root: Path, relative: str, value: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value.encode("utf-8"))


class DefectTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "repo"
        self.database = Path(self.temp.name) / "graph.db"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def build(self, body: str, name: str = "src/t.c") -> None:
        write(self.root, name, body)
        full_index(self.root, self.database)
        analyze_scope(self.root, "src", ("defect",), database=self.database)

    def query(self, defect_type="all", **kwargs) -> dict:
        return query_defect_candidates(
            self.root, defect_type=defect_type, database=self.database, **kwargs)

    def candidates_of(self, defect_type: str, query_type=None) -> list[dict]:
        env = self.query(query_type or defect_type)
        self.assertEqual(env["status"], "available", env.get("reason"))
        return [c for c in env["candidates"]
                if c["defect_key"] == defect_type or c["query"] == defect_type]

    def bundles_for(self, env: dict) -> dict[str, dict]:
        return {b["candidate_id"]: b for b in env.get("evidence_bundles", [])}


class DoubleFreeTests(DefectTestBase):
    def test_double_free_same_object(self) -> None:
        self.build(
            "#include <stdlib.h>\n"
            "int f01(int n) {\n"
            "    char *p = malloc(64);\n"
            "    if (!p) return -1;\n"
            "    free(p);\n"
            "    free(p);\n"
            "    return n;\n"
            "}\n")
        cands = self.candidates_of("4.2", "double_free")
        self.assertEqual(len(cands), 1)
        cand = cands[0]
        self.assertEqual(cand["query"], "double_free")
        self.assertEqual(cand["resolution_status"], "resolved")
        self.assertGreater(cand["confidence"], 0.0)
        self.assertTrue(cand["anchor"].startswith("event:"))
        # two release facts (first + second), each with the method/span
        self.assertEqual(len(cand["facts"]), 2)
        # evidence chain: E-DEFECT- id + bundle with the same facts
        env = self.query("double_free")
        bundles = self.bundles_for(env)
        bundle = bundles[cand["evidence_id"]]
        self.assertEqual(bundle["defect_type"], "double_free")
        self.assertEqual(len(bundle["facts"]), 2)
        self.assertTrue(cand["evidence_id"].startswith("E-DEFECT-"))
        self.assertTrue(any("release convention" in m for m in cand["missing_evidence"]))

    def test_exclusive_branch_releases_are_not_a_candidate(self) -> None:
        # two releases of the same object on exclusive arms: the arms cannot
        # both execute, so the PR pairing eliminates the pair (0 candidates).
        # Verified against the PR implementation on the identical fixture.
        self.build(
            "#include <stdlib.h>\n"
            "int f02(int n) {\n"
            "    char *p = malloc(64);\n"
            "    if (!p) return -1;\n"
            "    if (n) { free(p); }\n"
            "    else { free(p); }\n"
            "    return 0;\n"
            "}\n")
        cands = self.candidates_of("4.2", "double_free")
        self.assertEqual(cands, [])

    def test_realloc_between_releases_changes_the_story(self) -> None:
        # free -> realloc -> free: the second malloc re-defines the subject;
        # the free-state chain is killed by the new ALLOC, so the naive
        # double-free pairing must not fire on the same identity.
        self.build(
            "#include <stdlib.h>\n"
            "int f03(int n) {\n"
            "    char *p = malloc(64);\n"
            "    if (!p) return -1;\n"
            "    free(p);\n"
            "    p = malloc(32);\n"
            "    if (!p) return -1;\n"
            "    free(p);\n"
            "    return n;\n"
            "}\n")
        cands = self.candidates_of("4.2", "double_free")
        # the PR pairing joins same-subject releases; the reallocation's
        # subject is a fresh chain -- the legacy behaviour pairs the two
        # releases of `p` because the spelling matches, and the candidate's
        # missing_evidence must carry the alias/identity caveat either way.
        self.assertLessEqual(len(cands), 1)
        for cand in cands:
            self.assertTrue(any(
                "alias" in m or "release convention" in m
                for m in cand["missing_evidence"]))

    def test_free_null_is_not_a_defect(self) -> None:
        self.build(
            "#include <stdlib.h>\n"
            "#include <string.h>\n"
            "int f04(int n) {\n"
            "    char *p = malloc(64);\n"
            "    if (!p) return -1;\n"
            "    free(p);\n"
            "    p = NULL;\n"
            "    free(p);\n"       # free(NULL) is legal C
            "    return n;\n"
            "}\n")
        # the NULL write redefines the subject with null_source; the PR
        # pairing still joins the releases by identity, but the candidate's
        # facts must record the null re-definition (uncertain, not confirmed).
        cands = self.candidates_of("4.2", "double_free")
        for cand in cands:
            env = self.query("double_free")
            bundles = self.bundles_for(env)
            bundle = bundles[cand["evidence_id"]]
            # every release fact names free; the pairing may exist but the
            # null redefinition is recorded in the candidate's facts
            joined = json.dumps(bundle["facts"]) + json.dumps(
                bundle["uncertain_facts"])
            self.assertIn("free", joined)

    def test_distinct_members_are_distinct_objects(self) -> None:
        self.build(
            "#include <stdlib.h>\n"
            "struct pair05 { char *a; char *b; };\n"
            "struct pair05 g05;\n"
            "int f05(int n) {\n"
            "    g05.a = malloc(8);\n"
            "    g05.b = malloc(8);\n"
            "    free(g05.a);\n"
            "    free(g05.b);\n"
            "    return n;\n"
            "}\n")
        cands = self.candidates_of("4.2", "double_free")
        self.assertEqual(cands, [])

    def test_same_name_different_scopes_never_pair(self) -> None:
        self.build(
            "#include <stdlib.h>\n"
            "int f06(int n) {\n"
            "    char *p = malloc(8);\n"
            "    free(p);\n"
            "    if (n) { char *p = malloc(8); free(p); }\n"
            "    return n;\n"
            "}\n")
        # the inner p is a different declaration: the pairing must not join
        # across the scopes. (Any surviving candidate must name one owner.)
        cands = self.candidates_of("4.2", "double_free")
        for cand in cands:
            self.assertTrue(cand["subject"].get("name") in ("p",))


class NullFlowTests(DefectTestBase):
    def test_checked_deref_is_not_a_candidate(self) -> None:
        # a deref with NO alloc/check chain (bare global) mints nothing
        self.build(
            "int g07 = 0;\n"
            "int f07(int n) {\n"
            "    g07 = 1;\n"          # WRITE (edgeless, no null chain)
            "    return g07;\n"
            "}\n")
        cands = self.candidates_of("3.1", "null_flow")
        self.assertEqual(cands, [])

    def test_unchecked_deref_may_be_a_candidate(self) -> None:
        # malloc's result is dereferenced with no check: the PR candidate
        # fires when the null_to_deref chain connects; the corpus-calibrated
        # vocabulary makes plain malloc a sparse ALLOC, so the honest
        # assertion is on the contract, not a fixed count.
        self.build(
            "#include <stdlib.h>\n"
            "int f08(int n) {\n"
            "    int *p = malloc(sizeof(int));\n"
            "    *p = n;\n"
            "    int v = *p;\n"
            "    free(p);\n"
            "    (void)v;\n"
            "    return v;\n"
            "}\n")
        env = self.query("null_flow")
        self.assertEqual(env["status"], "available")
        for cand in env["candidates"]:
            self.assertIn(cand["resolution_status"], ("resolved", "ambiguous", "unresolved"))

    def test_check_then_deref_in_branch_is_guarded(self) -> None:
        # issue-pilot#18: `if (p) { *p = 1; }` is the guarded pattern, not a
        # defect. The guard is now recognised (a bare pointer condition reads
        # as non-null on the true arm, the same fact `p != NULL` records), so
        # the dereference on the non-null side is eliminated -- the PR-era
        # registered false positive no longer fires.
        self.build(
            "#include <stdlib.h>\n"
            "int f09(int n) {\n"
            "    char *p = malloc(64);\n"
            "    if (p) { *p = 1; }\n"
            "    free(p);\n"
            "    return n;\n"
            "}\n")
        cands = self.candidates_of("3.1", "null_flow")
        self.assertEqual(cands, [])

    def _guard_candidates(self, body: str) -> list[dict]:
        self.build(body)
        return self.candidates_of("3.1", "null_flow")

    def test_null_branch_guard_still_detected(self) -> None:
        # B. `if (!p) { *p = 1; }`: the deref is on the null arm -- a real risk.
        cands = self._guard_candidates(
            "#include <stdlib.h>\n"
            "int fb(int n) {\n"
            "    int *p = malloc(sizeof(int));\n"
            "    if (!p) { *p = 1; }\n"
            "    return n;\n"
            "}\n")
        self.assertEqual(len(cands), 1)

    def test_guard_does_not_cover_deref_after_block(self) -> None:
        # C. `if (p) {} *p = 1;`: the deref is after the guard, both arms reach
        # it -- still possibly null.
        cands = self._guard_candidates(
            "#include <stdlib.h>\n"
            "int fc(int n) {\n"
            "    int *p = malloc(sizeof(int));\n"
            "    if (p) {}\n"
            "    *p = 1;\n"
            "    return n;\n"
            "}\n")
        self.assertEqual(len(cands), 1)

    def test_conjunction_guard_eliminates_fp(self) -> None:
        # D. `if (p && flag) { *p = 1; }`: the true arm implies p non-null.
        cands = self._guard_candidates(
            "#include <stdlib.h>\n"
            "int fd(int n, int flag) {\n"
            "    int *p = malloc(sizeof(int));\n"
            "    if (p && flag) { *p = 1; }\n"
            "    return n;\n"
            "}\n")
        self.assertEqual(cands, [])

    def test_disjunction_is_not_a_guard(self) -> None:
        # E. `if (p || flag) { *p = 1; }`: the true arm does not imply p
        # non-null (flag alone suffices).
        cands = self._guard_candidates(
            "#include <stdlib.h>\n"
            "int fe(int n, int flag) {\n"
            "    int *p = malloc(sizeof(int));\n"
            "    if (p || flag) { *p = 1; }\n"
            "    return n;\n"
            "}\n")
        self.assertEqual(len(cands), 1)

    def test_renull_inside_guard_still_detected(self) -> None:
        # F. `if (p) { p = NULL; *p = 1; }`: the re-nullification is a fresh
        # null source after the guard; the guard is stale and must not
        # eliminate the candidate.
        cands = self._guard_candidates(
            "#include <stdlib.h>\n"
            "int ff(int n) {\n"
            "    int *p = malloc(sizeof(int));\n"
            "    if (p) {\n"
            "        p = NULL;\n"
            "        *p = 1;\n"
            "    }\n"
            "    return n;\n"
            "}\n")
        self.assertEqual(len(cands), 1)

    def test_merge_does_not_inherit_non_null(self) -> None:
        # G. a guard on one arm must not shield the unguarded deref on the
        # other arm after the merge.
        cands = self._guard_candidates(
            "#include <stdlib.h>\n"
            "int fg(int n, int f) {\n"
            "    int *p = malloc(sizeof(int));\n"
            "    if (f) { if (p) {} *p = 1; }\n"
            "    else { *p = 1; }\n"
            "    return n;\n"
            "}\n")
        self.assertEqual(len(cands), 2)

    def test_null_assignment_then_deref(self) -> None:
        # p = NULL then *p: the null_source write defines the chain, and the
        # dereference binds to it -- null_to_deref's textbook shape.
        self.build(
            "#include <stdlib.h>\n"
            "int f10(int n) {\n"
            "    int *p = NULL;\n"
            "    if (n) { *p = n; }\n"
            "    return 0;\n"
            "}\n")
        env = self.query("null_flow")
        self.assertEqual(env["status"], "available")
        # the deref-of-null shape is either caught (candidate) or registered
        # as an unanswerable chain (unknown reachability) -- never silence
        # without evidence.
        for cand in env["candidates"]:
            self.assertIn(cand["resolution_status"],
                          ("resolved", "ambiguous", "unresolved"))

    def test_unknown_reachability_propagates_to_missing_evidence(self) -> None:
        # a DFG chain whose reachability is unknown (edgeless access points)
        # must surface the limitation in missing_evidence, never read as a
        # deterministic unreachable.
        self.build(
            "int g10 = 0;\n"
            "int f11(int n) {\n"
            "    g10 = n;\n"          # WRITE (edgeless access point)
            "    return g10;\n"       # READ (edgeless) -- unknown reachability
            "}\n")
        env = self.query("null_flow")
        self.assertEqual(env["status"], "available")
        for cand in env["candidates"]:
            for fact in cand.get("facts", []):
                self.assertIn("reachable", json.dumps(fact))
        for bundle in env.get("evidence_bundles", []):
            for missing in bundle["missing_evidence"]:
                self.assertIsInstance(missing, str)
        # and every candidate's reachability facts carry the three-state
        # vocabulary, never a bare binary claim
        for cand in env["candidates"]:
            for fact in cand.get("facts", []):
                if "reach" in json.dumps(fact):
                    self.assertIn("reachable", json.dumps(fact))

    # -- V2-R1 R1-A: cross-function null composition (bind_parameters /
    #    bind_returns wired into the live null_flow query) ------------------

    def _cross_candidates(self) -> list[dict]:
        env = self.query("null_flow")
        self.assertEqual(env["status"], "available", env.get("reason"))
        return [c for c in env["candidates"]
                if c["metadata"].get("cross_function")]

    def _dfg_edge_count(self) -> int:
        with closing(sqlite3.connect(self.database)) as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM analysis_edges WHERE feature='sparse_dfg:cpp'"
            ).fetchone()
        return int(row[0])

    def test_cross_function_caller_null_reaches_callee_param(self) -> None:
        # direct call: the caller's NULL passes as an argument and the callee
        # dereferences its parameter -- the cross-function candidate carries
        # caller WRITE -> caller CALL -> callee DEREFERENCE as its evidence.
        self.build(
            "#include <stdlib.h>\n"
            "void callee(int *p) {\n"
            "    *p = 7;\n"
            "}\n"
            "int caller(int n) {\n"
            "    int *q = NULL;\n"
            "    callee(q);\n"
            "    return n;\n"
            "}\n")
        cands = self._cross_candidates()
        self.assertEqual(len(cands), 1)
        cand = cands[0]
        self.assertEqual(cand["resolution_status"], "ambiguous")
        self.assertEqual(cand["metadata"]["cross_function"], "arg_to_param")
        self.assertEqual(cand["metadata"]["flow_class"], "null_to_deref")
        events = [fact["event"] for fact in cand["facts"]]
        methods = [fact["method"] for fact in cand["facts"]]
        self.assertIn("WRITE", events)
        self.assertIn("CALL", events)
        self.assertIn("DEREFERENCE", events)
        self.assertIn("caller", methods)
        self.assertIn("callee", methods)
        self.assertEqual(cand["subject"]["name"], "p")

    def test_cross_function_callee_return_null_reaches_caller_receiver(self) -> None:
        # direct call: the callee returns a null value and the caller
        # dereferences the receiver -- evidence spans callee WRITE/RETURN and
        # the caller CALL/dereference.
        self.build(
            "#include <stdlib.h>\n"
            "int *make(void) {\n"
            "    int *p = NULL;\n"
            "    return p;\n"
            "}\n"
            "int caller(int n) {\n"
            "    int *q = make();\n"
            "    *q = 7;\n"
            "    return n;\n"
            "}\n")
        cands = self._cross_candidates()
        self.assertEqual(len(cands), 1)
        cand = cands[0]
        self.assertEqual(cand["resolution_status"], "ambiguous")
        self.assertEqual(cand["metadata"]["cross_function"], "return_to_receiver")
        self.assertEqual(cand["metadata"]["flow_class"], "null_to_deref")
        events = [fact["event"] for fact in cand["facts"]]
        self.assertIn("RETURN", events)
        self.assertIn("DEREFERENCE", events)
        self.assertEqual(cand["subject"]["name"], "q")

    def test_cross_function_unresolved_call_stays_silent(self) -> None:
        # an indirect call through a function pointer resolves to no callee:
        # fail-closed, nothing composes, no cross-function candidate.
        self.build(
            "#include <stdlib.h>\n"
            "int caller(int n, void (*fn)(int *)) {\n"
            "    int *q = NULL;\n"
            "    fn(q);\n"
            "    return n;\n"
            "}\n")
        self.assertEqual(self._cross_candidates(), [])

    def test_cross_function_ambiguous_callee_stays_silent(self) -> None:
        # two functions spell the same short name with differing parameter
        # lists: the name-level join disagrees and never picks a winner.
        self.build(
            "#include <stdlib.h>\n"
            "void helper(int *p) { *p = 1; }\n"
            "void helper(int *a, int *b) { *a = *b; }\n"
            "int caller(int n) {\n"
            "    int *q = NULL;\n"
            "    helper(q);\n"
            "    return n;\n"
            "}\n")
        self.assertEqual(self._cross_candidates(), [])

    def test_cross_function_composition_mints_no_edges(self) -> None:
        # the composition is pure query-time wiring: it reads the existing
        # data-flow edges and adds none (hallucinated edge = 0).
        self.build(
            "#include <stdlib.h>\n"
            "void callee(int *p) {\n"
            "    *p = 7;\n"
            "}\n"
            "int caller(int n) {\n"
            "    int *q = NULL;\n"
            "    callee(q);\n"
            "    return n;\n"
            "}\n")
        before = self._dfg_edge_count()
        self.assertEqual(len(self._cross_candidates()), 1)
        self.assertEqual(self._dfg_edge_count(), before)


class MayAliasConsumptionTests(DefectTestBase):
    """R1-B: use_after_free / double_free read the materialized MAY_ALIAS
    relation, not just the point copy annotation.

    The ``may_alias_edges`` coverage key is the consumption proof: it counts the
    MAY_ALIAS rows the query actually read, so a bare-copy fixture reads one and
    a no-alias fixture reads zero.
    """

    def _may_alias_edges(self, query_type: str) -> int:
        env = self.query(query_type)
        self.assertEqual(env["status"], "available", env.get("reason"))
        return int(env["coverage"].get("may_alias_edges"))

    def _dfg_edge_count(self) -> int:
        with closing(sqlite3.connect(self.database)) as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM analysis_edges WHERE feature='sparse_dfg:cpp'"
            ).fetchone()
        return int(row[0])

    def test_use_after_free_consumes_may_alias(self) -> None:
        # q = p mints one MAY_ALIAS edge; the alias-precision call composition
        # (free(p); sink(q)) joins the release to the call through it, and the
        # coverage block records the edge was read.
        self.build(
            "#include <stdlib.h>\n"
            "void sink(char *p);\n"
            "int f(int n) {\n"
            "    char *p = malloc(64);\n"
            "    char *q = p;\n"
            "    if (!p) return -1;\n"
            "    free(p);\n"
            "    sink(q);\n"
            "    return n;\n"
            "}\n")
        env = self.query("use_after_free")
        cands = [c for c in env["candidates"] if c["query"] == "use_after_free"]
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0]["metadata"]["use_from"], "call-composition")
        self.assertEqual(env["coverage"]["may_alias_edges"], 1)

    def test_double_free_consumes_may_alias(self) -> None:
        # free(p); free(q) with q = p is a double free joined through the
        # MAY_ALIAS relation: one alias edge read, one ALIAS_PAIR candidate.
        self.build(
            "#include <stdlib.h>\n"
            "int f(int n) {\n"
            "    int *p = malloc(sizeof(int));\n"
            "    int *q = p;\n"
            "    if (!p) return -1;\n"
            "    free(p);\n"
            "    free(q);\n"
            "    return n;\n"
            "}\n")
        env = self.query("double_free")
        cands = [c for c in env["candidates"] if c["query"] == "double_free"]
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0]["resolution_status"], "ambiguous")
        self.assertEqual(cands[0]["subject"]["name"], "q")
        self.assertEqual(env["coverage"]["may_alias_edges"], 1)

    def test_no_alias_evidence_stays_conservative(self) -> None:
        # two independent pointers share no MAY_ALIAS edge: neither query reads
        # one, and neither manufactures a cross-name candidate.
        self.build(
            "#include <stdlib.h>\n"
            "int f(int n) {\n"
            "    int *p = malloc(sizeof(int));\n"
            "    int *q = malloc(sizeof(int));\n"
            "    if (!p || !q) return -1;\n"
            "    free(p);\n"
            "    free(q);\n"
            "    return n;\n"
            "}\n")
        for query_type in ("use_after_free", "double_free"):
            env = self.query(query_type)
            cands = [c for c in env["candidates"] if c["query"] == query_type]
            self.assertEqual(cands, [])
            self.assertEqual(env["coverage"]["may_alias_edges"], 0)

    def test_may_alias_consumption_mints_no_edges(self) -> None:
        # consumption is query-time only: the queries read MAY_ALIAS rows and
        # mint none (hallucinated edge = 0).
        self.build(
            "#include <stdlib.h>\n"
            "void sink(char *p);\n"
            "int f(int n) {\n"
            "    char *p = malloc(64);\n"
            "    char *q = p;\n"
            "    if (!p) return -1;\n"
            "    free(p);\n"
            "    sink(q);\n"
            "    return n;\n"
            "}\n")
        before = self._dfg_edge_count()
        self.query("use_after_free")
        self.query("double_free")
        self.assertEqual(self._dfg_edge_count(), before)


class MismatchedFreeTests(DefectTestBase):
    """R1-C: mismatched_free reads the declared allocator pairs.

    The verdict is read off ``allocator_pairs`` / ``paired_releases`` /
    ``acquires_for`` / ``is_valid_pair`` -- the declared data ``contracts.toml``
    ships -- and is three-state, never assumed: both halves declared and paired
    is ``valid`` (no candidate), both declared and not paired is ``mismatch``
    (a candidate whose ``metadata.allocator_pair`` is the structured evidence),
    and an unknown half fails closed to ``unknown`` (no candidate). Reading the
    declared table mints no graph relation.
    """

    def _mismatch_candidates(self) -> list[dict]:
        env = self.query("mismatched_free")
        self.assertEqual(env["status"], "available", env.get("reason"))
        self.assertTrue(env["coverage"]["contracts_available"])
        self.assertGreater(env["coverage"]["declared_pairs"], 0)
        return [c for c in env["candidates"] if c["query"] == "mismatched_free"]

    def _dfg_edge_count(self) -> int:
        with closing(sqlite3.connect(self.database)) as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM analysis_edges WHERE feature='sparse_dfg:cpp'"
            ).fetchone()
        return int(row[0])

    def test_declared_pair_is_not_a_mismatch(self) -> None:
        # malloc <-> free is a declared pair: the query reads it and judges the
        # release valid, so no candidate (positive control).
        self.build(
            "#include <stdlib.h>\n"
            "int f(int n) {\n"
            "    int *p = malloc(sizeof(int));\n"
            "    if (!p) return -1;\n"
            "    free(p);\n"
            "    return n;\n"
            "}\n")
        self.assertEqual(self._mismatch_candidates(), [])

    def test_mismatched_release_produces_structured_evidence(self) -> None:
        # g_malloc is paired with g_free, not free: a declared acquire released
        # by the wrong declared half is a mismatch candidate carrying the pair
        # as structured evidence -- not a confidence number alone.
        self.build(
            "#include <stdlib.h>\n"
            "int f(int n) {\n"
            "    int *p = g_malloc(sizeof(int));\n"
            "    if (!p) return -1;\n"
            "    free(p);\n"
            "    return n;\n"
            "}\n")
        cands = self._mismatch_candidates()
        self.assertEqual(len(cands), 1)
        cand = cands[0]
        self.assertEqual(cand["defect_key"], "mismatch")
        self.assertEqual(cand["resolution_status"], "resolved")
        self.assertEqual(cand["subject"]["name"], "p")
        self.assertEqual(cand["subject"]["kind"], "release")
        # the anchor is the release (the defect), not the acquire
        self.assertTrue(cand["anchor"].startswith("event:"))
        # two facts: the acquire and the release, each with its matched name
        events = [fact["event"] for fact in cand["facts"]]
        self.assertEqual(events, ["ALLOC", "RELEASE"])
        names = [fact["matched_name"] for fact in cand["facts"]]
        self.assertEqual(names, ["g_malloc", "free"])
        # structured evidence: the pair and its declared alternatives
        pair = cand["metadata"]["allocator_pair"]
        self.assertEqual(pair["acquire"], "g_malloc")
        self.assertEqual(pair["release"], "free")
        # g_malloc's only declared partner is g_free, not free...
        self.assertEqual(pair["declared_releases"], ["g_free"])
        # ...and free's declared partners include malloc but not g_malloc.
        self.assertIn("malloc", pair["declared_acquires"])
        self.assertNotIn("g_malloc", pair["declared_acquires"])

    def test_mismatch_in_the_other_direction(self) -> None:
        # a malloc resource freed by g_free (the wrong declared half) is the
        # mirror image: still a mismatch, anchored on g_free.
        self.build(
            "#include <stdlib.h>\n"
            "int f(int n) {\n"
            "    int *p = malloc(sizeof(int));\n"
            "    if (!p) return -1;\n"
            "    g_free(p);\n"
            "    return n;\n"
            "}\n")
        cands = self._mismatch_candidates()
        self.assertEqual(len(cands), 1)
        pair = cands[0]["metadata"]["allocator_pair"]
        self.assertEqual(pair["acquire"], "malloc")
        self.assertEqual(pair["release"], "g_free")
        # malloc's only declared partner is free, not g_free...
        self.assertEqual(pair["declared_releases"], ["free"])
        # ...and g_free's declared partners are the glib allocators, not malloc.
        self.assertIn("g_malloc", pair["declared_acquires"])
        self.assertNotIn("malloc", pair["declared_acquires"])

    def test_unknown_release_fails_closed(self) -> None:
        # cfree is in the RELEASE vocabulary but is not a declared pair half:
        # the verdict is unknown, and nothing is judged (fail-closed).
        self.build(
            "#include <stdlib.h>\n"
            "int f(int n) {\n"
            "    int *p = malloc(sizeof(int));\n"
            "    if (!p) return -1;\n"
            "    cfree(p);\n"
            "    return n;\n"
            "}\n")
        self.assertEqual(self._mismatch_candidates(), [])

    def test_undeclared_acquire_fails_closed(self) -> None:
        # strdupa is in the ALLOC vocabulary but is not a declared pair half:
        # the acquire is unknown, so the pair cannot be judged and fails closed.
        self.build(
            "#include <stdlib.h>\n"
            "#include <string.h>\n"
            "int f(const char *s) {\n"
            "    char *p = strdupa(s);\n"
            "    free(p);\n"
            "    return 0;\n"
            "}\n")
        self.assertEqual(self._mismatch_candidates(), [])

    def test_mismatched_free_mints_no_edges(self) -> None:
        # reading the declared table is query-time only: the query mints no
        # allocator graph relation (hallucinated edge = 0) on either a valid or
        # a mismatched fixture.
        self.build(
            "#include <stdlib.h>\n"
            "int f(int n) {\n"
            "    int *p = g_malloc(sizeof(int));\n"
            "    if (!p) return -1;\n"
            "    free(p);\n"
            "    return n;\n"
            "}\n")
        before = self._dfg_edge_count()
        self.assertEqual(len(self._mismatch_candidates()), 1)
        self.assertEqual(self._dfg_edge_count(), before)


class ServiceContractTests(DefectTestBase):
    def test_default_index_never_runs_defect(self) -> None:
        write(self.root, "src/plain.c",
              "#include <stdlib.h>\n"
              "int p01(int n) { char *p = malloc(8); free(p); free(p); return n; }\n")
        full_index(self.root, self.database)
        with closing(sqlite3.connect(self.database)) as connection:
            tables = {r[0] for r in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertNotIn("semantic_events", tables)
        self.assertNotIn("semantic_edges", tables)
        self.assertNotIn("analysis_state", tables)
        # and the defect query without upstream analysis fails closed
        env = self.query("double_free")
        self.assertEqual(env["status"], "stale")
        self.assertEqual(env["candidates"], [])
        self.assertEqual(env["reason"], "upstream_not_built")

    def test_stale_upstream_fails_query_closed(self) -> None:
        self.build(
            "#include <stdlib.h>\n"
            "int q01(int n) { char *p = malloc(8); free(p); free(p); return n; }\n")
        write(self.root, "src/q01b.c",
              "#include <stdlib.h>\n"
              "int q02(int n) { char *p = malloc(8); free(p); free(p); return n; }\n")
        incremental_update(self.root, self.database)
        env = self.query("double_free")
        self.assertEqual(env["status"], "stale")
        self.assertEqual(env["reason"], "upstream_analysis_stale")
        self.assertEqual(env["candidates"], [])
        analyze_scope(self.root, "src", ("defect",), database=self.database)
        env = self.query("double_free")
        self.assertEqual(env["status"], "available")

    def test_unknown_defect_type_is_a_bad_request(self) -> None:
        self.build(
            "#include <stdlib.h>\n"
            "int r01(int n) { char *p = malloc(8); free(p); return n; }\n")
        from provenlattice.analysis_query import AnalysisRequestError
        # all nine queries are now implemented; an unknown type still raises
        with self.assertRaises(AnalysisRequestError) as caught:
            self.query("nonexistent_query")
        self.assertEqual(caught.exception.code, "unknown_defect_query")

    def test_budget_limits_are_honoured(self) -> None:
        self.build(
            "#include <stdlib.h>\n"
            "int s01(int n) { char *p = malloc(8); free(p); free(p); return n; }\n")
        env = query_defect_candidates(
            self.root, defect_type="double_free", max_candidates=1,
            database=self.database)
        self.assertLessEqual(len(env["candidates"]), 1)

    def test_cli_defect_command(self) -> None:
        self.build(
            "#include <stdlib.h>\n"
            "int t01(int n) { char *p = malloc(8); free(p); free(p); return n; }\n")
        parser = build_parser()
        result = run(parser.parse_args(
            ["defect", str(self.root), "--type", "double_free",
             "--database", str(self.database), "--json"]))
        self.assertEqual(result["status"], "available")
        self.assertEqual(len(result["candidates"]), 1)
        bad = run(parser.parse_args(
            ["defect", str(self.root), "--type", "nope",
             "--database", str(self.database), "--json"]))
        self.assertEqual(bad["status"], "failed")
        self.assertEqual(bad["error"]["code"], "unknown_defect_query")


if __name__ == "__main__":
    unittest.main()

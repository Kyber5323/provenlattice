"""I1-C sparse DFG correctness tests against hand-derived expectations.

The model under test (PR stage-5B, ported faithfully): a WRITE or ALLOC
defines under a flow identity -- (subject_decl, subject_storage,
subject_member, subject, subject_site) -- a READ/DEREFERENCE/CHECK/RETURN
binds to the newest live definition of that identity in source order, a CALL
binds per argument root, and each binding mints one direct DATA_FLOW_TO edge
whose metadata keeps the intermediates. Registered approximations travel with
it and are asserted as behaviour, never hidden:

* access events are filtered by storage class -- STORAGE_LOCAL is excluded
  (a local does not outlive the frame; ALLOC/RELEASE chains for local
  resources still flow), parameters publish their WRITE side only;
* a READ and a RETURN over the same expression are two separate uses, each
  minting its own edge;
* access points are published-and-never-connected
  (``_EDGELESS_EVENT_TYPES``), so edges whose chain runs through them carry
  ``control_reachable=unknown`` -- the graph cannot answer, and the evidence
  contract (I1C-R1) refuses to call that a binary false;
* same-line def/use pairs order by (line, event_type, ordinal) and never
  chain (macro-compression boundary);
* CONSUMES covers only the allocation handed through its own acquiring
  expression (``sink(malloc(n))``); ``p = malloc(n); sink(p)`` belongs to
  DATA_FLOW_TO instead -- the root cause of the relation's near-zero corpus
  output, asserted as behaviour.
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
from provenlattice.sparsedfg import bind_parameters, bind_returns


def write(root: Path, relative: str, value: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value.encode("utf-8"))


class DfgTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "repo"
        self.database = Path(self.temp.name) / "graph.db"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def build(self, body: str, name: str = "src/t.c",
              features=("sparse_dfg",)) -> dict:
        write(self.root, name, body)
        full_index(self.root, self.database)
        analyze_scope(self.root, "src", features, database=self.database)
        return self.dfg(name)

    def dfg(self, relative: str = "src/t.c") -> dict:
        env = query_analysis(self.root, "sparse_dfg", file_path=relative,
                             database=self.database)
        self.assertEqual(env["status"], "available", env.get("reason"))
        self.assertEqual(env["coverage"].get("level"),
                         "level0+structural+access+dfg")
        self.assertEqual(env["coverage"].get("grade"), "local-only, diagnostic")
        return env

    def events_for(self, relative: str) -> list[dict]:
        env = query_analysis(self.root, "sparse_cfg", file_path=relative,
                             database=self.database)
        self.assertEqual(env["status"], "available", env.get("reason"))
        return env["events"]

    def write_event_at(self, relative: str, line: int) -> dict:
        matches = [e for e in self.events_for(relative)
                   if e["event_type"] == "WRITE" and e["start_line"] == line]
        self.assertEqual(len(matches), 1, (relative, line))
        return matches[0]

    def value_edges(self, relative: str = "src/t.c") -> list[dict]:
        return [e for e in self.dfg(relative)["edges"]
                if e["payload"]["flow_class"] == "value_flow"]


class GlobalFlowTests(DfgTestBase):
    def test_def_then_use_mints_one_edge_per_use(self) -> None:
        env = self.build(
            "int g14 = 0;\n"
            "int f14(int n) {\n"
            "    g14 = n;\n"       # line 3: WRITE (global)
            "    return g14;\n"    # line 4: READ + RETURN -- two uses
            "}\n")
        edges = self.value_edges()
        # two uses (the READ and the RETURN), each bound to the line-3 WRITE
        self.assertEqual(len(edges), 2)
        write3 = self.write_event_at("src/t.c", 3)
        self.assertEqual({e["src_id"] for e in edges}, {write3["event_id"]})
        for edge in edges:
            payload = edge["payload"]
            self.assertEqual(payload["flow_class"], "value_flow")
            # access points are published-and-never-connected
            # (_EDGELESS_EVENT_TYPES): the walker cannot attest a control
            # path between them -- the evidence contract reports unknown,
            # never a binary false (I1C-R1).
            self.assertEqual(payload["control_reachable"], "unknown")
            self.assertIn("edgeless access point",
                          payload["control_reachable_note"])
            self.assertEqual(payload["relative_path"], "src/t.c")
        # the RETURN's edge rides the intervening same-identity READ in via
        via_all = [v for e in edges for v in e["payload"]["via"]]
        self.assertEqual(len(via_all), 1)

    def test_use_before_def_mints_nothing(self) -> None:
        # linear source order: the use is processed before the def exists --
        # the honest answer is no edge, never a speculative forward link.
        env = self.build(
            "int g15 = 0;\n"
            "int f15(int n) {\n"
            "    int r = g15;\n"   # use first (own identity: local r)
            "    g15 = n;\n"       # def after the use
            "    return r;\n"
            "}\n")
        self.assertEqual(self.value_edges(), [])


class FieldFlowTests(DfgTestBase):
    def test_field_def_use_through_member_path(self) -> None:
        env = self.build(
            "struct pair16 { int a; int b; };\n"
            "struct pair16 G16;\n"
            "int f16(int n) {\n"
            "    G16.a = n;\n"       # line 4: WRITE (member path 'a')
            "    return G16.a;\n"    # line 5: READ + RETURN (two uses)
            "}\n")
        edges = self.value_edges()
        self.assertEqual(len(edges), 2)
        write4 = self.write_event_at("src/t.c", 4)
        self.assertEqual({e["src_id"] for e in edges}, {write4["event_id"]})
        # the member path is part of the identity: the annotation channel
        # proves the WRITE and the READ name the same subject member
        reads = [e for e in self.events_for("src/t.c")
                 if e["event_type"] == "READ" and e["start_line"] == 5]
        self.assertEqual(len(reads), 1)
        self.assertEqual(reads[0]["annotations"]["subject_member"], "a")

    def test_distinct_members_are_distinct_chains(self) -> None:
        env = self.build(
            "struct pair17 { int a; int b; };\n"
            "struct pair17 G17;\n"
            "int f17(int n) {\n"
            "    G17.a = n;\n"
            "    G17.b = n + 1;\n"
            "    return G17.a;\n"
            "}\n")
        edges = self.value_edges()
        # the b-write (line 5) is a different flow identity; the a-read and
        # the a-return at line 6 bind to the a-write (line 4) alone
        self.assertEqual(len(edges), 2)
        write4 = self.write_event_at("src/t.c", 4)
        self.assertEqual({e["src_id"] for e in edges}, {write4["event_id"]})
        write5 = self.write_event_at("src/t.c", 5)
        self.assertNotIn(write5["event_id"], {e["src_id"] for e in edges})


class LocalBoundaryTests(DfgTestBase):
    def test_plain_local_def_use_is_outside_the_access_model(self) -> None:
        # Registered boundary: STORAGE_LOCAL is excluded from
        # ACCESS_STORAGES -- a local does not outlive the frame, so no
        # READ/WRITE access events are emitted for it and no DATA_FLOW_TO
        # edge can mint. The RETURN's subject annotation still names the
        # local (the fact channel works; only the access channel is scoped).
        env = self.build(
            "int f18(int n) {\n"
            "    int x = n;\n"
            "    return x;\n"
            "}\n")
        self.assertEqual(self.value_edges(), [])
        writes = [e for e in self.events_for("src/t.c")
                  if e["event_type"] == "WRITE"]
        self.assertEqual(writes, [])
        returns = [e for e in self.events_for("src/t.c")
                   if e["event_type"] == "RETURN"]
        self.assertEqual(len(returns), 1)
        self.assertEqual(returns[0]["annotations"]["subject"], "x")

    def test_resource_shaped_local_flows_via_alloc_chain(self) -> None:
        # a local RESOURCE (the defect-relevant shape) flows through its
        # ALLOC definition: check/call uses bind to the ALLOC root, with
        # control-reachable paths attested by the connected CFG points.
        env = self.build(
            "#include <stdlib.h>\n"
            "#include <string.h>\n"
            "int f19(int n) {\n"
            "    char *p = malloc(16);\n"   # ALLOC def (local pointer)
            "    if (!p) return -1;\n"      # CHECK binds to the ALLOC
            "    memset(p, 0, 16);\n"       # CALL binds p as input_to_sink
            "    free(p);\n"
            "    return n;\n"
            "}\n")
        edges = env["edges"]
        classes = {e["payload"]["flow_class"] for e in edges}
        self.assertIn("alloc_to_use", classes)
        self.assertIn("input_to_sink", classes)
        for edge in edges:
            self.assertEqual(edge["payload"]["control_reachable"], "true")

    def test_parameter_write_side_publishes_through_writes(self) -> None:
        # parameters publish their WRITE side only, and only through-writes
        # (`*p = v`) -- a bare slot write rebinds the callee's own name and
        # is not published (the registered cross-method discipline).
        env = self.build(
            "void f20(int *p, int n) {\n"
            "    *p = n;\n"          # through-write: published (parameter)
            "}\n")
        writes = [e for e in self.events_for("src/t.c")
                  if e["event_type"] == "WRITE"]
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0]["annotations"]["subject_storage"], "parameter")


class ControlFlowApproximationTests(DfgTestBase):
    def test_if_else_arms_bind_to_last_source_order_def(self) -> None:
        # Registered approximation: the linear pass keeps only the newest
        # same-identity def, so the merged use binds to the else arm's write
        # alone even though the arms are exclusive. Asserted as behaviour.
        env = self.build(
            "int g21 = 0;\n"
            "int f21(int n) {\n"
            "    if (n) { g21 = 1; }\n"     # line 3: def (arm 1)
            "    else { g21 = 2; }\n"       # line 4: def (arm 2)
            "    return g21;\n"             # line 5: READ + RETURN use
            "}\n")
        edges = self.value_edges()
        # two uses (READ + RETURN), both bound to the newest def (line 4)
        self.assertEqual(len(edges), 2)
        write4 = self.write_event_at("src/t.c", 4)
        self.assertEqual({e["src_id"] for e in edges}, {write4["event_id"]})
        write3 = self.write_event_at("src/t.c", 3)
        self.assertNotIn(write3["event_id"], {e["src_id"] for e in edges})

    def test_one_sided_def_still_flows(self) -> None:
        env = self.build(
            "int g22 = 0;\n"
            "int f22(int n) {\n"
            "    if (n) { g22 = n; }\n"     # line 3: def (may-executed)
            "    return g22;\n"             # line 4: READ + RETURN
            "}\n")
        edges = self.value_edges()
        self.assertEqual(len(edges), 2)
        write3 = self.write_event_at("src/t.c", 3)
        self.assertEqual({e["src_id"] for e in edges}, {write3["event_id"]})

    def test_loop_body_def_flows_to_post_loop_use(self) -> None:
        env = self.build(
            "int g23 = 0;\n"
            "int f23(int n) {\n"
            "    int i;\n"
            "    for (i = 0; i < n; i += 1) { g23 = i; }\n"  # line 4: def
            "    return g23;\n"             # line 5: READ + RETURN
            "}\n")
        edges = self.value_edges()
        self.assertEqual(len(edges), 2)
        write4 = self.write_event_at("src/t.c", 4)
        self.assertEqual({e["src_id"] for e in edges}, {write4["event_id"]})

    def test_early_return_use_binds_to_prior_def(self) -> None:
        env = self.build(
            "int g24 = 0;\n"
            "int f24(int n) {\n"
            "    g24 = n;\n"                 # line 3: def
            "    if (n) { return g24; }\n"   # line 4: READ + RETURN use
            "    g24 = n + 1;\n"             # line 5: redefinition
            "    return g24;\n"              # line 6: READ + RETURN use
            "}\n")
        edges = self.value_edges()
        # line-4 uses bind to the line-3 def (before the redefinition);
        # line-6 uses bind to the line-5 def (after it)
        self.assertEqual(len(edges), 4)
        write3 = self.write_event_at("src/t.c", 3)
        write5 = self.write_event_at("src/t.c", 5)
        self.assertEqual({e["src_id"] for e in edges},
                         {write3["event_id"], write5["event_id"]})

    def test_switch_default_def_is_the_newest(self) -> None:
        env = self.build(
            "int g25 = 0;\n"
            "int f25(int n) {\n"
            "    switch (n) {\n"
            "    case 1:\n"
            "        g25 = 1;\n"             # line 5: def (arm 1)
            "        break;\n"
            "    default:\n"
            "        g25 = 2;\n"             # line 8: def (arm 2)
            "        break;\n"
            "    }\n"
            "    return g25;\n"              # line 11: READ + RETURN use
            "}\n")
        edges = self.value_edges()
        self.assertEqual(len(edges), 2)
        write8 = self.write_event_at("src/t.c", 8)
        self.assertEqual({e["src_id"] for e in edges}, {write8["event_id"]})

    def test_same_line_def_use_never_chains(self) -> None:
        # macro-compression boundary: everything on one physical line orders
        # by (line, event_type, ordinal), so the WRITE sorts after the uses
        # and no edge mints. Registered, asserted honestly.
        env = self.build(
            "int g26 = 0;\n"
            "int f26(int n) { g26 = n; return g26; }\n")
        self.assertEqual(self.value_edges(), [])


class IdentityTests(DfgTestBase):
    def test_same_name_in_two_functions_never_links(self) -> None:
        env = self.build(
            "int g26 = 0;\n"
            "int alpha(int n) {\n"
            "    g26 = n;\n"
            "    return g26;\n"
            "}\n"
            "int beta(int n) {\n"
            "    g26 = n + 1;\n"
            "    return g26;\n"
            "}\n")
        edges = self.value_edges()
        # two def-use pairs (READ + RETURN per method) -- edges are
        # intra-method by construction (both endpoints share the owner triple)
        self.assertEqual(len(edges), 4)
        for edge in edges:
            self.assertEqual(edge["payload"]["relative_path"], "src/t.c")

    def test_local_resource_chain_binds_dereference_to_alloc(self) -> None:
        env = self.build(
            "#include <stdlib.h>\n"
            "int f27(int n) {\n"
            "    int *p = malloc(sizeof(int));\n"
            "    if (!p) return -1;\n"
            "    *p = n;\n"            # WRITE through the pointer
            "    int v = *p;\n"        # DEREFERENCE reads it back
            "    free(p);\n"
            "    (void)v;\n"
            "    return n;\n"
            "}\n")
        classes = {e["payload"]["flow_class"]: e for e in env["edges"]}
        self.assertIn("alloc_to_use", classes)
        # every edge's control_reachable flag is an honest evidence answer
        # (true = path attested; unknown = edgeless endpoint or partial parse)
        for edge in env["edges"]:
            self.assertIn(edge["payload"]["control_reachable"],
                          ("true", "unknown"))

    def test_copied_pointer_release_pairs_with_deref_through_copy(self) -> None:
        # the shape the README registered as an alias-precision limitation:
        # q = p copies the pointer, the release happens under p, and the
        # dereference reads through q. The release of the root value must
        # pair with the use (free_to_use) even though the names differ --
        # the copy itself is the chain (alias_parent), the release rides
        # the root identity.
        env = self.build(
            "#include <stdlib.h>\n"
            "int f40(int n) {\n"
            "    int *p = malloc(sizeof(int));\n"
            "    int *q = p;\n"        # bare copy: alias_source = p
            "    free(p);\n"           # release under the copied-from name
            "    int v = *q;\n"        # dereference through the copy
            "    (void)v;\n"
            "    return n;\n"
            "}\n")
        deref = [e for e in self.events_for("src/t.c")
                 if e["event_type"] == "DEREFERENCE" and e["start_line"] == 6]
        release = [e for e in self.events_for("src/t.c")
                   if e["event_type"] == "RELEASE" and e["start_line"] == 5]
        self.assertEqual(len(deref), 1)
        self.assertEqual(len(release), 1)
        paired = [e for e in env["edges"]
                  if e["payload"]["flow_class"] == "free_to_use"
                  and e["src_id"] == release[0]["event_id"]
                  and e["dst_id"] == deref[0]["event_id"]]
        self.assertEqual(len(paired), 1, env["edges"])
        # the copy site is not claimed as a via hop of the release edge:
        # the copy happened before the release, not between def and use.
        self.assertEqual(paired[0]["payload"]["via"], [])

    def test_copied_pointer_release_never_crosses_independent_names(self) -> None:
        # the negative: releases travel with the value's root identity only.
        # an independent allocation's use must not pair with another
        # pointer's release (hallucinated edge = 0).
        env = self.build(
            "#include <stdlib.h>\n"
            "int f41(int n) {\n"
            "    int *a = malloc(sizeof(int));\n"
            "    int *b = malloc(sizeof(int));\n"
            "    free(a);\n"
            "    int v = *b;\n"        # independent chain: must stay alloc_to_use
            "    (void)v;\n"
            "    return n;\n"
            "}\n")
        self.assertEqual([e for e in env["edges"]
                          if e["payload"]["flow_class"] == "free_to_use"], [])
        deref = [e for e in self.events_for("src/t.c")
                 if e["event_type"] == "DEREFERENCE" and e["start_line"] == 6]
        self.assertEqual(len(deref), 1)
        paired = [e for e in env["edges"]
                  if e["dst_id"] == deref[0]["event_id"]
                  and e["payload"]["flow_class"] == "alloc_to_use"]
        self.assertEqual(len(paired), 1)

    def test_unknown_alias_does_not_manufacture_certainty(self) -> None:
        # two pointers, no alias facts: each chain stays name-local and no
        # edge claims a cross-pointer flow the model cannot prove.
        env = self.build(
            "void f28(int *a, int *b, int n) {\n"
            "    *a = n;\n"
            "    *b = n + 1;\n"
            "}\n")
        for edge in env["edges"]:
            self.assertNotIn("alias", edge["payload"].get("annotations", {}))
        # the writes exist (parameter through-writes) but never link
        writes = [e for e in self.events_for("src/t.c")
                  if e["event_type"] == "WRITE"]
        self.assertEqual(len(writes), 2)

    def test_shadowed_global_chain_stays_its_own(self) -> None:
        # the inner g29 is a local (no access events); the global's chain
        # stays its own -- shadowing cannot merge what the identities separate.
        env = self.build(
            "int g29 = 0;\n"
            "int f29(int n) {\n"
            "    g29 = n;\n"
            "    if (n) { int g29 = n + 1; (void)g29; }\n"
            "    return g29;\n"
            "}\n")
        edges = self.value_edges()
        self.assertEqual(len(edges), 2)
        write3 = self.write_event_at("src/t.c", 3)
        self.assertEqual({e["src_id"] for e in edges}, {write3["event_id"]})


class AssignmentCopyTests(DfgTestBase):
    """Assignment-form pointer copies (issue-pilot#17).

    The declaration index's pointer fact (`is_pointer`, read off the declarator
    chain) lets `q = p;` on a pointer-typed local publish the bare copy the
    alias chain needs, where a value copy (`y = x`) stays excluded."""

    def _free_to_use(self, relative="src/t.c"):
        return [e for e in self.dfg(relative)["edges"]
                if e["payload"]["flow_class"] == "free_to_use"]

    def test_assignment_pointer_copy_pairs_release_with_use(self) -> None:
        # A. q declared pointer, then q = p: the assignment copy carries the
        # alias fact, and the release of the root value pairs with the deref
        # through the copy.
        env = self.build(
            "#include <stdlib.h>\n"
            "int f50(int n) {\n"
            "    int *p = malloc(sizeof(int));\n"
            "    int *q;\n"
            "    q = p;\n"
            "    free(p);\n"
            "    int v = *q;\n"
            "    (void)v;\n"
            "    return n;\n"
            "}\n")
        deref = [e for e in self.events_for("src/t.c")
                 if e["event_type"] == "DEREFERENCE" and e["start_line"] == 7]
        release = [e for e in self.events_for("src/t.c")
                   if e["event_type"] == "RELEASE" and e["start_line"] == 6]
        self.assertEqual(len(deref), 1)
        self.assertEqual(len(release), 1)
        paired = [e for e in self._free_to_use()
                  if e["src_id"] == release[0]["event_id"]
                  and e["dst_id"] == deref[0]["event_id"]]
        self.assertEqual(len(paired), 1, env["edges"])

    def test_value_copy_never_becomes_alias(self) -> None:
        # B. a non-pointer value copy publishes no write and mints no edge.
        env = self.build(
            "int f51(int n) {\n"
            "    int x = 1;\n"
            "    int y;\n"
            "    y = x;\n"
            "    return y;\n"
            "}\n")
        self.assertEqual(self._free_to_use(), [])
        writes = [e for e in self.events_for("src/t.c")
                  if e["event_type"] == "WRITE" and e["start_line"] == 5]
        self.assertEqual(writes, [])

    def test_write_through_after_declaration_stays_alloc_chain(self) -> None:
        # C. declaration + write-through (*p = n) still mints alloc_to_use,
        # no false alias.
        env = self.build(
            "#include <stdlib.h>\n"
            "int f52(int n) {\n"
            "    int *p = malloc(sizeof(int));\n"
            "    *p = n;\n"
            "    return n;\n"
            "}\n")
        self.assertEqual(self._free_to_use(), [])
        self.assertTrue(any(e["payload"]["flow_class"] == "alloc_to_use"
                            for e in env["edges"]))

    def test_shadowed_name_resolves_to_pointer_in_scope(self) -> None:
        # D. an inner int p shadows the outer pointer; the later q = p must
        # resolve to the outer pointer (position-sensitive index).
        env = self.build(
            "#include <stdlib.h>\n"
            "int f53(int n) {\n"
            "    int *p = malloc(sizeof(int));\n"
            "    { int p = 0; (void)p; }\n"
            "    int *q;\n"
            "    q = p;\n"
            "    free(p);\n"
            "    int v = *q;\n"
            "    (void)v;\n"
            "    return n;\n"
            "}\n")
        deref = [e for e in self.events_for("src/t.c")
                 if e["event_type"] == "DEREFERENCE" and e["start_line"] == 8]
        release = [e for e in self.events_for("src/t.c")
                   if e["event_type"] == "RELEASE" and e["start_line"] == 7]
        self.assertEqual(len(deref), 1)
        self.assertEqual(len(release), 1)
        paired = [e for e in self._free_to_use()
                  if e["src_id"] == release[0]["event_id"]
                  and e["dst_id"] == deref[0]["event_id"]]
        self.assertEqual(len(paired), 1, env["edges"])

    def test_reassignment_follows_new_value_not_stale(self) -> None:
        # E1. q = p then q = o: the use follows o's chain; p's release never
        # pairs with it.
        env = self.build(
            "#include <stdlib.h>\n"
            "int f54(int n) {\n"
            "    int *p = malloc(sizeof(int));\n"
            "    int *o = malloc(sizeof(int));\n"
            "    int *q;\n"
            "    q = p;\n"
            "    q = o;\n"
            "    free(p);\n"
            "    int v = *q;\n"
            "    (void)v;\n"
            "    return n;\n"
            "}\n")
        self.assertEqual(self._free_to_use(), [])
        deref = [e for e in self.events_for("src/t.c")
                 if e["event_type"] == "DEREFERENCE" and e["start_line"] == 9]
        alloc_o = [e for e in self.events_for("src/t.c")
                   if e["event_type"] == "ALLOC" and e["start_line"] == 4]
        self.assertEqual(len(deref), 1)
        self.assertEqual(len(alloc_o), 1)
        paired = [e for e in env["edges"]
                  if e["payload"]["flow_class"] == "alloc_to_use"
                  and e["src_id"] == alloc_o[0]["event_id"]
                  and e["dst_id"] == deref[0]["event_id"]]
        self.assertEqual(len(paired), 1, env["edges"])

    def test_realloc_after_free_never_inherits_stale_release(self) -> None:
        # E2. free(p); p = malloc(...); the new allocation is a new root id,
        # so the deref follows the new alloc and the stale release never pairs.
        env = self.build(
            "#include <stdlib.h>\n"
            "int f55(int n) {\n"
            "    int *p = malloc(sizeof(int));\n"
            "    free(p);\n"
            "    p = malloc(sizeof(int));\n"
            "    int v = *p;\n"
            "    (void)v;\n"
            "    return n;\n"
            "}\n")
        self.assertEqual(self._free_to_use(), [])
        deref = [e for e in self.events_for("src/t.c")
                 if e["event_type"] == "DEREFERENCE" and e["start_line"] == 6]
        alloc2 = [e for e in self.events_for("src/t.c")
                  if e["event_type"] == "ALLOC" and e["start_line"] == 5]
        self.assertEqual(len(deref), 1)
        self.assertEqual(len(alloc2), 1)
        paired = [e for e in env["edges"]
                  if e["payload"]["flow_class"] == "alloc_to_use"
                  and e["src_id"] == alloc2[0]["event_id"]
                  and e["dst_id"] == deref[0]["event_id"]]
        self.assertEqual(len(paired), 1, env["edges"])


class ConsumesTests(DfgTestBase):
    def test_direct_pass_through_mints_consumes(self) -> None:
        # the shape CONSUMES exists for: the allocation is the whole argument
        # of its own acquiring expression, so its subject root (the size
        # parameter) is also the call's argument root.
        env = self.build(
            "#include <stdlib.h>\n"
            "void sink01(char *p);\n"
            "void f30(int n) {\n"
            "    sink01(malloc((size_t)n));\n"
            "}\n")
        consumes = [e for e in env["edges"] if e["kind"] == "CONSUMES"]
        self.assertEqual(len(consumes), 1)
        payload = consumes[0]["payload"]
        self.assertEqual(payload["flow_class"], "input_to_sink")
        self.assertEqual(payload["control_reachable"], "true")
        self.assertEqual(payload["arg_root"], "n")

    def test_assigned_pass_through_joins_as_data_flow_to(self) -> None:
        # `r = sink(malloc(n))`: the allocation's subject root becomes the
        # assigned local `r`, which is not among the call's argument roots --
        # the positional join cannot fire. The value still flows as
        # DATA_FLOW_TO (alloc_to_use on the call). Registered join
        # narrowness, asserted as behaviour rather than silently widened.
        env = self.build(
            "#include <stdlib.h>\n"
            "void sink01(char *p);\n"
            "int f31(int n) {\n"
            "    int r = sink01(malloc((size_t)n));\n"
            "    return r;\n"
            "}\n")
        consumes = [e for e in env["edges"] if e["kind"] == "CONSUMES"]
        self.assertEqual(consumes, [])
        classes = {e["payload"]["flow_class"] for e in env["edges"]}
        self.assertIn("alloc_to_use", classes)

    def test_local_handoff_is_data_flow_to_not_consumes(self) -> None:
        # `p = malloc(n); sink(p)` -- the near-universal pattern: the
        # allocation hands to a local, so `passed_to` never fires. This is
        # the CONSUMES low-output root cause asserted as behaviour: the
        # relation covers only direct pass-through shapes.
        env = self.build(
            "#include <stdlib.h>\n"
            "void sink02(char *p);\n"
            "int f32(int n) {\n"
            "    char *p = malloc((size_t)n);\n"
            "    sink02(p);\n"
            "    return 0;\n"
            "}\n")
        consumes = [e for e in env["edges"] if e["kind"] == "CONSUMES"]
        self.assertEqual(consumes, [])
        sinks = [e for e in env["edges"]
                 if e["payload"]["flow_class"] == "input_to_sink"]
        self.assertEqual(len(sinks), 1)


class DeterminismTests(DfgTestBase):
    def test_repeat_builds_are_deterministic(self) -> None:
        write(self.root, "src/det.c",
              "#include <stdlib.h>\n"
              "int d01(int n) { char *p = malloc(8); free(p); return n; }\n")
        full_index(self.root, self.database)
        analyze_scope(self.root, "src", ("sparse_dfg",), database=self.database)
        first = self.dfg("src/det.c")
        result = analyze_scope(self.root, "src", ("sparse_dfg",), database=self.database)
        self.assertEqual(result["features"]["sparse_dfg"]["reused"], ["src/det.c"])
        second = self.dfg("src/det.c")
        self.assertEqual([e["analysis_edge_id"] for e in first["edges"]],
                         [e["analysis_edge_id"] for e in second["edges"]])

    def test_malformed_source_degrades(self) -> None:
        self.build(
            "#include <stdlib.h>\n"
            "int b01(int n) { char *p = malloc(8) return n; }\n")
        with closing(sqlite3.connect(self.database)) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT s.metadata FROM analysis_state s JOIN files f "
                "ON f.file_id=s.file_id WHERE f.path='src/t.c' "
                "AND s.feature='sparse_dfg:cpp'").fetchone()
        metadata = json.loads(row["metadata"])
        self.assertEqual(metadata["analysis_status"], "degraded")


class FreshnessTests(DfgTestBase):
    FRESH = (
        "#include <stdlib.h>\n"
        "int g30 = 0;\n"
        "int f30(int n) {\n"
        "    g30 = n;\n"
        "    return g30;\n"
        "}\n"
    )

    def setUp(self) -> None:
        super().setUp()
        write(self.root, "src/fresh.c", self.FRESH)
        full_index(self.root, self.database)
        analyze_scope(self.root, "src", ("sparse_dfg",), database=self.database)

    def _tamper_dependency(self, contract: str) -> None:
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                "UPDATE analysis_state SET metadata=json_set(metadata, "
                "'$.dependencies', json(?)) WHERE feature='sparse_dfg:cpp'",
                (json.dumps({"events:cpp": contract,
                             "sparse_cfg:cpp": contract}),))
            connection.commit()

    def test_events_dependency_change_reads_stale_and_rebuilds(self) -> None:
        self._tamper_dependency("cpp-events-v0")
        # direct query path: stale, zero leak (the I1-B lesson)
        env = query_analysis(self.root, "sparse_dfg", file_path="src/fresh.c",
                             database=self.database)
        self.assertEqual(env["status"], "stale")
        self.assertIn("dependency_changed", env["reason"])
        self.assertEqual(env["edges"], [])
        result = analyze_scope(self.root, "src", ("sparse_dfg",),
                               database=self.database)
        self.assertEqual(result["features"]["sparse_dfg"]["materialized"], ["src/fresh.c"])
        self.assertEqual(self.dfg("src/fresh.c")["status"], "available")

    def test_sparse_cfg_dependency_change_reads_stale_and_rebuilds(self) -> None:
        self._tamper_dependency("cpp-sparse_cfg-v0")
        env = query_analysis(self.root, "sparse_dfg", file_path="src/fresh.c",
                             database=self.database)
        self.assertEqual(env["status"], "stale")
        result = analyze_scope(self.root, "src", ("sparse_dfg",),
                               database=self.database)
        self.assertEqual(result["features"]["sparse_dfg"]["materialized"], ["src/fresh.c"])

    def test_source_change_invalidates_until_rebuild(self) -> None:
        write(self.root, "src/fresh.c",
              "#include <stdlib.h>\n"
              "int g30 = 0;\n"
              "int f30(int n) {\n"
              "    g30 = n;\n"
              "    g30 = g30 + 1;\n"     # a second def/use chain
              "    return g30;\n"
              "}\n")
        incremental_update(self.root, self.database)
        env = query_analysis(self.root, "sparse_dfg", file_path="src/fresh.c",
                             database=self.database)
        self.assertEqual(env["status"], "stale")
        analyze_scope(self.root, "src", ("sparse_dfg",), database=self.database)
        # three uses: the line-5 RHS read binds to the line-4 def; the line-6
        # READ and RETURN bind to the line-5 def (the newest)
        edges = self.value_edges("src/fresh.c")
        self.assertEqual(len(edges), 3)

    def test_cleared_tables_rebuild_deterministically(self) -> None:
        before = self.dfg("src/fresh.c")
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("DELETE FROM analysis_edges WHERE feature='sparse_dfg:cpp'")
            connection.execute("DELETE FROM analysis_state WHERE feature='sparse_dfg:cpp'")
            connection.commit()
        analyze_scope(self.root, "src", ("sparse_dfg",), database=self.database)
        after = self.dfg("src/fresh.c")
        self.assertEqual([e["analysis_edge_id"] for e in before["edges"]],
                         [e["analysis_edge_id"] for e in after["edges"]])


class ReachabilityEvidenceContractTests(DfgTestBase):
    """I1C-R1: control_reachable is a three-state evidence answer.

    true    = a control path exists in the observed graph (attestable)
    false   = both endpoints connected, BFS ran, no path found
    unknown = an endpoint is an edgeless access point, or the parse was
              partial -- the graph cannot answer, and the value must not be
              read as "deterministically unreachable".
    """

    def setUp(self) -> None:
        super().setUp()
        write(self.root, "src/contract.c",
              "#include <stdlib.h>\n"
              "#include <string.h>\n"
              "int g40 = 0;\n"
              "int f40(int n) {\n"
              "    char *p = malloc(16);\n"     # ALLOC (connected def)
              "    if (!p) return -1;\n"         # CHECK (connected use)
              "    memset(p, 0, 16);\n"          # CALL: input_to_sink, connected
              "    g40 = n;\n"                   # WRITE (edgeless)
              "    return g40 + n;\n"            # READ (edgeless use)
              "}\n")
        full_index(self.root, self.database)
        analyze_scope(self.root, "src", ("sparse_dfg",), database=self.database)

    def test_connected_chains_attest_true(self) -> None:
        env = self.dfg("src/contract.c")
        connected = [e for e in env["edges"]
                     if e["payload"]["flow_class"] in
                     ("alloc_to_use", "input_to_sink")]
        self.assertTrue(connected)
        for edge in connected:
            self.assertEqual(edge["payload"]["control_reachable"], "true")

    def test_edgeless_endpoint_chains_report_unknown(self) -> None:
        env = self.dfg("src/contract.c")
        value_edges = [e for e in env["edges"]
                       if e["payload"]["flow_class"] == "value_flow"]
        self.assertTrue(value_edges)
        for edge in value_edges:
            self.assertEqual(edge["payload"]["control_reachable"], "unknown")
            self.assertIn("edgeless access point",
                          edge["payload"]["control_reachable_note"])

    def test_three_states_cover_every_edge(self) -> None:
        env = self.dfg("src/contract.c")
        for edge in env["edges"]:
            self.assertIn(edge["payload"]["control_reachable"],
                          ("true", "false", "unknown"))

    def test_version_migration_rebuilds_v1_caches(self) -> None:
        # the v1 caches carried the binary field; the bump must invalidate
        # them and the rebuild must carry the three-state contract
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                "UPDATE analysis_state SET feature_version=1 "
                "WHERE feature='sparse_dfg:cpp'")
            connection.commit()
        env = query_analysis(self.root, "sparse_dfg", file_path="src/contract.c",
                             database=self.database)
        self.assertEqual(env["status"], "stale")
        analyze_scope(self.root, "src", ("sparse_dfg",), database=self.database)
        with closing(sqlite3.connect(self.database)) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT feature_version, metadata FROM analysis_state s "
                "JOIN files f ON f.file_id=s.file_id WHERE f.path='src/contract.c' "
                "AND s.feature='sparse_dfg:cpp'").fetchone()
        self.assertEqual(row["feature_version"], 2)
        metadata = json.loads(row["metadata"])
        self.assertEqual(metadata["contract"], "cpp-sparse_dfg-v2")

    def _dfg_metadata(self) -> dict:
        with closing(sqlite3.connect(self.database)) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT s.metadata FROM analysis_state s JOIN files f "
                "ON f.file_id=s.file_id WHERE f.path='src/contract.c' "
                "AND s.feature='sparse_dfg:cpp'").fetchone()
        self.assertIsNotNone(row)
        return json.loads(row["metadata"])


class _FakeCallees:
    """A stand-in for the contract layer's ``ContractTable``.

    ``callee(info)`` answers the name a call site resolves to, exactly the way
    ``ContractTable.callee`` does: joined on ``(file_id, owner_symbol_id,
    start_line)``, with ``""`` for every call the join cannot resolve -- which
    is how ambiguous / indirect / unresolved calls stay out of the composition.
    """

    def __init__(self, names: dict[tuple[str, str, int], str]) -> None:
        self._names = names

    def callee(self, info) -> str:
        return self._names.get(
            (
                str(info.get("file_id", "")),
                str(info.get("owner_symbol_id", "")),
                int(info.get("start_line", 0) or 0),
            ),
            "",
        )


def _make_point(point_id, event_type, owner, *, subject="", subject_from="",
                subject_decl="", subject_storage="", subject_member="",
                subject_type="", subject_site="", arguments="", file_id="",
                start_line=0, matched_name="") -> dict:
    """A materialised point carrying every fact the composition helpers read."""
    return {
        "event_type": event_type,
        "subject": subject,
        "subject_from": subject_from,
        "subject_decl": subject_decl,
        "subject_storage": subject_storage,
        "subject_member": subject_member,
        "subject_type": subject_type,
        "subject_site": subject_site,
        "arguments": arguments,
        "matched_name": matched_name,
        "owner_symbol_id": owner,
        "file_id": file_id,
        "start_line": start_line,
    }


class ParameterBindingCompositionTests(unittest.TestCase):
    """The completed query-time parameter join (S4 interprocedural DFG)."""

    CALLEE = "sym:callee"
    CALLER = "sym:caller"

    def test_bind_parameters_composes_direct_call_argument(self) -> None:
        points = {
            "param_write": _make_point(
                "param_write", "WRITE", self.CALLEE, subject="p",
                subject_decl="parameter:p", subject_storage="parameter",
                subject_member="", matched_name="*p"),
            "caller_g_write": _make_point(
                "caller_g_write", "WRITE", self.CALLER, subject="g",
                subject_decl="global:g", subject_storage="global",
                subject_member=""),
            "caller_call": _make_point(
                "caller_call", "CALL", self.CALLER, subject="g",
                subject_from="argument", arguments="g", file_id="f1",
                start_line=5),
        }
        callees = _FakeCallees({("f1", self.CALLER, 5): "callee"})
        names = {self.CALLEE: "callee", self.CALLER: "caller"}
        table = bind_parameters(points, callees=callees, names=names,
                                signatures={self.CALLEE: "(int *p)"})
        self.assertIn("param_write", table.bindings)
        records = table.bindings["param_write"]
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record["callee"], "callee")
        self.assertEqual(record["ordinal"], 0)
        self.assertEqual(record["parameter"], "p")
        self.assertEqual(record["arg_root"], "g")
        self.assertEqual(record["caller_owner"], self.CALLER)
        self.assertEqual(record["call_site"], "caller_call")
        self.assertEqual(record["join"], "declared")
        self.assertEqual(record["caller_facts"]["subject_decl"], "global:g")

    def test_bind_parameters_unresolved_callee_stays_unbound(self) -> None:
        points = {
            "caller_call": _make_point(
                "caller_call", "CALL", self.CALLER, subject="g", arguments="g",
                file_id="f1", start_line=5),
        }
        callees = _FakeCallees({})  # the call resolves to nothing
        table = bind_parameters(points, callees=callees, names={},
                                signatures={})
        self.assertEqual(table.bindings, {})
        self.assertEqual(table.coverage["calls_without_callee"], 1)

    def test_bind_parameters_disagreed_signature_stays_unbound(self) -> None:
        # two symbols spell the same short name with different parameter lists:
        # no agreed signature, so no argument is composed.
        points = {
            "caller_call": _make_point(
                "caller_call", "CALL", self.CALLER, subject="g", arguments="g",
                file_id="f1", start_line=5),
        }
        callees = _FakeCallees({("f1", self.CALLER, 5): "callee"})
        table = bind_parameters(
            points, callees=callees,
            names={"sym:a": "callee", "sym:b": "callee"},
            signatures={"sym:a": "(int *p)", "sym:b": "(int *p, int *q)"},
        )
        self.assertEqual(table.bindings, {})
        self.assertEqual(table.coverage["callees_without_agreed_signature"], 1)

    def test_bind_parameters_bare_slot_write_never_composes(self) -> None:
        # a bare parameter slot write (`p = v`) rebinds the callee's own name
        # (by-value), so it never reaches the caller -- registered discipline.
        points = {
            "param_slot_write": _make_point(
                "param_slot_write", "WRITE", self.CALLEE, subject="p",
                subject_decl="parameter:p", subject_storage="parameter",
                subject_member="", matched_name="p"),
            "caller_call": _make_point(
                "caller_call", "CALL", self.CALLER, subject="g",
                subject_from="argument", arguments="g", file_id="f1",
                start_line=5),
            "caller_g_write": _make_point(
                "caller_g_write", "WRITE", self.CALLER, subject="g",
                subject_decl="global:g", subject_storage="global",
                subject_member=""),
        }
        callees = _FakeCallees({("f1", self.CALLER, 5): "callee"})
        names = {self.CALLEE: "callee", self.CALLER: "caller"}
        table = bind_parameters(points, callees=callees, names=names,
                                signatures={self.CALLEE: "(int *p)"})
        self.assertEqual(table.bindings, {})
        self.assertEqual(table.coverage["parameter_slot_writes_skipped"], 1)


class ReturnBindingCompositionTests(unittest.TestCase):
    """The query-time RETURNS identity composition (S4 interprocedural DFG).

    RETURNS is an identity fact, never a graph edge: these functions return a
    composition table over materialised points, and no row of it becomes a
    relation.
    """

    CALLEE = "sym:callee"
    CALLER = "sym:caller"

    def test_bind_returns_composes_direct_call_return(self) -> None:
        points = {
            "callee_return": _make_point(
                "callee_return", "RETURN", self.CALLEE, subject="buf",
                subject_from="return", subject_decl="global:buf",
                subject_storage="global", subject_member=""),
            "caller_call": _make_point(
                "caller_call", "CALL", self.CALLER, subject="q",
                subject_from="assignment", file_id="f1", start_line=7),
        }
        callees = _FakeCallees({("f1", self.CALLER, 7): "callee"})
        names = {self.CALLEE: "callee", self.CALLER: "caller"}
        table = bind_returns(points, callees=callees, names=names)
        self.assertIn("caller_call", table.returns)
        records = table.returns["caller_call"]
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record["callee"], "callee")
        self.assertEqual(record["callee_symbol_id"], self.CALLEE)
        self.assertEqual(record["receive_subject"], "q")
        self.assertEqual(record["caller_owner"], self.CALLER)
        self.assertEqual(record["return_point"], "callee_return")
        self.assertEqual(record["subject_decl"], "global:buf")
        self.assertEqual(record["subject"], "buf")

    def test_bind_returns_unresolved_callee_stays_unbound(self) -> None:
        points = {
            "caller_call": _make_point(
                "caller_call", "CALL", self.CALLER, subject="q",
                subject_from="assignment", file_id="f1", start_line=7),
        }
        callees = _FakeCallees({})  # unresolved/indirect/ambiguous -> ""
        table = bind_returns(points, callees=callees,
                             names={self.CALLEE: "callee"})
        self.assertEqual(table.returns, {})
        self.assertEqual(table.coverage["calls_without_callee"], 1)

    def test_bind_returns_ambiguous_symbol_stays_unbound(self) -> None:
        # the callee name maps to two indexed symbols: never pick a winner.
        points = {
            "caller_call": _make_point(
                "caller_call", "CALL", self.CALLER, subject="q",
                subject_from="assignment", file_id="f1", start_line=7),
        }
        callees = _FakeCallees({("f1", self.CALLER, 7): "callee"})
        names = {"sym:a": "callee", "sym:b": "callee"}
        table = bind_returns(points, callees=callees, names=names)
        self.assertEqual(table.returns, {})
        self.assertEqual(table.coverage["callees_ambiguous"], 1)

    def test_bind_returns_discarded_result_stays_unbound(self) -> None:
        # a bare `f();` (subject not an assignment target) is not a receive
        # point: the returned identity cannot be composed to anything.
        points = {
            "callee_return": _make_point(
                "callee_return", "RETURN", self.CALLEE, subject="buf",
                subject_from="return", subject_decl="global:buf",
                subject_storage="global", subject_member=""),
            "caller_call": _make_point(
                "caller_call", "CALL", self.CALLER, subject="buf",
                subject_from="argument", arguments="buf", file_id="f1",
                start_line=7),
        }
        callees = _FakeCallees({("f1", self.CALLER, 7): "callee"})
        names = {self.CALLEE: "callee", self.CALLER: "caller"}
        table = bind_returns(points, callees=callees, names=names)
        self.assertEqual(table.returns, {})
        self.assertEqual(table.coverage["results_discarded"], 1)

    def test_bind_returns_no_return_identity_stays_unbound(self) -> None:
        # a callee with no RETURN identity (`return -1;`, or a void function)
        # publishes nothing to compose.
        points = {
            "caller_call": _make_point(
                "caller_call", "CALL", self.CALLER, subject="q",
                subject_from="assignment", file_id="f1", start_line=7),
        }
        callees = _FakeCallees({("f1", self.CALLER, 7): "callee"})
        names = {self.CALLEE: "callee", self.CALLER: "caller"}
        table = bind_returns(points, callees=callees, names=names)
        self.assertEqual(table.returns, {})
        self.assertEqual(table.coverage["callees_without_return_identity"], 1)

    def test_bind_returns_many_return_points_compose_all(self) -> None:
        # two `return` statements are two RETURN identity facts, and one call
        # site composes each.
        points = {
            "ret_a": _make_point(
                "ret_a", "RETURN", self.CALLEE, subject="a",
                subject_from="return", subject_decl="global:a",
                subject_storage="global", subject_member=""),
            "ret_b": _make_point(
                "ret_b", "RETURN", self.CALLEE, subject="b",
                subject_from="return", subject_decl="global:b",
                subject_storage="global", subject_member=""),
            "caller_call": _make_point(
                "caller_call", "CALL", self.CALLER, subject="q",
                subject_from="assignment", file_id="f1", start_line=7),
        }
        callees = _FakeCallees({("f1", self.CALLER, 7): "callee"})
        names = {self.CALLEE: "callee", self.CALLER: "caller"}
        table = bind_returns(points, callees=callees, names=names)
        records = table.returns["caller_call"]
        self.assertEqual({r["return_point"] for r in records}, {"ret_a", "ret_b"})
        self.assertEqual({r["subject"] for r in records}, {"a", "b"})


if __name__ == "__main__":
    unittest.main()

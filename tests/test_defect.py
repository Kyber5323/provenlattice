"""Stage 4 acceptance: the typed defect queries over the sparse graph.

Every test here drives the *published* artifacts -- `graph.build_graph` output
for the synthetic cases, `full_index` for the fixture and the query layer --
because the publisher is what a user's database contains. A test that agreed
with a private reimplementation while the publisher disagreed would be worse
than no test.

The three-state vocabulary these tests use is the graph's, never the model's:
`resolved` / `ambiguous` / `unresolved` say what the *graph* decided about a
decisive relation, and they are never silently upgraded. What an AI judge
concludes (`confirmed` / `likely` / `insufficient evidence` / `rejected`) lives
only in the review sheet's annotation block, and no test here asserts on it.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

try:
    from tests.test_sparsecfg import publish
except ImportError:  # `unittest discover -s tests` puts the directory on sys.path.
    from test_sparsecfg import publish

from experiments.defect_v1 import build_review

from provenlattice import cli, defect
from provenlattice.contracts import EMPTY as EMPTY_CONTRACTS
from provenlattice.evidence import parse_evidence_citations
from provenlattice.graph import full_index
from provenlattice.overlay import OverlayStore
from provenlattice.query import GraphQuery
from provenlattice.sparsecfg import (
    CONTROL_REACHES,
    leaked_allocations,
    materialize_points,
    unreleased_resources,
)
from provenlattice.sparsedfg import CONSUMES, DATA_FLOW_TO, bind_parameters
from provenlattice.storage import SQLiteStorage

REPO = "defect-repo"
FIXTURE = "tests/fixture_knowledge"


def annotations_by_type(events, edges) -> dict[str, list[dict]]:
    """Published point metadata keyed by event type, in event-id order.

    Built from `materialize_points` rather than from the events directly, so
    the tests read the same view the queries read -- a key that exists on the
    event but not in the point record would be invisible to every query.
    """
    table: dict[str, list[dict]] = {}
    for point_id, info in sorted(materialize_points(events, edges).items()):
        table.setdefault(info["event_type"], []).append({"point": point_id, **info})
    return table


def only(table: dict[str, list[dict]], event_type: str) -> dict:
    rows = table.get(event_type, [])
    assert len(rows) == 1, f"expected one {event_type}, got {len(rows)}"
    return rows[0]


def _event(event_id: str, owner: str, subject: str, line: int) -> dict:
    """One hand-built event row, as the queries receive it from SQLite.

    Ids are chosen rather than hashed because a test that needs a particular
    ordering of ids cannot get one out of a content hash. `metadata` stays JSON
    text, which is what the pipeline reads back.
    """
    return {
        "event_id": event_id, "event_type": "ALLOC", "owner_symbol_id": owner,
        "file_id": "file:unit", "ordinal": 1, "start_line": line, "end_line": line,
        "matched_name": "malloc", "matched_via": "calls", "flags": "",
        "metadata": json.dumps({
            "subject": subject, "subject_from": "assignment",
            "subject_exact": "true", "relative_path": "src/unit.cpp",
        }),
    }


def fixture_graph(test: unittest.TestCase):
    """The fixture through the real publisher, read back from SQLite.

    Events come from the database rather than from `build_graph`'s return
    value on purpose: the queries have to work on what a user's database
    holds, and the two differ in exactly the places that bite -- metadata is
    JSON text on the way in and a dict on the way out, and the exit ids only
    exist as edge endpoints.

    Returns `(events, edges, names)`, where `names` is the symbol-id to
    qualified-name table the events do not carry (identity is a hash, so a
    pure function cannot recover a name from it).
    """
    holder = tempfile.TemporaryDirectory()
    test.addCleanup(holder.cleanup)
    database = Path(holder.name) / "fixture.db"
    full_index(FIXTURE, database, repository_id_override="fixture")
    with SQLiteStorage(database) as storage:
        repo_id = storage.repository()["repo_id"]
        names = {
            row["id"]: row["qualified_name"]
            for row in storage.connection.execute(
                "SELECT id, qualified_name FROM nodes WHERE repo_id = ?", (repo_id,)
            )
        }
        return storage.semantic_events(repo_id), storage.semantic_edges(repo_id), names


class AnnotationChannelTests(unittest.TestCase):
    """The subject channel stage 4 widened: without these keys the typed
    queries have nothing to pair on. Stage 3 annotated ALLOC/RELEASE/CALL
    only, and every lock, spawn and return point carried an empty subject."""

    def publish_source(self, source: str):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        return publish(Path(holder.name), "src/unit.cpp", source)

    def test_lock_subject_comes_from_the_first_argument(self) -> None:
        events, edges = self.publish_source(
            "#include <pthread.h>\n"
            "void f() { pthread_mutex_lock(&g_index_lock); }\n"
        )
        lock = only(annotations_by_type(events, edges), "LOCK")
        self.assertEqual(lock["subject"], "g_index_lock")
        self.assertEqual(lock["subject_from"], "argument")

    def test_member_call_subject_comes_from_the_receiver(self) -> None:
        """`g_pool_lock.lock()` has no arguments at all, so the only name the
        source offers is the object the call is made on."""
        events, edges = self.publish_source(
            "#include <mutex>\n"
            "void f() { g_pool_lock.lock(); }\n"
        )
        lock = only(annotations_by_type(events, edges), "LOCK")
        self.assertEqual(lock["subject"], "g_pool_lock")
        self.assertEqual(lock["subject_from"], "receiver")

    def test_raii_declaration_subject_is_the_constructor_argument(self) -> None:
        """The subject is the mutex, not the handle: `guard` is a local whose
        identity is worthless to a lock-order query."""
        events, edges = self.publish_source(
            "#include <mutex>\n"
            "void f() { std::lock_guard<std::mutex> guard{g_pool_lock}; }\n"
        )
        lock = only(annotations_by_type(events, edges), "LOCK")
        self.assertEqual(lock["subject"], "g_pool_lock")
        self.assertEqual(lock["subject_from"], "raii")
        self.assertEqual(lock["raii"], "true")

    def test_the_parenthesised_raii_spelling_is_no_longer_a_gap(self) -> None:
        """`guard(m)` -- the spelling real C++ mostly writes -- reaches the CFG.

        It used to be the stage 4 registered gap: the declaration parses as a
        function declaration (the most-vexing-parse), so the symbol extractor
        promoted it to its own one-line Function symbol, the statement stopped
        belonging to the enclosing method, and the lock was attributed to a
        symbol that is not a function. The extractor now reads a declaration
        inside a body as the variable it is, so the lock belongs to `f` and the
        mutex is the constructor argument.
        """
        events, edges = self.publish_source(
            "#include <mutex>\n"
            "void f() { std::lock_guard<std::mutex> guard(g_pool_lock); }\n"
        )
        lock = only(annotations_by_type(events, edges), "LOCK")
        self.assertEqual(lock["subject"], "g_pool_lock")
        self.assertEqual(lock["subject_from"], "raii")
        self.assertEqual(lock["raii"], "true")
        # The point is inside `f`'s control flow, not stranded on a phantom:
        # stage 4's version of this test asserted the opposite.
        self.assertTrue([edge for edge in edges if edge.src_event_id == lock["point"]])

    def test_a_raii_argument_that_is_an_expression_is_not_a_phantom(self) -> None:
        """`guard(p->m)` parses as an ordinary declaration -- the most-vexing-
        parse needs a bare name to happen at all -- so it never was a phantom.
        It was still subject-less, because the initialiser is an
        `argument_list` and only `initializer_list` was unwrapped."""
        events, edges = self.publish_source(
            "#include <mutex>\n"
            "void f(Context *ctx) { std::lock_guard<std::mutex> guard(ctx->ctx_mutex); }\n"
        )
        lock = only(annotations_by_type(events, edges), "LOCK")
        self.assertEqual(lock["subject"], "ctx")
        self.assertEqual(lock["subject_from"], "raii")
        # A member path: `ctx` names the struct that holds the lock, not the
        # lock, so the subject is not the whole designation.
        self.assertEqual(lock["subject_exact"], "false")

    def test_a_two_declarator_raii_statement_claims_no_subject(self) -> None:
        """`a{m}, b{n}` acquires twice; naming one argument for both points
        would be a guess about which handle a lock point refers to. The RAII
        fact itself is still recorded, because that is what keeps the
        incomplete-cleanup query quiet about destructor-released locks."""
        events, edges = self.publish_source(
            "#include <mutex>\n"
            "void f() { std::lock_guard<std::mutex> a{m}, b{n}; }\n"
        )
        table = annotations_by_type(events, edges)
        self.assertEqual(len(table["LOCK"]), 2)
        for lock in table["LOCK"]:
            self.assertEqual(lock["subject"], "")
            self.assertEqual(lock["raii"], "true")

    def test_an_unbound_new_allocation_claims_no_subject(self) -> None:
        """`sink.emplace_back(new X(k));` binds nothing, and `k` is a different
        object from the allocation. Taking the constructor's first argument
        would attribute the candidate to `k` and call the identity resolved --
        which is what this rule did until a review sheet showed a leak
        candidate whose subject was `GGML_TYPE_F32`."""
        events, edges = self.publish_source(
            "#include <vector>\n"
            "void f(std::vector<X*> &sink, int k) { sink.emplace_back(new X(k)); }\n"
            "X *g(int k) { return new X(k); }\n"
        )
        table = annotations_by_type(events, edges)
        self.assertEqual(len(table["ALLOC"]), 2)
        for alloc in table["ALLOC"]:
            self.assertEqual(alloc["subject"], "")
            self.assertEqual(alloc["subject_from"], "")

    def test_a_bound_new_allocation_still_names_its_target(self) -> None:
        """The rule above must not fire when the source does name the object:
        `X *p = new X(k);` binds `p`, and that is the allocation's identity."""
        events, edges = self.publish_source(
            "#include <vector>\n"
            "void f(int k) { X *p = new X(k); }\n"
        )
        alloc = only(annotations_by_type(events, edges), "ALLOC")
        self.assertEqual(alloc["subject"], "p")
        self.assertEqual(alloc["subject_from"], "assignment")

    def test_a_name_that_is_a_path_root_is_recorded_as_inexact(self) -> None:
        """`&g_index_lock` names the lock; `&index->slot_locks[i]` names the
        struct that holds an array of them. Both recover a name, and only the
        first one identifies an object, so the annotation says which."""
        events, edges = self.publish_source(
            "#include <pthread.h>\n"
            "void f(HNSW *index, int i) {\n"
            "  pthread_mutex_lock(&g_index_lock);\n"
            "  pthread_mutex_unlock(&g_index_lock);\n"
            "  pthread_mutex_lock(&index->slot_locks[i]);\n"
            "  pthread_mutex_unlock(&index->slot_locks[i]);\n"
            "}\n"
        )
        table = annotations_by_type(events, edges)
        self.assertEqual(
            [(row["subject"], row["subject_exact"]) for row in table["LOCK"]],
            [("g_index_lock", "true"), ("index", "false")],
        )

    def test_an_assignment_to_a_field_is_recorded_as_inexact(self) -> None:
        """The same distinction on the binding side: `p = malloc(n)` binds the
        name `p`, `s->buf = malloc(n)` binds a field of `s`, and two calls
        through the same parameter would otherwise share the name `s`."""
        events, edges = self.publish_source(
            "#include <stdlib.h>\n"
            "void f(struct S *s) {\n"
            "  char *p = (char *)malloc(8);\n"
            "  s->buf = (char *)malloc(8);\n"
            "  free(p);\n"
            "}\n"
        )
        table = annotations_by_type(events, edges)
        self.assertEqual(
            [(row["subject"], row["subject_exact"]) for row in table["ALLOC"]],
            [("p", "true"), ("s", "false")],
        )

    def test_return_subject_names_the_returned_value_only(self) -> None:
        """`return buffer;` hands the resource to the caller; `return -1;`
        names nothing. The difference is the whole reason the subject is
        recorded -- and it is what keeps an ordinary early return from reading
        as an ownership transfer."""
        events, edges = self.publish_source(
            "#include <stdlib.h>\n"
            "char *give(char *buffer) {\n"
            "  return buffer;\n"
            "}\n"
            "int fail(void) {\n"
            "  return -1;\n"
            "}\n"
            "int zero(void) {\n"
            "  return 0;\n"
            "}\n"
        )
        table = annotations_by_type(events, edges)
        subjects = sorted(row["subject"] for row in table["RETURN"])
        self.assertEqual(subjects, ["", "", "buffer"])

    def test_thread_spawn_records_target_and_thread_function(self) -> None:
        """The entry point sits in the third argument of `pthread_create`, and
        the table that says so is positional: a spawn whose name is absent from
        it gets no thread function rather than a guess."""
        events, edges = self.publish_source(
            "#include <pthread.h>\n"
            "void *worker(void *);\n"
            "int f(pthread_t *handle) {\n"
            "  return pthread_create(handle, NULL, worker, NULL);\n"
            "}\n"
        )
        spawn = only(annotations_by_type(events, edges), "THREAD_SPAWN")
        self.assertEqual(spawn["subject"], "handle")
        self.assertEqual(spawn["subject_from"], "argument")
        self.assertEqual(spawn["thread_function"], "worker")

    def test_thread_spawn_of_a_qualified_type_names_the_declared_handle(self) -> None:
        """`std::thread t{fn};` is a declaration, not a call: the handle is the
        declared name and the entry point is the constructor argument."""
        events, edges = self.publish_source(
            "#include <thread>\n"
            "void *fn(void *);\n"
            "void f() { std::thread t{fn}; }\n"
        )
        spawn = only(annotations_by_type(events, edges), "THREAD_SPAWN")
        self.assertEqual(spawn["subject"], "t")
        self.assertEqual(spawn["thread_function"], "fn")

    def test_the_parenthesised_thread_declaration_is_no_longer_a_gap(self) -> None:
        """Same most-vexing-parse as the RAII gap above, and it falls to the
        same fix: the declaration binds `t` and the entry point is the
        constructor argument, exactly as in the brace spelling."""
        events, edges = self.publish_source(
            "#include <thread>\n"
            "void *fn(void *);\n"
            "void f() { std::thread t(fn); }\n"
        )
        spawn = only(annotations_by_type(events, edges), "THREAD_SPAWN")
        self.assertEqual(spawn["subject"], "t")
        self.assertEqual(spawn["thread_function"], "fn")

    def test_an_unknown_spawn_gets_no_thread_function(self) -> None:
        """A spawn whose argument position is unknown must not guess: a wrong
        edge is worse than a missing one, and `run_task(a, b)` says nothing
        about which argument is the entry point."""
        events, edges = self.publish_source(
            "#include <pthread.h>\n"
            "void f() { run_task(alpha, beta); }\n"
        )
        table = annotations_by_type(events, edges)
        self.assertNotIn("THREAD_SPAWN", table)

    def test_leaked_allocations_is_the_alloc_spelling_of_unreleased_resources(self) -> None:
        """The stage 3 function survives the generalisation unchanged: same
        walk, same arguments, same answer."""
        source = (
            "#include <stdlib.h>\n"
            "int leak(void) {\n"
            "  char *buffer = (char *)malloc(256);\n"
            "  if (!buffer) { return -1; }\n"
            "  return 0;\n"
            "}\n"
            "int clean(void) {\n"
            "  char *buffer = (char *)malloc(256);\n"
            "  free(buffer);\n"
            "  return 0;\n"
            "}\n"
        )
        events, edges = self.publish_source(source)
        points = materialize_points(events, edges)
        old, old_truncated = leaked_allocations(edges, points)
        new, new_truncated = unreleased_resources(edges, points)
        self.assertEqual(old_truncated, new_truncated)
        self.assertEqual(
            [(path.points, path.types) for path in old],
            [(path.points, path.types) for path in new],
        )
        self.assertEqual(len(old), 1)


class DeclarationIdentityTests(unittest.TestCase):
    """Stage 4.5: a subject name is not an object.

    The annotation channel carried a *name* -- `n`, `index`, `g_pool_lock` --
    and every query joined on it, which is what made the race query return
    557,315 candidates for redis-50: two methods that each declare `int n` are
    two variables, and joining on the spelling says they are one. These tests
    cover the keys that replace the spelling with a declaration identity, and
    they read them the way the queries do, through `materialize_points`.
    """

    def publish_source(self, source: str):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        return publish(Path(holder.name), "src/unit.cpp", source)

    def test_storage_class_separates_the_places_a_name_can_live(self) -> None:
        """One name, four objects: the parameter, the local, the file static
        and the global. The identity token has to tell them apart, because
        `int n` in one method and `int n` in another is the whole of the race
        query's false-positive population."""
        events, edges = self.publish_source(
            "#include <pthread.h>\n"
            "static pthread_mutex_t g_static;\n"
            "pthread_mutex_t g_global;\n"
            "void f(pthread_mutex_t n) {\n"
            "  static pthread_mutex_t cache;\n"
            "  pthread_mutex_t local;\n"
            "  pthread_mutex_lock(&n);\n"
            "  pthread_mutex_lock(&local);\n"
            "  pthread_mutex_lock(&cache);\n"
            "  pthread_mutex_lock(&g_static);\n"
            "  pthread_mutex_lock(&g_global);\n"
            "}\n"
        )
        by_name = {row["subject"]: row for row in annotations_by_type(events, edges)["LOCK"]}
        self.assertEqual(
            [(name, by_name[name]["subject_storage"], by_name[name]["subject_decl"])
             for name in ("n", "local", "cache", "g_static", "g_global")],
            [
                ("n", "parameter", "parameter:n"),
                ("local", "local", "local:local"),
                ("cache", "function_static", "function_static:cache"),
                # A file static is per-file; a global is not. Two files that
                # mention the same global are naming the same object, and
                # splitting it by path would invent two objects where the
                # program has one -- so the static's token carries the path and
                # the global's does not.
                ("g_static", "file_static", "file_static:src/unit.cpp:g_static"),
                ("g_global", "global", "global:g_global"),
            ],
        )

    def test_a_reference_parameter_is_a_parameter(self) -> None:
        """The declarator shapes a parameter can arrive in.

        tree-sitter-cpp leaves a `reference_declarator`'s name unfielded --
        `Ctx & ctx` is `&` then `ctx` with no `declarator` field, where
        `Ctx * p` carries one -- so a walk that reads only the field drops
        every reference parameter. A reference is the ordinary way to pass a
        context object in C++, so the drop lands on exactly the names the race
        query reads: in llama.cpp it left `ctx` undeclared in 1,454 events,
        which the spelling fallback then merged into one subject across 401
        methods. This pins all five shapes, because the value of the test is
        that they agree.
        """
        events, edges = self.publish_source(
            "struct Ctx { pthread_mutex_t m; };\n"
            "void by_value(Ctx ctx) { pthread_mutex_lock(&ctx.m); }\n"
            "void by_pointer(Ctx *ctx) { pthread_mutex_lock(&ctx->m); }\n"
            "void by_reference(Ctx &ctx) { pthread_mutex_lock(&ctx.m); }\n"
            "void by_const_reference(const Ctx &ctx) { pthread_mutex_lock(&ctx.m); }\n"
            "void by_rvalue(Ctx &&ctx) { pthread_mutex_lock(&ctx.m); }\n"
        )
        table = annotations_by_type(events, edges)
        self.assertEqual(
            sorted(
                (row["start_line"], row["subject_storage"], row["subject_decl"], row["subject_type"])
                for row in table["LOCK"] if row["subject"] == "ctx"
            ),
            [
                (2, "parameter", "parameter:ctx", "Ctx"),
                (3, "parameter", "parameter:ctx", "Ctx"),
                (4, "parameter", "parameter:ctx", "Ctx"),
                (5, "parameter", "parameter:ctx", "Ctx"),
                (6, "parameter", "parameter:ctx", "Ctx"),
            ],
        )

    def test_a_local_is_resolved_at_the_position_it_is_used(self) -> None:
        """Position sensitivity, which is what makes the index a declaration
        index rather than a name table: `use(n)` inside the inner block reads
        the inner `int n`, and the one after it reads the parameter."""
        events, edges = self.publish_source(
            "void use(int n);\n"
            "void f(int n) {\n"
            "  { int n = 1; use(n); }\n"
            "  use(n);\n"
            "}\n"
        )
        table = annotations_by_type(events, edges)
        calls = [row for row in table["CALL"] if row["subject"] == "n"]
        self.assertEqual(
            [(row["start_line"], row["subject_decl"]) for row in calls],
            [(3, "local:n"), (4, "parameter:n")],
        )

    def test_a_member_path_keeps_two_locks_of_one_struct_apart(self) -> None:
        """The redis-50 shape: `index->slot_locks[i]` and `index->global_lock`
        share a root and share a type, and they are different locks. The path
        is kept as source text, so `slot_locks[0]` and `slot_locks[1]` stay
        apart too -- collapsing them to the member name would report an array
        of locks as one lock acquired twice."""
        events, edges = self.publish_source(
            "struct HNSW { pthread_mutex_t global_lock; pthread_mutex_t slot_locks[8]; };\n"
            "void f(HNSW *index, int i) {\n"
            "  pthread_mutex_lock(&index->global_lock);\n"
            "  pthread_mutex_lock(&index->slot_locks[i]);\n"
            "  pthread_mutex_lock(&index->slot_locks[i + 1]);\n"
            "}\n"
        )
        table = annotations_by_type(events, edges)
        # Source order, not id order: `annotations_by_type` sorts by the
        # hashed point id, which is stable but arbitrary.
        locks = sorted(table["LOCK"], key=lambda row: row["start_line"])
        self.assertEqual(
            [(row["subject_member"], row["subject_exact"]) for row in locks],
            # The path is source text with the whitespace removed, so `i + 1`
            # and `i+1` are one identity: a token that changes when someone
            # reformats an expression splits one object into two candidates.
            [("global_lock", "false"), ("slot_locks[i]", "false"), ("slot_locks[i+1]", "false")],
        )
        # All three hang off the one declared root, which is what the identity
        # is built from: `parameter:index#global_lock` and
        # `parameter:index#slot_locks[i]` are two objects, and both are capped
        # at `ambiguous` downstream because a member path is not a declaration.
        self.assertEqual({row["subject_decl"] for row in table["LOCK"]}, {"parameter:index"})

    def test_a_field_of_this_is_the_field_and_not_the_token_this(self) -> None:
        """`this->m_` and a bare `m_` inside a method are the same object, and
        neither is named `this`: the token would join every field of every
        class into one identity."""
        events, edges = self.publish_source(
            "struct Ctx {\n"
            "  pthread_mutex_t m_;\n"
            "  void a() { this->m_.lock(); }\n"
            "  void b() { m_.lock(); }\n"
            "};\n"
        )
        table = annotations_by_type(events, edges)
        self.assertEqual(
            [(row["subject"], row["subject_decl"], row["subject_storage"]) for row in table["LOCK"]],
            [("m_", "field:Ctx:m_", "field"), ("m_", "field:Ctx:m_", "field")],
        )
        self.assertTrue(all(row["subject_exact"] == "true" for row in table["LOCK"]))

    def test_a_name_the_file_does_not_declare_is_kept_as_unknown(self) -> None:
        """A macro, an enum constant, a global declared in a header this parser
        never sees. `unknown` is an answer and not a failure -- and the
        candidate must survive it: a cross-file global is the most important
        shared state a race query can see, so dropping it would lose the
        finding to save a number."""
        events, edges = self.publish_source(
            "#include <pthread.h>\n"
            "void f(void) { pthread_mutex_lock(&g_total); }\n"
        )
        row = only(annotations_by_type(events, edges), "LOCK")
        self.assertEqual(row["subject"], "g_total")
        self.assertEqual(row["subject_storage"], "unknown")
        self.assertEqual(row["subject_decl"], "")

    def test_a_call_records_every_argument_not_only_the_first(self) -> None:
        """`RedisModule_SetKeyMeta(cls, key, new_str)` hands the resource over
        as the third argument. Recording only the first made the hand-off
        invisible, so the leak query called the allocation still-owned."""
        events, edges = self.publish_source(
            "void f(void *cls, void *key) {\n"
            "  char *buffer = (char *)malloc(8);\n"
            "  RedisModule_SetKeyMeta(cls, key, buffer);\n"
            "}\n"
        )
        table = annotations_by_type(events, edges)
        hand_off = [row for row in table["CALL"] if "\x1f" in row["arguments"]]
        self.assertEqual(len(hand_off), 1)
        self.assertEqual(hand_off[0]["arguments"].split("\x1f"), ["cls", "key", "buffer"])

    def test_a_later_argument_hand_off_caps_the_leak(self) -> None:
        """The stage 4 adjudication this closes: `RedisModule_SetKeyMeta(cls,
        key, buffer)` hands the resource over as its *third* argument, and a
        query that reads only the first one reported the allocation still-owned
        -- `resolved`, a false positive with the highest confidence the band
        has. The hand-off is now visible, and it caps rather than clears: a
        callee's ownership contract is still not in the graph.

        The second method is the falsification half: a local nobody passes
        anywhere stays `resolved`, so the cap is the hand-off and not the
        allocation."""
        events, edges = self.publish_source(
            "void f(void *cls, void *key) {\n"
            "  char *buffer = (char *)malloc(8);\n"
            "  RedisModule_SetKeyMeta(cls, key, buffer);\n"
            "}\n"
            "void g(void) {\n"
            "  char *plain = (char *)malloc(8);\n"
            "}\n"
        )
        result = defect.resource_lifetime(events, edges)
        by_name = {candidate.subject["name"]: candidate for candidate in result.candidates}
        self.assertEqual(sorted(by_name), ["buffer", "plain"])
        self.assertEqual(by_name["plain"].resolution_status, defect.RESOLVED)
        self.assertEqual(by_name["buffer"].resolution_status, defect.AMBIGUOUS)
        self.assertEqual(
            [fact["fact"] for fact in by_name["buffer"].uncertain_facts],
            ["OWNERSHIP_TRANSFER"],
        )
        self.assertIn("matched by spelling", by_name["buffer"].uncertain_facts[0]["detail"])

    def test_an_anonymous_allocation_is_identified_by_its_site(self) -> None:
        """`new X(k)` binds no name, so there is no name to resolve -- but it
        does have an identity, and the identity is the allocation site. That is
        what turns the candidate from *identity unknown* into *ownership
        unknown*, which is a question the ownership stage can act on."""
        events, edges = self.publish_source(
            "void f(int k) { sink(new X(k)); }\n"
            "void g(int k) { sink(new X(k)); }\n"
        )
        table = annotations_by_type(events, edges)
        sites = [row["subject_site"] for row in table["ALLOC"]]
        self.assertEqual(len(sites), 2)
        self.assertTrue(all(site for site in sites))
        self.assertNotEqual(sites[0], sites[1])
        self.assertEqual([row["subject"] for row in table["ALLOC"]], ["", ""])

    def test_the_identity_does_not_move_when_the_file_shifts(self) -> None:
        """The token is a declaration, not a position: inserting a blank line
        at the top of the file may not create a new object, or every candidate
        id would churn on every reformat."""
        source = (
            "static pthread_mutex_t g_pool_lock;\n"
            "void f(HNSW *index) {\n"
            "  pthread_mutex_lock(&g_pool_lock);\n"
            "  pthread_mutex_lock(&index->global_lock);\n"
            "}\n"
        )
        before, edges = self.publish_source(source)
        after, _ = self.publish_source("\n\n" + source)
        keys = ("subject", "subject_decl", "subject_storage", "subject_type",
                "subject_member", "subject_exact")
        self.assertEqual(
            [tuple(row[key] for key in keys) for row in annotations_by_type(before, edges)["LOCK"]],
            [tuple(row[key] for key in keys) for row in annotations_by_type(after, edges)["LOCK"]],
        )

    def test_the_identity_keys_do_not_collide_with_the_transport_keys(self) -> None:
        """`graph.py` splats the annotations *after* the file and shard keys,
        so a new key named `relative_path` would silently overwrite the file a
        point belongs to -- no error, no failing query, just the wrong path on
        every candidate."""
        events, edges = self.publish_source(
            "void f(HNSW *index) { pthread_mutex_lock(&index->global_lock); }\n"
        )
        lock = only(annotations_by_type(events, edges), "LOCK")
        self.assertEqual(lock["relative_path"], "src/unit.cpp")
        self.assertTrue(lock["file_id"])
        for key in ("subject_decl", "subject_storage", "subject_type",
                    "subject_member", "subject_site", "arguments"):
            self.assertIn(key, lock)


    def test_a_qualifier_macro_does_not_swallow_the_parameter(self) -> None:
        """`type * QUALIFIER name`, which neither grammar can parse.

        A macro between the `*` and the name is how C spells a restrict
        qualifier portably, and both grammars recover the same way: the macro
        becomes the declarator and the real name is pushed into an error node
        beside it. Read literally, `float * GGML_RESTRICT s` declares a
        parameter named `GGML_RESTRICT` and loses `s` -- which is the shape of
        every ggml kernel's output parameter, and therefore the name a race
        query is most likely to be reading when it looks at one. llama.cpp has
        2,148 of them and redis-50 has none.

        Both halves are pinned, because the recovery is only sound if it is
        narrow: the qualified parameter keeps its real name, and the plain
        pointer parameter beside it is untouched. The pair is the grammar's own
        error shape, so this is reading the parse back rather than guessing at
        C.

        The leading `int n` is not decoration. The recovery needs the parameter
        list to survive, and whether it does depends on the type of the first
        parameter: with a primitive type the function still parses and the
        error stays inside the list, while a type_identifier first makes the
        grammar read the whole function as a variable declaration and there is
        no list left to read. That second shape is the one function the
        recovery cannot reach; see the stage 4.5 record.
        """
        events, edges = self.publish_source(
            "void qualified(int n, pthread_mutex_t * GGML_RESTRICT m,\n"
            "                pthread_mutex_t *plain) {\n"
            "  pthread_mutex_lock(m);\n"
            "  pthread_mutex_lock(plain);\n"
            "}\n"
        )
        table = annotations_by_type(events, edges)["LOCK"]
        self.assertEqual(
            sorted((row["subject"], row["subject_storage"], row["subject_decl"])
                   for row in table),
            [
                ("m", "parameter", "parameter:m"),
                ("plain", "parameter", "parameter:plain"),
            ],
        )
        # The macro is a qualifier, not a name: nothing may be declared under
        # it, or the graph carries a parameter that does not exist.
        self.assertEqual(
            [row["subject"] for row in table if row["subject"] == "GGML_RESTRICT"], []
        )


class ResourceLifetimeTests(unittest.TestCase):
    """4.1 (memory leak) and the lock half of 4.6, one walk with two spellings.

    Every assertion here is about what the *graph* resolved, never about
    whether the code has a bug: a `resolved` candidate says the graph answered
    every decisive relation, and it says nothing about whether the leak is
    real.
    """

    def publish_source(self, source: str):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        return publish(Path(holder.name), "src/unit.cpp", source)

    def test_the_fixture_leak_is_one_resolved_candidate(self) -> None:
        events, edges, names = fixture_graph(self)
        result = defect.resource_lifetime(events, edges, names=names)

        self.assertEqual(result.query, "resource_lifetime")
        self.assertEqual(result.keys, ("4.1", "4.6"))
        leaks = [candidate for candidate in result.candidates if candidate.defect_key == "4.1"]
        self.assertEqual(len(leaks), 1)
        leak = leaks[0]
        self.assertEqual(leak.subject["name"], "buffer")
        self.assertEqual(leak.subject["method"], "storage.load_index")
        self.assertEqual(leak.resolution_status, defect.RESOLVED)
        self.assertEqual(leak.confidence, 0.6)
        self.assertEqual(leak.uncertain_facts, ())

        # The path is what makes the candidate a candidate: an acquisition that
        # reaches the method's exit without releasing. Asserting both ends
        # rather than just the length, because either end alone is satisfiable
        # by the wrong path.
        self.assertEqual(len(leak.paths), 1)
        path = leak.paths[0]
        self.assertEqual(path["types"][0], "ALLOC")
        self.assertEqual(path["points"][-1], leak.metadata["exit_point"])
        self.assertTrue(leak.metadata["exit_point"].startswith("cfg-exit:"))

    def test_a_released_allocation_produces_no_candidate(self) -> None:
        """The falsification half: the same shape with a `free` on every path
        is not a candidate. Without this, a query that reported every
        allocation would pass the test above."""
        events, edges = self.publish_source(
            "#include <stdlib.h>\n"
            "int clean(void) {\n"
            "  char *buffer = (char *)malloc(256);\n"
            "  if (!buffer) { return -1; }\n"
            "  free(buffer);\n"
            "  return 0;\n"
            "}\n"
        )
        result = defect.resource_lifetime(events, edges)
        self.assertEqual(result.candidates, ())
        self.assertEqual(result.truncated, False)
        self.assertEqual(result.coverage["acquire_points"], 1)
        self.assertEqual(result.coverage["candidates"], 0)

    def test_an_anonymous_new_allocation_is_ambiguous_not_unresolved(self) -> None:
        """An unbound `new` has no *name* but it does have an identity, and the
        identity is the allocation site -- that is what "this object" means for
        a value never bound to a variable.

        Stage 4 reported this `unresolved` with `SUBJECT_UNRESOLVED`, which said
        "the graph cannot attribute this candidate". It can: the missing fact
        is not which allocation this is, it is who owns the object. That is a
        question the ownership stage can act on, and reporting it as an absent
        identity hid it behind a word that means the graph failed.
        """
        events, edges = self.publish_source(
            "#include <vector>\n"
            "void f(std::vector<X*> &sink, int k) { sink.emplace_back(new X(k)); }\n"
        )
        result = defect.resource_lifetime(events, edges)
        self.assertEqual(len(result.candidates), 1)
        candidate = result.candidates[0]
        self.assertEqual(candidate.defect_key, "4.1")
        # Still no name: it really does not have one.
        self.assertEqual(candidate.subject["name"], "")
        self.assertTrue(candidate.subject["identity"].startswith("site:"))
        self.assertEqual(candidate.resolution_status, defect.AMBIGUOUS)
        self.assertEqual(candidate.confidence, 0.4)
        self.assertEqual(
            [fact["fact"] for fact in candidate.uncertain_facts], ["OWNERSHIP_UNKNOWN"]
        )

    def test_the_fixture_error_path_lock_is_one_resolved_candidate(self) -> None:
        """4.6's material on the fixture: `resize_pool` locks, allocates, and
        returns early when the allocation fails. The early return is what makes
        it a 4.6 candidate, and it is exactly the shape mode A excludes.

        Stage 4.5's one loosening shows here. The subject still comes from a
        receiver -- a name for an expression, not for the object -- but the
        receiver's declaration is in the file and says `std::mutex`, so the
        question stage 4 could not answer ("is this name a lock or a struct
        that holds one?") now has an answer. `pool->lock()` on an `HNSW *`
        still gets the old cap; only a declared mutex is lifted.
        """
        events, edges, names = fixture_graph(self)
        result = defect.resource_lifetime(events, edges, names=names)

        locks = [candidate for candidate in result.candidates if candidate.defect_key == "4.6"]
        self.assertEqual(len(locks), 1)
        lock = locks[0]
        self.assertEqual(lock.subject["name"], "g_pool_lock")
        self.assertEqual(lock.subject["method"], "storage.resize_pool")
        self.assertEqual(lock.subject["kind"], "lock")
        self.assertEqual(lock.resolution_status, defect.RESOLVED)
        self.assertEqual(lock.confidence, 0.6)

        facts = {fact["event"]: fact for fact in lock.facts}
        self.assertEqual(facts["LOCK"]["subject_from"], "receiver")
        self.assertEqual(lock.subject["type"], "mutex")
        self.assertEqual(lock.subject["storage"], "file_static")
        self.assertNotIn("LOCK_IDENTITY", [fact["fact"] for fact in lock.uncertain_facts])

        # And the leak this method also has is *not* a 4.1 candidate: the only
        # early return sits inside the null guard, where the allocation failed.
        self.assertEqual(
            [c.subject["name"] for c in result.candidates if c.defect_key == "4.1"],
            ["buffer"],
        )

    def test_a_member_named_lock_on_a_weak_ptr_is_not_a_lock(self) -> None:
        """`pipeline.lock()` on a `std::weak_ptr` returns a `shared_ptr`.

        The vocabulary matches the member name and the receiver is a smart
        pointer, so stage 4 reported a lock that was never taken. The declared
        type is what separates the two, and the discrimination is inside one
        file: the mutex next door is still a candidate, and it is `resolved`
        because its own declaration says what it is."""
        events, edges = self.publish_source(
            "#include <memory>\n"
            "#include <mutex>\n"
            "static std::mutex g_pool_lock;\n"
            "void guarded(std::weak_ptr<Ctx> pipeline) { pipeline.lock(); }\n"
            "void real_lock(void) { g_pool_lock.lock(); }\n"
        )
        result = defect.resource_lifetime(events, edges)
        locks = [candidate for candidate in result.candidates if candidate.defect_key == "4.6"]
        self.assertEqual([candidate.subject["name"] for candidate in locks], ["g_pool_lock"])
        self.assertEqual(locks[0].resolution_status, defect.RESOLVED)
        self.assertEqual(result.coverage["non_mutex_lock_points"], 1)
        # The lock-order walk sees the same universe, and the excluded point is
        # not an identity failure either: the graph answered, and the answer was
        # "this is not a lock".
        order = defect.lock_order(events, edges)
        self.assertEqual(order.coverage["non_mutex_lock_points"], 1)
        self.assertEqual(order.coverage["lock_points"], 1)
        self.assertEqual(order.coverage["unidentified_lock_points"], 0)

    def test_a_receiver_of_unknown_type_is_kept_and_capped(self) -> None:
        """The falsification half of the exclusion: a type the graph has not
        seen as a mutex is not evidence of anything.

        `pool->lock()` on an `HNSW *` is a genuine acquisition as far as the
        graph can tell -- a class wrapping a mutex behind `.lock()` is ordinary
        -- so it stays a candidate and carries the stage 4 cap. Excluding it
        would drop findings; the exclusion list is one entry long for this
        reason."""
        events, edges = self.publish_source(
            "struct HNSW { void lock(); };\n"
            "void f(HNSW *pool) { pool->lock(); }\n"
        )
        result = defect.resource_lifetime(events, edges)
        locks = [candidate for candidate in result.candidates if candidate.defect_key == "4.6"]
        self.assertEqual([candidate.subject["name"] for candidate in locks], ["pool"])
        self.assertEqual(locks[0].resolution_status, defect.AMBIGUOUS)
        self.assertEqual(locks[0].subject["type"], "HNSW")
        self.assertIn("LOCK_IDENTITY", [fact["fact"] for fact in locks[0].uncertain_facts])
        self.assertEqual(result.coverage["non_mutex_lock_points"], 0)

    def test_raii_locks_are_excluded_from_the_lock_walk(self) -> None:
        """`snapshot` has a LOCK and no UNLOCK, so the naive walk reports it.
        Its release is a destructor at scope exit -- a fact the CFG cannot see,
        and a false positive the query is allowed to drop because it can name
        the reason."""
        events, edges, names = fixture_graph(self)
        result = defect.resource_lifetime(events, edges, names=names)

        self.assertEqual(result.coverage["excluded_raii_locks"], 1)
        anchors = {candidate.anchor for candidate in result.candidates}
        snapshot_lock = [
            info for point_id, info in materialize_points(events, edges).items()
            if info["event_type"] == "LOCK" and info.get("raii") == "true"
        ]
        self.assertEqual(len(snapshot_lock), 1)
        self.assertNotIn(
            next(point_id for point_id, info in materialize_points(events, edges).items()
                 if info.get("raii") == "true"),
            anchors,
        )

    def test_returning_the_resource_caps_the_candidate_at_ambiguous(self) -> None:
        """`return buffer;` may hand ownership to the caller, so the graph can
        no longer say the resource was never released. `return -1;` names
        nothing and must not trigger the same doubt -- if it did, every leak in
        the repository would be downgraded and the state would carry no
        information."""
        events, edges = self.publish_source(
            "#include <stdlib.h>\n"
            "char *keep(void) {\n"
            "  char *buffer = (char *)malloc(256);\n"
            "  if (!buffer) { return NULL; }\n"
            "  return buffer;\n"
            "}\n"
            "int drop(void) {\n"
            "  char *buffer = (char *)malloc(256);\n"
            "  if (!buffer) { return -1; }\n"
            "  return -1;\n"
            "}\n"
        )
        result = defect.resource_lifetime(events, edges)
        # Keyed by the anchor's line rather than by method name: no names table
        # was supplied, so the subject's `method` is empty by design (identity
        # is a hash, and a pure function cannot invent a name from it).
        by_line = {
            candidate.metadata["anchor_span"]["start_line"]: candidate
            for candidate in result.candidates
        }
        self.assertEqual(sorted(by_line), [3, 8])

        kept = by_line[3]
        self.assertEqual(kept.subject["method"], "")
        self.assertEqual(kept.resolution_status, defect.AMBIGUOUS)
        self.assertIn("RETURN_TRANSFER", [fact["fact"] for fact in kept.uncertain_facts])
        transfer = next(f for f in kept.uncertain_facts if f["fact"] == "RETURN_TRANSFER")
        self.assertEqual(transfer["source"], "src/unit.cpp:5-5")

        dropped = by_line[8]
        self.assertEqual(dropped.resolution_status, defect.RESOLVED)
        self.assertEqual(dropped.uncertain_facts, ())

    def test_the_lock_half_can_be_switched_off(self) -> None:
        """`include_locks=False` is the mode A-only walk. It exists because the
        two modes have different disqualifiers, and a caller that only wants
        4.1 should not have to filter 4.6 candidates out afterwards."""
        events, edges, names = fixture_graph(self)
        only_allocations = defect.resource_lifetime(events, edges, names=names, include_locks=False)

        self.assertEqual(only_allocations.keys, ("4.1",))
        self.assertEqual(only_allocations.coverage["excluded_raii_locks"], 0)
        self.assertEqual(
            only_allocations.coverage["acquire_points"],
            sum(
                1 for info in materialize_points(events, edges).values()
                if info["event_type"] == "ALLOC"
            ),
        )
        self.assertEqual(
            [(c.subject["name"], c.resolution_status) for c in only_allocations.candidates],
            [("buffer", defect.RESOLVED)],
        )

    def test_an_alias_return_disqualifies_exactly_when_the_flow_edges_reach_it(self) -> None:
        """Stage 5B's registered 4.1 miss, closed: `q = p; return q;` returns
        the allocation, but the name-level ruling reads only `q` -- pre-5B
        that candidate walked out `resolved`, the false positive with the
        highest confidence the band has.

        The copy target is a parameter because it is the one shape the miss
        is sharpest in: a local target publishes no WRITE row (registered),
        and a global target is already disqualified by the 5C-2 escape
        ruling before any return question is asked. A parameter slot is
        frame-local, so no escape is claimed and the return is the only
        channel the value leaves by.

        The flow edge the ruling reads is pinned here in its root-def form:
        sourced at the ALLOC, with the copy site as the whole `via` chain.
        Read over control edges alone -- contracts armed, flow adjacency
        empty, the observable form of every pre-5B call shape -- the same
        fixture keeps the candidate."""
        events, edges = self.publish_source(
            "#include <stdlib.h>\n"
            "char *f(char *q) {\n"
            "  char *p = (char *)malloc(8);\n"
            "  q = p;\n"
            "  return q;\n"
            "}\n"
        )
        points = materialize_points(events, edges)
        alloc_ids = [pid for pid, info in points.items() if info["event_type"] == "ALLOC"]
        flow_edges = [edge for edge in edges if edge.relation == DATA_FLOW_TO]
        self.assertEqual(len(flow_edges), 1)
        self.assertEqual([edge.src_event_id for edge in flow_edges], alloc_ids)
        self.assertEqual(flow_edges[0].metadata["flow_class"], "alloc_to_use")
        self.assertEqual(
            flow_edges[0].metadata["via"], flow_edges[0].metadata["via_copies"]
        )
        self.assertNotEqual(flow_edges[0].metadata["via"], [])

        result = defect.resource_lifetime(events, edges, contracts=EMPTY_CONTRACTS)
        self.assertEqual(list(result.candidates), [])
        eliminated = result.coverage["disqualified_evidence"]
        self.assertEqual([record["reason"] for record in eliminated], ["RETURN_TRANSFER"])
        self.assertEqual([record["subject"] for record in eliminated], ["p"])

        control_only = [edge for edge in edges if edge.relation == CONTROL_REACHES]
        without = defect.resource_lifetime(events, control_only, contracts=EMPTY_CONTRACTS)
        by_name = {candidate.subject["name"]: candidate for candidate in without.candidates}
        self.assertEqual(sorted(by_name), ["p"])
        self.assertEqual(by_name["p"].resolution_status, defect.RESOLVED)

    def test_the_alias_return_is_the_only_candidate_the_flow_edges_move(self) -> None:
        """The fixture-level gate for the widening: one method returns the
        resource by name, the other through a copy. The name-level return is
        disqualified with and without the flow edges; the alias return is the
        only candidate whose fate the new edges move."""
        events, edges = self.publish_source(
            "#include <stdlib.h>\n"
            "char *keep(void) {\n"
            "  char *buffer = (char *)malloc(256);\n"
            "  return buffer;\n"
            "}\n"
            "char *alias_keep(char *q) {\n"
            "  char *p = (char *)malloc(8);\n"
            "  q = p;\n"
            "  return q;\n"
            "}\n"
        )
        control_only = [edge for edge in edges if edge.relation == CONTROL_REACHES]
        before = defect.resource_lifetime(events, control_only, contracts=EMPTY_CONTRACTS)
        after = defect.resource_lifetime(events, edges, contracts=EMPTY_CONTRACTS)

        self.assertEqual(
            {candidate.subject["name"] for candidate in after.candidates}, set()
        )
        self.assertEqual(
            {candidate.subject["name"] for candidate in before.candidates}, {"p"}
        )
        moved = {
            record["anchor"]
            for record in after.coverage["disqualified_evidence"]
        } - {record["anchor"] for record in before.coverage["disqualified_evidence"]}
        self.assertEqual(len(moved), 1)
        moved_record = next(
            record for record in after.coverage["disqualified_evidence"]
            if record["anchor"] in moved
        )
        self.assertEqual(moved_record["reason"], "RETURN_TRANSFER")
        self.assertEqual(moved_record["subject"], "p")


class LockOrderTests(unittest.TestCase):
    """1.3 (inversion) and 1.4 (a lock taken twice without a release).

    The CFG's job here is narrow and worth stating: acquisition order is the
    method's lexical order, and the graph is asked only whether the first lock
    is *still held* when the second is taken. A pair whose only connection
    runs through a release is not a pair.
    """

    def publish_source(self, source: str):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        return publish(Path(holder.name), "src/unit.cpp", source)

    def test_the_fixture_inversion_is_one_resolved_candidate(self) -> None:
        events, edges, names = fixture_graph(self)
        result = defect.lock_order(events, edges, names=names)

        self.assertEqual(result.query, "lock_order")
        self.assertEqual(len(result.candidates), 1)
        candidate = result.candidates[0]
        self.assertEqual(candidate.defect_key, "1.3")
        self.assertEqual(candidate.resolution_status, defect.RESOLVED)
        self.assertEqual(candidate.confidence, 0.7)

        # The pair is ordered as taken, not as sorted: the name is what the
        # candidate is about, and sorting it would make both directions of the
        # same inversion print identically.
        self.assertEqual(candidate.subject["locks"], ["g_index_lock", "g_log_lock"])
        self.assertEqual(candidate.subject["name"], "g_index_lock|g_log_lock")
        self.assertEqual(candidate.subject["method"], "storage.reindex")

        # Both sides, each with its own holding path. One side alone is not an
        # inversion, and a candidate carrying one path would not show it.
        facts = {(fact["side"], fact["role"]): fact for fact in candidate.facts}
        self.assertEqual(
            sorted({fact["method"] for fact in candidate.facts}),
            ["storage.flush_log", "storage.reindex"],
        )
        self.assertEqual(
            [(side, role, facts[(side, role)]["lock"], facts[(side, role)]["source"])
             for side, role in (("a", "first"), ("a", "second"), ("b", "first"), ("b", "second"))],
            [
                ("a", "first", "g_index_lock", "src/resource.cpp:48-48"),
                ("a", "second", "g_log_lock", "src/resource.cpp:49-49"),
                ("b", "first", "g_log_lock", "src/resource.cpp:56-56"),
                ("b", "second", "g_index_lock", "src/resource.cpp:57-57"),
            ],
        )
        self.assertTrue(all(fact["holding_confirmed"] for fact in candidate.facts))
        self.assertEqual(len(candidate.paths), 2)

        # Concurrency is the one thing the graph cannot answer, and it says so
        # on every 1.3 candidate rather than in a footnote.
        self.assertEqual(
            [fact["fact"] for fact in candidate.uncertain_facts], ["MAY_PARALLEL"]
        )
        self.assertEqual(candidate.uncertain_facts[0]["status"], defect.AMBIGUOUS)
        self.assertTrue(
            any("execution context" in line for line in candidate.missing_evidence)
        )

    def test_the_result_does_not_depend_on_input_order(self) -> None:
        """A candidate whose id moves when SQLite returns rows in a different
        order cannot be cited or adjudicated twice. Reversing both inputs is
        the cheapest way to catch an accidental dependence on iteration
        order."""
        events, edges, names = fixture_graph(self)
        forward = defect.lock_order(events, edges, names=names)
        backward = defect.lock_order(list(reversed(events)), list(reversed(edges)), names=names)
        self.assertEqual(
            [candidate.to_dict() for candidate in forward.candidates],
            [candidate.to_dict() for candidate in backward.candidates],
        )
        self.assertEqual(forward.coverage, backward.coverage)

    def test_a_repository_without_spawns_has_no_inversions(self) -> None:
        """Two methods taking the same locks in opposite orders is only a
        deadlock if something can run them at once. With no THREAD_SPAWN point
        anywhere, the graph can say they cannot -- the one form of
        `MAY_PARALLEL resolved = false` it can produce -- so the candidates are
        dropped and counted instead of reported as ambiguous."""
        events, edges = self.publish_source(
            "#include <pthread.h>\n"
            "void first(void) {\n"
            "  pthread_mutex_lock(&a);\n"
            "  pthread_mutex_lock(&b);\n"
            "  pthread_mutex_unlock(&b);\n"
            "  pthread_mutex_unlock(&a);\n"
            "}\n"
            "void second(void) {\n"
            "  pthread_mutex_lock(&b);\n"
            "  pthread_mutex_lock(&a);\n"
            "  pthread_mutex_unlock(&a);\n"
            "  pthread_mutex_unlock(&b);\n"
            "}\n"
        )
        result = defect.lock_order(events, edges)
        self.assertEqual(result.candidates, ())
        self.assertEqual(result.coverage["disqualified_no_threads"], 1)
        self.assertEqual(result.coverage["inverted_pairs"], 1)
        self.assertEqual(result.coverage["spawn_points"], 0)

    def test_a_second_acquisition_without_a_release_is_a_self_deadlock(self) -> None:
        """1.4: the same lock taken twice with nothing releasing it in between.
        `unlock` then `lock` again is not the pattern, and neither is a second
        acquisition the graph cannot show is reached while the lock is held."""
        events, edges = self.publish_source(
            "#include <pthread.h>\n"
            "pthread_mutex_t m;\n"
            "void twice(void) {\n"
            "  pthread_mutex_lock(&m);\n"
            "  pthread_mutex_lock(&m);\n"
            "  pthread_mutex_unlock(&m);\n"
            "}\n"
            "void alternating(void) {\n"
            "  pthread_mutex_lock(&m);\n"
            "  pthread_mutex_unlock(&m);\n"
            "  pthread_mutex_lock(&m);\n"
            "  pthread_mutex_unlock(&m);\n"
            "}\n"
        )
        result = defect.lock_order(events, edges)
        self.assertEqual([candidate.defect_key for candidate in result.candidates], ["1.4"])
        candidate = result.candidates[0]
        self.assertEqual(candidate.subject["name"], "m")
        self.assertEqual(candidate.subject["identity"], "global:m")
        self.assertEqual(candidate.metadata["anchor_span"]["start_line"], 4)
        self.assertEqual(candidate.resolution_status, defect.RESOLVED)
        self.assertTrue(all(fact["holding_confirmed"] for fact in candidate.facts))

        # A recursive mutex makes this legal, which is a fact about the mutex
        # the graph does not hold -- so it is named rather than assumed away.
        self.assertIn(
            "MUTEX_RECURSIVE", [fact["fact"] for fact in candidate.uncertain_facts]
        )
        self.assertIn("PTHREAD_MUTEX_RECURSIVE", candidate.missing_evidence[0])
        self.assertEqual(result.coverage["reentrant_acquires"], 1)
        self.assertEqual(result.coverage["lock_points"], 4)


    def test_two_locks_named_by_the_same_root_are_not_a_self_deadlock(self) -> None:
        """The redis-50 shape, found by reading a generated review sheet:
        `pthread_mutex_trylock(&index->slot_locks[i])` and
        `pthread_rwlock_rdlock(&index->global_lock)` both recover the name
        `index`, and stage 4 called the pair a re-entrant acquisition of one
        lock. They are two locks behind one name. The member path is part of
        the identity, so the two are two objects and there is no re-entrant
        acquisition left to report -- the candidate is gone rather than
        capped, which is the strongest form of the fix."""
        events, edges = self.publish_source(
            "#include <pthread.h>\n"
            "int slot(HNSW *index) {\n"
            "  pthread_mutex_lock(&index->slot_locks[0]);\n"
            "  pthread_rwlock_rdlock(&index->global_lock);\n"
            "  pthread_rwlock_unlock(&index->global_lock);\n"
            "  pthread_mutex_unlock(&index->slot_locks[0]);\n"
            "  return 0;\n"
            "}\n"
        )
        result = defect.lock_order(events, edges)
        self.assertEqual([candidate.defect_key for candidate in result.candidates], [])

    def test_the_same_member_path_twice_is_still_a_self_deadlock(self) -> None:
        """The falsification half of the rule above: separating the paths must
        not separate a lock from itself. `slot_locks[0]` acquired twice without
        a release is one object taken twice, and the array is not what makes it
        two -- the *index* is."""
        events, edges = self.publish_source(
            "#include <pthread.h>\n"
            "int slot(HNSW *index) {\n"
            "  pthread_mutex_lock(&index->slot_locks[0]);\n"
            "  pthread_mutex_lock(&index->slot_locks[0]);\n"
            "  pthread_mutex_unlock(&index->slot_locks[0]);\n"
            "  return 0;\n"
            "}\n"
        )
        result = defect.lock_order(events, edges)
        self.assertEqual([candidate.defect_key for candidate in result.candidates], ["1.4"])
        candidate = result.candidates[0]
        self.assertEqual(candidate.subject["name"], "index")
        self.assertIn("#slot_locks[0]", candidate.subject["identity"])
        # A path is not a plain designation, so the object is known and the
        # *element* is not: capped, never resolved.
        self.assertEqual(candidate.resolution_status, defect.AMBIGUOUS)
        self.assertIn("SUBJECT_PATH", [fact["fact"] for fact in candidate.uncertain_facts])

    def test_a_plain_global_lock_is_still_a_resolved_self_deadlock(self) -> None:
        """The falsification half of the declaration gate: `&g_lock` is the
        whole designation of a declared mutex, so it must stay `resolved`."""
        events, edges = self.publish_source(
            "#include <pthread.h>\n"
            "pthread_mutex_t g_lock;\n"
            "void twice(void) {\n"
            "  pthread_mutex_lock(&g_lock);\n"
            "  pthread_mutex_lock(&g_lock);\n"
            "  pthread_mutex_unlock(&g_lock);\n"
            "}\n"
        )
        result = defect.lock_order(events, edges)
        self.assertEqual(len(result.candidates), 1)
        self.assertEqual(result.candidates[0].subject["identity"], "global:g_lock")
        self.assertEqual(result.candidates[0].resolution_status, defect.RESOLVED)
        self.assertNotIn(
            "SUBJECT_PATH", [fact["fact"] for fact in result.candidates[0].uncertain_facts]
        )

    def test_a_lock_the_file_never_declares_is_capped_not_resolved(self) -> None:
        """A mutex declared in a header this parser does not read.

        The graph can see two acquisitions of one spelling and cannot see that
        they are one object, because the declaration is in a file it never
        looked at. Stage 4 called that resolved; the identity rule calls it
        ambiguous, and the candidate survives -- the conservative direction,
        since a cross-file lock is exactly the shared state worth reporting."""
        events, edges = self.publish_source(
            "#include <pthread.h>\n"
            "void twice(void) {\n"
            "  pthread_mutex_lock(&g_lock);\n"
            "  pthread_mutex_lock(&g_lock);\n"
            "  pthread_mutex_unlock(&g_lock);\n"
            "}\n"
        )
        result = defect.lock_order(events, edges)
        self.assertEqual(len(result.candidates), 1)
        candidate = result.candidates[0]
        self.assertEqual(candidate.subject["identity"], "name:g_lock")
        self.assertEqual(candidate.resolution_status, defect.AMBIGUOUS)
        self.assertIn("IDENTITY_UNKNOWN", [fact["fact"] for fact in candidate.uncertain_facts])


class RaceConditionTests(unittest.TestCase):
    """1.1, the query where stage 4's falsifiability question gets its answer.

    Every candidate it produces is `ambiguous`, by construction and not by
    accident: MAY_PARALLEL is not a relation the graph holds. Since stage 5A
    the access kind is a fact when the walker saw the access and the syntactic
    proxy only when it did not -- `metadata["access"]` says which, per side.
    The tests below pin both the candidates it does find and the ones it
    cannot -- a subject touched only as a read on both sides is dropped,
    which is the one filter the graph can still decide.
    """

    def publish_source(self, source: str):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        return publish(Path(holder.name), "src/unit.cpp", source)

    def test_a_write_and_a_read_across_two_methods_is_ambiguous(self) -> None:
        events, edges = self.publish_source(
            "#include <pthread.h>\n"
            "#include <stdlib.h>\n"
            "static int *g_total;\n"
            "void *worker(void *arg) {\n"
            "  g_total = (int *)malloc(64);\n"
            "  return NULL;\n"
            "}\n"
            "int main(void) {\n"
            "  pthread_t handle;\n"
            "  pthread_create(&handle, NULL, worker, NULL);\n"
            "  free(g_total);\n"
            "  return 0;\n"
            "}\n"
        )
        result = defect.race_condition(events, edges)
        self.assertEqual(result.coverage["subjects_multi_method"], 1)
        self.assertEqual(len(result.candidates), 1)
        candidate = result.candidates[0]
        self.assertEqual(candidate.defect_key, "1.1")
        self.assertEqual(candidate.subject["name"], "g_total")
        self.assertEqual(candidate.resolution_status, defect.AMBIGUOUS)
        self.assertEqual(candidate.confidence, 0.5)
        self.assertEqual(candidate.paths, ())

        # Since stage 5A the kinds are read from the access events, not from
        # the proxy: the assignment is a WRITE, and `free(g_total)`'s argument
        # is a READ. The bundle marks both sides decided and cites the events.
        self.assertEqual(
            sorted(candidate.metadata["access_kinds"].values()), ["read", "write"]
        )
        self.assertIn(
            "access kind from READ/WRITE events", candidate.uncertain_facts[-1]["detail"]
        )
        for summary in candidate.metadata["access"].values():
            self.assertTrue(summary["decided"])
            self.assertTrue(summary["writer_event"] or summary["reader_event"])

        # Concurrency is named as the undecidable thing it is, with the
        # strongest reason the spawn points support: one method starts a thread.
        may_parallel = next(f for f in candidate.uncertain_facts if f["fact"] == "MAY_PARALLEL")
        self.assertEqual(may_parallel["status"], defect.AMBIGUOUS)
        self.assertEqual(may_parallel["detail"], "one of the methods starts a thread")

    def test_two_reads_are_not_a_race(self) -> None:
        """The one reliable filter left without READ/WRITE: if neither side
        binds the name, neither can be the writer. It is the reason a shared
        pointer checked for null in two methods does not fill the review sheet."""
        events, edges = self.publish_source(
            "#include <pthread.h>\n"
            "#include <stdlib.h>\n"
            "static int *g_ptr;\n"
            "void *worker(void *arg) {\n"
            "  if (!g_ptr) { return NULL; }\n"
            "  return NULL;\n"
            "}\n"
            "int main(void) {\n"
            "  pthread_t handle;\n"
            "  pthread_create(&handle, NULL, worker, NULL);\n"
            "  if (!g_ptr) { return -1; }\n"
            "  return 0;\n"
            "}\n"
        )
        result = defect.race_condition(events, edges)
        self.assertEqual(result.candidates, ())
        self.assertEqual(result.coverage["subjects_multi_method"], 1)
        self.assertEqual(result.coverage["read_read_pairs"], 1)

    def test_the_unknown_root_keeps_its_member(self) -> None:
        """Stage 5B's identity rung: an unknown root keeps its member path.

        `server.io_threads_num` and `server.clients` are two objects even when
        nothing can resolve the type of `server` -- the collapse into
        `name:server` is what merged 765 member paths into one bucket after
        5A. Here three methods touch two members: the write/read pair on
        `io_threads_num` is one candidate, `clients` has no pair, and the
        bare-name control (`identity=False`, the stage 4 join) still shows the
        three collapsed pairs the rung removes.
        """
        events, edges = self.publish_source(
            "void writer_a(void) {\n"
            "  server.io_threads_num = 2;\n"
            "}\n"
            "int reader_a(void) {\n"
            "  return server.io_threads_num;\n"
            "}\n"
            "void writer_b(void) {\n"
            "  server.clients = 0;\n"
            "}\n"
        )
        result = defect.race_condition(events, edges)
        self.assertEqual(len(result.candidates), 1)
        candidate = result.candidates[0]
        self.assertEqual(candidate.subject["identity"], "name:server#io_threads_num")
        self.assertEqual(candidate.subject["name"], "server")
        self.assertEqual(
            sorted(candidate.metadata["access_kinds"].values()), ["read", "write"]
        )
        self.assertEqual(result.coverage["subjects_multi_method"], 1)
        self.assertEqual(result.coverage["candidates_by_identity_storage"], {"unknown": 1})

        # The control column: the stage 4 bare-name join collapses all three
        # methods into one `server` bucket and pairs all of them. This is the
        # false-positive shape the rung exists to remove.
        control = defect.race_condition(events, edges, identity=False)
        self.assertEqual(len(control.candidates), 3)

    def test_errno_is_exempt_from_race_pairing(self) -> None:
        """Stage 5B's thread-local exemption, applied at pairing time only.

        `errno` is a thread-local lvalue (C11 §7.5), so two methods touching it
        are never a race -- but the access events themselves must stay in the
        graph, so the coverage still counts the subject as considered and
        reports the exemption separately instead of hiding it.
        """
        events, edges = self.publish_source(
            "int touch_a(void) {\n"
            "  errno = 0;\n"
            "  return errno;\n"
            "}\n"
            "int touch_b(void) {\n"
            "  errno = 3;\n"
            "  return errno + 1;\n"
            "}\n"
        )
        result = defect.race_condition(events, edges)
        self.assertEqual(result.candidates, ())
        self.assertEqual(result.coverage["exempt_thread_local_subjects"], 1)
        self.assertEqual(result.coverage["exempt_thread_local_methods"], 2)
        # The events were seen, not deleted: the subject was considered and
        # its accesses are in the access accounting.
        self.assertGreaterEqual(result.coverage["subjects_considered"], 4)
        self.assertGreaterEqual(result.coverage["access_events"], 4)

        # A named non-thread-local global under the same shape still pairs.
        events, edges = self.publish_source(
            "int g_code;\n"
            "int touch_a(void) {\n"
            "  g_code = 0;\n"
            "  return g_code;\n"
            "}\n"
            "int touch_b(void) {\n"
            "  g_code = 3;\n"
            "  return g_code + 1;\n"
            "}\n"
        )
        result = defect.race_condition(events, edges)
        self.assertEqual(len(result.candidates), 1)
        self.assertEqual(result.coverage["exempt_thread_local_subjects"], 0)




    def test_the_fixture_yields_no_race_candidates(self) -> None:
        """Not a shortcoming of the query: no name in the fixture is touched by
        two methods, so there is nothing to pair. The coverage block carries
        the denominators, which is what makes the zero readable as "nothing to
        find" rather than "the query did not run"."""
        events, edges, names = fixture_graph(self)
        result = defect.race_condition(events, edges, names=names)

        self.assertEqual(result.candidates, ())
        self.assertEqual(result.coverage["subjects_multi_method"], 0)
        self.assertGreater(result.coverage["subjects_considered"], 0)
        self.assertEqual(result.coverage["spawn_points"], 1)
        self.assertEqual(result.coverage["resolved_thread_functions"], 1)
        self.assertTrue(result.coverage["may_parallel_decided"])
        self.assertEqual(result.coverage["candidates"], 0)

    def test_one_shared_lock_on_both_sides_disqualifies(self) -> None:
        """Two methods that both hold `g_lock` around their access are not a
        race candidate -- this is the matrix's own disqualifying evidence, and
        it is decidable because domination is a control-flow question. The
        protection has to be *proved*: a lock taken somewhere in the method
        leaves the candidate standing."""
        protected = self.publish_source(
            "#include <pthread.h>\n"
            "#include <stdlib.h>\n"
            "static int *g_total;\n"
            "static pthread_mutex_t g_lock;\n"
            "void *worker(void *arg) {\n"
            "  pthread_mutex_lock(&g_lock);\n"
            "  g_total = (int *)malloc(64);\n"
            "  pthread_mutex_unlock(&g_lock);\n"
            "  return NULL;\n"
            "}\n"
            "int main(void) {\n"
            "  pthread_t handle;\n"
            "  pthread_create(&handle, NULL, worker, NULL);\n"
            "  pthread_mutex_lock(&g_lock);\n"
            "  free(g_total);\n"
            "  pthread_mutex_unlock(&g_lock);\n"
            "  return 0;\n"
            "}\n"
        )
        events, edges = protected
        result = defect.race_condition(events, edges)
        self.assertEqual(result.candidates, ())
        self.assertEqual(result.coverage["disqualified_same_lock"], 1)

        # The falsification of the falsifier: drop one side's lock and the
        # candidate comes back, with the protected side reported as resolved
        # and the unprotected one as ambiguous.
        half = self.publish_source(
            "#include <pthread.h>\n"
            "#include <stdlib.h>\n"
            "static int *g_total;\n"
            "static pthread_mutex_t g_lock;\n"
            "void *worker(void *arg) {\n"
            "  g_total = (int *)malloc(64);\n"
            "  return NULL;\n"
            "}\n"
            "int main(void) {\n"
            "  pthread_t handle;\n"
            "  pthread_create(&handle, NULL, worker, NULL);\n"
            "  pthread_mutex_lock(&g_lock);\n"
            "  free(g_total);\n"
            "  pthread_mutex_unlock(&g_lock);\n"
            "  return 0;\n"
            "}\n"
        )
        events, edges = half
        partial = defect.race_condition(events, edges)
        self.assertEqual(len(partial.candidates), 1)
        protections = [
            fact for fact in partial.candidates[0].uncertain_facts
            if fact["fact"] == "PROTECTED_BY"
        ]
        self.assertEqual(
            sorted(fact["status"] for fact in protections),
            [defect.AMBIGUOUS, defect.RESOLVED],
        )
        self.assertEqual(partial.candidates[0].confidence, 0.4)

    def test_every_race_bundle_names_what_is_still_missing(self) -> None:
        """What the bundle owes the reader changed with stage 5A: the READ/WRITE
        gap it used to confess is delivered -- the access events are cited in
        `metadata["access"]` -- and `missing_evidence` keeps only the relation
        side: concurrency, whether the lock guards the variable, and whether
        the variable tolerates the race. Those are the DFG's to close."""
        events, edges = self.publish_source(
            "#include <pthread.h>\n"
            "#include <stdlib.h>\n"
            "static int *g_total;\n"
            "void *worker(void *arg) {\n"
            "  g_total = (int *)malloc(64);\n"
            "  return NULL;\n"
            "}\n"
            "int main(void) {\n"
            "  pthread_t handle;\n"
            "  pthread_create(&handle, NULL, worker, NULL);\n"
            "  free(g_total);\n"
            "  return 0;\n"
            "}\n"
        )
        result = defect.race_condition(events, edges)
        self.assertTrue(result.candidates)
        for candidate in result.candidates:
            self.assertEqual(len(candidate.missing_evidence), 3)
            self.assertFalse(any("READ/WRITE" in item for item in candidate.missing_evidence))
            self.assertIn("counter", candidate.missing_evidence[2])
            # The evidence that replaced the confession: per side, which event
            # says write and which says read.
            for summary in candidate.metadata["access"].values():
                self.assertTrue(summary["decided"])
                self.assertTrue(summary["writer_event"] or summary["reader_event"])

    def test_the_pair_is_the_identity_not_the_larger_owner(self) -> None:
        """The anchor is the smaller *point* id and the discriminator has to
        identify the pair on its own, because the two orderings are unrelated:
        point ids are content hashes and so are owner ids, so the side an
        anchor came from can hold either owner id.

        Naming only the larger owner gave every pair that shared an anchor the
        same discriminator, and since the citation is built from (anchor,
        discriminator), the same evidence id: on redis-50, 889 distinct
        candidates minted one. The graph is hand-built here rather than
        published, because the collision needs the anchor's owner to be the
        larger one and no source text can promise a hash order.
        """
        events = [
            _event("event:001", "symbol:zz", "c", 10),
            _event("event:100", "symbol:aa", "c", 20),
            _event("event:200", "symbol:mm", "c", 30),
        ]
        names = {"symbol:aa": "A.a", "symbol:mm": "M.m", "symbol:zz": "Z.z"}
        result = defect.race_condition(events, [], names=names)

        self.assertEqual(len(result.candidates), 3)
        identities = {
            (candidate.defect_key, candidate.anchor, candidate.discriminator)
            for candidate in result.candidates
        }
        self.assertEqual(len(identities), 3)
        for candidate in result.candidates:
            # The subject's method and its owner are the anchor's, as they are
            # on the other two queries; the pair is named in the discriminator.
            self.assertEqual(
                candidate.subject["owner_symbol_id"],
                "symbol:zz" if candidate.anchor == "event:001" else
                "symbol:aa" if candidate.anchor == "event:100" else "symbol:mm",
            )
            self.assertEqual(candidate.subject["method"], names[candidate.subject["owner_symbol_id"]])
            pair = set(candidate.metadata["access_kinds"])
            self.assertEqual(len(pair), 2)
            self.assertIn(candidate.subject["owner_symbol_id"], pair)
            for owner in pair:
                self.assertIn(owner, candidate.discriminator)

        # What the collision actually cost: a citation is built from (anchor,
        # discriminator), so the two pairs sharing one were also one citable
        # candidate, and a review sheet that cites it would be citing both.
        citations = {
            defect.candidate_evidence(
                candidate, repository="repo:x", commit="0" * 40, generation=1
            ).evidence_id
            for candidate in result.candidates
        }
        self.assertEqual(len(citations), 3)

    # -- stage 5B: the query-time parameter join -----------------------

    BINDING_SOURCE = (
        "#include <pthread.h>\n"
        "#include <stdlib.h>\n"
        "struct conf { int flags; };\n"
        "static struct conf g_conf;\n"
        "static void consume(struct conf *item) { item->flags = 0; }\n"
        "static void *writer(void *arg) {\n"
        "  consume(&g_conf);\n"
        "  g_conf.flags = 1;\n"
        "  return NULL;\n"
        "}\n"
        "int main(void) {\n"
        "  pthread_t a;\n"
        "  pthread_create(&a, NULL, writer, NULL);\n"
        "  return 0;\n"
        "}\n"
    )

    def test_a_bound_parameter_write_composes_the_cross_method_pair(self) -> None:
        """Stage 5B's query-time join, at the defect layer. The callee's
        through-the-parameter write is frame-local on its own --
        `parameter:item#flags` can never pair with anything outside
        `consume` -- so unjoined, the shared member `g_conf.flags` has one
        toucher and no candidate. The binding composes the write into the
        caller's declared facts plus the callee row's member path, the callee
        joins the subject's owner set, and the pair the sparse graph could
        not see becomes one ambiguous candidate carrying its own provenance.
        A bare slot write (`item = 0`) would compose into a claim that the
        caller's object was written when only the callee's own slot was;
        `bind_parameters` declines those, which is why the fixture writes
        through the parameter."""
        events, edges = self.publish_source(self.BINDING_SOURCE)
        points = materialize_points(events, edges)
        before = defect.race_condition(events, edges)
        self.assertEqual(before.candidates, ())
        self.assertNotIn("parameter_binding_pairs", before.coverage)

        class _CalleeByName:
            """Test double for the contract table: answers the callee's
            qualified name from the CALL point's subject. The real join lives
            in `contracts` (5C-tested) and is exercised against a database by
            the query-layer test; this double fixes only the name, so what
            this test observes is `bind_parameters`' own join."""

            def __init__(self, mapping: dict):
                self._mapping = mapping

            def callee(self, info: dict) -> str:
                return self._mapping.get(info.get("subject"), "")

        write_id, write = next(
            (point_id, info) for point_id, info in points.items()
            if info["event_type"] == "WRITE" and info.get("subject_storage") == "parameter"
        )
        call_id, call = next(
            (point_id, info) for point_id, info in points.items()
            if info["event_type"] == "CALL" and info.get("arguments") == "g_conf"
        )
        consume_id = write["owner_symbol_id"]
        bindings = bind_parameters(
            points,
            callees=_CalleeByName({"g_conf": "unit.consume"}),
            signatures={consume_id: "void consume(struct conf *item)"},
            names={consume_id: "unit.consume"},
        )
        self.assertEqual(list(bindings.bindings), [write_id])
        record = bindings.bindings[write_id][0]
        self.assertEqual(record["callee"], "unit.consume")
        self.assertEqual(record["ordinal"], 0)
        self.assertEqual(record["parameter"], "item")
        self.assertEqual(record["arg_root"], "g_conf")
        self.assertEqual(record["join"], "declared")
        self.assertEqual(record["caller_owner"], call["owner_symbol_id"])
        self.assertEqual(record["call_site"], call_id)
        self.assertEqual(
            {key: record["caller_facts"][key] for key in
             ("subject", "subject_decl", "subject_storage", "subject_member")},
            {"subject": "g_conf", "subject_decl": "file_static:src/unit.cpp:g_conf",
             "subject_storage": "file_static", "subject_member": ""},
        )
        self.assertEqual(bindings.coverage["arguments_seen"], 1)
        self.assertEqual(bindings.coverage["arguments_bound"], 1)

        result = defect.race_condition(events, edges, bindings=bindings)
        self.assertEqual(len(result.candidates), 1)
        candidate = result.candidates[0]
        self.assertEqual(candidate.subject["name"], "g_conf")
        self.assertEqual(candidate.resolution_status, defect.AMBIGUOUS)
        self.assertEqual(candidate.confidence, 0.5)
        self.assertEqual(
            candidate.metadata["parameter_binding"], [{**record, "event": write_id}]
        )
        self.assertEqual(result.coverage["parameter_binding_pairs"], 1)
        self.assertEqual(result.coverage["parameter_binding_unjoined"], 0)
        self.assertEqual(result.coverage["parameter_binding_join"]["join_declared"], 1)

    def test_passing_bindings_none_is_byte_identical_to_omitting_it(self) -> None:
        """The default-off contract, pinned: `bindings=None` is not a third
        mode. Whatever the joined path does, the unjoined path -- the only
        path every pre-5B caller and test knows -- must be untouched, down to
        the coverage dict's exact contents."""
        events, edges = self.publish_source(self.BINDING_SOURCE)
        self.assertEqual(
            defect.race_condition(events, edges, bindings=None),
            defect.race_condition(events, edges),
        )


class AccessLayerTests(unittest.TestCase):
    """Stage 5A: the access layer's READ/WRITE events and their scope.

    An access has no keyword -- only position says who reads and who writes --
    so the rules pinned here are positional: one event per outermost access
    expression, the storage filter that keeps method-scoped objects out, and
    the skips that keep declarations and callees from reading as accesses.
    Every event goes through the real publisher, so `matched_via="access"`
    and the annotation keys are the ones the queries actually read.
    """

    def publish_source(self, source: str):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        return publish(Path(holder.name), "src/unit.cpp", source)

    @staticmethod
    def access(events) -> list:
        return [event for event in events if event.event_type in ("READ", "WRITE")]

    def test_a_plain_assignment_is_one_write_event(self) -> None:
        """`g_counter = 0` has no vocabulary hit, so before stage 5A it
        produced no event at all and the race query could not see the write.
        The one event is a WRITE; the number on the right is not a READ."""
        events, edges = self.publish_source(
            "static int g_counter;\n"
            "void f(void) { g_counter = 0; }\n"
        )
        rows = self.access(events)
        self.assertEqual([row.event_type for row in rows], ["WRITE"])
        row = rows[0]
        self.assertEqual(row.matched_via, "access")
        self.assertEqual(row.metadata["subject"], "g_counter")
        self.assertEqual(row.metadata["subject_decl"], "file_static:src/unit.cpp:g_counter")
        self.assertEqual(row.metadata["subject_storage"], "file_static")

    def test_the_outermost_access_is_one_write_not_three(self) -> None:
        """`g_pairs->buf[g_i] = 5` writes one object: the outermost access is
        one WRITE, and the finer spellings of that same access (`g_pairs`,
        `buf`, `buf[g_i]` as a read) never appear. The one READ is the
        subscript index -- a different object the statement genuinely reads,
        not a re-spelling of the write."""
        events, edges = self.publish_source(
            "struct pair { int buf[8]; };\n"
            "struct pair *g_pairs;\n"
            "static int g_i;\n"
            "void f(void) { g_pairs->buf[g_i] = 5; }\n"
        )
        writes = [row for row in self.access(events) if row.event_type == "WRITE"]
        reads = [row for row in self.access(events) if row.event_type == "READ"]
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0].metadata["subject_member"], "buf[g_i]")
        self.assertEqual(writes[0].metadata["subject_decl"], "global:g_pairs")
        self.assertEqual(len(reads), 1)
        self.assertEqual(reads[0].metadata["subject"], "g_i")

    def test_a_condition_is_one_read_not_a_write(self) -> None:
        """The validity check reads its subject; nothing on that line writes."""
        events, edges = self.publish_source(
            "static int g_flag;\n"
            "void f(void) { if (g_flag > 0) { } }\n"
        )
        rows = self.access(events)
        self.assertEqual([row.event_type for row in rows], ["READ"])
        self.assertEqual(rows[0].metadata["subject"], "g_flag")

    def test_parameters_publish_only_their_write_side(self) -> None:
        """Stage 5A's storage rule, narrowed by 5B's registered exception. A
        local is method-scoped under the 4.5 identity and can never be one
        half of a cross-method pair, so its accesses publish nothing. A
        parameter publishes its WRITE side only: the value arrives from the
        caller, an in-frame read has no consumer of its own, and the
        query-time parameter join composes the cross-method pairs from these
        writes in the caller's terms. The read side staying unpublished is
        the registered approximation, not an oversight."""
        events, edges = self.publish_source(
            "void f(int n) {\n"
            "  int local = 0;\n"
            "  local = local + n;\n"
            "  n = local;\n"
            "}\n"
        )
        rows = self.access(events)
        self.assertEqual([row.event_type for row in rows], ["WRITE"])
        self.assertEqual(rows[0].metadata["subject"], "n")
        self.assertEqual(rows[0].metadata["subject_storage"], "parameter")
        self.assertEqual(rows[0].metadata["subject_decl"], "parameter:n")

    def test_each_shared_storage_gets_events_under_the_same_ruler(self) -> None:
        """Four storage classes, one ruler: the declaration tokens are spelled
        exactly as the structural events spell them, because the pair join
        must not be able to tell which walker produced a point's facts."""
        events, edges = self.publish_source(
            "struct S {\n"
            "  int m;\n"
            "  void touch(void) { m = 1; }\n"
            "};\n"
            "int g_g;\n"
            "static int g_s;\n"
            "void f(void) {\n"
            "  g_g = UNDECLARED_GLOBAL;\n"
            "  g_s = g_g;\n"
            "}\n"
        )
        by_subject = {row.metadata["subject"]: row for row in self.access(events)}
        self.assertEqual(
            by_subject["g_g"].metadata["subject_decl"], "global:g_g"
        )
        self.assertEqual(
            by_subject["g_s"].metadata["subject_decl"], "file_static:src/unit.cpp:g_s"
        )
        self.assertEqual(by_subject["m"].metadata["subject_decl"], "field:S:m")
        # The undeclared name: `unknown` is a value, not a failure, and the
        # ruler omits `subject_decl` rather than writing an empty token.
        undeclared = by_subject["UNDECLARED_GLOBAL"]
        self.assertEqual(undeclared.metadata["subject_storage"], "unknown")
        self.assertNotIn("subject_decl", undeclared.metadata)
        self.assertEqual(undeclared.metadata["subject_member"], "")

    def test_the_callee_is_not_a_read(self) -> None:
        """A callee names what is invoked, not a variable that is read."""
        events, edges = self.publish_source(
            "static void g_helper(void) { }\n"
            "void f(void) { g_helper(); }\n"
        )
        self.assertEqual(self.access(events), [])

    def test_a_bare_declaration_is_not_an_event(self) -> None:
        """`static int counter;` declares; it does not write. The initialized
        spelling is a different statement -- the definition does run -- and is
        pinned separately below."""
        events, edges = self.publish_source(
            "static int g_declared;\n"
            "void f(void) { static int counter; int local; }\n"
        )
        self.assertEqual(self.access(events), [])

    def test_a_function_static_is_a_shared_object(self) -> None:
        """A function static has cross-call identity (the declaration index
        gives it a repo-wide name token), so its accesses are published --
        including the write the initializer performs on first call. This is
        the storage class the sizing probe wrongly excluded and the plan
        corrected: excluding it would drop the whole function-static-counter
        race family."""
        events, edges = self.publish_source(
            "void f(void) {\n"
            "  static int counter = 0;\n"
            "  counter = counter + 1;\n"
            "}\n"
        )
        writes = [row for row in self.access(events) if row.event_type == "WRITE"]
        reads = [row for row in self.access(events) if row.event_type == "READ"]
        self.assertEqual(len(writes), 2)  # the initializer and the assignment
        self.assertEqual(len(reads), 1)   # the right-hand side of the assignment
        for row in [*writes, *reads]:
            self.assertEqual(row.metadata["subject_decl"], "function_static:counter")

    def test_access_events_never_reach_the_edges(self) -> None:
        """The access layer publishes rows and nothing else. An access id as a
        CONTROL_REACHES endpoint would mean the CFG was polluted, which would
        move every path-based query -- the one thing the stage must not do.
        Stage 5B's DATA_FLOW_TO edges are the registered exception: they land
        in the same table but never in the control adjacency (the relation
        filter sees to that), and each names its flow class."""
        events, edges = self.publish_source(
            "#include <stdlib.h>\n"
            "static int *g_total;\n"
            "void f(void) {\n"
            "  if (g_total == 0) { g_total = (int *)malloc(8); }\n"
            "}\n"
        )
        access_ids = {event.event_id for event in self.access(events)}
        self.assertTrue(access_ids)
        self.assertTrue(edges)
        flow_classes = {"input_to_sink", "alloc_to_use", "free_to_use", "null_to_deref", "value_flow"}
        for edge in edges:
            if edge.relation in (DATA_FLOW_TO, CONSUMES):
                self.assertIn(edge.metadata["flow_class"], flow_classes)
                continue
            self.assertNotIn(edge.src_event_id, access_ids)
            self.assertNotIn(edge.dst_event_id, access_ids)
        # The positive half: the same-line allocation feeds the condition's
        # read of `g_total` (the registered same-line approximation -- the
        # type-ordered tie-break puts ALLOC before READ), so the fixture
        # really does carry a flow edge into an access row.
        touching = [edge for edge in edges
                    if edge.src_event_id in access_ids or edge.dst_event_id in access_ids]
        self.assertTrue(touching)
        for edge in touching:
            self.assertEqual(edge.relation, DATA_FLOW_TO)
            self.assertEqual(edge.metadata["flow_class"], "alloc_to_use")

    def test_the_other_queries_do_not_move_when_access_events_exist(self) -> None:
        """The fixture-scale form of the acceptance gate: the same graph with
        the READ/WRITE rows removed is the pre-5A database, and 4.1 / 4.6 must
        answer it identically. A candidate that appears or vanishes with the
        access rows present means an access point leaked into the walk."""
        events, edges = self.publish_source(
            "#include <stdlib.h>\n"
            "#include <pthread.h>\n"
            "static int *g_total;\n"
            "static pthread_mutex_t g_lock;\n"
            "void f(void) {\n"
            "  char *buffer = (char *)malloc(64);\n"
            "  pthread_mutex_lock(&g_lock);\n"
            "  g_total = (int *)malloc(8);\n"
            "  pthread_mutex_unlock(&g_lock);\n"
            "}\n"
            "void g(void) {\n"
            "  pthread_mutex_lock(&g_lock);\n"
            "  free(g_total);\n"
            "  pthread_mutex_unlock(&g_lock);\n"
            "}\n"
        )
        kept = [event for event in events if event.event_type not in ("READ", "WRITE")]
        self.assertLess(len(kept), len(events))
        for query, kwargs in (
            (defect.resource_lifetime, {}),
            (defect.lock_order, {}),
        ):
            with_all = query(events, edges, max_candidates=100, **kwargs)
            without = query(kept, edges, max_candidates=100, **kwargs)
            self.assertEqual(
                [candidate.discriminator for candidate in with_all.candidates],
                [candidate.discriminator for candidate in without.candidates],
                query.__name__,
            )

    def test_the_race_bundle_cites_writer_and_reader_events(self) -> None:
        """The acceptance's payload: the evidence bundle names the event that
        writes and the event that reads, with ids that resolve to published
        rows -- not a re-derivation the reader must trust."""
        events, edges = self.publish_source(
            "#include <stdlib.h>\n"
            "static int *g_total;\n"
            "void f(void) { g_total = (int *)malloc(64); }\n"
            "void g(void) { if (g_total) { free(g_total); } }\n"
        )
        result = defect.race_condition(events, edges)
        self.assertEqual(len(result.candidates), 1)
        candidate = result.candidates[0]
        published = {event.event_id for event in events}
        for summary in candidate.metadata["access"].values():
            self.assertTrue(summary["decided"])
            # The side cites the event that justifies its own kind; the other
            # slot is empty unless the method both reads and writes.
            cited = summary["writer_event"] if summary["kind"] == "write" \
                else summary["reader_event"]
            self.assertIn(cited, published)
        kinds = candidate.metadata["access"]
        self.assertEqual(
            {side["kind"] for side in kinds.values()}, {"read", "write"}
        )


class Stage5C2FactsTests(unittest.TestCase):
    """Stage 5C-2's five registered facts and the two rulings they feed.

    Every fact is read through `annotations_by_type`, so a key that exists on
    the event but not in the materialised point record fails here rather than
    silently in a query -- which is exactly how the first delivery broke.

    The two end-to-end ruling tests need a CFG point *after* the escape write
    (the verifier reads path lines), so their shapes follow the real one this
    was built on: `spt_init`, whose escape write is followed by more
    statements, not by the method's bare exit.
    """

    def publish_source(self, source: str):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        return publish(Path(holder.name), "src/unit.cpp", source)

    # -- 2.9 out-of-line bare members ---------------------------------

    def test_an_out_of_line_definition_resolves_its_bare_member(self) -> None:
        """`void S::touch()` sits outside every scope `S` opens, so the bare
        `m_` used to fall through to `unknown` and split from the in-class
        spelling of the same field."""
        events, edges = self.publish_source(
            "struct S { char *m_; void touch(); };\n"
            "void S::touch() { m_ = 0; }\n"
        )
        write = only(annotations_by_type(events, edges), "WRITE")
        self.assertEqual(write["subject_decl"], "field:S:m_")
        self.assertEqual(write["subject_storage"], "field")

    def test_an_out_of_line_member_alloc_carries_field_storage(self) -> None:
        """The allocation into a bare out-of-line member is what the 4.1
        STORAGE_OWNER rule reads: the ALLOC point's subject must say `field`,
        not `unknown`."""
        events, edges = self.publish_source(
            "#include <stdlib.h>\n"
            "struct S { char *m_; void touch(); };\n"
            "void S::touch() { m_ = (char *) malloc(8); }\n"
        )
        alloc = only(annotations_by_type(events, edges), "ALLOC")
        self.assertEqual(alloc["subject"], "m_")
        self.assertEqual(alloc["subject_storage"], "field")

    # -- 2.6 guard -> mutex binding ------------------------------------

    def test_the_vexing_parse_guard_records_its_mutex(self) -> None:
        """`ULock lk(g_mutex);` parses as a function declaration (the
        most-vexing-parse), so the guard/mutex binding lived in a shape
        nothing read. The declaration index records the constructor's first
        argument, and the guard's UNLOCK carries it as `subject_init_arg0`."""
        events, edges = self.publish_source(
            "struct Mutex { };\n"
            "struct ULock { ULock(Mutex *mu); void unlock(); };\n"
            "Mutex g_mutex;\n"
            "void f(void) { ULock lk(g_mutex); lk.unlock(); }\n"
        )
        unlock = only(annotations_by_type(events, edges), "UNLOCK")
        self.assertEqual(unlock["subject_init_arg0"], "g_mutex")

    # -- 2.12 / 2.4 allocation hand-offs -------------------------------

    def test_a_returned_allocation_says_so(self) -> None:
        """`return strdup(x)` has no RETURN-side subject to join on, which is
        why the walk never saw the hand-off; the allocation itself records
        that it is the method's return value."""
        events, edges = self.publish_source(
            "#include <stdlib.h>\n"
            'char *f(void) { return strdup("x"); }\n'
        )
        alloc = only(annotations_by_type(events, edges), "ALLOC")
        self.assertEqual(alloc["returned"], "true")

    def test_an_allocation_passed_to_a_callee_says_so(self) -> None:
        """The anonymous-new gap (2.4): the allocation is an argument, so
        nothing else in the statement names where it went. Fact only -- a
        ruling would be unsound without consumes contracts."""
        events, edges = self.publish_source(
            "#include <stdlib.h>\n"
            "void sink(char *p);\n"
            'void f(void) { sink(strdup("x")); }\n'
        )
        alloc = only(annotations_by_type(events, edges), "ALLOC")
        self.assertEqual(alloc["passed_to"], "sink")

    # -- 2.11 escape writes ---------------------------------------------

    def test_a_plain_assignment_into_shared_storage_marks_the_escape(self) -> None:
        """The write side knows what no vocabulary hit does: which name the
        assignment read, and whether it sits outside every conditional. The
        allocation joins the two into `escaped_to`."""
        events, edges = self.publish_source(
            "#include <stdlib.h>\n"
            "char *g_second;\n"
            "void f(void) {\n"
            '  char *tmp = strdup("x");\n'
            "  g_second = tmp;\n"
            "}\n"
        )
        table = annotations_by_type(events, edges)
        write = only(table, "WRITE")
        self.assertEqual(write["subject"], "g_second")
        self.assertEqual(write["write_source"], "tmp")
        self.assertEqual(write["unconditional"], "true")
        alloc = only(table, "ALLOC")
        self.assertEqual(alloc["escaped_to"], "g_second")
        self.assertEqual(alloc["escaped_to_line"], str(write["start_line"]))

    def test_a_conditional_or_nameless_write_is_no_escape(self) -> None:
        """An if-armed write is not a fact about every path (`unconditional`
        says so), and a ternary on the right names no root, so it records no
        write source; with neither qualifying, the allocation records
        nothing."""
        events, edges = self.publish_source(
            "#include <stdlib.h>\n"
            "char *g_first;\n"
            "char *g_third;\n"
            "void f(int n) {\n"
            '  char *tmp = strdup("x");\n'
            "  if (n > 0) { g_first = tmp; }\n"
            "  g_third = n > 1 ? tmp : 0;\n"
            "}\n"
        )
        table = annotations_by_type(events, edges)
        writes = {row["subject"]: row for row in table["WRITE"]}
        self.assertEqual(writes["g_first"]["unconditional"], "false")
        self.assertEqual(writes["g_third"]["write_source"], "")
        alloc = only(table, "ALLOC")
        self.assertEqual(alloc["escaped_to"], "")

    # -- the rulings, end to end -----------------------------------------

    def test_the_return_ruling_eliminates_without_contracts(self) -> None:
        """ESCAPE_RETURN reads the allocation's own annotation, so it holds in
        the contracts-free run too -- that is what makes it a graph fact
        rather than a contract inference."""
        events, edges = self.publish_source(
            "#include <stdlib.h>\n"
            'char *f(void) { return strdup("x"); }\n'
        )
        result = defect.resource_lifetime(events, edges)
        self.assertEqual(result.candidates, ())
        reasons = [
            record["reason"] for record in result.coverage["disqualified_evidence"]
        ]
        self.assertEqual(reasons, ["ESCAPE_RETURN"])

    def test_the_escape_write_ruling_eliminates_without_contracts(self) -> None:
        """Every path from the allocation passes the plain assignment into
        the global, so the allocation cannot leak inside this frame."""
        events, edges = self.publish_source(
            "#include <stdlib.h>\n"
            "char *g_second;\n"
            "char *f(void) {\n"
            '  char *tmp = strdup("x");\n'
            "  g_second = tmp;\n"
            "  return tmp;\n"
            "}\n"
        )
        result = defect.resource_lifetime(events, edges)
        self.assertEqual(result.candidates, ())
        reasons = [
            record["reason"] for record in result.coverage["disqualified_evidence"]
        ]
        self.assertEqual(reasons, ["ESCAPE_TO_SHARED"])


class DispatchTests(unittest.TestCase):
    """`run` and `query_for`: the two ways a caller names a query, and the
    difference between a bad request and an empty answer."""

    def test_run_dispatches_to_the_named_query(self) -> None:
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        events, edges = publish(
            Path(holder.name), "src/unit.cpp",
            "#include <stdlib.h>\n"
            "int leak(void) {\n"
            "  char *buffer = (char *)malloc(256);\n"
            "  if (!buffer) { return -1; }\n"
            "  return 0;\n"
            "}\n",
        )
        for name, function in (
            (defect.RESOURCE_LIFETIME, defect.resource_lifetime),
            (defect.LOCK_ORDER, defect.lock_order),
            (defect.RACE_CONDITION, defect.race_condition),
            (defect.NULL_FLOW, defect.null_flow),
            (defect.USE_AFTER_FREE, defect.use_after_free),
            (defect.DOUBLE_FREE, defect.double_free),
            (defect.TAINT_PATH, defect.taint_path),
            (defect.ERROR_HANDLING, defect.error_handling),
        ):
            self.assertEqual(
                [c.to_dict() for c in defect.run(name, events, edges).candidates],
                [c.to_dict() for c in function(events, edges).candidates],
            )

    def test_an_unknown_query_names_the_known_ones(self) -> None:
        with self.assertRaises(ValueError) as caught:
            defect.run("race", [], [])
        self.assertIn("unknown defect query", str(caught.exception))
        self.assertIn("race_condition", str(caught.exception))

    def test_query_for_maps_a_key_to_its_query(self) -> None:
        self.assertEqual(defect.query_for("4.1"), defect.RESOURCE_LIFETIME)
        self.assertEqual(defect.query_for("4.6"), defect.RESOURCE_LIFETIME)
        self.assertEqual(defect.query_for("1.3"), defect.LOCK_ORDER)
        self.assertEqual(defect.query_for("1.4"), defect.LOCK_ORDER)
        self.assertEqual(defect.query_for("1.1"), defect.RACE_CONDITION)
        self.assertEqual(defect.query_for("3.1"), defect.NULL_FLOW)
        self.assertEqual(defect.query_for("4.2"), defect.DOUBLE_FREE)
        self.assertEqual(defect.query_for("4.3"), defect.USE_AFTER_FREE)
        self.assertEqual(defect.query_for("9.1"), defect.ERROR_HANDLING)

    def test_an_unexpanded_key_is_an_error_rather_than_an_empty_result(self) -> None:
        """A query that ran and found nothing has answered a good question; a
        key with no query has not been asked one. Returning an empty result
        would make the two indistinguishable, and the coverage report's whole
        job is to keep them apart.

        The list is the matrix's remaining gap, and it is asserted rather than
        described: 4.5 needs OPEN/CLOSE, which the stage 2 level 0 list omits,
        and 9.2/9.3/9.4 need AI-side API semantics, cross-method control edges
        and exception handlers respectively. 9.1 left this list in stage 6, when
        the error-handling query landed -- and leaving it is a statement about
        *candidate generation* only: the pattern still cannot be confirmed, and
        `missing_relations()` still names `CHECKS` and `PRODUCES`.
        """
        for key in ("4.5", "9.2", "9.3", "9.4"):
            with self.assertRaises(ValueError) as caught:
                defect.query_for(key)
            self.assertIn("not expanded", str(caught.exception))
        with self.assertRaises(ValueError):
            defect.query_for("99.9")


class CandidateEvidenceTests(unittest.TestCase):
    """The citable form: `E-DEFECT-...` ids, the §15 bundle, and the two
    properties everything downstream depends on -- determinism and the fact
    that a candidate's identity survives reformatting."""

    def publish_source(self, source: str):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        return publish(Path(holder.name), "src/unit.cpp", source)

    def every_candidate(self):
        """One candidate of each of the three shapes, from the fixture and from
        synthetic sources: 4.1 resolved, 4.6 ambiguous, 1.3 resolved, 1.1
        ambiguous. A bundle test that only ever saw a resolved candidate would
        not be testing the interesting half."""
        events, edges, names = fixture_graph(self)
        fixture = [
            *defect.resource_lifetime(events, edges, names=names).candidates,
            *defect.lock_order(events, edges, names=names).candidates,
        ]
        synthetic = (
            "#include <pthread.h>\n"
            "#include <stdlib.h>\n"
            "static int *g_total;\n"
            "void *worker(void *arg) {\n"
            "  g_total = (int *)malloc(64);\n"
            "  return NULL;\n"
            "}\n"
            "int main(void) {\n"
            "  pthread_t handle;\n"
            "  pthread_create(&handle, NULL, worker, NULL);\n"
            "  free(g_total);\n"
            "  return 0;\n"
            "}\n"
        )
        events, edges = self.publish_source(synthetic)
        return [*fixture, *defect.race_condition(events, edges).candidates]

    def test_the_bundle_carries_the_design_doc_members(self) -> None:
        """Design doc §15's shape, member for member. A reader comparing a
        bundle against the document should meet the same block first."""
        candidates = self.every_candidate()
        self.assertTrue(candidates)
        for candidate in candidates:
            bundle = defect.candidate_bundle(
                candidate, evidence_id="E-DEFECT-" + "0" * 24, generation=7
            ).to_dict()
            for member in (
                "defect_type", "defect_key", "subject", "facts", "uncertain_facts",
                "paths", "source_evidence", "missing_evidence",
            ):
                self.assertIn(member, bundle, candidate.defect_key)
            self.assertEqual(bundle["defect_key"], candidate.defect_key)
            self.assertEqual(bundle["defect_type"], candidate.query)
            self.assertEqual(bundle["resolution_status"], candidate.resolution_status)
            self.assertEqual(bundle["candidate_id"], "E-DEFECT-" + "0" * 24)
            self.assertEqual(bundle["generation"], 7)

            # JSON-native: the bundle goes straight into a query result and out
            # through the CLI without a second conversion pass.
            json.dumps(bundle)

    def test_every_candidate_becomes_a_citable_evidence_id(self) -> None:
        candidates = self.every_candidate()
        ids = []
        for candidate in candidates:
            evidence = defect.candidate_evidence(
                candidate, repository=REPO, commit="c0ffee", generation=3
            )
            self.assertEqual(evidence.kind, "DEFECT_CANDIDATE")
            self.assertTrue(evidence.evidence_id.startswith("E-DEFECT-"))
            self.assertEqual(parse_evidence_citations(evidence.evidence_id), [evidence.evidence_id])
            self.assertEqual(evidence.source_id, candidate.anchor)
            self.assertEqual(evidence.relation, candidate.defect_key)
            self.assertEqual(evidence.metadata["resolution_status"], candidate.resolution_status)
            ids.append(evidence.evidence_id)

        # Distinct candidates get distinct ids. The relation carries the defect
        # key precisely so that a 4.1 and a 4.6 candidate anchored on the same
        # event -- which the fixture has -- do not collide.
        self.assertEqual(len(ids), len(set(ids)))
        self.assertGreater(len(ids), len({candidate.anchor for candidate in candidates}) - 1)

    def test_the_candidate_identity_survives_reformatting(self) -> None:
        """Prepending a blank line shifts every line number in the file. Every
        id, anchor and discriminator must be unchanged: a candidate whose name
        moves when the file is reindented cannot be cited, diffed, or
        adjudicated twice."""
        events, edges, names = fixture_graph(self)
        before = [
            *defect.resource_lifetime(events, edges, names=names).candidates,
            *defect.lock_order(events, edges, names=names).candidates,
        ]

        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        copy = Path(holder.name) / "fixture_knowledge"
        shutil.copytree(FIXTURE, copy)
        source = copy / "src" / "resource.cpp"
        source.write_text("\n\n" + source.read_text(encoding="utf-8"), encoding="utf-8")

        database = Path(holder.name) / "shifted.db"
        full_index(copy, database, repository_id_override="fixture")
        with SQLiteStorage(database) as storage:
            repo_id = storage.repository()["repo_id"]
            shifted_names = {
                row["id"]: row["qualified_name"]
                for row in storage.connection.execute(
                    "SELECT id, qualified_name FROM nodes WHERE repo_id = ?", (repo_id,)
                )
            }
            shifted = [
                *defect.resource_lifetime(
                    storage.semantic_events(repo_id), storage.semantic_edges(repo_id),
                    names=shifted_names,
                ).candidates,
                *defect.lock_order(
                    storage.semantic_events(repo_id), storage.semantic_edges(repo_id),
                    names=shifted_names,
                ).candidates,
            ]

        def identity(candidate):
            return (
                candidate.defect_key, candidate.anchor, candidate.discriminator,
                candidate.subject["name"], candidate.resolution_status,
                defect.candidate_evidence(
                    candidate, repository="fixture", commit="c0ffee", generation=1
                ).evidence_id,
            )

        self.assertEqual([identity(c) for c in before], [identity(c) for c in shifted])
        # The spans move, and that is the one thing allowed to: they are
        # citations for a human, not identity.
        self.assertNotEqual(
            [c.source_evidence for c in before], [c.source_evidence for c in shifted]
        )

    def test_the_three_states_are_never_silently_upgraded(self) -> None:
        """A decisive fact the graph could not settle forces the candidate to
        be ambiguous. The `decisive` flag is what makes this checkable: a
        non-decisive uncertainty (MAY_PARALLEL on an inversion) does not cap
        the status, because it is a different question from the one the
        candidate answers."""
        candidates = self.every_candidate()
        self.assertTrue(candidates)
        for candidate in candidates:
            blocking = [
                fact for fact in candidate.uncertain_facts
                if fact["decisive"] and fact["status"] != defect.RESOLVED
            ]
            if blocking:
                self.assertNotEqual(
                    candidate.resolution_status, defect.RESOLVED,
                    f"{candidate.defect_key} resolved with decisive {blocking}",
                )
            for fact in candidate.uncertain_facts:
                self.assertIn(fact["status"], {"ambiguous", "unresolved"})

        # And the distinction is exercised, not just declared: the fixture's
        # inversion is resolved while carrying an undecidable MAY_PARALLEL.
        inversion = [
            candidate for candidate in candidates
            if candidate.defect_key == "1.3" and candidate.resolution_status == defect.RESOLVED
        ]
        self.assertTrue(inversion)
        may_parallel = next(
            fact for fact in inversion[0].uncertain_facts if fact["fact"] == "MAY_PARALLEL"
        )
        self.assertFalse(may_parallel["decisive"])

    def test_source_evidence_is_a_relative_span_and_nothing_else(self) -> None:
        candidates = self.every_candidate()
        for candidate in candidates:
            self.assertTrue(candidate.source_evidence, candidate.defect_key)
            for citation in candidate.source_evidence:
                path, _, span = citation.rpartition(":")
                self.assertTrue(span.count("-") == 1, citation)
                start, _, end = span.partition("-")
                self.assertTrue(start.isdigit() and end.isdigit(), citation)
                self.assertNotIn(":", path)
                self.assertNotIn("\\", path)
                self.assertFalse(Path(path).is_absolute(), citation)

    def test_candidate_evidence_metadata_is_what_ranking_reads(self) -> None:
        """`rank_evidence` orders bundles by four metadata keys. They have to be
        present and of the right type, or ranking silently treats every
        candidate as equal."""
        events, edges, names = fixture_graph(self)
        for candidate in defect.resource_lifetime(events, edges, names=names).candidates:
            metadata = defect.candidate_evidence(
                candidate, repository=REPO, commit="c0ffee", generation=1
            ).metadata
            self.assertIn(metadata["resolution_status"], {"resolved", "ambiguous", "unresolved"})
            self.assertIs(metadata["direct"], True)
            self.assertEqual(metadata["distance"], 0)
            self.assertIs(metadata["boundary_relevant"], False)


class QueryLayerTests(unittest.TestCase):
    """The layer between `defect.py` and a caller: storage read, evidence
    ranking, the citation ids, and the one caveat the query cannot compute.

    `defect.py` itself is a pure function of `(events, edges)` and is covered
    above; everything here is the part that is not pure, so it is tested
    against a real database rather than against the functions directly.
    """

    def query(self, **kwargs) -> GraphQuery:
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        database = Path(holder.name) / "fixture.db"
        full_index(FIXTURE, database, repository_id_override="fixture")
        query = GraphQuery(database, **kwargs)
        self.addCleanup(query.close)
        return query

    def test_candidates_are_positionally_aligned_with_the_evidence(self) -> None:
        """`data["candidates"][i]` and `evidence[i]` must describe the same
        thing. The query layer gets this by ordering candidates *by* the
        ranked list rather than by re-deriving the same order, so the test
        checks the outcome, not the mechanism."""
        result = self.query().get_defect_candidates(defect_type="all")
        for key in ("query_id", "query_type", "graph_generation", "query_time_ms",
                    "returned_evidence_ids", "returned_evidence_count", "bundle_size", "data"):
            self.assertIn(key, result)
        candidates = result["data"]["candidates"]
        self.assertEqual(result["returned_evidence_count"], len(candidates))
        self.assertEqual(
            result["returned_evidence_ids"],
            [candidate["candidate_id"] for candidate in candidates],
        )
        self.assertEqual(result["data"]["defect_keys"], ["1.3", "4.1", "4.2", "4.6"])
        # Coverage is per query, and it holds the denominators the precision
        # number will be read against -- not just the numerator.
        coverage = result["data"]["coverage"]
        self.assertEqual(coverage["resource_lifetime"]["candidates"], 2)
        self.assertEqual(coverage["lock_order"]["lock_points"], 6)
        # Stage 5A's access events join the subject index, so the population
        # the race query considered grew by the ten fixture names that only
        # ever appeared in plain reads and writes.
        self.assertEqual(coverage["race_condition"]["subjects_considered"], 36)
        # The stage 6 queries are in the same result now, and four of them
        # report a population with no candidate: the fixture has one release
        # pair (4.2, resolved), one `free(p); free(p)`-shaped call the
        # composition excludes because the use is the release itself, two
        # `input_to_sink` edges whose ends the taint vocabulary does not name,
        # and no call whose result is stored in a variable at all (9.1's
        # population). A coverage block is where those four different zeroes
        # are told apart.
        self.assertEqual(
            list(coverage), [
                "resource_lifetime", "lock_order", "race_condition",
                "null_flow", "use_after_free", "double_free", "taint_path",
                "error_handling",
            ],
        )
        self.assertEqual(coverage["double_free"]["states"], {"resolved": 1})
        self.assertEqual(
            coverage["use_after_free"]["disqualified"], {"USE_IS_RELEASE": 1}
        )
        self.assertEqual(coverage["use_after_free"]["population"], {"call-composition": 1})
        self.assertEqual(coverage["taint_path"]["population"], {"input_to_sink": 2})
        self.assertEqual(coverage["null_flow"]["candidates"], 0)
        self.assertFalse(result["data"]["truncated"])

    def test_defect_type_accepts_a_key_a_query_name_and_all(self) -> None:
        query = self.query()
        self.assertEqual(query.get_defect_candidates(defect_type="4.1")["data"]["defect_keys"], ["4.1"])
        # One query can serve two keys, so the key form is what filters and
        # the query form is what runs: asking for the query name gets both.
        by_query = query.get_defect_candidates(defect_type="resource_lifetime")
        self.assertEqual(by_query["data"]["defect_keys"], ["4.1", "4.6"])
        self.assertEqual(list(by_query["data"]["coverage"]), ["resource_lifetime"])
        self.assertEqual(query.get_races()["data"]["defect_keys"], [])
        self.assertEqual(
            query.get_lock_order()["data"]["coverage"]["lock_order"]["candidates"], 1
        )

    def test_a_key_filter_is_applied_before_the_candidate_cap(self) -> None:
        """Asking for 4.6 with a cap of one must return the 4.6 candidate.

        The filter used to run after the query had already truncated, so a
        request for a key whose candidates sort late got the *other* key's
        truncated prefix and then filtered it to nothing. On `llama.cpp` that
        made `--type 4.6` return zero candidates while the same database's
        coverage block said thirty existed -- an empty answer and a false one.
        """
        result = self.query().get_defect_candidates(defect_type="4.6", max_candidates=1)
        self.assertEqual(result["data"]["defect_keys"], ["4.6"])
        self.assertEqual(result["returned_evidence_count"], 1)
        # And the flag describes the requested population: one candidate out of
        # one is not a truncation, however many 4.1 candidates exist.
        self.assertFalse(result["data"]["truncated"])

    def test_a_key_with_no_query_is_a_bad_request_not_an_empty_answer(self) -> None:
        """4.5 is in the matrix and has no query. Returning zero candidates
        would make "not implemented" and "ran and found nothing" identical in
        the one report that has to tell them apart."""
        query = self.query()
        with self.assertRaises(ValueError) as caught:
            query.get_defect_candidates(defect_type="4.5")
        self.assertIn("not expanded", str(caught.exception))
        with self.assertRaises(ValueError):
            query.get_defect_candidates(defect_type="9.9")

    def test_semantic_events_carry_no_evidence_of_their_own(self) -> None:
        """Events are the queries' input, not evidence for a defect. An
        `E-CODE-` id on a raw event would let a citation point at a fact that
        asserts nothing."""
        query = self.query()
        result = query.get_semantic_events(event_type="LOCK")
        events = result["data"]["events"]
        self.assertEqual(len(events), 6)
        self.assertEqual(result["evidence"], [])
        self.assertFalse(result["data"]["truncated"])
        self.assertTrue(all(event["event_type"] == "LOCK" for event in events))
        self.assertEqual({event["event_type"] for event in events}, {"LOCK"})
        self.assertEqual(len({event["owner_symbol_id"] for event in events}), 4)

        # The limit is a real cap and the result says when it bit.
        self.assertTrue(query.get_semantic_events(event_type="LOCK", limit=2)["data"]["truncated"])

        # The file filter reads the persisted relative path, and the owner
        # filter accepts either the symbol id or the name that was matched.
        path = events[0]["metadata"]["relative_path"]
        by_file = query.get_semantic_events(event_type="LOCK", file=path)["data"]["events"]
        self.assertEqual([event["event_id"] for event in by_file],
                         [event["event_id"] for event in events])
        owner = events[0]["owner_symbol_id"]
        by_owner = query.get_semantic_events(event_type="LOCK", owner=owner)["data"]["events"]
        self.assertTrue(by_owner)
        self.assertTrue(all(event["owner_symbol_id"] == owner for event in by_owner))

        # Edges are opt-in and only those touching a selected event.
        with_edges = query.get_semantic_events(event_type="LOCK", include_edges=True)
        ids = {event["event_id"] for event in with_edges["data"]["events"]}
        edges = with_edges["data"]["edges"]
        self.assertTrue(edges)
        for edge in edges:
            self.assertTrue(edge["src_event_id"] in ids or edge["dst_event_id"] in ids)
        self.assertNotIn("edges", result["data"])

    def test_an_active_overlay_is_declared_rather_than_silently_ignored(self) -> None:
        """The semantic tables are read from the base snapshot, so an overlay
        makes every candidate a statement about a graph the user is not
        looking at. The gap has to travel with the answer."""
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        database = Path(holder.name) / "fixture.db"
        full_index(FIXTURE, database, repository_id_override="fixture")
        with SQLiteStorage(database) as storage:
            repository = storage.repository()
        overlay = OverlayStore.create(
            Path(holder.name) / "session.db", overlay_type="SESSION",
            repository_id=repository["repo_id"], base_commit="c0ffee",
            base_generation=int(repository["current_generation"]),
        )
        self.addCleanup(overlay.close)
        with GraphQuery(database, session_overlay=overlay) as query:
            result = query.get_defect_candidates(defect_type="all")

        self.assertTrue(result["data"]["candidates"])
        for candidate in result["data"]["candidates"]:
            self.assertTrue(
                any("base snapshot" in line for line in candidate["missing_evidence"]),
                candidate["candidate_id"],
            )
        for coverage in result["data"]["coverage"].values():
            self.assertEqual(coverage["semantic_overlay"], "base_snapshot_only")

        # And with no overlay the caveat is absent. A line that appeared
        # unconditionally would be boilerplate, and a reader would stop
        # treating it as information.
        plain = self.query().get_defect_candidates(defect_type="all")
        self.assertNotIn("semantic_overlay", plain["data"]["coverage"]["lock_order"])
        for candidate in plain["data"]["candidates"]:
            self.assertFalse(
                any("base snapshot" in line for line in candidate["missing_evidence"])
            )

    def test_a_defect_citation_traces_back_to_its_candidate(self) -> None:
        """`E-DEFECT-` ids appear in reports; the loop only closes if the id
        resolves back to the candidate that produced it, without a table."""
        query = self.query()
        result = query.get_defect_candidates(defect_type="all")
        self.assertTrue(result["returned_evidence_ids"])
        for evidence_id in result["returned_evidence_ids"]:
            self.assertEqual(parse_evidence_citations(evidence_id), [evidence_id])
            traced = query.trace_evidence(evidence_id)
            self.assertEqual(traced["returned_evidence_ids"], [evidence_id])
            bundle = traced["bundle"]
            self.assertEqual(bundle["primary_evidence"][0]["kind"], "DEFECT_CANDIDATE")
            self.assertEqual(bundle["primary_evidence"][0]["evidence_id"], evidence_id)
            self.assertEqual(bundle["uncertainties"], [])
            # The candidate's own subject leads the related entities, and its
            # facts follow verbatim -- a leak fact carries `event`/`subject`
            # where a lock-order fact carries `lock`/`side`, so a projection
            # onto a common subset would drop what a reader came for.
            self.assertIn("name", bundle["related_entities"][0])
            facts = bundle["related_entities"][1:]
            self.assertTrue(facts)
            for fact in facts:
                self.assertIn("point", fact)
                self.assertIn("source", fact)

        missing = query.trace_evidence("E-DEFECT-" + "f" * 24)
        self.assertEqual(missing["returned_evidence_ids"], [])
        self.assertEqual(missing["bundle"]["uncertainties"], ["unknown evidence ID"])

    def test_the_parameter_join_assembles_with_the_contracts_and_moves_one_pair(self) -> None:
        """The 5B wiring, against a real database: with the contracts armed
        the query layer assembles the binding table next to them, and the
        composed pair appears in the race query's candidates and coverage.
        The same query with the contracts -- and therefore the join, which is
        deliberately one dial -- switched off sees the same graph with no
        pair. The composed candidate is observable end to end through its
        coverage; the `parameter_binding` provenance rides the defect-layer
        candidate object, which the envelope's bundle deliberately does not
        re-shape."""
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        tree = Path(holder.name) / "binding-repo" / "src"
        tree.mkdir(parents=True)
        (tree / "unit.cpp").write_text(
            "#include <pthread.h>\n"
            "#include <stdlib.h>\n"
            "struct conf { int flags; };\n"
            "static struct conf g_conf;\n"
            "static void consume(struct conf *item) { item->flags = 0; }\n"
            "static void *writer(void *arg) {\n"
            "  consume(&g_conf);\n"
            "  g_conf.flags = 1;\n"
            "  return NULL;\n"
            "}\n"
            "int main(void) {\n"
            "  pthread_t a;\n"
            "  pthread_create(&a, NULL, writer, NULL);\n"
            "  return 0;\n"
            "}\n",
            encoding="utf-8",
        )
        database = Path(holder.name) / "binding.db"
        full_index(tree.parent, database, repository_id_override="binding-repo")
        with GraphQuery(database) as query:
            joined = query.get_defect_candidates(defect_type="1.1")
            candidates = joined["data"]["candidates"]
            self.assertEqual([row["subject"]["name"] for row in candidates], ["g_conf"])
            self.assertEqual(candidates[0]["resolution_status"], "ambiguous")
            coverage = joined["data"]["coverage"]["race_condition"]
            self.assertEqual(coverage["parameter_binding_pairs"], 1)
            self.assertEqual(coverage["parameter_binding_unjoined"], 0)
            self.assertEqual(coverage["parameter_binding_join"]["join_declared"], 1)

            bare = query.get_defect_candidates(defect_type="1.1", use_contracts=False)
            self.assertEqual(bare["data"]["candidates"], [])
            self.assertNotIn("parameter_binding_pairs", bare["data"]["coverage"]["race_condition"])


class CliTests(unittest.TestCase):
    """The command-line surface, which is how stage 4's acceptance runs were
    produced -- so it has to be tested as a command, not only as a function."""

    def run_cli(self, *argv: str) -> dict:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = cli.main([*argv, "--json"])
        self.assertEqual(code, 0)
        return json.loads(buffer.getvalue())

    def test_defect_list_answers_the_falsifiability_question_without_a_database(self) -> None:
        """`--list` is a fact about the matrix and the vocabulary, so it is
        answered before a graph is opened. Pointing it at a database that does
        not exist is what proves that."""
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        missing = str(Path(holder.name) / "no-such.db")
        matrix = self.run_cli("defect", "--list", "--database", missing)
        by_key = {row["key"]: row for row in matrix["patterns"]}
        # The whole matrix, asserted as a set rather than a count: a row that
        # appears or disappears should fail this test by name.
        self.assertEqual(sorted(by_key), [
            "1.1", "1.3", "1.4", "3.1", "4.1", "4.2", "4.3", "4.5", "4.6",
            "9.1", "9.2", "9.3", "9.4",
        ])
        self.assertEqual(matrix["queries"], list(defect.QUERY_NAMES))
        # The pair that is stage 4's answer in checkable form: confirmation
        # cannot run, candidate generation can.
        self.assertFalse(by_key["1.1"]["coverable_now"])
        self.assertTrue(by_key["1.1"]["candidate_coverable_now"])
        # Stage 5A delivered the event side: READ/WRITE are produced (as
        # access events), so `missing_events` is empty while the relation side
        # -- MAY_PARALLEL and friends -- still keeps `coverable_now` false.
        self.assertEqual(by_key["1.1"]["missing_events"], [])
        self.assertEqual(by_key["3.1"]["missing_events"], [])
        # Stage 6 expanded the query without making the pattern confirmable,
        # and `--list` is where that distinction has to survive: the guard test
        # is a reverse seeding of the walker's null arms, not a
        # `DOMINATES_CHECK` relation.
        self.assertTrue(by_key["3.1"]["expanded"])
        self.assertEqual(by_key["3.1"]["query"], "null_flow")
        self.assertFalse(by_key["3.1"]["coverable_now"])
        self.assertEqual(
            by_key["3.1"]["missing_relations"], ["CHECKS", "DOMINATES_CHECK", "RETURNS"]
        )
        # The same pair of facts for the pattern stage 6 expanded last, and the
        # one whose confirmation column is worth reading closely: `CHECKS` and
        # `PRODUCES` are both still missing. `PRODUCES` is the relation the
        # dropped extraction change would have half-delivered, so the row says
        # out loud that the query exists and the pattern is not confirmable.
        self.assertTrue(by_key["9.1"]["expanded"])
        self.assertEqual(by_key["9.1"]["query"], "error_handling")
        self.assertEqual(by_key["9.1"]["missing_events"], [])
        self.assertEqual(by_key["9.1"]["missing_relations"], ["CHECKS", "PRODUCES"])
        self.assertFalse(by_key["9.1"]["coverable_now"])
        self.assertTrue(by_key["9.1"]["candidate_coverable_now"])
        # An unexpanded pattern says so rather than claiming coverage.
        self.assertFalse(by_key["4.5"]["expanded"])
        self.assertIsNone(by_key["4.5"]["query"])

    def test_the_defect_command_prints_the_same_envelope_as_the_query_layer(self) -> None:
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        database = str(Path(holder.name) / "fixture.db")
        full_index(FIXTURE, database, repository_id_override="fixture")
        result = self.run_cli("defect", "--type", "4.1", "--database", database)
        self.assertEqual(result["data"]["defect_keys"], ["4.1"])
        self.assertEqual(result["returned_evidence_count"], 1)
        self.assertEqual(
            result["returned_evidence_ids"],
            [candidate["candidate_id"] for candidate in result["data"]["candidates"]],
        )
        with GraphQuery(database) as query:
            self.assertEqual(
                result["returned_evidence_ids"],
                query.get_defect_candidates(defect_type="4.1")["returned_evidence_ids"],
            )

    def test_events_command_prints_the_raw_layer_and_its_edges(self) -> None:
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        database = str(Path(holder.name) / "fixture.db")
        full_index(FIXTURE, database, repository_id_override="fixture")
        result = self.run_cli("events", "--type", "LOCK", "--edges", "--database", database)
        events = result["data"]["events"]
        self.assertEqual(len(events), 6)
        self.assertEqual(result["evidence"], [])
        ids = {event["event_id"] for event in events}
        for edge in result["data"]["edges"]:
            self.assertTrue(edge["src_event_id"] in ids or edge["dst_event_id"] in ids)

    def test_a_bad_type_is_a_usage_error_and_an_unexpanded_one_is_not(self) -> None:
        """Two different failures. A typo never reaches the graph; a matrix key
        with no query does, and gets the message that says why."""
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                cli.main(["defect", "--type", "4.11"])
        self.assertEqual(caught.exception.code, 2)
        with self.assertRaises(ValueError) as caught:
            cli.run(argparse.Namespace(
                command="defect", database=None, json=False, list=False,
                type="4.5", subject=None, max_hops=64, max_paths=8, max_candidates=20,
                no_contracts=False,
            ))
        self.assertIn("not expanded", str(caught.exception))

    def test_redirected_json_is_utf8_whatever_the_console_encoding_is(self) -> None:
        """The acceptance runs redirect this output into artifacts. Encoding
        them in the console's locale would make the file unreadable to the
        tool that wrote it, on exactly the machines that need it."""
        environment = dict(os.environ, PYTHONPATH="src", PYTHONIOENCODING="cp936")
        completed = subprocess.run(
            [sys.executable, "-m", "provenlattice.cli", "defect", "--list", "--json"],
            cwd=Path(__file__).resolve().parent.parent, env=environment,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
        )
        payload = json.loads(completed.stdout.decode("utf-8"))
        self.assertEqual(len(payload["patterns"]), 13)
        self.assertTrue(any(not row["name"].isascii() for row in payload["patterns"]))


class ReviewSheetTests(unittest.TestCase):
    """The review package stage 4's acceptance runs are recorded from.

    It is a measurement instrument, so the two things it has to get right are
    reproducibility (the same database and seed give the same sheet) and
    arithmetic (precision is computed from the verdicts, never typed).
    """

    def build(self, directory: Path, **overrides) -> dict:
        database = directory / "fixture.db"
        if not database.is_file():
            full_index(FIXTURE, database, repository_id_override="fixture")
        arguments = {
            "databases": {"fixture": database}, "types": ["4.1", "4.6", "1.3", "1.1"],
            "sample": 20, "seed": "test-seed", "workspace": Path(__file__).resolve().parent.parent,
        }
        arguments.update(overrides)
        return build_review.build_review(**arguments)

    def test_the_sheet_records_a_population_and_a_sample_for_every_stratum(self) -> None:
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        package = self.build(Path(holder.name))
        by_stratum = {(item["defect_key"], item["resolution_status"]): item
                      for item in package["availability"]}
        # 4.6 is `resolved` on the fixture since stage 4.5: the lock's subject
        # is a receiver, and the receiver's declaration says `std::mutex`.
        self.assertEqual(set(by_stratum), {("1.3", "resolved"), ("4.1", "resolved"),
                                           ("4.6", "resolved")})
        for item in by_stratum.values():
            self.assertEqual(item["population_N"], 1)
            self.assertEqual(item["sample_n"], 1)
        # 1.1 is requested and has no candidates at all. It appears in no
        # stratum, and the coverage block is where a reader finds out why.
        self.assertNotIn(("1.1", "ambiguous"), by_stratum)
        self.assertEqual(package["coverage"]["fixture"]["race_condition"]["subjects_multi_method"], 0)
        # Every case is citable and carries the blank verdict block, including
        # the two fields that answer the acceptance question.
        for case in package["cases"]:
            self.assertTrue(parse_evidence_citations(case["candidate_id"]))
            self.assertEqual(case["annotation"]["verdict"], "PENDING")
            self.assertIsNone(case["annotation"]["is_true_positive"])
            self.assertIsNone(case["annotation"]["needs_dfg"])
            self.assertTrue(case["missing_evidence"])
            self.assertEqual(case["source_spans"], [item["span"] for item in case["source_snippets"]])

    def test_the_sheet_is_byte_identical_on_a_second_run(self) -> None:
        """Nothing in the package may vary between runs -- not a timestamp,
        not a duration, not a dict order. Two builds of the same database
        under the same seed are compared as bytes, which is the only
        comparison that catches an added `time.time()`."""
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        first, second = Path(holder.name) / "a", Path(holder.name) / "b"
        for output in (first, second):
            output.mkdir()
            package = self.build(Path(holder.name))
            (output / "defect_review.json").write_text(
                json.dumps(package, indent=2, ensure_ascii=False), encoding="utf-8")
            (output / "defect_review.md").write_text(build_review._render(package), encoding="utf-8")
        for name in ("defect_review.json", "defect_review.md"):
            self.assertEqual(
                (first / name).read_bytes(), (second / name).read_bytes(), name
            )

    def test_the_sampling_spreads_across_strata_rather_than_filling_from_one(self) -> None:
        """With three strata and a target of two, a sampler that took the first
        two candidates would report on one family twice and leave the others
        unmeasured."""
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        package = self.build(Path(holder.name), sample=2)
        self.assertEqual(len(package["cases"]), 2)
        strata = {(case["defect_key"], case["resolution_status"]) for case in package["cases"]}
        self.assertEqual(len(strata), 2)

    def test_the_sample_target_is_spent_across_repositories_not_per_repository(self) -> None:
        """The sheet is one adjudication. Sampling per repository would make its
        size a function of how many `--database` flags were passed, which is a
        property of the command line and not of the code being reviewed."""
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        database = Path(holder.name) / "fixture.db"
        full_index(FIXTURE, database, repository_id_override="fixture")
        package = build_review.build_review(
            databases={"alpha": database, "beta": database},
            types=["4.1", "4.6", "1.3", "1.1"], sample=3, seed="test-seed",
            workspace=Path(__file__).resolve().parent.parent,
        )
        # The same database twice, so the two repositories have the same six
        # strata with material: a per-repository sampler returns six cases.
        self.assertEqual(len(package["availability"]), 6)
        self.assertEqual(len(package["cases"]), 3)
        # ... and the quota is spread, not spent on whichever repository sorts
        # first: three cases out of six strata reach both of them.
        self.assertEqual({case["repository"] for case in package["cases"]}, {"alpha", "beta"})
        # Truncation is a per-query fact, so a stratum is not flagged by
        # another query's limit -- and the two truncations are kept apart,
        # because one says the sample is a prefix and the other only says the
        # walk behind the candidates stopped early.
        for source in package["sources"]:
            self.assertEqual(sorted(source["limits"]), ["lock_order", "race_condition",
                                                        "resource_lifetime"])
            for item in source["limits"].values():
                self.assertEqual(item, {"recall_truncated": False, "population_capped": False})
        for item in package["availability"]:
            self.assertFalse(item["population_truncated"])
            self.assertFalse(item["recall_truncated"])
        # The reviewed code is identified by revision, not just by a path: the
        # index records no commit, so the sheet asks the repository itself.
        for source in package["sources"]:
            self.assertRegex(source["repository_commit"], r"^[0-9a-f]{40}$|^unknown$")
            self.assertIn(source["repository_dirty"], (True, False, None))

    def test_dirtiness_is_about_the_reviewed_code_not_the_index(self) -> None:
        """The tool writes its index inside the repository it indexed, so a
        plain `git status` reports every run as dirty and the column stops
        saying anything -- which is the one thing it is there for. The artifact
        directory is excluded; an edit to the code is not."""
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        root = Path(holder.name)
        (root / "unit.c").write_bytes(b"int main(void) { return 0; }\n")
        for argv in (
            ("init",), ("add", "unit.c"),
            ("-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-m", "init"),
        ):
            build_review._git(root, *argv)
        self.assertRegex(build_review._git(root, "rev-parse", "HEAD"), r"^[0-9a-f]{40}$")
        database = root / ".provenlattice" / "codegraph.db"
        database.parent.mkdir()
        database.write_bytes(b"")

        commit, dirty = build_review._repository_state(root, "", database)
        self.assertRegex(commit, r"^[0-9a-f]{40}$")
        self.assertFalse(dirty)

        (root / "unit.c").write_bytes(b"int main(void) { return 1; }\n")
        self.assertTrue(build_review._repository_state(root, "", database)[1])

    def test_sources_come_from_where_the_tree_is_now_not_where_it_was_indexed(self) -> None:
        """The database records the root it indexed, and that path is a
        default rather than an authority: a tree that moved after indexing is
        still the tree the graph describes, and reading the recorded path would
        turn every snippet into a missing one -- the sheet would still look
        complete and would no longer carry the material the verdict needs.
        `--root` says where the sources are, and the row records both paths so
        a reader can see the sheet's snippets came from an override."""
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        directory = Path(holder.name)
        database = directory / "fixture.db"
        if not database.is_file():
            full_index(FIXTURE, database, repository_id_override="fixture")
        # A copy of the database whose recorded root no longer exists: what a
        # moved or renamed checkout leaves behind.
        moved_database = directory / "moved.db"
        shutil.copyfile(database, moved_database)
        gone = directory / "gone"
        # `with sqlite3.connect(...)` commits and does not close, and a
        # connection left open holds a Windows lock on the file.
        with contextlib.closing(sqlite3.connect(moved_database)) as connection:
            connection.execute("UPDATE repositories SET root_path = ?", (str(gone),))
            connection.commit()

        def build(**overrides) -> dict:
            arguments = {
                "databases": {"fixture": moved_database}, "types": ["4.1", "4.6"],
                "sample": 20, "seed": "test-seed",
                "workspace": Path(__file__).resolve().parent.parent,
            }
            arguments.update(overrides)
            return build_review.build_review(**arguments)

        without = build()
        self.assertEqual(len(without["cases"]), 2)
        for case in without["cases"]:
            self.assertFalse(without["sources"][0]["root_available"])
            self.assertTrue(case["source_spans"])
            for snippet in case["source_snippets"]:
                self.assertIsNone(snippet["text"])

        moved = directory / "moved"
        shutil.copytree(Path(FIXTURE) / "src", moved / "src")
        with_override = build(roots={"fixture": moved})
        source = with_override["sources"][0]
        self.assertTrue(source["root_available"])
        self.assertTrue(source["root_override"])
        self.assertEqual(source["root_path"], str(gone))
        self.assertEqual(source["root_path_used"], str(moved))
        self.assertEqual(len(with_override["cases"]), 2)
        for case in with_override["cases"]:
            self.assertEqual(case["source_spans"],
                             [item["span"] for item in case["source_snippets"]])
            for snippet in case["source_snippets"]:
                self.assertIsNotNone(snippet["text"], snippet["span"])
                path, _, line_range = snippet["span"].partition(":")
                first, _, last = line_range.partition("-")
                lines = (moved / path).read_text(encoding="utf-8").splitlines()
                cited = [line.strip() for line in lines[int(first) - 1 : int(last)]]
                cited = [line for line in cited if line]
                self.assertTrue(cited, snippet["span"])
                for line in cited:
                    self.assertIn(line, snippet["text"])

    def test_a_recall_limit_is_not_reported_as_a_population_floor(self) -> None:
        """`max_paths` stopping the walk and this script's own cap both leave
        candidates unfound, but only the second makes the sample a sorted
        prefix of the population. A stratum that reported the first as the
        second would tell its reader that 24 candidates are a floor of a
        million, which is a different and much worse claim."""
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        real = defect.run

        def stopped_early(query, events, edges, **kwargs):
            return dataclasses.replace(real(query, events, edges, **kwargs), truncated=True)

        with mock.patch.object(defect, "run", stopped_early):
            package = self.build(Path(holder.name))
        self.assertTrue(package["availability"])
        for item in package["availability"]:
            self.assertTrue(item["recall_truncated"])
            self.assertFalse(item["population_truncated"])
        for source in package["sources"]:
            for limits in source["limits"].values():
                self.assertEqual(limits, {"recall_truncated": True, "population_capped": False})

    def test_precision_is_computed_from_the_verdicts(self) -> None:
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        package = self.build(Path(holder.name))
        # One true positive, one false positive, one left pending -- and the
        # pending one must not be counted as either. Cases are picked by key
        # rather than by position, so the assertions are about the arithmetic
        # and not about the sampling order.
        by_key = {case["defect_key"]: case for case in package["cases"]}
        by_key["1.3"]["annotation"].update(is_true_positive=True, needs_dfg=True)
        by_key["4.1"]["annotation"].update(is_true_positive=False, reason="already freed")
        sheet = Path(holder.name) / "filled.json"
        sheet.write_text(json.dumps(package, ensure_ascii=False), encoding="utf-8")
        summary = build_review.summarize(sheet)
        overall = summary["overall"]
        self.assertEqual((overall["n"], overall["tp"], overall["fp"], overall["pending"]), (3, 1, 1, 1))
        self.assertEqual(overall["precision"], 0.5)
        self.assertEqual(overall["needs_dfg"], 1)
        judged = {row["defect_key"]: row for row in summary["strata"]}
        self.assertEqual(judged["1.3"]["precision"], 1.0)
        self.assertEqual(judged["4.1"]["precision"], 0.0)
        # A stratum with no verdicts reports "no answer", not zero precision.
        self.assertIsNone(judged["4.6"]["precision"])
        self.assertIn("already freed", build_review._format(summary) + json.dumps(summary))

    def test_a_reviewed_case_with_no_judgment_is_uncertain_not_pending(self) -> None:
        """`REVIEWED` with `is_true_positive` left null means the annotator
        looked and the sheet did not carry what the decision needed. Counting
        that as `pending` would report a finished adjudication as an unfinished
        one, and the baseline would be quoted against a denominator that still
        claims work to do."""
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        package = self.build(Path(holder.name))
        by_key = {case["defect_key"]: case for case in package["cases"]}
        by_key["1.3"]["annotation"].update(
            verdict="REVIEWED", failure_reason="insufficient_context",
            notes="needs the callee's contract",
        )
        by_key["4.1"]["annotation"].update(
            verdict="REVIEWED", is_true_positive=False, failure_reason="identity",
        )
        by_key["4.6"]["annotation"].update(
            verdict="REVIEWED", is_true_positive=False, failure_reason="not_a_reason",
        )
        sheet = Path(holder.name) / "filled.json"
        sheet.write_text(json.dumps(package, ensure_ascii=False), encoding="utf-8")
        summary = build_review.summarize(sheet)
        overall = summary["overall"]
        self.assertEqual(
            (overall["n"], overall["tp"], overall["fp"], overall["uncertain"], overall["pending"]),
            (3, 0, 2, 1, 0),
        )
        # Uncertain cases are outside the denominator, exactly as pending ones
        # are: the two are different findings, and neither is evidence of a
        # wrong candidate.
        self.assertEqual(overall["precision"], 0.0)
        self.assertEqual(overall["failure_reasons"]["insufficient_context"], 1)
        self.assertEqual(overall["failure_reasons"]["identity"], 1)
        # A value outside the taxonomy is counted rather than dropped -- a typo
        # that vanished would silently shrink the tally the sheet exists for.
        self.assertEqual(overall["failure_reasons"]["not_a_reason"], 1)
        rendered = build_review._format(summary)
        self.assertIn("| uncertain |", rendered)
        self.assertIn("not_a_reason *(not in the taxonomy)*", rendered)
        # Every reason the taxonomy names is printed, zeros included: an
        # all-zero row says the verdicts did not use it, which is worth seeing
        # before the number is quoted.
        for failure in build_review.FAILURE_REASONS:
            self.assertIn(f"| {failure} |", rendered)

    def test_the_reading_copy_renders_the_recorded_verdict_not_the_prompt(self) -> None:
        """The JSON is the record and the Markdown is derived from it. A reading
        copy that still asked `is_true_positive` = ____ beside a case that has
        been judged would report a finished adjudication as an open one."""
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        package = self.build(Path(holder.name))
        self.assertIn("`is_true_positive` = ____", build_review._render(package))
        case = package["cases"][0]
        case["annotation"].update(
            verdict="REVIEWED", is_true_positive=False, failure_reason="ownership",
            needs_dfg=False, missing_capability="RAII", confidence=0.9,
            reason="freed by the destructor at tools/x.cpp:12", annotator="a human",
            notes="none",
        )
        for other in package["cases"][1:]:
            other["annotation"].update(verdict="REVIEWED", is_true_positive=False)
        rendered = build_review._render(package)
        # A sheet with every case judged has no blanks left to fill.
        self.assertNotIn("____", rendered)
        self.assertIn("Verdict (recorded in `defect_review.json`):", rendered)
        self.assertIn(f"- `verdict`: `{case['annotation']['verdict']}`", rendered)
        self.assertIn("- `is_true_positive`: `false` — **false positive**", rendered)
        self.assertIn("- `failure_reason`: `ownership`", rendered)
        self.assertIn("- `needs_dfg`: `false`", rendered)
        self.assertIn("- `confidence`: `0.9`", rendered)
        self.assertIn("freed by the destructor at tools/x.cpp:12", rendered)
        self.assertIn("- `annotator`: `a human`", rendered)
        # The reading copy is derived, so rendering it twice is the same copy.
        self.assertEqual(rendered, build_review._render(package))
        # A recorded reason cites code, so it contains backticks. A single
        # delimiter would be closed by the first one and the rest of the line
        # would render as prose, so the delimiter is lengthened and the value
        # padded -- without the padding the content's own backticks merge into
        # the delimiter and no span forms at all.
        case["annotation"]["reason"] = "`free` at tools/x.cpp:12"
        self.assertIn("- `reason`: `` `free` at tools/x.cpp:12 ``",
                      build_review._render(package))

    def test_an_uncertain_verdict_reads_as_uncertain_not_as_false(self) -> None:
        """`REVIEWED` with `is_true_positive` left null is a verdict of its own.
        Rendered as a false positive it would libel the candidate; rendered as
        the blank prompt it would look like work still to do."""
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        package = self.build(Path(holder.name))
        package["cases"][0]["annotation"].update(
            verdict="REVIEWED", failure_reason="insufficient_context",
            reason="the callee is not in this repository",
        )
        for other in package["cases"][1:]:
            other["annotation"].update(verdict="REVIEWED", is_true_positive=True)
        rendered = build_review._render(package)
        self.assertIn("- `is_true_positive`: `null` — **uncertain**", rendered)
        self.assertIn("- `is_true_positive`: `true` — **true positive**", rendered)
        self.assertNotIn("**false positive**", rendered)
        self.assertNotIn("____", rendered)
        self.assertIn("- `failure_reason`: `insufficient_context`", rendered)
        # The uncertain case's unrecorded fields read as `null`, not as blanks:
        # the field was left empty on purpose and the copy says so.
        self.assertIn("- `needs_dfg`: `null`", rendered)
        self.assertIn("- `confidence`: `null`", rendered)

    def test_the_reading_copy_is_refreshed_from_the_record_without_a_database(self) -> None:
        """Filling in verdicts must not require rebuilding the sheet: the build
        path would re-sample and reset every annotation to `PENDING`."""
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        package = self.build(Path(holder.name))
        package["cases"][0]["annotation"].update(
            verdict="REVIEWED", is_true_positive=True, needs_dfg=False,
        )
        sheet = Path(holder.name) / "defect_review.json"
        sheet.write_text(json.dumps(package, indent=2, ensure_ascii=False) + "\n",
                         encoding="utf-8")
        stderr, stdout = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "argv", ["build_review.py", "--render", str(sheet)]), \
                contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(stdout):
            self.assertEqual(build_review.main(), 0)
        copy = sheet.with_suffix(".md")
        self.assertTrue(copy.is_file())
        self.assertEqual(copy.read_text(encoding="utf-8"), build_review._render(package))
        self.assertIn("**true positive**", copy.read_text(encoding="utf-8"))
        # The database is not an input here, and a path that is not a sheet is
        # an error rather than an empty reading copy.
        for argv in (["--render", str(sheet), "--database", "x=y"],
                     ["--render", str(Path(holder.name) / "absent.json")]):
            with mock.patch.object(sys, "argv", ["build_review.py", *argv]), \
                    contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(stdout):
                with self.assertRaises(SystemExit):
                    build_review.main()

    def test_a_rebuild_refuses_to_discard_recorded_verdicts(self) -> None:
        """Re-sampling is cheap and adjudication is not, so the destructive
        command is the one that has to ask."""
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        output = Path(holder.name)
        package = self.build(output)
        package["cases"][0]["annotation"].update(verdict="REVIEWED", is_true_positive=False)
        sheet = output / "defect_review.json"
        sheet.write_text(json.dumps(package, indent=2, ensure_ascii=False) + "\n",
                         encoding="utf-8")
        before = sheet.read_bytes()
        stderr, stdout = io.StringIO(), io.StringIO()
        argv = ["build_review.py", "--database", f"fixture={output / 'fixture.db'}",
                "--type", "4.1", "--output", str(output)]
        with mock.patch.object(sys, "argv", argv), contextlib.redirect_stderr(stderr), \
                contextlib.redirect_stdout(stdout):
            with self.assertRaises(SystemExit):
                build_review.main()
        self.assertIn("recorded verdict", stderr.getvalue())
        self.assertEqual(sheet.read_bytes(), before)
        # `--overwrite` is the way to say it on purpose.
        with mock.patch.object(sys, "argv", argv + ["--overwrite"]), \
                contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(stdout):
            self.assertEqual(build_review.main(), 0)
        self.assertNotEqual(sheet.read_bytes(), before)
        self.assertEqual(
            json.loads(sheet.read_text(encoding="utf-8"))["cases"][0]["annotation"]["verdict"],
            "PENDING",
        )


class Stage6QueryTests(unittest.TestCase):
    """The four stage-6 queries: null flow, use after free, double free, taint.

    Every fixture here is spelled in the storage the rule actually reads. That
    is not a stylistic choice. `ACCESS_STORAGES` has no `local`, so a local
    `p = NULL` or `p = malloc(n)` leaves no WRITE point at all -- a fixture
    written with one would assert about a point the query never saw. The two
    cases that are blind anyway (`uaf_local_nulled`, `df_local_nulled`) are
    pinned as candidates *because* of it, with the reason, so the limit is a
    test result rather than a footnote.

    The three-state vocabulary is the graph's, as everywhere in this file, and
    the middle state is where most of these candidates land: a WRITE has no
    position in the control-flow graph, so a flow edge sourced at one has
    nothing to search from and no elimination rule may fire on that. The
    fixtures below pin both directions of that rule, because reading it as
    "unreachable" would empty the corpus's candidate list.
    """

    def publish_source(self, source: str):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        return publish(Path(holder.name), "src/unit.cpp", source)

    def indexed(self, source: str) -> GraphQuery:
        """A real tree through `full_index`, for the one query that needs it.

        The taint query resolves both of its ends through `raw_references`, and
        the in-memory publisher has no reference table -- a test that handed the
        query a hand-made one would be testing the hand-made table. Everything
        else runs in memory, where the point record is what a database row
        materialises to anyway.
        """
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        root = Path(holder.name) / "tree"
        (root / "src").mkdir(parents=True)
        (root / "src" / "unit.cpp").write_text(source, encoding="utf-8")
        database = Path(holder.name) / "index.db"
        full_index(root, database, repository_id_override=REPO)
        query = GraphQuery(database)
        self.addCleanup(query.close)
        return query

    @staticmethod
    def facts(result) -> list[str]:
        return [
            fact["fact"] for candidate in result.candidates
            for fact in candidate.uncertain_facts
        ]

    @staticmethod
    def reasons(result) -> list[str]:
        return [record["reason"] for record in result.coverage["disqualified_evidence"]]

    # -- 3.1 null flow ----------------------------------------------------

    def test_null_flow_calls_a_write_source_unknown_rather_than_unreachable(self) -> None:
        """`g_p = NULL; *g_p` is one flow edge and one candidate.

        It is `ambiguous`, and the candidate's own facts say why: the definition
        is a WRITE, a WRITE is a sink in the control-flow graph, and the query
        answers "no position to search from" with `unknown` instead of
        eliminating. On the corpora this is not a corner: 3,454 of redis-50's
        3,491 `input_to_sink` edges and *all* of its `null_to_deref` edges are
        write-sourced, so a rule that read the flag's `false` as "unreachable"
        would return nothing at all.
        """
        events, edges = self.publish_source(
            "#include <stdlib.h>\n"
            "char *g_p;\n"
            "void f(void) {\n"
            "  g_p = NULL;\n"
            "  *g_p = 1;\n"
            "}\n"
        )
        result = defect.null_flow(events, edges)
        self.assertEqual(result.coverage["population"], {"null_to_deref": 1})
        self.assertEqual(result.coverage["disqualified"], {})
        self.assertEqual(len(result.candidates), 1)
        candidate = result.candidates[0]
        self.assertEqual(candidate.defect_key, defect.NULL_FLOW_KEY)
        self.assertEqual(candidate.resolution_status, defect.AMBIGUOUS)
        self.assertEqual(candidate.confidence, 0.3)
        self.assertEqual(self.facts(result), ["REACH_UNKNOWN"])
        self.assertEqual(candidate.metadata["reach"], defect.REACH_UNKNOWN)
        self.assertEqual(result.coverage["states"], {"ambiguous": 1})
        # The missing evidence names what would settle it, and it is not a
        # relation the graph is missing -- it is an API contract the AI side
        # holds, which is why 3.1 stays unconfirmable after this stage.
        self.assertEqual(len(candidate.missing_evidence), 2)
        self.assertIn("return NULL", candidate.missing_evidence[0])

    def test_null_flow_eliminates_the_guarded_dereference_and_keeps_a_partial_one(self) -> None:
        """The guard test reads the null arm, not the check's existence.

        `if (g_p == NULL) { return; }` puts the dereference out of the null
        arm's reach, so it is the checked pattern and is eliminated. A check
        that returns to the same path (`report()` then fall through) leaves the
        null arm reaching the dereference, so the candidate survives -- at
        `ambiguous`, carrying `PARTIAL_GUARD`, which is the matrix's own
        "part of the path is unchecked" case rather than a defect claim.
        """
        events, edges = self.publish_source(
            "#include <stdlib.h>\n"
            "char *g_p;\n"
            "void f(void) {\n"
            "  g_p = NULL;\n"
            "  if (g_p == NULL) { return; }\n"
            "  *g_p = 1;\n"
            "}\n"
        )
        guarded = defect.null_flow(events, edges)
        self.assertEqual(guarded.candidates, ())
        self.assertEqual(guarded.coverage["population"], {"null_to_deref": 1})
        self.assertEqual(self.reasons(guarded), ["GUARDED"])

        events, edges = self.publish_source(
            "#include <stdlib.h>\n"
            "char *g_p;\n"
            "void report(void);\n"
            "void f(void) {\n"
            "  g_p = NULL;\n"
            "  if (g_p == NULL) { report(); }\n"
            "  *g_p = 1;\n"
            "}\n"
        )
        partial = defect.null_flow(events, edges)
        self.assertEqual(len(partial.candidates), 1)
        self.assertEqual(partial.candidates[0].resolution_status, defect.AMBIGUOUS)
        self.assertIn("PARTIAL_GUARD", self.facts(partial))
        self.assertEqual(partial.coverage["disqualified"], {})

    def test_null_flow_reads_the_allocation_and_the_alias_chain(self) -> None:
        """Two sources and one path, both measured.

        The allocation source is the more common shape in C -- `p = malloc(n)`
        with the failure path never checked -- and on llama.cpp-69 it is 618 of
        626 edges. It is the only source that reaches `resolved`, because the
        definition has a position and no check on the identity exists.

        The alias chain is the `_root_def` folding: `g_p = NULL; g_q = g_p;
        *g_q` is one `null_to_deref` edge, and the candidate is `ambiguous`
        because the value travelled through a copy the annotation ties, not
        through the object the definition named.
        """
        events, edges = self.publish_source(
            "#include <stdlib.h>\n"
            "void f(char *p) {\n"
            "  p = malloc(8);\n"
            "  *p = 1;\n"
            "}\n"
        )
        allocated = defect.null_flow(events, edges)
        self.assertEqual(len(allocated.candidates), 1)
        candidate = allocated.candidates[0]
        self.assertEqual(candidate.resolution_status, defect.RESOLVED)
        self.assertEqual(candidate.confidence, 0.5)
        self.assertEqual(candidate.uncertain_facts, ())
        self.assertEqual(allocated.coverage["population"], {"alloc_to_deref": 1})
        self.assertEqual(candidate.metadata["reach"], defect.REACHABLE)
        self.assertEqual(candidate.metadata["flow_class"], "alloc_to_deref")

        events, edges = self.publish_source(
            "#include <stdlib.h>\n"
            "char *g_p;\n"
            "char *g_q;\n"
            "void f(void) {\n"
            "  g_p = NULL;\n"
            "  g_q = g_p;\n"
            "  *g_q = 1;\n"
            "}\n"
        )
        aliased = defect.null_flow(events, edges)
        self.assertEqual(len(aliased.candidates), 1)
        self.assertEqual(aliased.candidates[0].resolution_status, defect.AMBIGUOUS)
        self.assertIn("ALIAS_PATH", self.facts(aliased))
        self.assertEqual(
            aliased.candidates[0].metadata["definition"]["via_copies"],
            list(aliased.candidates[0].metadata["definition"]["via"]),
        )

    def test_null_flow_eliminates_only_what_the_definition_cannot_reach(self) -> None:
        """Three shapes that differ only in where the definition sits.

        An ALLOC on an arm that returns cannot reach a dereference on the other
        arm, so the pair is eliminated as disjoint. An ALLOC *before* the branch
        reaches it either way -- the def dominates the branch, so both arms
        leave it -- and that one is kept, which is the counterexample to reading
        `p = malloc(); if (e) return; *p` as two paths. And a WRITE source is
        neither: it has no position, so it stays.

        Getting this wrong in either direction is expensive: the first shape is
        a false positive, and the third is 52 of redis-50's 52 `null_to_deref`
        edges.
        """
        events, edges = self.publish_source(
            "#include <stdlib.h>\n"
            "void f(int e) {\n"
            "  char *p;\n"
            "  if (e) { p = malloc(8); return; }\n"
            "  *p = 1;\n"
            "}\n"
        )
        disjoint = defect.null_flow(events, edges)
        self.assertEqual(disjoint.candidates, ())
        self.assertEqual(self.reasons(disjoint), ["PATH_DISJOINT"])

        events, edges = self.publish_source(
            "#include <stdlib.h>\n"
            "void f(int e) {\n"
            "  char *p = malloc(8);\n"
            "  if (e) { return; }\n"
            "  *p = 1;\n"
            "}\n"
        )
        dominating = defect.null_flow(events, edges)
        self.assertEqual(len(dominating.candidates), 1)
        self.assertEqual(dominating.candidates[0].resolution_status, defect.RESOLVED)
        self.assertEqual(dominating.coverage["disqualified"], {})

    # -- 4.3 use after free -----------------------------------------------

    def test_use_after_free_reads_the_edge_and_eliminates_the_legal_uses(self) -> None:
        """The direct dereference, the comparison, and the escape.

        `free(p); *p = 1` is the pattern. `free(p); if (p == NULL)` is not: a
        comparison reads the pointer value, and reading a dangling value is
        legal even though the value is unusable -- the graph separates those two
        readings for a CHECK and does not for a DEREFERENCE.

        `return p` is `ambiguous` rather than eliminated, because handing a
        dangling pointer back is a defect only if the caller uses it, and that
        question is the AI's.
        """
        events, edges = self.publish_source(
            "#include <stdlib.h>\n"
            "void f(char *p) {\n"
            "  free(p);\n"
            "  *p = 1;\n"
            "}\n"
        )
        direct = defect.use_after_free(events, edges)
        self.assertEqual(len(direct.candidates), 1)
        candidate = direct.candidates[0]
        self.assertEqual(candidate.defect_key, defect.USE_AFTER_FREE_KEY)
        self.assertEqual(candidate.resolution_status, defect.RESOLVED)
        self.assertEqual(candidate.metadata["use_from"], "flow-edge")
        self.assertEqual(candidate.metadata["release"]["subject"], "p")
        self.assertEqual(direct.coverage["population"], {"edge": 1})

        events, edges = self.publish_source(
            "#include <stdlib.h>\n"
            "void f(char *p) {\n"
            "  free(p);\n"
            "  if (p == NULL) { return; }\n"
            "}\n"
        )
        compared = defect.use_after_free(events, edges)
        self.assertEqual(compared.candidates, ())
        self.assertEqual(self.reasons(compared), ["USE_READS_POINTER_VALUE"])

        events, edges = self.publish_source(
            "#include <stdlib.h>\n"
            "char *f(char *p) {\n"
            "  free(p);\n"
            "  return p;\n"
            "}\n"
        )
        escaped = defect.use_after_free(events, edges)
        self.assertEqual(len(escaped.candidates), 1)
        self.assertEqual(escaped.candidates[0].resolution_status, defect.AMBIGUOUS)
        self.assertEqual(self.facts(escaped), ["ESCAPING_USE"])

    def test_use_after_free_eliminates_the_path_disjoint_edge(self) -> None:
        """`free_to_use` is minted in source order and is path-blind.

        `live_free` is never cleared, so a release on one arm pairs with a use
        on the other, and on the acceptance corpora that is nearly all of them:
        11 of 11 on redis-50, 46 of 47 on llama.cpp-69, and the two sampled by
        hand were `free(x); return;` cleanup arms. Reachability is not a
        refinement here; it is what separates the flow class from a UAF.
        """
        events, edges = self.publish_source(
            "#include <stdlib.h>\n"
            "void f(int e, char *p) {\n"
            "  if (e) {\n"
            "    free(p);\n"
            "  } else {\n"
            "    *p = 1;\n"
            "  }\n"
            "}\n"
        )
        result = defect.use_after_free(events, edges)
        self.assertEqual(result.candidates, ())
        self.assertEqual(result.coverage["population"], {"edge": 1})
        self.assertEqual(self.reasons(result), ["PATH_DISJOINT"])

    def test_use_after_free_composes_the_call_the_dfg_mints_no_edge_for(self) -> None:
        """`free(p); sink(p)` is stage 3's registered gap, closed at query time.

        A CALL is not a USE event, so the release kills the live definition and
        the argument path finds nothing -- 5B mints no edge. The query composes
        the pair itself, which is why the candidate is always `ambiguous`:
        `_root_identifier` strips `&`, so `f(p)` and `f(&p)` are one fact here,
        and one passes the object out while the other only reads it. On
        redis-50 that ambiguity is the bulk of the result rather than a corner.

        The intervening redefinition excludes the idiom, and the exclusion is
        what the next two fixtures measure: `p = NULL` between the release and
        the call is a definition of the identity, so the call is of a new value.
        """
        events, edges = self.publish_source(
            "#include <stdlib.h>\n"
            "void sink(char *p);\n"
            "void f(char *p) {\n"
            "  free(p);\n"
            "  sink(p);\n"
            "}\n"
        )
        composed = defect.use_after_free(events, edges)
        self.assertEqual(len(composed.candidates), 1)
        candidate = composed.candidates[0]
        self.assertEqual(candidate.resolution_status, defect.AMBIGUOUS)
        self.assertEqual(candidate.metadata["use_from"], "call-composition")
        self.assertEqual(self.facts(composed), ["USE_KIND_UNKNOWN"])
        self.assertEqual(composed.coverage["population"], {"call-composition": 1})

        events, edges = self.publish_source(
            "#include <stdlib.h>\n"
            "void sink(char *p);\n"
            "void f(char *p) {\n"
            "  free(p);\n"
            "  p = NULL;\n"
            "  sink(p);\n"
            "}\n"
        )
        nulled = defect.use_after_free(events, edges)
        self.assertEqual(nulled.candidates, ())
        self.assertEqual(self.reasons(nulled), ["CALL_INTERVENING_DEF"])

    def test_use_after_free_is_blind_to_a_local_redefinition(self) -> None:
        """The registered blindness, measured in both directions.

        The same three lines on a *parameter* are eliminated above. On a local
        they produce a candidate, because `ACCESS_STORAGES` has no `local`: the
        intervening `p = NULL` leaves no WRITE point, so the composition sees no
        redefinition and cannot apply the rule. Two fixtures, one difference in
        the declaration, opposite outcomes.

        This is a graph gap and not a rule that could be tightened here -- there
        is no fact to tighten it against. It is pinned rather than noted so that
        closing the gap fails this test loudly instead of quietly improving a
        number nobody is watching.
        """
        events, edges = self.publish_source(
            "#include <stdlib.h>\n"
            "void sink(char *p);\n"
            "void f(void) {\n"
            "  char *p = malloc(8);\n"
            "  free(p);\n"
            "  p = NULL;\n"
            "  sink(p);\n"
            "}\n"
        )
        result = defect.use_after_free(events, edges)
        self.assertEqual(len(result.candidates), 1)
        self.assertEqual(result.candidates[0].resolution_status, defect.AMBIGUOUS)
        self.assertEqual(result.coverage["population"], {"call-composition": 1})
        self.assertEqual(result.coverage["disqualified"], {})

        # The same source with the redefinition written as a plain call: the
        # candidate is right here, and it is the same candidate. A local
        # redefinition the graph cannot see makes these two sources one fact.
        events, edges = self.publish_source(
            "#include <stdlib.h>\n"
            "void sink(char *p);\n"
            "void f(void) {\n"
            "  char *p = malloc(8);\n"
            "  free(p);\n"
            "  sink(p);\n"
            "}\n"
        )
        plain = defect.use_after_free(events, edges)
        self.assertEqual(len(plain.candidates), 1)
        self.assertEqual(
            plain.candidates[0].discriminator.split("|")[-2:],
            result.candidates[0].discriminator.split("|")[-2:],
        )

    def test_use_after_free_pairs_through_the_alias_annotation(self) -> None:
        """`g_q = g_p; free(g_p); sink(g_q)` is one candidate, not none.

        The copy annotation ties the two spellings, and the closure has to be
        walked to find it. This fixture is why the closure is tested at all: on
        both acceptance corpora no release pair is joined by the annotation, so
        a closure that silently returned the bare root changed every number by
        nothing. The first version of `_alias_closure` took the owner-keyed
        table and looked the owner up itself while both callers passed the group
        they had already looked up -- so it never fired, and only a fixture
        could say so.
        """
        events, edges = self.publish_source(
            "#include <stdlib.h>\n"
            "void sink(char *p);\n"
            "char *g_p;\n"
            "char *g_q;\n"
            "void f(void) {\n"
            "  g_q = g_p;\n"
            "  free(g_p);\n"
            "  sink(g_q);\n"
            "}\n"
        )
        result = defect.use_after_free(events, edges)
        self.assertEqual(len(result.candidates), 1)
        candidate = result.candidates[0]
        self.assertEqual(candidate.resolution_status, defect.AMBIGUOUS)
        self.assertEqual(candidate.metadata["release"]["subject"], "g_p")
        self.assertEqual(candidate.subject["name"], "g_q")

    # -- 4.2 double free --------------------------------------------------

    def test_double_free_pairs_two_releases_of_one_identity(self) -> None:
        """The positive case, and both halves of the population count.

        `same-owner-pair` is every pair of releases in one method; `same-
        identity-pair` is the pairs that survive the identity test. The gap
        between them is the member path: `free(n->value); free(n)` is two
        objects sharing a root, and `MEMBER_MISMATCH` is what says so.
        """
        events, edges = self.publish_source(
            "#include <stdlib.h>\n"
            "void f(char *p) {\n"
            "  free(p);\n"
            "  free(p);\n"
            "}\n"
        )
        result = defect.double_free(events, edges)
        self.assertEqual(len(result.candidates), 1)
        candidate = result.candidates[0]
        self.assertEqual(candidate.defect_key, defect.DOUBLE_FREE_KEY)
        self.assertEqual(candidate.resolution_status, defect.RESOLVED)
        self.assertEqual(candidate.metadata["first_release"]["subject"], "p")
        self.assertEqual(candidate.metadata["second_release"]["subject"], "p")
        self.assertEqual(
            result.coverage["population"],
            {"same-identity-pair": 1, "same-owner-pair": 1},
        )
        self.assertEqual(candidate.metadata["reach"], defect.REACHABLE)

        events, edges = self.publish_source(
            "#include <stdlib.h>\n"
            "struct node { char *value; };\n"
            "void f(struct node *n) {\n"
            "  free(n->value);\n"
            "  free(n);\n"
            "}\n"
        )
        member = defect.double_free(events, edges)
        self.assertEqual(member.candidates, ())
        self.assertEqual(self.reasons(member), ["MEMBER_MISMATCH"])
        self.assertEqual(
            member.coverage["population"],
            {"same-identity-pair": 1, "same-owner-pair": 1},
        )

    def test_double_free_eliminates_a_redefinition_and_a_mutually_exclusive_arm(self) -> None:
        """One rule catches both legal shapes of a second release.

        `free(p); p = malloc(n); free(p)` releases two different values, and
        `free(p); p = NULL; free(p)` releases null -- defined behaviour, since
        the repository's convention of nulling after a release is exactly what
        the rule reads. Both are one intervening definition, so both are one
        elimination.

        The mutually exclusive arms are the matrix's Disqualifying column
        without a branch flag anywhere: two arms have no control path between
        them, so the reachability requirement already excludes the pair.
        """
        for source in (
            "#include <stdlib.h>\n"
            "void f(char *p) {\n"
            "  free(p);\n"
            "  p = malloc(16);\n"
            "  free(p);\n"
            "}\n",
            "#include <stdlib.h>\n"
            "void f(char *p) {\n"
            "  free(p);\n"
            "  p = NULL;\n"
            "  free(p);\n"
            "}\n",
        ):
            events, edges = self.publish_source(source)
            result = defect.double_free(events, edges)
            self.assertEqual(result.candidates, ())
            self.assertEqual(self.reasons(result), ["INTERVENING_DEF"])

        events, edges = self.publish_source(
            "#include <stdlib.h>\n"
            "void f(int e, char *p) {\n"
            "  if (e) { free(p); } else { free(p); }\n"
            "}\n"
        )
        arms = defect.double_free(events, edges)
        self.assertEqual(arms.candidates, ())
        self.assertEqual(self.reasons(arms), ["PATH_DISJOINT"])

    def test_double_free_is_blind_to_a_local_redefinition(self) -> None:
        """The same blindness as the UAF composition, on the other query.

        `free(p); p = NULL; free(p)` on a parameter is eliminated above. On a
        local the nulling is invisible, so the pair is reported `resolved` --
        the strongest status the query has, on the shape the matrix calls a
        guard. One gap in the access layer, two queries, and this is the worse
        of the two readings.
        """
        events, edges = self.publish_source(
            "#include <stdlib.h>\n"
            "void f(void) {\n"
            "  char *p = malloc(8);\n"
            "  free(p);\n"
            "  p = NULL;\n"
            "  free(p);\n"
            "}\n"
        )
        result = defect.double_free(events, edges)
        self.assertEqual(len(result.candidates), 1)
        self.assertEqual(result.candidates[0].resolution_status, defect.RESOLVED)
        self.assertEqual(result.coverage["disqualified"], {})

    def test_double_free_pairs_through_the_alias_annotation(self) -> None:
        """`g_q = g_p; free(g_p); free(g_q)` is a candidate, at `ambiguous`.

        The same closure the UAF composition walks, on the release pairing: the
        pair is joined by a same-statement copy fact, which is a candidate and
        not an identity, so the status is capped there rather than eliminated.
        """
        events, edges = self.publish_source(
            "#include <stdlib.h>\n"
            "char *g_p;\n"
            "char *g_q;\n"
            "void f(void) {\n"
            "  g_q = g_p;\n"
            "  free(g_p);\n"
            "  free(g_q);\n"
            "}\n"
        )
        result = defect.double_free(events, edges)
        self.assertEqual(len(result.candidates), 1)
        candidate = result.candidates[0]
        self.assertEqual(candidate.resolution_status, defect.AMBIGUOUS)
        self.assertEqual(self.facts(result), ["ALIAS_PAIR"])
        self.assertEqual(candidate.metadata["first_release"]["subject"], "g_p")
        self.assertEqual(candidate.metadata["second_release"]["subject"], "g_q")

    # -- taint path -------------------------------------------------------

    def test_taint_path_walks_a_declared_source_to_a_declared_sink(self) -> None:
        """Both ends are callee names, and neither is in the event.

        A CALL point carries the node type (`call_expression`) and the first
        argument root, so both ends are resolved through `raw_references` by
        `(file_id, owner_symbol_id, start_line)`. This test runs the real
        pipeline for that reason -- the reference table is what the query reads,
        and a synthesised one would prove nothing about it.

        The defect key is `taint`, which is a name and not a matrix number, and
        the candidate says so: `defect_patterns.toml` has no row for it and the
        query is addressable only by its query name.
        """
        query = self.indexed(
            "#include <stdlib.h>\n"
            "#include <stdio.h>\n"
            "char *g_p;\n"
            "void f(void) {\n"
            '  g_p = getenv("HOME");\n'
            "  printf(g_p);\n"
            "}\n"
        )
        data = query.get_defect_candidates(defect_type="taint_path")["data"]
        self.assertEqual(data["defect_keys"], [defect.TAINT_KEY])
        self.assertEqual(len(data["candidates"]), 1)
        candidate = data["candidates"][0]
        self.assertEqual(candidate["defect_type"], defect.TAINT_PATH)
        self.assertEqual(candidate["resolution_status"], defect.RESOLVED)
        self.assertEqual(candidate["confidence"], 0.3)
        self.assertEqual(candidate["subject"]["identity"], "global:g_p")
        self.assertEqual(
            [fact["event"] for fact in candidate["facts"]], ["WRITE", "CALL"]
        )
        self.assertEqual(
            candidate["source_evidence"], ["src/unit.cpp:5-5", "src/unit.cpp:6-6"]
        )
        # No sanitiser model, and the bundle says so rather than a footnote
        # somewhere else: this query's confidence band is the lowest in the file
        # for exactly this reason.
        self.assertTrue(
            any("sanitiser" in item for item in candidate["missing_evidence"]),
            candidate["missing_evidence"],
        )
        coverage = data["coverage"]["taint_path"]
        self.assertEqual(
            coverage["population"],
            {"input_to_sink": 1, "sink-side": 1, "source-side": 1},
        )
        self.assertEqual(coverage["sources_declared"], ["fgets", "fread", "getenv", "read"])
        self.assertEqual(coverage["sinks_declared"], ["memcpy", "printf", "system"])

    def test_taint_path_downgrades_on_a_check_rather_than_eliminating(self) -> None:
        """A check is not a sanitiser, and the graph cannot tell them apart.

        `if (g_p == NULL) { return; }` between the source and the sink makes the
        candidate `ambiguous` and leaves it in the list. Eliminating on a check
        would be the query claiming a validation it never modelled -- and this
        is the shape that would otherwise be the query's most common false
        positive, since a null check on a `getenv` result is the idiom.
        """
        query = self.indexed(
            "#include <stdlib.h>\n"
            "#include <stdio.h>\n"
            "char *g_p;\n"
            "void f(void) {\n"
            '  g_p = getenv("HOME");\n'
            "  if (g_p == NULL) { return; }\n"
            "  printf(g_p);\n"
            "}\n"
        )
        data = query.get_defect_candidates(defect_type="taint_path")["data"]
        self.assertEqual(len(data["candidates"]), 1)
        candidate = data["candidates"][0]
        self.assertEqual(candidate["resolution_status"], defect.AMBIGUOUS)
        self.assertEqual(candidate["confidence"], 0.2)
        self.assertEqual(
            [fact["fact"] for fact in candidate["uncertain_facts"]], ["CHECK_ON_PATH"]
        )
        # A downgrade is not an elimination: the population still counts it and
        # the disqualified block is empty.
        self.assertEqual(data["coverage"]["taint_path"]["disqualified"], {})

    def test_taint_path_counts_an_undeclared_name_as_population(self) -> None:
        """An edge whose ends are not declared is not an eliminated candidate.

        Both fixtures produce zero candidates, and they are different zeroes:
        `malloc` reaching `printf` is an `input_to_sink` edge the vocabulary
        does not name, so the population counts it and no rule fires;
        `getenv` with nothing to reach produces no edge at all, so the
        population is empty. A coverage block that reported these the same way
        would make "the vocabulary is narrow" and "the corpus has no flow"
        indistinguishable.
        """
        query = self.indexed(
            "#include <stdlib.h>\n"
            "#include <stdio.h>\n"
            "char *g_p;\n"
            "void f(void) {\n"
            "  g_p = malloc(8);\n"
            "  printf(g_p);\n"
            "}\n"
        )
        data = query.get_defect_candidates(defect_type="taint_path")["data"]
        self.assertEqual(data["candidates"], [])
        self.assertEqual(data["defect_keys"], [])
        coverage = data["coverage"]["taint_path"]
        self.assertEqual(coverage["population"], {"input_to_sink": 1})
        self.assertNotIn("source-side", coverage["population"])
        self.assertEqual(coverage["disqualified"], {})

        query = self.indexed(
            "#include <stdlib.h>\n"
            "char *g_p;\n"
            "char *g_q;\n"
            "void f(void) {\n"
            '  g_p = getenv("HOME");\n'
            "  g_q = g_p;\n"
            "}\n"
        )
        data = query.get_defect_candidates(defect_type="taint_path")["data"]
        self.assertEqual(data["candidates"], [])
        self.assertEqual(data["coverage"]["taint_path"]["population"], {})

    def test_the_taint_table_is_auditable_or_it_does_not_load(self) -> None:
        """Every refusal the loader has, and the shipped table against them.

        An entry that creates findings without a citation is folklore with a
        head start, and the two invariants are the ones that keep a candidate
        checkable: the argument index has to be the declared position. A
        variadic sink cannot prove that for any position but the first (a
        literal format and a rooted format have the same argument count), and a
        non-variadic sink cannot declare a position its arity does not have.
        """
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        path = Path(holder.name) / "taint.toml"

        # A missing table is an empty one rather than an error: the query runs
        # and finds nothing, which is a different report from a crash.
        absent = defect.load_taint(Path(holder.name) / "absent.toml")
        self.assertEqual(absent.sources, {})
        self.assertEqual(absent.sinks, {})

        def load(body: str):
            path.write_text(body, encoding="utf-8")
            return defect.load_taint(path)

        with self.assertRaises(ValueError) as caught:
            load('[[source]]\nname = "getenv"\nspellings = ["getenv"]\n')
        self.assertIn("has no evidence", str(caught.exception))

        with self.assertRaises(ValueError) as caught:
            load(
                '[[source]]\nname = "getenv"\nspellings = ["printf"]\n'
                'evidence = "x"\n'
                '[[sink]]\nname = "printf"\nspellings = ["printf"]\n'
                'ordinals = [0]\narity = 1\nevidence = "x"\n'
            )
        self.assertIn("declared twice", str(caught.exception))

        with self.assertRaises(ValueError) as caught:
            load(
                '[[sink]]\nname = "printf"\nspellings = ["printf"]\n'
                'ordinals = [1]\narity = 1\nvariadic = true\nevidence = "x"\n'
            )
        self.assertIn("can only declare position 0", str(caught.exception))

        with self.assertRaises(ValueError) as caught:
            load(
                '[[sink]]\nname = "memcpy"\nspellings = ["memcpy"]\n'
                'ordinals = [2]\narity = 2\nevidence = "x"\n'
            )
        self.assertIn("beyond arity", str(caught.exception))

        with self.assertRaises(ValueError) as caught:
            load('sources = []\n')
        self.assertIn("unknown top-level keys", str(caught.exception))

        # And the shipped table, read as the query reads it. The mapping is
        # spelling to *name*, so a table that declares two spellings for one
        # function has one name and two keys -- `std.getenv` is the qualified
        # spelling of `getenv`, not a second source.
        vocabulary = defect.load_taint()
        self.assertEqual(vocabulary.path, str(defect.TAINT_FILE))
        self.assertEqual(
            sorted(set(vocabulary.sources.values())),
            ["fgets", "fread", "getenv", "read"],
        )
        self.assertEqual(vocabulary.source_name("std.getenv"), "getenv")
        self.assertEqual(vocabulary.source_name("getenv"), "getenv")
        self.assertEqual(vocabulary.source_name("atoi"), "")
        self.assertEqual(
            {sink.name: sink.ordinals for sink in vocabulary.sinks.values()},
            {"memcpy": (1, 2), "printf": (0,), "system": (0,)},
        )
        self.assertTrue(vocabulary.sink("printf").variadic)
        self.assertFalse(vocabulary.sink("memcpy").variadic)

    # -- 9.1 error handling ------------------------------------------------

    def test_error_handling_reads_the_fact_on_the_call_point(self) -> None:
        """The one query in this class that runs on a local, and why.

        The plan called for a `result_of` annotation on WRITE points. The frozen
        corpora are what killed it: `ACCESS_STORAGES` has no `local`, so a local
        assignment produces no WRITE point at all -- `int x = g();`, the shape
        this pattern is mostly about, would have had nothing to annotate. The
        fact was already on the CALL point instead: a call on an assignment's
        right-hand side carries the lvalue it is stored into, with a resolved
        declaration. This fixture is a local on purpose, so a regression to the
        WRITE-side design fails here rather than shrinking a corpus number.

        `window_end` is the exit: `return x + 1` never reads the identity the
        call stored, so nothing closed the window but the end of the method --
        which is still "nothing examined it".
        """
        events, edges = self.publish_source(
            "int g(void);\n"
            "int f(void) {\n"
            "  int x = g();\n"
            "  return x + 1;\n"
            "}\n"
        )
        result = defect.error_handling(events, edges)
        self.assertEqual(len(result.candidates), 1)
        candidate = result.candidates[0]
        self.assertEqual(candidate.defect_key, defect.ERROR_HANDLING_KEY)
        self.assertEqual(candidate.query, defect.ERROR_HANDLING)
        self.assertEqual(candidate.resolution_status, defect.RESOLVED)
        self.assertEqual(candidate.confidence, 0.4)
        self.assertEqual(candidate.subject["storage"], "local")
        self.assertEqual(candidate.subject["name"], "x")
        self.assertTrue(candidate.subject["identity"].endswith("#local:x"))
        self.assertEqual([fact["event"] for fact in candidate.facts], ["CALL"])
        self.assertEqual(list(candidate.source_evidence), ["src/unit.cpp:3-3"])
        self.assertEqual(candidate.metadata["call"]["subject_from"], "assignment")
        self.assertTrue(candidate.metadata["call"]["declared"])
        self.assertEqual(candidate.metadata["window_end"]["event"], "EXIT")
        self.assertEqual(candidate.metadata["scan"]["verdict"], "unchecked")
        # The pattern is not confirmable and the bundle says so: matrix 9.1 puts
        # "which APIs must be checked" in its AI-required semantics column, and
        # the graph cannot read a result that was thrown away.
        self.assertEqual(len(candidate.missing_evidence), 2)
        self.assertTrue(any("API semantics" in item for item in candidate.missing_evidence))
        self.assertTrue(any("discarded" in item for item in candidate.missing_evidence))
        self.assertEqual(result.coverage["population"], {"stored-call-result": 1})
        self.assertEqual(result.coverage["states"], {"resolved": 1})
        self.assertEqual(result.coverage["scan"], {"unchecked": 1})
        self.assertEqual(result.coverage["disqualified"], {})

    def test_error_handling_eliminates_the_result_a_branch_examined(self) -> None:
        """Three shapes, one verdict, and none of them is a CHECK point.

        `x < 0`, `g_x > 3` and `(g_p = g()) == 0` are all comparisons the graph
        does not read: a CHECK is a null comparison, so `fd == -1` and `ret != 0`
        are not CHECKs. "Was the result examined" is therefore answered by branch
        dominance -- control that branches between the call and the next use --
        and that is the whole reason this query walks the graph instead of
        looking for a comparison.

        The last fixture is the shape where the assignment and the branch share
        a line: the result is stored *inside* the condition, so the branch is not
        before the call's own line and the guard that skips backward branches
        must not swallow it.
        """
        for source in (
            "int g(void);\n"
            "int f(void) {\n"
            "  int x = g();\n"
            "  if (x < 0) { return -1; }\n"
            "  return x + 1;\n"
            "}\n",
            "int g(void);\n"
            "int g_x;\n"
            "int f(void) {\n"
            "  g_x = g();\n"
            "  if (g_x > 3) { return 1; }\n"
            "  return 0;\n"
            "}\n",
            "int g(void);\n"
            "int g_p;\n"
            "int f(void) {\n"
            "  if ((g_p = g()) == 0) { return -1; }\n"
            "  return g_p;\n"
            "}\n",
        ):
            events, edges = self.publish_source(source)
            result = defect.error_handling(events, edges)
            self.assertEqual(result.candidates, ())
            self.assertEqual(result.coverage["scan"], {"checked": 1})
            self.assertEqual(result.coverage["states"], {})
            # Eliminated is not invisible: the population still counts it and
            # the reason is recorded.
            self.assertEqual(result.coverage["population"], {"stored-call-result": 1})
            self.assertEqual(self.reasons(result), ["BRANCHED_BEFORE_USE"])

    def test_error_handling_records_the_route_the_scan_walked(self) -> None:
        """The evidence route is read off the walk, not searched for afterwards.

        Two reasons, and the second is the one that made the first cut of this
        query unusable. A route the walk already knows cannot disagree with the
        verdict it produced -- the decision and the evidence are the same
        traversal -- while handing both ends to `find_paths` re-derives the
        connection by a different method and can come back empty. And it is
        free: `find_paths` enumerates simple paths with a per-path visited set
        and caps only what it returns, so describing a window that closes after
        a loop costs 2^iterations. On redis-50 that is 3,989 windows, and the
        query did not finish in ten minutes; bounded to 32 hops it still cost
        43 s. The route below is three points because the window is three points
        long, and the edges join them in order.
        """
        events, edges = self.publish_source(
            "int g(void);\n"
            "int g_x;\n"
            "int f(void) {\n"
            "  g_x = g();\n"
            "  return g_x;\n"
            "}\n"
        )
        result = defect.error_handling(events, edges)
        self.assertEqual(len(result.candidates), 1)
        candidate = result.candidates[0]
        self.assertEqual(len(candidate.paths), 1)
        path = candidate.paths[0]
        self.assertEqual(path["points"][0], candidate.anchor)
        self.assertEqual(path["points"][-1], candidate.metadata["window_end"]["point"])
        self.assertEqual(len(path["edges"]), len(path["points"]) - 1)
        self.assertTrue(candidate.metadata["path_recorded"])
        # The route's points are the anchor, whatever the walk passed through,
        # and the point that closed the window -- so the citation and the route
        # name the same two ends.
        self.assertEqual(
            sorted({path["points"][0], path["points"][-1]}),
            sorted({candidate.anchor, candidate.metadata["window_end"]["point"]}),
        )

    def test_error_handling_keeps_the_frozen_window_ceiling(self) -> None:
        """`max_hops` is a floor here, not a knob.

        For the six sibling queries the hop ceiling shapes the evidence walk,
        and 64 hops is the behaviour the frozen five-column comparison
        describes. Here the ceiling decides the *verdict*: a walk that stops
        early reads "no branch found yet" where the frozen rule reads
        "checked", so a caller's smaller ceiling would manufacture candidates
        -- on llama.cpp-69 the delivery layer's 64-hop default turned 6,189
        windows into spurious candidates. The 80-statement window below closes
        with a branch that 8 hops can never reach; the clamp keeps the verdict
        the frozen rule demands, so nothing is proposed.
        """
        chain = "\n".join(f"  t{i} = t{i - 1} + 1;" for i in range(1, 80))
        events, edges = self.publish_source(
            "int f(void);\n"
            "int g(void) {\n"
            "  int x = f();\n"
            f"{chain}\n"
            "  if (x > 0) return 1;\n"
            "  return 0;\n"
            "}\n"
        )
        result = defect.error_handling(events, edges, max_hops=8)
        self.assertEqual(result.coverage["scan"].get("checked"), 1)
        self.assertNotIn("truncated", result.coverage["scan"])
        self.assertEqual(result.candidates, ())

    def test_error_handling_ignores_the_branch_that_precedes_the_call(self) -> None:
        """A loop header is not a check on a value that does not exist yet.

        The control-flow graph reaches backwards as readily as forwards: a call
        in a loop body reaches the header. Read naively, that header is "control
        branched after the call" and every call in every loop is examined. The
        guard is a fact about the shape -- a check cannot precede the value --
        and it is counted rather than silent: on redis-50 it rejects 2,006 branch
        points and on llama.cpp-69 50,403, which is the size of the false
        negative the naive rule would have carried.
        """
        events, edges = self.publish_source(
            "int g(void);\n"
            "int g_x;\n"
            "int f(void) {\n"
            "  int i;\n"
            "  for (i = 0; i < 3; i++) {\n"
            "    g_x = g();\n"
            "  }\n"
            "  return g_x;\n"
            "}\n"
        )
        result = defect.error_handling(events, edges)
        self.assertEqual(len(result.candidates), 1)
        candidate = result.candidates[0]
        self.assertEqual(candidate.resolution_status, defect.RESOLVED)
        self.assertEqual(candidate.metadata["scan"]["backward_branches_skipped"], 1)
        self.assertEqual(
            [(fact["fact"], fact["decisive"]) for fact in candidate.uncertain_facts],
            [("BACKWARD_BRANCH_SKIPPED", False)],
        )
        self.assertEqual(result.coverage["backward_branches_skipped"], 1)

    def test_error_handling_cannot_see_a_result_that_was_discarded(self) -> None:
        """The registered half of the pattern, and it is a population of zero.

        `g();` as a bare statement is matrix 9.1's other shape, and this query
        cannot report it: a CALL point records where a result went, never that
        it went nowhere, so there is no fact to build the candidate on. The
        coverage block is what keeps that apart from "the rule eliminated it" --
        the population is empty and no rule fired.
        """
        events, edges = self.publish_source(
            "int g(void);\n"
            "int f(void) {\n"
            "  g();\n"
            "  return 0;\n"
            "}\n"
        )
        result = defect.error_handling(events, edges)
        self.assertEqual(result.candidates, ())
        self.assertEqual(result.coverage["population"], {})
        self.assertEqual(result.coverage["disqualified"], {})
        self.assertEqual(result.coverage["scan"], {})

    def test_error_handling_cannot_separate_two_shapes_the_point_merges(self) -> None:
        """Two registered impurities, pinned where they are produced.

        tree-sitter-cpp parses `b += g()` as an `assignment_expression` with the
        same field layout as `b = g()`, and the operator field is not read -- so
        a compound assignment is a candidate even though the call's result is
        combined with the old value rather than stored into it. And `int c =
        h(g())` gives *both* calls the subject `c`, because the lvalue is
        attached to the whole right-hand side rather than to the call it came
        from. Neither is a rule that could be tightened against a fact the point
        does not carry; both would be over-claims if they were hidden.
        """
        events, edges = self.publish_source(
            "int g(void);\n"
            "int g_b;\n"
            "int f(void) {\n"
            "  g_b += g();\n"
            "  return g_b;\n"
            "}\n"
        )
        result = defect.error_handling(events, edges)
        self.assertEqual(len(result.candidates), 1)
        self.assertEqual(result.candidates[0].subject["name"], "g_b")

        events, edges = self.publish_source(
            "int g(void);\n"
            "int h(int);\n"
            "int f(void) {\n"
            "  int c = h(g());\n"
            "  return c;\n"
            "}\n"
        )
        result = defect.error_handling(events, edges)
        self.assertEqual(
            [candidate.subject["name"] for candidate in result.candidates],
            ["c", "c"],
        )
        self.assertEqual(result.coverage["population"], {"stored-call-result": 2})

    def test_error_handling_degrades_a_member_store_rather_than_claiming_it(self) -> None:
        """`g_s.v = g()` names an object the declaration index cannot resolve
        on its own, so the candidate is ambiguous and says which fact capped it.

        This is the file's oldest rule in its stage-6 form: the walk said
        `unchecked`, and the identity is what keeps the status short of
        `resolved`.
        """
        events, edges = self.publish_source(
            "struct S { int v; };\n"
            "int g(void);\n"
            "struct S g_s;\n"
            "int f(void) {\n"
            "  g_s.v = g();\n"
            "  return g_s.v;\n"
            "}\n"
        )
        result = defect.error_handling(events, edges)
        self.assertEqual(len(result.candidates), 1)
        candidate = result.candidates[0]
        self.assertEqual(candidate.resolution_status, defect.AMBIGUOUS)
        self.assertEqual(candidate.confidence, 0.2)
        self.assertEqual(candidate.subject["member"], "v")
        self.assertEqual(
            [(fact["fact"], fact["decisive"]) for fact in candidate.uncertain_facts],
            [("SUBJECT_PATH", True)],
        )

    # -- dispatch, determinism, and the one key that is not a matrix number -

    def test_query_for_resolves_the_three_new_matrix_keys(self) -> None:
        self.assertEqual(defect.query_for("3.1"), defect.NULL_FLOW)
        self.assertEqual(defect.query_for("4.2"), defect.DOUBLE_FREE)
        self.assertEqual(defect.query_for("4.3"), defect.USE_AFTER_FREE)
        # `taint` is a defect key and not a matrix number, and the pattern table
        # has no row for it -- so it is addressable by query name only, and a
        # caller asking for it by key gets the error a wrong key deserves.
        with self.assertRaises(ValueError) as caught:
            defect.query_for(defect.TAINT_KEY)
        self.assertIn("unknown defect pattern", str(caught.exception))

    def test_run_dispatches_to_the_five_new_queries(self) -> None:
        """The dispatch table, checked against a fixture that produces a
        candidate -- an empty result would agree with any query at all."""
        for source, name, function in (
            (
                "#include <stdlib.h>\n"
                "char *g_p;\n"
                "void f(void) {\n  g_p = NULL;\n  *g_p = 1;\n}\n",
                defect.NULL_FLOW, defect.null_flow,
            ),
            (
                "#include <stdlib.h>\n"
                "void sink(char *p);\n"
                "void f(char *p) {\n  free(p);\n  sink(p);\n}\n",
                defect.USE_AFTER_FREE, defect.use_after_free,
            ),
            (
                "#include <stdlib.h>\n"
                "void f(char *p) {\n  free(p);\n  free(p);\n}\n",
                defect.DOUBLE_FREE, defect.double_free,
            ),
            (
                "int g(void);\n"
                "int f(void) {\n  int x = g();\n  return x + 1;\n}\n",
                defect.ERROR_HANDLING, defect.error_handling,
            ),
        ):
            events, edges = self.publish_source(source)
            direct = function(events, edges)
            self.assertTrue(direct.candidates, name)
            self.assertEqual(
                [c.to_dict() for c in defect.run(name, events, edges).candidates],
                [c.to_dict() for c in direct.candidates],
            )

    def test_the_five_new_queries_are_deterministic_and_never_upgrade(self) -> None:
        """Two properties that hold across all of them, on one fixture each.

        Determinism is two runs of the same query on the same points. The second
        is the stage-6 form of the file's oldest rule: a decisive fact the graph
        could not settle caps the status, and no query may report `resolved` on
        top of one.
        """
        sources = (
            ("#include <stdlib.h>\nchar *g_p;\nvoid f(void) {\n  g_p = NULL;\n  *g_p = 1;\n}\n",
             defect.null_flow),
            ("#include <stdlib.h>\nvoid sink(char *p);\nvoid f(char *p) {\n  free(p);\n  sink(p);\n}\n",
             defect.use_after_free),
            ("#include <stdlib.h>\nvoid f(char *p) {\n  free(p);\n  free(p);\n}\n",
             defect.double_free),
            ("struct S { int v; };\nint g(void);\nstruct S g_s;\n"
             "int f(void) {\n  g_s.v = g();\n  return g_s.v;\n}\n",
             defect.error_handling),
        )
        for source, function in sources:
            events, edges = self.publish_source(source)
            first, second = function(events, edges), function(events, edges)
            self.assertEqual(
                [c.to_dict() for c in first.candidates],
                [c.to_dict() for c in second.candidates],
            )
            self.assertEqual(first.coverage, second.coverage)
            self.assertTrue(first.candidates)
            for candidate in first.candidates:
                blocking = [
                    fact for fact in candidate.uncertain_facts
                    if fact["decisive"] and fact["status"] != defect.RESOLVED
                ]
                self.assertFalse(
                    blocking and candidate.resolution_status == defect.RESOLVED,
                    f"{candidate.defect_key} resolved with decisive {blocking}",
                )

    def test_the_new_candidates_survive_reformatting(self) -> None:
        """A prepended blank line moves every span and no identity.

        The discriminator, the anchor and the candidate id have to be the same
        after the file is reindented, or a candidate cannot be cited, diffed or
        adjudicated twice. The evidence span is the one thing that *should*
        move -- it names where the code is now, not where it was -- so the test
        asserts the shift rather than tolerating it.
        """
        taint = (
            "#include <stdlib.h>\n"
            "#include <stdio.h>\n"
            "char *g_p;\n"
            "void f(void) {\n"
            '  g_p = getenv("HOME");\n'
            "  printf(g_p);\n"
            "}\n"
        )
        for source, function in (
            ("#include <stdlib.h>\nchar *g_p;\nvoid f(void) {\n  g_p = NULL;\n  *g_p = 1;\n}\n",
             defect.null_flow),
            ("#include <stdlib.h>\nvoid sink(char *p);\nvoid f(char *p) {\n  free(p);\n  sink(p);\n}\n",
             defect.use_after_free),
            ("#include <stdlib.h>\nchar *g_p;\nchar *g_q;\nvoid f(void) {\n  g_q = g_p;\n  free(g_p);\n  free(g_q);\n}\n",
             defect.double_free),
            ("int g(void);\nint f(void) {\n  int x = g();\n  return x + 1;\n}\n",
             defect.error_handling),
            (taint, None),
        ):
            if function is None:
                before = [
                    (c["candidate_id"], c["anchor"], c["source_evidence"])
                    for c in self.indexed(source).get_defect_candidates(
                        defect_type="taint_path"
                    )["data"]["candidates"]
                ]
                after = [
                    (c["candidate_id"], c["anchor"], c["source_evidence"])
                    for c in self.indexed("\n" + source).get_defect_candidates(
                        defect_type="taint_path"
                    )["data"]["candidates"]
                ]
            else:
                events, edges = self.publish_source(source)
                before = [
                    (c.discriminator, c.anchor, list(c.source_evidence))
                    for c in function(events, edges).candidates
                ]
                events, edges = self.publish_source("\n" + source)
                after = [
                    (c.discriminator, c.anchor, list(c.source_evidence))
                    for c in function(events, edges).candidates
                ]
            self.assertTrue(before, function)
            self.assertEqual(
                [(row[0], row[1]) for row in before],
                [(row[0], row[1]) for row in after],
            )
            for old, new in zip(before, after):
                for moved, original in zip(new[2], old[2]):
                    path, _, span = moved.rpartition(":")
                    self.assertEqual(path, original.rpartition(":")[0])
                    start, _, end = span.partition("-")
                    self.assertEqual(int(start), int(original.rpartition(":")[2].partition("-")[0]) + 1)
                    self.assertEqual(int(end), int(original.rpartition(":")[2].rpartition("-")[2]) + 1)


if __name__ == "__main__":
    unittest.main()

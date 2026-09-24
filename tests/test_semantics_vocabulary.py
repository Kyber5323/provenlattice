from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from provenlattice.semantics import (
    LEVEL_0_EVENTS,
    VocabularyError,
    available_languages,
    load_defect_patterns,
    load_vocabulary,
    vocabulary_for_path,
)
from provenlattice.semantics import vocabulary as vocabulary_module
from provenlattice.semantics.vocabulary import DEFECT_PATTERN_FILE


def load_cold(language: str):
    """Load with the cache dropped, so each call really re-parses the file."""
    vocabulary_module.clear_vocabulary_cache()
    return load_vocabulary(language)


class VocabularyLoadingTests(unittest.TestCase):
    def test_every_declared_language_loads(self) -> None:
        languages = available_languages()
        self.assertEqual(languages, tuple(sorted(languages)))
        self.assertIn("cpp", languages)
        self.assertIn("python", languages)
        for language in languages:
            self.assertEqual(load_vocabulary(language).language, language)

    def test_stage_2_events_all_have_a_vocabulary_entry(self) -> None:
        """Every event type stage 2 extracts must be spelled out in every
        language's file. A missing table is not "no keywords yet" -- it is an
        event type the extractor would silently never produce."""
        for language in available_languages():
            declared = set(load_vocabulary(language).events)
            self.assertEqual(
                declared,
                set(LEVEL_0_EVENTS),
                f"{language}.toml declares {sorted(declared ^ set(LEVEL_0_EVENTS))} "
                "differently from the stage 2 event list",
            )
            for spec in load_vocabulary(language).level_0():
                self.assertEqual(spec.level, 0)

    def test_loading_is_deterministic(self) -> None:
        """Byte-level reproducibility is the project's moat, and these files are
        hand-edited TOML where table order is not guaranteed to match read
        order. Compare two independent parses, not one cached object."""
        for language in available_languages():
            first, second = load_cold(language), load_cold(language)
            self.assertEqual(first, second)
            self.assertEqual(first.keywords(), second.keywords())
            self.assertEqual(list(first.keywords()), sorted(first.keywords()))
            self.assertEqual(
                [spec.event_type for spec in first.at_level(0)],
                [spec.event_type for spec in second.at_level(0)],
            )
            self.assertEqual(dict(first.ambiguous), dict(second.ambiguous))

    def test_extension_lookup_picks_one_language(self) -> None:
        self.assertEqual(vocabulary_for_path("src/thing.cpp").language, "cpp")
        self.assertEqual(vocabulary_for_path("pkg/mod.py").language, "python")
        self.assertIsNone(vocabulary_for_path("notes.md"))
        # Case is not significant: the suffix is lowercased before lookup.
        self.assertEqual(vocabulary_for_path("SRC/THING.CPP").language, "cpp")

    def test_a_keyword_claimed_twice_is_reported_not_resolved(self) -> None:
        """`WaitForSingleObject` is genuinely both a lock and a thread join.

        The loader must surface the clash rather than silently letting one
        event type win, because every downstream consumer would inherit a
        fabricated certainty.
        """
        ambiguous = load_vocabulary("cpp").ambiguous
        self.assertEqual(ambiguous["WaitForSingleObject"], ("LOCK", "THREAD_JOIN"))
        self.assertEqual(ambiguous["WaitForMultipleObjects"], ("LOCK", "THREAD_JOIN"))
        # And the claim survives on both sides.
        self.assertIn("WaitForSingleObject", load_vocabulary("cpp").spec_for("LOCK").calls)
        self.assertIn("WaitForSingleObject", load_vocabulary("cpp").spec_for("THREAD_JOIN").calls)

    def test_candidate_generators_do_not_overreach_on_obvious_names(self) -> None:
        """Bare `load`/`store` are the obvious std::atomic member names and also
        the member names on every cache and buffer. Their absence is a decision,
        so it is asserted rather than left to drift."""
        atomic = load_vocabulary("cpp").spec_for("ATOMIC")
        self.assertNotIn("load", atomic.calls)
        self.assertNotIn("store", atomic.calls)
        self.assertIn("fetch_add", atomic.calls)
        self.assertIn("__sync_fetch_and_add", atomic.calls)

    def test_python_absences_are_declared_rather_than_implied(self) -> None:
        """Python has no explicit heap allocation and no reachable atomic
        primitive. Saying so with a reason is a finding; an omitted table would
        read as an oversight and invite someone to "fix" it with list()/dict()."""
        python = load_vocabulary("python")
        absent = {spec.event_type for spec in python.absent_events()}
        self.assertEqual(absent, {"ALLOC", "ATOMIC"})
        for spec in python.absent_events():
            self.assertFalse(spec.has_matchers())
            self.assertTrue(spec.absent.strip())
        # The absent tables still occupy their slot in the event list, so a
        # coverage report can distinguish "no keywords yet" from "no such event".
        self.assertIn("ALLOC", python.events)
        self.assertIn("ALLOC", {spec.event_type for spec in python.level_0()})
        self.assertEqual(python.spec_for("ALLOC").keywords(), ())
        # cpp, by contrast, declares no absences: every event has matchers.
        self.assertEqual(load_vocabulary("cpp").absent_events(), ())


class VocabularyValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self._original = vocabulary_module.VOCABULARY_DIR
        vocabulary_module.VOCABULARY_DIR = self.directory

    def tearDown(self) -> None:
        vocabulary_module.VOCABULARY_DIR = self._original
        vocabulary_module.clear_vocabulary_cache()
        self.temp.cleanup()

    def write(self, name: str, body: str) -> None:
        (self.directory / f"{name}.toml").write_text(body, encoding="utf-8")
        vocabulary_module.clear_vocabulary_cache()

    def test_unknown_event_type_is_rejected(self) -> None:
        self.write("fake", 'language = "fake"\nextensions = [".fk"]\n'
                           '[events.MALLOC]\ncalls = ["x"]\n')
        with self.assertRaises(VocabularyError) as caught:
            load_vocabulary("fake")
        self.assertIn("unknown event type", str(caught.exception))

    def test_language_must_match_filename(self) -> None:
        self.write("fake", 'language = "other"\nextensions = [".fk"]\n'
                           '[events.CALL]\ncalls = ["x"]\n')
        with self.assertRaises(VocabularyError) as caught:
            load_vocabulary("fake")
        self.assertIn("and match the filename", str(caught.exception))

    def test_blank_keyword_is_rejected(self) -> None:
        self.write("fake", 'language = "fake"\nextensions = [".fk"]\n'
                           '[events.CALL]\ncalls = ["ok", "  "]\n')
        with self.assertRaises(VocabularyError) as caught:
            load_vocabulary("fake")
        self.assertIn("blank entry", str(caught.exception))

    def test_empty_event_table_is_rejected(self) -> None:
        """An empty table would look like coverage while extracting nothing."""
        self.write("fake", 'language = "fake"\nextensions = [".fk"]\n[events.CALL]\n')
        with self.assertRaises(VocabularyError) as caught:
            load_vocabulary("fake")
        self.assertIn("declares no matchers", str(caught.exception))

    def test_unknown_matcher_axis_is_rejected(self) -> None:
        self.write("fake", 'language = "fake"\nextensions = [".fk"]\n'
                           '[events.CALL]\nmethod_names = ["x"]\n')
        with self.assertRaises(VocabularyError) as caught:
            load_vocabulary("fake")
        self.assertIn("unknown keys", str(caught.exception))

    def test_an_absence_may_not_also_list_matchers(self) -> None:
        """The claim and the keywords would contradict each other, and a
        coverage report would read whichever one it happened to consult."""
        self.write("fake", 'language = "fake"\nextensions = [".fk"]\n'
                           '[events.ALLOC]\nabsent = "no such thing"\ncalls = ["malloc"]\n')
        with self.assertRaises(VocabularyError) as caught:
            load_vocabulary("fake")
        self.assertIn("claims to be absent", str(caught.exception))

    def test_an_absence_must_carry_a_reason(self) -> None:
        self.write("fake", 'language = "fake"\nextensions = [".fk"]\n'
                           '[events.ALLOC]\nabsent = "   "\n')
        with self.assertRaises(VocabularyError) as caught:
            load_vocabulary("fake")
        self.assertIn("non-empty reason", str(caught.exception))

    def test_an_absent_table_is_accepted(self) -> None:
        self.write("fake", 'language = "fake"\nextensions = [".fk"]\n'
                           '[events.CALL]\ncalls = ["x"]\n'
                           '[events.ALLOC]\nabsent = "nothing to match on"\n')
        vocabulary = load_vocabulary("fake")
        self.assertEqual([spec.event_type for spec in vocabulary.absent_events()], ["ALLOC"])

    def test_two_languages_claiming_one_extension_is_an_error(self) -> None:
        body = 'language = "{0}"\nextensions = [".shared"]\n[events.CALL]\ncalls = ["x"]\n'
        self.write("one", body.format("one"))
        self.write("two", body.format("two"))
        with self.assertRaises(VocabularyError) as caught:
            vocabulary_for_path("thing.shared")
        self.assertIn("claimed by", str(caught.exception))

    def test_unknown_language_names_the_available_ones(self) -> None:
        with self.assertRaises(VocabularyError) as caught:
            load_vocabulary("cobol")
        self.assertIn("no vocabulary for", str(caught.exception))


class DefectPatternTests(unittest.TestCase):
    def test_patterns_load_and_reference_known_events(self) -> None:
        patterns = load_defect_patterns()
        self.assertTrue(patterns)
        keys = [pattern.key for pattern in patterns]
        self.assertEqual(keys, sorted(keys))
        self.assertIn("4.1", keys)

    def test_requirements_are_the_matrix_events_plus_the_matrix_relations(self) -> None:
        """The two columns stage 4 finally copied out of the matrix.

        Before the refresh these were seeded from design doc §9 and disagreed
        with the matrix on 3.1 (`CHECK`/`DEREFERENCE` vs `CALL`/`RETURN`/...)
        and 4.2 (`RELEASE` vs `ALLOC`/`RELEASE`/`THROW`). Asserting the matrix's
        own values is what makes a later divergence a test failure rather than
        a silent drift.
        """
        by_key = {pattern.key: pattern for pattern in load_defect_patterns()}
        self.assertEqual(
            by_key["4.1"].requires, ("ALLOC", "RELEASE", "RETURN", "THROW")
        )
        self.assertEqual(
            by_key["4.1"].relations,
            ("CONSUMES", "CONTROL_REACHES", "PRODUCES", "RELEASES", "RETURNS"),
        )
        self.assertEqual(
            by_key["3.1"].requires, ("CALL", "READ", "RETURN", "THROW", "WRITE")
        )
        self.assertEqual(by_key["4.2"].requires, ("ALLOC", "RELEASE", "THROW"))

    def test_missing_events_separates_stage_2_coverage(self) -> None:
        by_key = {pattern.key: pattern for pattern in load_defect_patterns()}

        # Every event this one needs is level 0, and CONTROL_REACHES is built.
        # Stage 5B realized CONSUMES as a graph relation (ALLOC -> same-line
        # CALL, minted from the 5C `passed_to` fact), so it leaves the
        # missing set. The rest stay absent by implementation mapping:
        # PRODUCES/RELEASES/RETURNS remain event-subject identity facts, not
        # edges -- which is why 4.1 still cannot confirm.
        leak = by_key["4.1"]
        self.assertEqual(leak.missing_events(), ())
        self.assertEqual(leak.missing_relations(), ("PRODUCES", "RELEASES", "RETURNS"))
        self.assertFalse(leak.coverable_now())

        # Stage 3's CFG walker produces CHECK and DEREFERENCE structurally
        # (matched_via="structural"), and stage 5A's access layer produces
        # READ and WRITE (matched_via="access"), so the null-dereference
        # pattern's events are all producible. Stage 6 expanded the query, and
        # the relation side is why that did not make the pattern confirmable:
        # the guard test is a reverse seeding of the walker's null arms rather
        # than a `DOMINATES_CHECK` relation, so candidate generation needs
        # neither `CHECKS` nor `DOMINATES_CHECK` -- and confirmation still
        # needs both, plus `RETURNS`. This is the stage-6 nail: an expanded
        # query must not read as a confirmed defect.
        null_deref = by_key["3.1"]
        self.assertEqual(null_deref.missing_events(), ())
        self.assertEqual(null_deref.missing_relations(), ("CHECKS", "DOMINATES_CHECK", "RETURNS"))
        self.assertTrue(null_deref.expanded)
        self.assertFalse(null_deref.coverable_now())
        self.assertTrue(null_deref.candidate_coverable_now())

        # Stage 5A delivered the event side: READ/WRITE are access events now.
        # What keeps a race unconfirmable is the relation side -- MAY_PARALLEL,
        # PROTECTED_BY and the consistency question, which is the DFG's.
        race = by_key["1.1"]
        self.assertEqual(race.missing_events(), ())
        self.assertFalse(race.coverable_now())

        # Resource leak needs OPEN/CLOSE, absent from the stage 2 level 0 list
        # even though the matrix grades this pattern A. Asserting the gap keeps
        # it visible instead of letting it look supported.
        resource = by_key["4.5"]
        self.assertEqual(resource.grade, "A")
        self.assertEqual(resource.missing_events(), ("CLOSE", "OPEN"))
        self.assertFalse(resource.coverable_now())

    def test_candidate_requirements_answer_the_falsifiability_question(self) -> None:
        """1.1 is the case the whole two-column split exists for.

        The pattern is not coverable now -- confirming a race needs READ/WRITE
        and four relations -- and its candidate generation *is*, from events
        and CONTROL_REACHES alone. Both answers are true at once, and that pair
        is stage 4's acceptance answer ③ in mechanical form. Before the split
        `coverable_now()` could only say "no", which read as "cannot attack".
        """
        by_key = {pattern.key: pattern for pattern in load_defect_patterns()}
        race = by_key["1.1"]
        self.assertFalse(race.coverable_now())
        self.assertTrue(race.candidate_coverable_now())
        self.assertEqual(race.candidate_missing_events(), ())
        self.assertEqual(race.candidate_missing_relations(), ())

        # The same for the leak family: candidates from ALLOC/RELEASE/RETURN,
        # confirmation from relations the DFG will have to supply.
        leak = by_key["4.1"]
        self.assertTrue(leak.candidate_coverable_now())
        self.assertFalse(leak.coverable_now())

        # An unexpanded pattern is false on both counts, and that is not a
        # contradiction -- there is no query to run. 4.5 is the case that
        # stays: the double free and the two 4.x/3.1 neighbours were in this
        # position until stage 6.
        unexpanded = by_key["4.5"]
        self.assertFalse(unexpanded.expanded)
        self.assertFalse(unexpanded.coverable_now())
        self.assertFalse(unexpanded.candidate_coverable_now())

    def test_expanded_patterns_name_a_query_that_exists(self) -> None:
        from provenlattice.defect import QUERY_NAMES

        patterns = load_defect_patterns()
        expanded = {pattern.key: pattern.query for pattern in patterns if pattern.expanded}
        self.assertEqual(
            expanded,
            {
                "1.1": "race_condition", "1.3": "lock_order", "1.4": "lock_order",
                "3.1": "null_flow", "4.2": "double_free", "4.3": "use_after_free",
                "4.1": "resource_lifetime", "4.6": "resource_lifetime",
                "9.1": "error_handling",
            },
        )
        for query in expanded.values():
            self.assertIn(query, QUERY_NAMES)
        # The four keys that must stay absent, and the reason they are asserted
        # by name rather than left to a comment: 4.5 is the two-document
        # OPEN/CLOSE conflict, and 9.2/9.3/9.4 are the 9.x rows whose
        # requirements the graph does not hold. A row that quietly acquired a
        # query would be a coverage claim nobody made.
        self.assertEqual(
            {pattern.key for pattern in patterns if not pattern.expanded},
            {"4.5", "9.2", "9.3", "9.4"},
        )

    def test_grade_is_carried_from_the_matrix(self) -> None:
        by_key = {pattern.key: pattern for pattern in load_defect_patterns()}
        self.assertEqual(by_key["4.2"].grade, "B")
        self.assertEqual(by_key["9.1"].grade, "A")

    def test_an_unknown_relation_is_rejected_not_reported_as_a_gap(self) -> None:
        """A typo must fail loudly. If it were tolerated, `MAY_PARALELL` would
        sit in `missing_relations()` forever looking like unfinished work."""
        original = DEFECT_PATTERN_FILE.read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as holder:
            path = Path(holder) / "defect_patterns.toml"
            path.write_text(
                original.replace('relations = ["EXECUTES_IN"', 'relations = ["EXECUTES_INN"'),
                encoding="utf-8",
            )
            with mock.patch.object(vocabulary_module, "DEFECT_PATTERN_FILE", path):
                with self.assertRaises(VocabularyError) as caught:
                    load_defect_patterns()
        self.assertIn("unknown relations", str(caught.exception))
        self.assertIn("EXECUTES_INN", str(caught.exception))

    def test_every_list_is_sorted_for_every_pattern(self) -> None:
        for pattern in load_defect_patterns():
            for name in ("requires", "relations", "candidate_requires", "candidate_relations"):
                value = getattr(pattern, name)
                self.assertEqual(list(value), sorted(value), f"{pattern.key}.{name}")
            for name in ("missing_events", "missing_relations",
                         "candidate_missing_events", "candidate_missing_relations"):
                value = getattr(pattern, name)()
                self.assertEqual(list(value), sorted(value), f"{pattern.key}.{name}()")


if __name__ == "__main__":
    unittest.main()

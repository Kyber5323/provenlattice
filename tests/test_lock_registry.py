"""Declared lock registry tests: the vocabulary a repository's own locks need.

The failure this guards is silent, which is why it needs a test: when a repo's
lock wrapper is spelled outside `cpp.toml`'s lists, the events layer mints no
LOCK point at all, `lock_order` answers "no findings", and nothing anywhere
reports that the query never saw a lock. These tests pin the registry that
closes that gap, and the validation that keeps it honest.
"""

import textwrap
import unittest

import provenlattice.semantics.vocabulary as vocabulary


class LockRegistryContractTests(unittest.TestCase):
    def test_registry_loads_the_declared_names(self) -> None:
        acquire, release = vocabulary.load_lock_registry()
        self.assertIn("spin_lock", acquire)
        self.assertIn("spin_unlock", release)
        self.assertIn("mutex_lock", acquire)
        self.assertIn("mutex_unlock", release)

    def test_registry_has_no_name_on_both_sides(self) -> None:
        # A callee that both acquires and releases cannot be given one event;
        # the loader rejects this, so the shipped file must already satisfy it.
        acquire, release = vocabulary.load_lock_registry()
        self.assertEqual(acquire & release, frozenset())

    def test_cpp_vocabulary_merges_the_registry(self) -> None:
        vocab = vocabulary.load_vocabulary("cpp")
        lock = vocab.spec_for("LOCK")
        unlock = vocab.spec_for("UNLOCK")
        self.assertIn("spin_lock", lock.calls)
        self.assertIn("spin_unlock", unlock.calls)
        # The built-in spellings must survive the merge untouched.
        self.assertIn("pthread_mutex_lock", lock.calls)
        self.assertIn("pthread_mutex_unlock", unlock.calls)

    def test_merge_is_additive_only(self) -> None:
        merged = vocabulary.load_vocabulary("cpp").spec_for("LOCK").calls
        acquire, _release = vocabulary.load_lock_registry()
        # every built-in name is still there: a superset, never a replacement
        with open(vocabulary.VOCABULARY_DIR / "cpp.toml", "rb") as handle:
            raw = handle.read().decode("utf-8")
        for builtin in ("pthread_spin_lock", "EnterCriticalSection", "g_mutex_lock", "lock"):
            self.assertIn(builtin, merged)
        self.assertTrue(acquire <= merged)
        self.assertIn("[events.LOCK]", raw)

    def test_other_languages_do_not_read_the_registry(self) -> None:
        # The registry is C spelling; merging it into another language would be
        # a guess. `cpp` is the only vocabulary shipped today, so the guard is
        # exercised directly rather than through a second TOML file.
        from provenlattice.semantics.vocabulary import EventSpec, _merge_lock_registry

        spec = EventSpec(
            event_type="LOCK", syntax=frozenset(), calls=frozenset({"m.lock"}),
            qualified=frozenset(),
        )
        events = {"LOCK": spec}
        _merge_lock_registry(events, "java")
        self.assertEqual(events["LOCK"].calls, frozenset({"m.lock"}))
        _merge_lock_registry(events, "cpp")
        self.assertIn("spin_lock", events["LOCK"].calls)

    def test_absent_specs_are_not_given_matchers(self) -> None:
        vocab = vocabulary.load_vocabulary("cpp")
        absent = vocab.absent_events()
        for spec in absent:
            self.assertFalse(spec.has_matchers(), spec.event_type)


class LockRegistryValidationTests(unittest.TestCase):
    """The loader must reject a registry that contradicts itself."""

    def setUp(self) -> None:
        import tempfile
        from pathlib import Path

        self._temp = tempfile.TemporaryDirectory()
        self.path = Path(self._temp.name) / "locks.toml"
        self._saved = vocabulary.LOCK_FILE
        vocabulary.LOCK_FILE = self.path

    def tearDown(self) -> None:
        vocabulary.LOCK_FILE = self._saved
        vocabulary.load_lock_registry.cache_clear()
        self._temp.cleanup()

    def write(self, body: str) -> None:
        self.path.write_text(textwrap.dedent(body), encoding="utf-8")
        vocabulary.load_lock_registry.cache_clear()

    def test_missing_file_is_an_empty_registry(self) -> None:
        vocabulary.load_lock_registry.cache_clear()
        self.assertEqual(vocabulary.load_lock_registry(), (frozenset(), frozenset()))

    def test_same_name_on_both_sides_is_rejected(self) -> None:
        self.write(
            """
            [acquire]
            calls = ["wds_lock"]
            [release]
            calls = ["wds_lock"]
            """
        )
        with self.assertRaises(vocabulary.VocabularyError):
            vocabulary.load_lock_registry()

    def test_blank_entry_is_rejected(self) -> None:
        self.write(
            """
            [acquire]
            calls = ["wds_lock", "  "]
            """
        )
        with self.assertRaises(vocabulary.VocabularyError):
            vocabulary.load_lock_registry()

    def test_unknown_key_is_rejected(self) -> None:
        self.write(
            """
            [acquire]
            matches = ["wds_lock"]
            """
        )
        with self.assertRaises(vocabulary.VocabularyError):
            vocabulary.load_lock_registry()

    def test_empty_calls_list_is_legal(self) -> None:
        self.write(
            """
            [acquire]
            calls = []
            """
        )
        self.assertEqual(vocabulary.load_lock_registry(), (frozenset(), frozenset()))


if __name__ == "__main__":
    unittest.main()

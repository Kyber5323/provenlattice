"""Document discovery: the default layout, and a repository that says otherwise.

The knowledge layer used to scan only `spec/`, `docs/`, `doc/` at the repository
root. A checkout that keeps its documents in `src/…/docs/` therefore produced
zero documents, zero requirements and zero evidence links -- silently, because
"the repository has no documents" and "we did not look where they are" print the
same numbers.

`_document_roots` is that seam. The tests below pin the default (unchanged, so
existing repositories keep their behaviour), the override, the validation that
keeps a path inside the repository, and the metrics that make an empty result
diagnosable.
"""

import tempfile
import unittest
from pathlib import Path

from provenlattice.knowledge import (
    DEFAULT_DOC_DIRS,
    KNOWLEDGE_CONFIG,
    _document_roots,
    index_knowledge,
)
from provenlattice.graph import full_index


def write(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class DocumentRootsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "repo"
        (self.root / "src").mkdir(parents=True)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_default_is_the_root_spec_docs_doc_layout(self) -> None:
        for name in ("docs", "spec", "doc"):
            (self.root / name).mkdir()
        roots = _document_roots(self.root)
        self.assertEqual(
            sorted(path.name for path in roots), sorted(DEFAULT_DOC_DIRS))

    def test_absent_directories_are_skipped_not_errors(self) -> None:
        self.assertEqual(_document_roots(self.root), ())

    def test_config_replaces_the_default(self) -> None:
        # A repository that names its documents has told us; mixing the default
        # back in would resurrect the directory that missed in the first place.
        (self.root / "docs").mkdir()
        (self.root / "src" / "userdocs").mkdir(parents=True)
        write(self.root, str(KNOWLEDGE_CONFIG), '[documents]\npaths = ["src/userdocs"]\n')
        roots = _document_roots(self.root)
        self.assertEqual([path.name for path in roots], ["userdocs"])

    def test_absolute_path_is_rejected(self) -> None:
        write(self.root, str(KNOWLEDGE_CONFIG), '[documents]\npaths = ["C:/elsewhere"]\n')
        with self.assertRaises(ValueError):
            _document_roots(self.root)

    def test_escaping_path_is_rejected(self) -> None:
        write(self.root, str(KNOWLEDGE_CONFIG), '[documents]\npaths = ["../outside"]\n')
        with self.assertRaises(ValueError):
            _document_roots(self.root)

    def test_unknown_key_is_rejected(self) -> None:
        write(self.root, str(KNOWLEDGE_CONFIG), '[documents]\nglobs = ["docs"]\n')
        with self.assertRaises(ValueError):
            _document_roots(self.root)

    def test_empty_paths_falls_back_to_the_default(self) -> None:
        (self.root / "docs").mkdir()
        write(self.root, str(KNOWLEDGE_CONFIG), '[documents]\npaths = []\n')
        self.assertEqual([path.name for path in _document_roots(self.root)], ["docs"])


class KnowledgeDiscoveryMetricsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "repo"
        self.database = Path(self.temp.name) / "g.db"
        write(self.root, "src/main.c", "int f(void){ return 0; }\n")
        full_index(self.root, self.database)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_a_configured_document_inside_the_tree_is_indexed(self) -> None:
        write(self.root, "src/userdocs/design.md",
              "# Design\n\nCalls `f`.\n\n[symbol:f]\n")
        write(self.root, str(KNOWLEDGE_CONFIG), '[documents]\npaths = ["src/userdocs"]\n')
        metrics = index_knowledge(self.root, self.database, incremental=False)
        self.assertEqual(metrics["documents_found"], 1)
        self.assertEqual(metrics["document_roots"], ["src/userdocs"])
        self.assertGreaterEqual(metrics["knowledge_nodes"], 1)

    def test_zero_documents_is_diagnosable(self) -> None:
        metrics = index_knowledge(self.root, self.database, incremental=False)
        self.assertEqual(metrics["documents_found"], 0)
        # the roots that were searched are reported, so the empty result says
        # *where we looked* rather than only that nothing was found
        self.assertEqual(metrics["document_roots"], [])

    def test_default_layout_still_works(self) -> None:
        write(self.root, "docs/guide.md", "# Guide\n\nUses `f`.\n")
        metrics = index_knowledge(self.root, self.database, incremental=False)
        self.assertEqual(metrics["documents_found"], 1)
        self.assertEqual(metrics["document_roots"], ["docs"])


if __name__ == "__main__":
    unittest.main()

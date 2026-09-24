from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
import formal_harness as h
import formal_runner as runner


FORMAL_ROOT = Path(__file__).parent.parent / "formal"


class FormalRunnerCutoverTests(unittest.TestCase):
    def bundle(self):
        return h.load_canonical_bundle(FORMAL_ROOT)

    def binding(self, root: Path, *, swap: bool = False, origin: str = "https://github.com/facebook/zstd") -> Path:
        commits = sorted({cell["base_commit"] for cell in self.bundle()["manifest"]["cells"]})
        payload = {
            "artifact_class": "execution_environment_binding",
            "schema": "d9-c5-execution-binding-v1",
            "source_repositories": {"facebook/zstd": {"path": str(root / "source"), "expected_origin": origin,
                                                            "required_base_commits": commits}},
            "runtime_roots": {
                "A": {"path": str(root / ("runtime-b" if swap else "runtime-a")), "expected_sha": h.ARM_SHA["A"]},
                "B": {"path": str(root / ("runtime-a" if swap else "runtime-b")), "expected_sha": h.ARM_SHA["B"]},
            },
        }
        path = root / "binding.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def resolve(self, binding: Path):
        def fake_git(repo, args, approved):
            return subprocess.CompletedProcess(["git"], 0, "https://github.com/facebook/zstd\n", "")

        def fake_revision(repo, approved, ref="HEAD"):
            name = Path(repo).name
            if name == "runtime-a": return h.ARM_SHA["A"]
            if name == "runtime-b": return h.ARM_SHA["B"]
            return ref

        with patch.object(h, "_git", fake_git), patch.object(h, "_revision", fake_revision):
            return h.resolve_execution_environment(self.bundle(), binding)

    def test_resolver_issues_only_validated_environment_and_constructs_invocation(self):
        with tempfile.TemporaryDirectory() as tmp:
            environment = self.resolve(self.binding(Path(tmp)))
            self.assertTrue(environment.is_resolver_issued())
            cell = self.bundle()["manifest"]["cells"][0]
            attestation = {"attestation_sha256": "attestation", "runner_identity": h.REQUIRED_RUNNER_IDENTITY, "dispatch_context": "formal-approved"}
            invocation = h.construct_formal_invocation(self.bundle(), environment, attestation, cell["cell_id"])
            self.assertEqual(environment.binding_sha256, invocation["binding_sha256"])
            self.assertEqual(environment.source_repository, invocation["target_repository"])
            self.assertEqual(environment.runtime_for(cell["arm"]), invocation["runtime_root"])
            self.assertIn("provenance_sha256", invocation["cell"])

    def test_raw_paths_and_unsealed_environment_cannot_construct_formal_invocation(self):
        cell = self.bundle()["manifest"]["cells"][0]
        with self.assertRaises(h.FormalDataError):
            h.construct_formal_invocation(self.bundle(), {"runtime_roots": {}, "target_repositories": {}}, {}, cell["cell_id"])
        unsealed = h.ValidatedExecutionEnvironment("x", "source", (("A", "a"), ("B", "b")))
        with self.assertRaises(h.FormalDataError):
            h.construct_formal_invocation(self.bundle(), unsealed, {}, cell["cell_id"])

    def test_binding_source_and_runtime_identity_mismatch_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(h.FormalDataError):
                self.resolve(self.binding(root, origin="https://example.test/not-zstd"))
            with self.assertRaises(h.FormalDataError):
                self.resolve(self.binding(root, swap=True))

    def test_binding_freeze_rejects_tamper_and_is_deterministic(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); binding = self.binding(root); freeze = root / "binding-freeze.json"
            def fake_git(repo, args, approved):
                return subprocess.CompletedProcess(["git"], 0, "https://github.com/facebook/zstd\n", "")
            def fake_revision(repo, approved, ref="HEAD"):
                name = Path(repo).name
                if name == "runtime-a": return h.ARM_SHA["A"]
                if name == "runtime-b": return h.ARM_SHA["B"]
                return ref
            with patch.object(h, "_git", fake_git), patch.object(h, "_revision", fake_revision):
                first = h.freeze_execution_binding(self.bundle(), binding, freeze, "2026-09-24T00:00:00+00:00")
                resolved = h.validate_frozen_execution_binding(self.bundle(), binding, freeze)
                self.assertEqual(first["execution_binding_sha256"], resolved.binding_sha256)
                original = json.loads(binding.read_text(encoding="utf-8"))
                def source_path_changed(payload): payload["source_repositories"]["facebook/zstd"]["path"] = str(root / "other-source")
                def runtime_sha_changed(payload): payload["runtime_roots"]["A"]["expected_sha"] = "wrong"
                def runtime_paths_swapped(payload): payload["runtime_roots"]["A"]["path"], payload["runtime_roots"]["B"]["path"] = payload["runtime_roots"]["B"]["path"], payload["runtime_roots"]["A"]["path"]
                def same_schema_substitution(payload): payload["binding_id"] = "another-binding"
                for mutate in (source_path_changed, runtime_sha_changed, runtime_paths_swapped, same_schema_substitution):
                    payload = json.loads(json.dumps(original)); mutate(payload)
                    binding.write_text(json.dumps(payload), encoding="utf-8")
                    with self.assertRaises(h.FormalDataError):
                        h.validate_frozen_execution_binding(self.bundle(), binding, freeze)

    def test_formal_cli_has_no_legacy_or_raw_path_options(self):
        result = subprocess.run([sys.executable, str(Path(__file__).parent / "formal_runner.py"), "--help"], capture_output=True, text=True)
        self.assertEqual(0, result.returncode)
        self.assertNotIn("--config", result.stdout)
        self.assertNotIn("runtime_roots", result.stdout)
        self.assertNotIn("target_repositories", result.stdout)
        rejected = subprocess.run([sys.executable, str(Path(__file__).parent / "formal_runner.py"), "--config", "legacy.json"], capture_output=True, text=True)
        self.assertNotEqual(0, rejected.returncode)
        self.assertIn("error:", rejected.stderr)
        direct = subprocess.run([sys.executable, str(Path(__file__).parent / "formal_runner.py"), "--validate-invocation", "--formal-root", "formal", "--binding", "binding", "--binding-freeze", "freeze", "--dispatch-context-freeze", "context", "--output", "out", "--cell-id", "cell"], capture_output=True, text=True)
        self.assertNotEqual(0, direct.returncode)
        self.assertIn("--dispatch-attestation", direct.stderr)

    def test_dispatch_context_attestation_rejects_direct_or_mismatched_launch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); binding = self.binding(root); binding_freeze = root / "binding-freeze.json"
            context_freeze = root / "context-freeze.json"; attestation_root = root / "attestations"; attestation = attestation_root / "a.json"
            def fake_git(repo, args, approved): return subprocess.CompletedProcess(["git"], 0, "https://github.com/facebook/zstd\n", "")
            def fake_revision(repo, approved, ref="HEAD"):
                return h.ARM_SHA["A"] if Path(repo).name == "runtime-a" else h.ARM_SHA["B"] if Path(repo).name == "runtime-b" else ref
            with patch.object(h, "_git", fake_git), patch.object(h, "_revision", fake_revision), patch.object(h, "_runner_identity", return_value=h.REQUIRED_RUNNER_IDENTITY):
                environment = self.resolve(binding)
                h.freeze_execution_binding(self.bundle(), binding, binding_freeze, "t")
                h.freeze_dispatch_context(self.bundle(), environment, context_freeze, attestation_root, "t")
                h.create_dispatch_attestation(self.bundle(), environment, context_freeze, attestation, "t")
                valid = h.validate_dispatch_attestation(self.bundle(), environment, context_freeze, attestation)
                self.assertEqual(h.REQUIRED_RUNNER_IDENTITY, valid["runner_identity"])
                original = json.loads(attestation.read_text(encoding="utf-8")); payload = dict(original); payload["execution_binding_sha256"] = "wrong"; attestation.write_text(json.dumps(payload), encoding="utf-8")
                with self.assertRaises(h.FormalDataError): h.validate_dispatch_attestation(self.bundle(), environment, context_freeze, attestation)
                attestation.write_text(json.dumps(original), encoding="utf-8")
                with patch.object(h, "_runner_identity", return_value="other\\user"):
                    with self.assertRaises(h.FormalDataError): h.validate_dispatch_attestation(self.bundle(), environment, context_freeze, attestation)

    def test_runner_calls_resolver_then_formal_constructor_not_legacy_constructor(self):
        env = SimpleNamespace(binding_sha256="synthetic-binding")
        args = argparse.Namespace(formal_root=Path("formal"), binding=Path("binding"), binding_freeze=Path("freeze"), dispatch_context_freeze=Path("context"), dispatch_attestation=Path("attestation"), output=Path("out"),
                                  cell_id="cell", execute=False, materialize_only=False, validate_invocation=True)
        with patch.object(runner, "parse_args", return_value=args), \
             patch.object(runner, "load_canonical_bundle", return_value={"bundle": True}) as load, \
             patch.object(runner, "validate_frozen_execution_binding", return_value=env) as resolve, \
             patch.object(runner, "validate_dispatch_attestation", return_value={"attestation_sha256":"x", "runner_identity":h.REQUIRED_RUNNER_IDENTITY, "dispatch_context":"formal-approved"}) as attest, \
             patch.object(runner, "preflight_formal_execution", return_value={"state": "EXECUTION_READY"}), \
             patch.object(runner, "construct_formal_invocation", return_value={"cell": {}, "runtime_root": "runtime", "target_repository": "source", "runner_identity":h.REQUIRED_RUNNER_IDENTITY, "dispatch_context":"formal-approved"}) as construct, \
             patch.object(runner, "dispatch_cell", side_effect=self.fail):
            with patch("builtins.print"):
                runner.main()
        load.assert_called_once_with(args.formal_root)
        resolve.assert_called_once_with({"bundle": True}, args.binding, args.binding_freeze)
        attest.assert_called_once_with({"bundle": True}, env, args.dispatch_context_freeze, args.dispatch_attestation)
        construct.assert_called_once_with({"bundle": True}, env, attest.return_value, args.cell_id)
        self.assertNotIn("construct_invocation", runner.__dict__)


if __name__ == "__main__":
    unittest.main()

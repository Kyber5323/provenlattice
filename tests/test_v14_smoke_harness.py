"""V1.4 release smoke harness: the release-facing availability gate.

Deliberately thin - deep functional coverage lives in the existing
qualification suite. This harness re-uses existing capability surfaces
(MCP server wiring, defect fixtures, the installed distribution) and fails
loudly if any release-facing basic regresses:

1. fresh install path (distribution + console entry point resolve)
2. index/query basic flow through the real CLI
3. CLI command availability
4. MCP basic availability (reuses test_mcp_defect_e2e wiring, no copy)
5. version/package consistency (pyproject == distribution == vocabularies)
"""

import importlib.metadata
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest

import test_mcp_defect_e2e as mcp_e2e

REPO = pathlib.Path(__file__).resolve().parent.parent
PYPROJECT = REPO / "pyproject.toml"
EXPECTED_VERSION = "1.7.0"

CORE_COMMANDS = (
    "index", "update", "knowledge", "analyze", "defect", "status",
    "symbol", "definition", "callers", "callees", "references",
    "task-context",
)
DEFECT_TYPES = (
    "resource_lifetime", "lock_order", "race_condition", "null_flow",
    "use_after_free", "double_free", "taint_path", "error_handling",
)


def cli_exe():
    found = shutil.which("provenlattice")
    if found:
        return found
    for name in ("provenlattice.exe", "provenlattice"):
        sibling = pathlib.Path(sys.executable).parent / name
        if sibling.exists():
            return str(sibling)
    raise AssertionError(
        "provenlattice console script not found on PATH or next to the "
        "interpreter; fresh install path is broken"
    )


def run_cli(args):
    proc = subprocess.run(
        [cli_exe(), *args], capture_output=True, text=True, timeout=300)
    if proc.returncode != 0:
        raise AssertionError(
            f"provenlattice {' '.join(args)} failed rc={proc.returncode}\n"
            f"stdout: {proc.stdout[:2000]}\nstderr: {proc.stderr[:2000]}")
    return proc


def mcp_rpc(proc, method, params=None, rid=0):
    msg = {"jsonrpc": "2.0", "method": method, "id": rid}
    if params is not None:
        msg["params"] = params
    proc.stdin.write((json.dumps(msg) + "\n").encode("utf-8"))
    proc.stdin.flush()
    while True:
        line = proc.stdout.readline()
        if not line:
            raise AssertionError(f"MCP server closed stdout during {method}")
        resp = json.loads(line.strip())
        if resp.get("id") == rid:
            return resp


class V14SmokeHarness(unittest.TestCase):
    """Release gate: every check here is a release-blocking basic."""

    def test_fresh_install_path_resolves_distribution(self):
        dist = importlib.metadata.distribution("provenlattice")
        self.assertIsNotNone(
            dist, "provenlattice distribution is not installed; "
                  "the fresh install path is broken")
        eps = [ep for ep in importlib.metadata.entry_points(group="console_scripts")
               if ep.name == "provenlattice"]
        self.assertTrue(
            eps, "console_scripts entry point 'provenlattice' is missing")
        self.assertTrue(
            callable(eps[0].load()),
            "entry point does not resolve to a callable CLI main")

    def test_index_query_basic_flow(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td) / "repo"
            (root / "src").mkdir(parents=True)
            (root / "src" / "sample.c").write_text(
                mcp_e2e.FIXTURE, encoding="utf-8")
            db = pathlib.Path(td) / "graph.db"
            run_cli(["index", str(root), "--database", str(db), "--json"])
            run_cli(["analyze", str(root), "--scope", "src",
                     "--features", "defect", "--database", str(db), "--json"])
            out = run_cli(["defect", str(root), "--type", "double_free",
                           "--database", str(db), "--json"])
            data = json.loads(out.stdout)
            self.assertEqual(
                data.get("status"), "available",
                f"defect query over a freshly indexed repo degraded: {data}")
            self.assertIn("grade", data, "defect response missing grade")

    def test_cli_command_availability(self):
        help_text = run_cli(["--help"]).stdout
        missing = [c for c in CORE_COMMANDS if c not in help_text]
        self.assertEqual(
            missing, [], f"core CLI commands missing from --help: {missing}")
        defect_help = run_cli(["defect", "--help"]).stdout
        missing_types = [t for t in DEFECT_TYPES if t not in defect_help]
        self.assertEqual(
            missing_types, [],
            f"defect --help is missing defect query types: {missing_types}")

    def test_mcp_basic_availability(self):
        env = os.environ.copy()
        env["PYTHONPATH"] = str(mcp_e2e.REPO / "src")
        proc = subprocess.Popen(
            [sys.executable, str(mcp_e2e.MCP_SERVER), str(mcp_e2e.CONTRACT)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, env=env)
        try:
            init = mcp_rpc(proc, "initialize", {
                "protocolVersion": "2024-11-05", "capabilities": {},
                "clientInfo": {"name": "v14-smoke", "version": "1.4"},
            }, rid=1)
            self.assertIsNotNone(
                init.get("result"), f"MCP initialize failed: {init}")
            tools = mcp_rpc(proc, "tools/list", {}, rid=2)
            names = [t["name"] for t in tools["result"]["tools"]]
            self.assertIn(
                "query", names, f"tools/list missing the query tool: {names}")
        finally:
            proc.terminate()
            proc.wait(timeout=10)

    def test_version_and_package_consistency(self):
        pyproject = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
        self.assertEqual(
            pyproject["project"]["version"], EXPECTED_VERSION,
            "pyproject version drifted from the frozen release version")
        self.assertEqual(
            importlib.metadata.version("provenlattice"), EXPECTED_VERSION,
            "installed distribution version does not match the release")
        import provenlattice
        semantics = pathlib.Path(provenlattice.__file__).parent / "semantics"
        tomls = {p.name for p in semantics.rglob("*.toml")}
        expected = {"defect_patterns.toml", "taint.toml", "cpp.toml"}
        self.assertTrue(
            expected <= tomls,
            f"semantic vocabularies missing from the package: "
            f"{sorted(expected - tomls)}")


if __name__ == "__main__":
    unittest.main()

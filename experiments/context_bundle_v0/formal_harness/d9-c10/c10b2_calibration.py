"""D9-C10-B2 oracle v2 calibration: BASE -> BUG_PRESENT; KNOWN FIX -> FIX_PRESENT.

The known fix (upstream lib/doc/zlibWrapper diff, harness-side only) is applied
to a separate sealed calibration repo that the Agent never sees; it is removed
after calibration (digests recorded).  Baseline uses the sealed-base built by
c10_workspace_v2.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, r"D:\ChatGPT\codegraph\provenlattice\experiments\context_bundle_v0\formal_harness")
sys.path.insert(0, str(Path(__file__).parent))
from c10_oracle_v2 import oracle_v2  # noqa: E402
from c10_workspace_v2 import build_sealed, git  # noqa: E402

ZSTD = Path(r"D:\pl-c4-pilot\zstd")
BASE = "3128e03be69b9d3db054a3765ae1310c7b3666f6"
FIX_TIP = "ce31004f"
SEALED = Path(r"D:\pl-c10-sealed\calibration")
ORACLE_DIR = Path(__file__).parent / "oracle-v2"
TEST_PATCH = Path(r"D:\pl-c4-pilot\p1-memory-quarantine\case1-test-patch.diff")


def main() -> None:
    ORACLE_DIR.mkdir(parents=True, exist_ok=True)
    calibration = {"schema": "d9-c10-b2-oracle-v2-calibration-v1",
                   "oracle_impl_sha256": hashlib.sha256(
                       (Path(__file__).parent / "c10_oracle_v2.py").read_bytes()).hexdigest(),
                   "test_patch_sha256": hashlib.sha256(TEST_PATCH.read_bytes()).hexdigest()}

    # known-fix fixture materialization (harness-side only)
    gold = git(["diff", BASE, FIX_TIP, "--", ".", ":(exclude)tests/"]).stdout.encode()
    gold_path = ORACLE_DIR / "case1-gold.diff"
    gold_path.write_bytes(gold)
    calibration["gold_diff"] = {"path": str(gold_path),
                                "sha256": hashlib.sha256(gold).hexdigest(),
                                "bytes": len(gold),
                                "visibility": "harness-only; removed from Agent reach; audit token registered"}

    # BASE calibration on the sealed-base
    base_repo = SEALED / "sealed-base"
    base_result = oracle_v2(base_repo, TEST_PATCH)
    calibration["baseline"] = {"repo": str(base_repo),
                               "class_v2": base_result["class_v2"],
                               "legacy_class": base_result["legacy"]["class"],
                               "fuzzer_build_exit": base_result["addendum"]["fuzzer_build_exit"],
                               "fuzzer_build_tail": base_result["addendum"]["fuzzer_build_tail"][-500:]}

    # KNOWN FIX calibration: sealed repo + gold + test patch
    knownfix_repo = SEALED / "sealed-knownfix"
    if knownfix_repo.exists():
        shutil.rmtree(knownfix_repo, ignore_errors=True)
    build_sealed("calibration-knownfix", BASE, knownfix_repo)
    r = subprocess.run(["git", "-C", str(knownfix_repo), "apply", "--3way", str(gold_path)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    calibration["knownfix_gold_apply_rc"] = r.returncode
    if r.returncode != 0:
        raise SystemExit(f"gold apply failed: {r.stderr[-300:]}")
    fix_result = oracle_v2(knownfix_repo, TEST_PATCH)
    calibration["knownfix"] = {"repo": str(knownfix_repo),
                               "class_v2": fix_result["class_v2"],
                               "legacy_class": fix_result["legacy"]["class"],
                               "fuzzer_build_exit": fix_result["addendum"]["fuzzer_build_exit"],
                               "fuzzer_run_exit": fix_result["addendum"]["fuzzer_run_exit"]}

    calibration["calibration_gate"] = {
        "baseline_bug_present": base_result["class_v2"] == "BUG_PRESENT",
        "knownfix_fix_present": fix_result["class_v2"] == "FIX_PRESENT",
    }
    # persist results BEFORE cleanup (cleanup may hit NUL device-name artifacts)
    (ORACLE_DIR / "oracle-v2-calibration.json").write_text(
        json.dumps(calibration, indent=2, ensure_ascii=False), encoding="utf-8")

    # clean base snapshot: rebuild sealed-base fresh (calibration dirtied it)
    nul = base_repo / "tests" / "NUL"
    if nul.exists():
        subprocess.run(["cmd", "/c", f"del /f /q \\\\?\\{nul}"], capture_output=True)
    shutil.rmtree(base_repo, ignore_errors=True)
    if base_repo.exists():
        subprocess.run(["cmd", "/c", f"rd /s /q \"{base_repo}\""], capture_output=True)
    build_sealed("calibration-base", BASE, base_repo)
    calibration["baseline_snapshot_restored"] = True
    # destroy the known-fix repo (contains the solution)
    nk_nul = knownfix_repo / "tests" / "NUL"
    if nk_nul.exists():
        subprocess.run(["cmd", "/c", f"del /f /q \\\\?\\{nk_nul}"], capture_output=True)
    shutil.rmtree(knownfix_repo, ignore_errors=True)
    calibration["knownfix_repo_destroyed_after_calibration"] = True
    (ORACLE_DIR / "oracle-v2-calibration.json").write_text(
        json.dumps(calibration, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"baseline_class": base_result["class_v2"],
                      "baseline_build_tail": base_result["addendum"]["fuzzer_build_tail"][-200:],
                      "knownfix_class": fix_result["class_v2"],
                      "knownfix_fuzz_exit": fix_result["addendum"]["fuzzer_run_exit"],
                      "calibration_gate": calibration["calibration_gate"]}, indent=2))


if __name__ == "__main__":
    main()

"""D9-C10-B2 C10_ORACLE_CONTRACT_V2.

Discriminating oracle for case 1 (the frozen playTests path cannot detect the
#835 regression because upstream coverage lives in the fuzz binaries).

Phases:
  1. legacy compatibility signal: build + datagen + test patch --3way + playTests
     (with the pipeline exit-code fix: ${PIPESTATUS[0]}; recorded but NOT primary)
  2. discriminative addendum: tests/fuzzer build + short fuzz run
       - build failure on the base = BUG_PRESENT (compile: ZSTD_decodingBufferSize_min undefined)
       - build + fuzz pass on a fixed tree = FIX_PRESENT

Exit-code semantics: the raw make/bash exit codes are used verbatim (no pipes on
the captured command).  Timeout: build 1800s, datagen 900s, fuzz 1200s.
Environment: msys bash (C:/msys64/usr/bin/bash.exe), PATH=/usr/bin, OS inherited.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

BASH = r"C:/msys64/usr/bin/bash.exe"


def _bash(script: str, timeout: int, cwd=None) -> subprocess.CompletedProcess:
    return subprocess.run([BASH, "-lc", script], capture_output=True,
                          encoding="utf-8", errors="replace", timeout=timeout, cwd=cwd)


def oracle_v2(repo: Path, test_patch: Path, run_fuzz: bool = True) -> dict:
    result: dict = {"oracle": "c10-oracle-v2",
                    "legacy": {}, "addendum": {}}
    build = _bash(f"cd '{repo.as_posix()}' && export PATH=/usr/bin:$PATH && "
                  f"make -j8 OS= 2>&1 | tail -5; exit ${{PIPESTATUS[0]}}", 1800)
    result["legacy"]["build_exit"] = build.returncode
    result["legacy"]["build_ok"] = build.returncode == 0
    result["legacy"]["build_log_tail"] = (build.stdout or "")[-1200:]
    datagen = _bash(f"cd '{repo.as_posix()}' && export PATH=/usr/bin:$PATH && "
                    f"make -C tests datagen OS= 2>&1 | tail -3; exit ${{PIPESTATUS[0]}}", 900)
    result["legacy"]["datagen_ok"] = datagen.returncode == 0
    apply_patch = subprocess.run(["git", "-C", str(repo), "apply", "--3way", str(test_patch)],
                                 capture_output=True, text=True, encoding="utf-8", errors="replace")
    result["legacy"]["test_patch_applied"] = apply_patch.returncode == 0
    result["legacy"]["test_patch_stderr_tail"] = (apply_patch.stderr or "")[-800:]
    tests = _bash(f"cd '{repo.as_posix()}/tests' && export PATH=/usr/bin:$PATH && "
                  f"ZSTD=\"$PWD/../programs/zstd\" bash playTests.sh 2>&1 | tail -25; "
                  f"exit ${{PIPESTATUS[0]}}", 3000)
    result["legacy"]["tests_exit"] = tests.returncode
    result["legacy"]["tests_log_tail"] = (tests.stdout or "")[-1200:]
    result["legacy"]["class"] = ("PASS" if tests.returncode == 0 and result["legacy"]["build_ok"]
                                 and result["legacy"]["test_patch_applied"]
                                 else "TESTS_FAILURE" if result["legacy"]["build_ok"] and result["legacy"]["test_patch_applied"]
                                 else "BUILD_FAILURE" if not result["legacy"]["build_ok"]
                                 else "TEST_PATCH_CONFLICT")

    build = _bash(f"cd '{repo.as_posix()}' && export PATH=/usr/bin:$PATH && "
                  f"make -C tests fuzzer OS= 2>&1 | tail -8; exit ${{PIPESTATUS[0]}}", 1800)
    result["addendum"]["fuzzer_build_exit"] = build.returncode
    result["addendum"]["fuzzer_build_ok"] = build.returncode == 0
    result["addendum"]["fuzzer_build_tail"] = (build.stdout or "")[-1500:]
    if build.returncode != 0:
        result["addendum"]["class"] = "BUG_PRESENT"
        result["addendum"]["fuzzer_run_exit"] = None
    else:
        fuzz = _bash(f"cd '{repo.as_posix()}/tests' && export PATH=/usr/bin:$PATH && "
                     f"./fuzzer -i200 2>&1 | tail -3; exit ${{PIPESTATUS[0]}}", 1200)
        result["addendum"]["fuzzer_run_exit"] = fuzz.returncode
        result["addendum"]["fuzz_tail"] = (fuzz.stdout or "")[-600:]
        result["addendum"]["class"] = "FIX_PRESENT" if fuzz.returncode == 0 else "INCONCLUSIVE"
    result["class_v2"] = result["addendum"]["class"]
    return result

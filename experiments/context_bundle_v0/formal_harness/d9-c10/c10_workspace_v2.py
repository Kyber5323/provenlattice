"""D9-C10-B2 sealed standalone workspace builder.

Builds a per-cell SEALED_STANDALONE_REPOSITORY:
  - fresh `git init` (no remote, no alternates, no shared object store)
  - object transfer = the frozen base COMMIT object + the complete closure of
    its TREE (all subtrees/blobs) — nothing else (no ancestors, no descendants)
  - refs/heads/master = original frozen base commit SHA (identity preserved)
  - git replace --graft <base> (no parents) so `git log` walks exactly one commit
  - checkout materializes the working tree byte-faithfully from the sealed store

Leakage negative tests verify the known fix commits are OBJECT-UNAVAILABLE.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, r"D:\ChatGPT\codegraph\provenlattice\experiments\context_bundle_v0\formal_harness")
from formal_harness import _atomic_json

ZSTD = Path(r"D:\pl-c4-pilot\zstd")
SEALED_ROOT = Path(r"D:\pl-c10-sealed")
KNOWN_FIX_SHAS = ["058ed2ad3349187e985a481b6db654952729c307",
                  "b3f33ccfb3b4fcc73df82126fd5ecfb751268fc6",
                  "ce31004f205bb5dcb5ba6766b86009ffbcc90f00"]


def git(args, cwd=None, check=True):
    r = subprocess.run(["git", "-c", f"safe.directory={ZSTD}", "-C", str(cwd or ZSTD), *args],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    if check and r.returncode != 0:
        raise SystemExit(f"git {' '.join(args[:3])} failed (rc={r.returncode}): {r.stderr[-300:]}")
    return r


def build_sealed(cell_id: str, base_commit: str, dest: Path) -> dict:
    if dest.exists():
        raise SystemExit(f"sealed destination already exists: {dest}")
    sealed_git = dest / ".git"
    sealed_git.parent.mkdir(parents=True, exist_ok=True)
    r = git(["init", "-q", str(dest)], check=True)
    # 1. object transfer: base commit + complete tree closure
    tree = git(["rev-parse", f"{base_commit}^{{tree}}"]).stdout.strip()
    revs = git(["rev-list", "--objects", f"{base_commit}^{{tree}}"]).stdout
    object_list = f"{base_commit}\n{revs}"
    pack_proc = subprocess.run(["git", "-c", f"safe.directory={ZSTD}", "-C", str(ZSTD),
                                "pack-objects", "--stdout"],
                               input=object_list.encode(), capture_output=True)
    if pack_proc.returncode != 0 or not pack_proc.stdout:
        raise SystemExit(f"pack-objects failed: {pack_proc.stderr[-300:]}")
    pack_file = dest / ".git" / "incoming.pack"
    pack_file.write_bytes(pack_proc.stdout)
    idx = subprocess.run(["git", "-C", str(dest), "index-pack", "--stdin", "--fix-thin"],
                         stdin=open(pack_file, "rb"), capture_output=True)
    if idx.returncode != 0:
        raise SystemExit(f"index-pack failed: {idx.stderr[-300:]}")
    pack_file.unlink()
    # 2. graft: base commit gets no parents inside the sealed store
    git(["-c", "user.name=D9-C10-B2", "-c", "user.email=c10b2@sealed.local",
         "replace", "--graft", base_commit], cwd=dest, check=True)
    # 3. refs + checkout
    git(["symbolic-ref", "HEAD", "refs/heads/master"], cwd=dest, check=True)
    git(["update-ref", "refs/heads/master", base_commit], cwd=dest, check=True)
    git(["checkout", "-f", "-q", "master"], cwd=dest, check=True)
    git(["config", "core.autocrlf", "false"], cwd=dest, check=True)
    git(["status"], cwd=dest, check=False)
    # 4. leakage negative tests
    leak = {}
    for sha in KNOWN_FIX_SHAS:
        r = git(["cat-file", "-e", sha], cwd=dest, check=False)
        leak[f"cat_file_{sha[:8]}"] = {"available": r.returncode == 0}
    r = git(["log", "--all", "--oneline", "--grep=ZSTD_decodingBufferSize_min"], cwd=dest, check=False)
    leak["log_all_grep_hits"] = r.stdout.strip().splitlines() if r.stdout.strip() else []
    r = git(["show", KNOWN_FIX_SHAS[2]], cwd=dest, check=False)
    leak["show_fix_tip"] = {"succeeded": r.returncode == 0}
    r = git(["branch", "-a"], cwd=dest, check=False)
    leak["branches"] = r.stdout.strip().splitlines()
    r = git(["tag"], cwd=dest, check=False)
    leak["tags"] = r.stdout.strip().splitlines()
    r = git(["reflog"], cwd=dest, check=False)
    leak["reflog_lines"] = len(r.stdout.strip().splitlines()) if r.stdout.strip() else 0
    r = git(["fsck", "--no-reflogs"], cwd=dest, check=False)
    leak["fsck_fix_objects_reachable"] = any(s[:8] in (r.stdout + r.stderr) for s in KNOWN_FIX_SHAS)
    leak["alternates_file_exists"] = (dest / ".git" / "objects" / "info" / "alternates").exists()
    r = git(["remote", "-v"], cwd=dest, check=False)
    leak["remotes"] = r.stdout.strip().splitlines()
    r = git(["log", "--oneline", "--all"], cwd=dest, check=False)
    leak["log_all_commits"] = len(r.stdout.strip().splitlines())
    leak["head_tree_sha"] = git(["rev-parse", "HEAD^{tree}"], cwd=dest).stdout.strip()
    leak["head_commit_sha"] = git(["rev-parse", "HEAD"], cwd=dest).stdout.strip()
    # object inventory digest
    packs = sorted(p.name for p in (dest / ".git" / "objects" / "pack").glob("*")) \
        if (dest / ".git" / "objects" / "pack").exists() else []
    loose_count = sum(1 for _ in (dest / ".git" / "objects").rglob("*")) if (dest / ".git" / "objects").exists() else 0
    leak["object_inventory"] = {"pack_files": packs, "loose_entries": loose_count}
    passed = (not leak["log_all_grep_hits"]
              and not any(v["available"] for k, v in leak.items() if k.startswith("cat_file_"))
              and not leak["show_fix_tip"]["succeeded"]
              and not leak["alternates_file_exists"]
              and not leak["remotes"]
              and leak["log_all_commits"] <= 2
              and leak["head_tree_sha"] == git(["rev-parse", f"{base_commit}^{{tree}}"]).stdout.strip())
    return {"sealed_git": str(sealed_git), "dest": str(dest), "leak": leak,
            "base_tree_sha": tree, "leakage_tests_pass": passed}


if __name__ == "__main__":
    base = "3128e03be69b9d3db054a3765ae1310c7b3666f6"
    dest = SEALED_ROOT / "calibration" / "sealed-base"
    if Path(dest, ".git").exists():
        print(json.dumps({"mode": "ALREADY_BUILT", "dest": str(dest)}))
        sys.exit(0)
    result = build_sealed("calibration-base", base, Path(dest))
    result["cell_id"] = "calibration-base"
    out = SEALED_ROOT / "workspace-git-leakage-test.json"
    out.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"mode": "C10B2_SEALED_BASE_BUILT", "dest": str(dest),
                      "leakage_tests_pass": result["leakage_tests_pass"],
                      "head_commit": result["leak"]["head_commit_sha"],
                      "head_tree": result["leak"]["head_tree_sha"],
                      "log_all_commits": result["leak"]["log_all_commits"],
                      "cat_file_fixes_unavailable": not any(
                          v["available"] for k, v in result["leak"].items() if k.startswith("cat_file_"))},
                     indent=2))

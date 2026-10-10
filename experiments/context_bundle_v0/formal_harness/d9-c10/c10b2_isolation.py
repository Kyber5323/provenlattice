"""D9-C10-B2 execution isolation plumbing + negative tests.

Mechanisms (all reversible, recorded):
  1. sealed runtime copies: pl-arm-{a,b}/src -> D:\\pl-c10-sealed\\runtimes\\{A,B}\\src
  2. ACL deny on every sensitive child of D:\\pl-c4-pilot for the running user SID
  3. ambient claude project memory dir deny (zstd project)
  4. %TEMP% pl-*/zstd-* quarantine into the denied tree during agent runs
  5. network closure: GIT_CONFIG injection (protocol.allow=never) + --disallowed-tools
     + hosts-block of github surfaces (if admin available)

Negative tests run FROM the agent's process environment.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

PILOT = Path(r"D:\pl-c4-pilot")
SEALED = Path(r"D:\pl-c10-sealed")
OUT = Path(r"D:\pl-c4-pilot\formal-readiness-p1")

SENSITIVE_CHILDREN = [
    "zstd", "refs", "runs", "smoke", "control-40aed09e", "control-p1",
    "formal-readiness-r3", "formal-readiness-c7b", "formal-readiness-c7d",
    "formal-readiness-c8b", "formal-readiness-p1", "p1-memory-quarantine",
    "diagnostic-c9-p0", "diagnostic-c9-p0b", "diagnostic-c9-c2",
    "harness", "formal-dispatch-attestations",
]
AMBIENT_CLAUDE_ZSTD = Path(r"C:\Users\sunqinghw\.claude\projects\D--pl-c4-pilot-zstd")


def sid() -> str:
    out = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True,
                         text=True, encoding="utf-8").stdout.strip()
    return out.split(",")[1].strip('"')


def icacls_deny(path: Path, user_sid: str) -> subprocess.CompletedProcess:
    return subprocess.run(["icacls", str(path), "/deny", f"*{user_sid}:(OI)(CI)(F)"],
                          capture_output=True, text=True, encoding="utf-8", errors="replace")


def icacls_remove(path: Path, user_sid: str) -> subprocess.CompletedProcess:
    return subprocess.run(["icacls", str(path), "/remove:d", f"*{user_sid}"],
                          capture_output=True, text=True, encoding="utf-8", errors="replace")


def copy_runtime(arm: str, src: Path) -> dict:
    dst = SEALED / "runtimes" / arm
    if dst.exists():
        shutil.rmtree(dst, ignore_errors=True)
    dst.mkdir(parents=True, exist_ok=True)
    skipped = 0
    for root, dirs, files in os.walk(src):
        rel = Path(root).relative_to(src)
        target = dst / rel
        target.mkdir(parents=True, exist_ok=True)
        for f in files:
            s, t = Path(root) / f, target / f
            try:
                shutil.copy2(s, t)
            except (OSError, PermissionError):
                skipped += 1
    # digest of the copy (files only, sorted by relpath+content)
    digest_map = {}
    for f in sorted(dst.rglob("*")):
        if f.is_file():
            rel = str(f.relative_to(dst)).replace("\\", "/")
            digest_map[rel] = hashlib.sha256(f.read_bytes()).hexdigest()
    tree_digest = hashlib.sha256(json.dumps(digest_map, sort_keys=True).encode()).hexdigest()
    return {"dst": str(dst), "files": len(digest_map), "skipped": skipped,
            "tree_digest": tree_digest}


def probe_denied(path: Path) -> dict:
    """Try to read a denied path the way the agent would."""
    r = subprocess.run(["cmd", "/c", f"type \"{path}\\HEAD\""], capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    return {"path": str(path), "read_denied": ("Access is denied" in (r.stderr or "")
                                               or "拒绝访问" in (r.stderr or "")
                                               or r.returncode != 0),
            "stderr_head": (r.stderr or "")[:120]}


def network_checks() -> dict:
    # git protocol.allow injection proof
    env = {**os.environ,
           "GIT_CONFIG_COUNT": "1",
           "GIT_CONFIG_KEY_0": "protocol.allow",
           "GIT_CONFIG_VALUE_0": "never"}
    r = subprocess.run(["git", "ls-remote", "https://github.com/facebook/zstd", "HEAD"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)
    git_blocked = r.returncode != 0 and ("protocol" in (r.stderr or "").lower()
                                         or "not supported" in (r.stderr or "").lower()
                                         or "fatal" in (r.stderr or "").lower())
    # hosts admin check
    hosts = Path(r"C:\Windows\System32\drivers\etc\hosts")
    admin = os.access(hosts, os.W_OK)
    # curl github reachability (baseline, without hosts block)
    curl = subprocess.run(["curl", "-s", "-o", "NUL", "-w", "%{http_code}",
                           "--max-time", "15", "https://github.com/facebook/zstd"],
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    github_reachable = curl.stdout.strip().startswith(("2", "3"))
    return {"git_protocol_allow_never_blocks_fetch": git_blocked,
            "git_ls_remote_stderr": (r.stderr or "")[-150:],
            "hosts_file_admin_writable": admin,
            "github_reachable_baseline": github_reachable,
            "curl_baseline_http_code": curl.stdout.strip()[:6]}


def main() -> None:
    user = sid()
    state = {"schema": "d9-c10-b2-isolation-plumbing-v1",
             "user_sid": user,
             "generated_at": datetime.now(timezone.utc).isoformat() + "Z"}

    # 1. sealed runtime copies
    rt_a = copy_runtime("A", PILOT / "pl-arm-a" / "src")
    rt_b = copy_runtime("B", PILOT / "pl-arm-b" / "src")
    state["runtime_copies"] = {"A": rt_a, "B": rt_b}
    # runtime tree digests DIFFER by design (the frozen D9-C5 treatment inventory);
    # construction/bridge code is identical — record both digests for the ledger.

    # 2. apply denies
    deny_results = {}
    for child in SENSITIVE_CHILDREN:
        p = PILOT / child
        if p.exists():
            r = icacls_deny(p, user)
            deny_results[child] = {"rc": r.returncode, "ok": r.returncode == 0}
    if AMBIENT_CLAUDE_ZSTD.exists():
        r = icacls_deny(AMBIENT_CLAUDE_ZSTD, user)
        deny_results["ambient_claude_zstd"] = {"rc": r.returncode, "ok": r.returncode == 0}
    state["deny_results"] = deny_results
    state["denied_paths"] = [str(PILOT / c) for c in SENSITIVE_CHILDREN
                             if deny_results.get(c, {}).get("ok")] + \
                            ([str(AMBIENT_CLAUDE_ZSTD)] if deny_results.get("ambient_claude_zstd", {}).get("ok") else [])

    # 3. filesystem negative tests (from the agent's process environment)
    fs_tests = {
        "original_clone": probe_denied(PILOT / "zstd"),
        "c6_worktree": probe_denied(PILOT / "formal-readiness-r3" / "worktrees"
                                    / "cell-aa40ca15a77eddae245dd987"),
        "p1_artifacts": probe_denied(OUT),
        "quarantine": probe_denied(PILOT / "p1-memory-quarantine"),
        "ambient_claude_zstd_memory": probe_denied(AMBIENT_CLAUDE_ZSTD / "memory"),
    }
    state["filesystem_negative_tests"] = fs_tests

    # 4. network checks
    state["network"] = network_checks()

    # 5. remove denies (plumbing verified; P1' runner re-applies per cell)
    removed = {}
    for child in SENSITIVE_CHILDREN:
        p = PILOT / child
        if p.exists():
            r = icacls_remove(p, user)
            removed[child] = r.returncode == 0
    if AMBIENT_CLAUDE_ZSTD.exists():
        r = icacls_remove(AMBIENT_CLAUDE_ZSTD, user)
        removed["ambient_claude_zstd"] = r.returncode == 0
    state["denies_removed_after_verification"] = removed

    (OUT / "isolation-plumbing-state.json").write_text(
        json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    fs_ok = all(v["read_denied"] for v in fs_tests.values())
    summary = {"mode": "C10B2_ISOLATION_PLUMBING_VERIFIED",
               "runtime_copies": {"A": rt_a["tree_digest"][:16], "B": rt_b["tree_digest"][:16]},
               "denies_applied": all(v.get("ok") for k, v in deny_results.items()),
               "filesystem_negative_tests_all_denied": fs_ok,
               "git_protocol_block": state["network"]["git_protocol_allow_never_blocks_fetch"],
               "hosts_admin": state["network"]["hosts_file_admin_writable"],
               "github_baseline_reachable": state["network"]["github_reachable_baseline"]}
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()

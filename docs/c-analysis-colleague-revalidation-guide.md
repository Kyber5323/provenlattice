# C Analysis — Colleague 16-case Revalidation Guide (R1)

Purpose: an environment-controlled rerun of the 16 ContextBench-zstd cases
with the integrated post-V1.2 C analysis chain, comparable with the prior
Graphify-era local run (14/16 resolved, 2 infra-limited, 0 genuine failures).

## Fixed artifacts

- ProvenLattice commit (source the package was built from):
  `8e194ab76faea086cb6ed873e4acfdf2da46318a` (merged main HEAD; the packaging
  commit adds only docs/manifest/wheel metadata, no source changes).
- Package: `revalidation/packages/provenlattice-1.0.0-py3-none-any.whl`
- Package SHA256: `3F7F3434753F3A7AA9BA3D3063FD8F3DE6862A851F69C64D5172386DCBF10774`
- Python requirement: `>=3.11`

## Install

```powershell
pip install revalidation/packages/provenlattice-1.0.0-py3-none-any.whl
python -m provenlattice --help
```

Minimal verification after install (index + optional C analysis on a real
file):

```powershell
python -m provenlattice index <repo> --json
python -m provenlattice analyze <repo> --scope <dir> --features ast,cfg --json
python -m provenlattice analysis <repo> --feature ast --file-path <file.c> --json
python -m provenlattice explain-symbol <symbol> --analysis cfg --json
```

Expected: `ast:c` / `cfg:c` analyzers for C files; `explain-symbol` returns
`call_semantics` and definition-preferred targets.

## Environment recording (required for every run)

Record and publish alongside results:

- OS / OS version
- shell (and whether MSYS2/WSL/cmd/PowerShell)
- Python version
- git version
- Agent/model and runner version
- ProvenLattice commit used (`git rev-parse HEAD`)
- package SHA256 (verify with `Get-FileHash` / `sha256sum`)
- per-case base SHA

## Per-case protocol

- Do not modify case patches, test patches, oracle, or playTests.
- Run each case at its own `base_commit`.
- Capture stdout/stderr to files; do not rely on terminal-only observation.

### Special requirements for 35ed56f6 and 8bffb1b1

These two cases previously hung in the 2016-era zstd CLI benchmark stage
(`zstd -i0b0e3 ...` progress spinner under MSYS2 pipes; the official
reference fix also hung). For an honest environment attribution record:

- the exact benchmark command line
- whether a TTY is attached to the benchmark process
- whether stdout/stderr are pipes (and which)
- whether the run floods progress lines and enters "cooling down" without
  completing
- the final exit code (or timeout + elapsed time)
- whether the official reference patch also hangs in your environment
  (probe run, same recording)

If the benchmark stage completes in your environment, note that as an
environment difference — it directly explains the prior INFRA_LIMITED
classification.

## Reporting

- Record per-case outcome as resolved / failed / infra-limited with evidence.
- Do not compare against the prior run's per-case facts as a pass condition;
  the comparison is environment-controlled and descriptive.
- Do not pre-assume results: this manifest pre-records none.

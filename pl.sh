#!/usr/bin/env sh
# ProvenLattice CLI wrapper -- the shortest way to run this checkout.
#
# Why a wrapper exists at all: the editable install points at the ORIGINAL
# checkout (D:\代码理解\provenlattice), so `provenlattice.exe` and a bare
# `python -m provenlattice.cli` run *that* repository's code, not this mirror's.
# Every result produced without PYTHONPATH=src is therefore untrustworthy in a
# way that looks like success. This puts this checkout's src first on sys.path.
#
#   ./pl.sh index /path/to/repo
#   ./pl.sh defect --type 4.1 --database d:/tmp/stage5b/redis50-5b.db
#   ./pl.sh defect --list
#
# Which interpreter? This used to be hardcoded to D:\program\Python312, which is
# one machine's layout. It is now detected, so a fresh machine needs no edit:
#
#   $PL_PYTHON   if set, tried first -- the escape hatch for a machine where
#                none of the candidates below is the one you want
#   py -3.12, py -3.11, py -3     the Windows launcher, most specific first
#   python, python3               whatever PATH offers
#
# Each candidate must answer a version probe before it is used. The floor is
# 3.11, not 3.12: the package declares requires-python >= 3.11 and its only
# version-sensitive import is tomllib, which is 3.11+. The probe is not
# ceremony -- in Git Bash on Windows `python3` resolves to the WindowsApps stub,
# which is not an interpreter at all and must be skipped rather than run.
set -e

root=$(cd "$(dirname "$0")" && pwd)

usable() {
    "$@" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' \
        >/dev/null 2>&1
}

if [ -n "${PL_PYTHON:-}" ]; then
    if usable "$PL_PYTHON"; then
        exec "$PL_PYTHON" -m provenlattice.cli "$@"
    fi
    echo "pl.sh: PL_PYTHON=$PL_PYTHON is not a Python >= 3.11; falling through" >&2
fi

if command -v py >/dev/null 2>&1; then
    for version in -3.12 -3.11 -3; do
        if usable py "$version"; then
            PYTHONPATH="$root/src" exec py "$version" -m provenlattice.cli "$@"
        fi
    done
fi

for name in python python3; do
    if command -v "$name" >/dev/null 2>&1 && usable "$name"; then
        PYTHONPATH="$root/src" exec "$name" -m provenlattice.cli "$@"
    fi
done

cat >&2 <<'EOF'
pl.sh: no Python >= 3.11 found.

Find one with any of these, then set PL_PYTHON to it:
  py -0p                 # Windows launcher: every install, with its path
  where.exe python       # PowerShell
  which python           # Git Bash

  PL_PYTHON=/path/to/python ./pl.sh status --database <db>
EOF
exit 1

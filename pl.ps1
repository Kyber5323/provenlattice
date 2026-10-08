# ProvenLattice CLI wrapper for PowerShell -- the shortest way to run this checkout.
#
# Why a wrapper exists at all: the editable install points at the ORIGINAL
# checkout (D:\代码理解\provenlattice), so a bare `python -m provenlattice` runs
# *that* repository's code, not this mirror's. The difference is not cosmetic:
# the original checkout has no semantic layer, so a graph it builds has no
# CFG and no DFG, and `defect` cannot answer anything. It does not fail -- it
# quietly produces a smaller graph. This puts this checkout's src first.
#
#   .\pl.ps1 index "D:\代码理解\测试代码仓\libgit2-24"
#   .\pl.ps1 status --database "D:\代码理解\测试代码仓\libgit2-24\.provenlattice\codegraph.db"
#   .\pl.ps1 defect --type 4.1 --database <db>
#
# Which interpreter? This used to be hardcoded to D:\program\Python312, which is
# one machine's layout. It is now detected, so a fresh machine needs no edit:
#
#   $env:PL_PYTHON   if set, tried first -- the escape hatch for a machine where
#                    none of the candidates below is the one you want
#   py -3.12, py -3.11, py -3   the Windows launcher, most specific first
#   python, python3             whatever PATH offers
#
# Each candidate must answer a version probe before it is used. The floor is
# 3.11, not 3.12: the package declares requires-python >= 3.11 and its only
# version-sensitive import is tomllib, which is 3.11+. The probe is not
# ceremony -- PATH `python` on Windows is sometimes the WindowsApps stub, which
# is not an interpreter at all and must be skipped rather than run.

$env:PYTHONPATH = Join-Path $PSScriptRoot "src"

function Test-Python {
    param([string]$Exe, [string[]]$Prefix = @())
    if (-not (Get-Command $Exe -ErrorAction SilentlyContinue)) { return $false }
    try {
        & $Exe @Prefix -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" *> $null
        return $LASTEXITCODE -eq 0
    } catch {
        return $false
    }
}

if ($env:PL_PYTHON) {
    if (Test-Python $env:PL_PYTHON) {
        & $env:PL_PYTHON -m provenlattice.cli @args
        exit $LASTEXITCODE
    }
    Write-Warning "PL_PYTHON=$env:PL_PYTHON is not a Python >= 3.11; falling through"
}

foreach ($version in @("-3.12", "-3.11", "-3")) {
    if (Test-Python "py" @($version)) {
        & py $version -m provenlattice.cli @args
        exit $LASTEXITCODE
    }
}

foreach ($name in @("python", "python3")) {
    if (Test-Python $name) {
        & $name -m provenlattice.cli @args
        exit $LASTEXITCODE
    }
}

Write-Error @"
pl.ps1: no Python >= 3.11 found.

Find one with `py -0p` (Windows launcher: every install, with its path) or
`where.exe python`, then set PL_PYTHON to it:

  `$env:PL_PYTHON = "D:\program\Python312\python.exe"
  .\pl.ps1 status --database <db>
"@
exit 1

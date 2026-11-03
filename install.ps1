# learning-harness installer launcher for Windows (PowerShell 5.1+).
#
#   From a clone:  powershell -ExecutionPolicy Bypass -File install.ps1 [options]   (or double-click install.bat)
#   One line:      irm https://raw.githubusercontent.com/longlephucwork/claude-learning-harness/main/install.ps1 | iex
#
# Finds Python 3.10+, clones (or updates) the repo if this script isn't inside one, then runs install.py,
# which asks before every change. Options are passed through: -- --yes --check --uninstall ...
# Clone location: $env:LEARNING_HARNESS_DIR (default %USERPROFILE%\claude-learning-harness).
#
# Note: native Windows has no network sandbox for generated code, so the installer will ask whether to
# allow running learned tools without one. For full isolation, install inside WSL with install.sh instead.

$RepoUrl = "https://github.com/longlephucwork/claude-learning-harness.git"

$py = $null
foreach ($cand in @("py -3", "python", "python3")) {
    $parts = $cand.Split(" ")
    if (-not (Get-Command $parts[0] -ErrorAction SilentlyContinue)) { continue }
    $extra = @(); if ($parts.Length -gt 1) { $extra = $parts[1..($parts.Length - 1)] }
    & $parts[0] @extra -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" *> $null
    if ($LASTEXITCODE -eq 0) { $py = $parts; break }
}
if (-not $py) {
    Write-Host "error: Python 3.10+ not found. Install it with:  winget install Python.Python.3.12" -ForegroundColor Red
    Write-Host "       (or from https://www.python.org/downloads/ ; tick 'Add python.exe to PATH')"
    exit 1
}

$here = $PSScriptRoot
if ($here -and (Test-Path (Join-Path $here "install.py"))) {
    $repo = $here
} else {
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
        Write-Host "error: git not found. Install it with:  winget install Git.Git" -ForegroundColor Red
        exit 1
    }
    $repo = if ($env:LEARNING_HARNESS_DIR) { $env:LEARNING_HARNESS_DIR } else { Join-Path $env:USERPROFILE "claude-learning-harness" }
    if (Test-Path (Join-Path $repo ".git")) {
        Write-Host "updating existing clone in $repo"
        git -C $repo pull --ff-only
    } elseif (Test-Path $repo) {
        Write-Host "error: $repo exists but is not a git clone; set LEARNING_HARNESS_DIR to another folder" -ForegroundColor Red
        exit 1
    } else {
        Write-Host "cloning into $repo"
        git clone $RepoUrl $repo
    }
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

$extra = @(); if ($py.Length -gt 1) { $extra = $py[1..($py.Length - 1)] }
& $py[0] @extra (Join-Path $repo "install.py") @args
exit $LASTEXITCODE

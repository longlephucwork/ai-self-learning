# learning-harness installer launcher for Windows (PowerShell 5.1+).
#
#   From a clone:  powershell -ExecutionPolicy Bypass -File install.ps1 [options]   (or double-click install.bat)
#   One line:      irm https://raw.githubusercontent.com/longlephucwork/ai-self-learning/main/install.ps1 | iex
#
# Finds Python 3.10+, clones (or updates) the repo if this script isn't inside one, then runs install.py,
# which asks before every change. Options are passed through: -- --yes --check --uninstall ...
# Clone location: $env:LEARNING_HARNESS_DIR (default %USERPROFILE%\ai-self-learning; an install from before the rename, in %USERPROFILE%\claude-learning-harness, is kept).
#
# Note: native Windows has no network sandbox for generated code, so the installer will ask whether to
# allow running learned tools without one. For full isolation, install inside WSL with install.sh instead.

$RepoUrl = if ($env:LEARNING_HARNESS_REPO_URL) { $env:LEARNING_HARNESS_REPO_URL } else { "https://github.com/longlephucwork/ai-self-learning.git" }  # override for forks

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
    $repo = if ($env:LEARNING_HARNESS_DIR) { $env:LEARNING_HARNESS_DIR } else { Join-Path $env:USERPROFILE "ai-self-learning" }
    # installed before the repo was renamed: keep using that clone (its hooks and PATH entry point there)
    $old = Join-Path $env:USERPROFILE "claude-learning-harness"
    if (-not $env:LEARNING_HARNESS_DIR -and -not (Test-Path $repo) -and (Test-Path (Join-Path $old ".git"))) { $repo = $old }
    if (Test-Path (Join-Path $repo ".git")) {
        Write-Host "already installed in $repo (re-running the installer; to get a new release: python install.py --update)"
    } elseif (Test-Path $repo) {
        Write-Host "error: $repo exists but is not a git clone; set LEARNING_HARNESS_DIR to another folder" -ForegroundColor Red
        exit 1
    } else {
        Write-Host "cloning into $repo"
        git clone $RepoUrl $repo
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
        # start from the newest release (vX.Y.Z tag on main), not whatever is on main right now
        $latest = git -C $repo tag --merged origin/main --list "v[0-9]*" --sort=-v:refname | Select-Object -First 1
        if ($latest) {
            Write-Host "using release $latest"
            git -C $repo reset --quiet --hard $latest
        }
    }
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

$extra = @(); if ($py.Length -gt 1) { $extra = $py[1..($py.Length - 1)] }
& $py[0] @extra (Join-Path $repo "install.py") @args
exit $LASTEXITCODE

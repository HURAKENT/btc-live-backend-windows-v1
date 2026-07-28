$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $ProjectRoot

$PythonPath = ".\.venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) {
    Write-Error "Windows virtual environment is missing."
    exit 30
}

$env:PYTHONNOUSERSITE = "1"
$env:PYTHONDONTWRITEBYTECODE = "1"

$PythonVersion = & $PythonPath -c "import sys; print('.'.join(str(value) for value in sys.version_info[:3]))"
if ($LASTEXITCODE -ne 0 -or $PythonVersion -notmatch '^3\.12\.') {
    Write-Error "Python 3.12.x is required."
    exit 30
}

if (-not (Test-Path -LiteralPath ".\pyproject.toml" -PathType Leaf)) {
    Write-Error "Project root validation failed."
    exit 30
}
if (-not (Test-Path -LiteralPath ".\config\c0_c1_frozen_config.json" -PathType Leaf)) {
    Write-Error "Frozen runtime config is missing."
    exit 30
}

& $PythonPath -m pip check
if ($LASTEXITCODE -ne 0) {
    exit 30
}

$RuntimeDirectory = Join-Path $ProjectRoot "data\runtime"
if (-not (Test-Path -LiteralPath $RuntimeDirectory -PathType Container)) {
    New-Item -ItemType Directory -Path $RuntimeDirectory | Out-Null
}

$RunLogPath = Join-Path $RuntimeDirectory "backend-run.log"
& $PythonPath ".\run_backend.py" 2>&1 | Tee-Object -FilePath $RunLogPath
$BackendExitCode = $LASTEXITCODE
exit $BackendExitCode

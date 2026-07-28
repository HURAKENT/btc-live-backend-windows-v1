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

& $PythonPath -m unittest discover -s tests -v
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

& $PythonPath -m compileall -q src tests tools run_backend.py
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

& $PythonPath -m pip check
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

Write-Output "EXTERNAL_PROVIDER_CAPABILITY_NOT_RUN: Task 13 remains required."
Write-Output "DOWNTIME_ACCEPTANCE_NOT_RUN: Task 14 remains required."
Write-Output "C1 acceptance remains incomplete until Tasks 13 and 14 pass."
exit 0

param(
    [string]$PythonPath
)

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $ProjectRoot

if ([string]::IsNullOrWhiteSpace($PythonPath)) {
    $PythonPath = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
}
$PythonPath = [IO.Path]::GetFullPath($PythonPath)
if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) {
    [Console]::Error.WriteLine("WINDOWS_VENV_NOT_FOUND: $PythonPath")
    exit 30
}
if (-not (Test-Path -LiteralPath (Join-Path $ProjectRoot "run_windows_backend.py") -PathType Leaf)) {
    [Console]::Error.WriteLine("C9_BACKEND_LAUNCHER_NOT_FOUND")
    exit 30
}
if (-not (Test-Path -LiteralPath (Join-Path $ProjectRoot "config\mvp_runtime_v1.json") -PathType Leaf)) {
    [Console]::Error.WriteLine("MVP_RUNTIME_CONFIG_NOT_FOUND")
    exit 30
}

$env:PYTHONNOUSERSITE = "1"
$env:PYTHONDONTWRITEBYTECODE = "1"

$PythonVersion = & $PythonPath -c "import sys; print('.'.join(str(value) for value in sys.version_info[:3]))"
if ($LASTEXITCODE -ne 0 -or $PythonVersion -notmatch '^3\.12\.') {
    [Console]::Error.WriteLine("PYTHON_3_12_REQUIRED")
    exit 30
}

& $PythonPath (Join-Path $ProjectRoot "run_windows_backend.py")
exit $LASTEXITCODE

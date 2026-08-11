param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("Start", "Status", "Restart", "Stop")]
    [string]$Operation,

    [string]$RunDirectory,

    [string]$ObservationRoot,

    [string]$DatabasePath,

    [int]$TimeoutSeconds = 45,

    [string]$PythonPath
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($PythonPath)) {
    $PythonPath = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
}
$PythonPath = [IO.Path]::GetFullPath($PythonPath)
$Controller = Join-Path $ProjectRoot "c11_observation.py"

if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) {
    [Console]::Error.WriteLine("WINDOWS_VENV_NOT_FOUND")
    exit 50
}
if (-not (Test-Path -LiteralPath $Controller -PathType Leaf)) {
    [Console]::Error.WriteLine("C11_OBSERVATION_CONTROLLER_NOT_FOUND")
    exit 50
}

$Arguments = @($Controller, $Operation.ToLowerInvariant())
if ($Operation -eq "Start") {
    if (-not [string]::IsNullOrWhiteSpace($ObservationRoot)) {
        $Arguments += @("--observation-root", [IO.Path]::GetFullPath($ObservationRoot))
    }
    if (-not [string]::IsNullOrWhiteSpace($DatabasePath)) {
        $Arguments += @("--database-path", [IO.Path]::GetFullPath($DatabasePath))
    }
}
else {
    if ([string]::IsNullOrWhiteSpace($RunDirectory)) {
        [Console]::Error.WriteLine("C11_RUN_DIRECTORY_REQUIRED")
        exit 50
    }
    $Arguments += @("--run-dir", [IO.Path]::GetFullPath($RunDirectory))
    if ($Operation -eq "Restart" -or $Operation -eq "Stop") {
        $Arguments += @("--timeout-seconds", $TimeoutSeconds)
    }
}

& $PythonPath @Arguments
exit $LASTEXITCODE

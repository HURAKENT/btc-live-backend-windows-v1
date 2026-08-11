param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("Backup", "Validate", "RestoreDrill")]
    [string]$Operation,

    [Parameter(Mandatory = $true)]
    [string]$Source,

    [string]$Destination,

    [string]$PythonPath
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($PythonPath)) {
    $PythonPath = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
}
$PythonPath = [IO.Path]::GetFullPath($PythonPath)
$CliPath = Join-Path $ProjectRoot "windows_database.py"

if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) {
    [Console]::Error.WriteLine("WINDOWS_VENV_NOT_FOUND: $PythonPath")
    exit 30
}
if (-not (Test-Path -LiteralPath $CliPath -PathType Leaf)) {
    [Console]::Error.WriteLine("C9_DATABASE_CLI_NOT_FOUND")
    exit 30
}

$Command = switch ($Operation) {
    "Backup" { "backup" }
    "Validate" { "validate" }
    "RestoreDrill" { "restore-drill" }
}
if ($Command -ne "validate" -and [string]::IsNullOrWhiteSpace($Destination)) {
    [Console]::Error.WriteLine("DATABASE_DESTINATION_REQUIRED")
    exit 30
}

$Arguments = @($CliPath, $Command, [IO.Path]::GetFullPath($Source))
if ($Command -ne "validate") {
    $Arguments += [IO.Path]::GetFullPath($Destination)
}
& $PythonPath @Arguments
exit $LASTEXITCODE

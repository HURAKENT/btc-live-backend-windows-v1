param(
    [ValidateSet("Run", "Preflight", "Offline")]
    [string]$Mode = "Run",

    [string]$PythonPath = ".\.venv\Scripts\python.exe"
)

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $ProjectRoot

if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) {
    [Console]::Error.WriteLine("Windows virtual environment is missing: $PythonPath")
    exit 30
}

$env:PYTHONNOUSERSITE = "1"
$env:PYTHONDONTWRITEBYTECODE = "1"

function Invoke-NativePython {
    param([string[]]$ChildArguments)

    $ResolvedPython = (Resolve-Path -LiteralPath $PythonPath).Path
    foreach ($Argument in $ChildArguments) {
        if ($Argument -match '[\s"]') {
            throw "Unsafe native child argument: $Argument"
        }
    }

    $StartInfo = New-Object System.Diagnostics.ProcessStartInfo
    $StartInfo.FileName = $ResolvedPython
    $StartInfo.WorkingDirectory = $ProjectRoot
    $StartInfo.UseShellExecute = $false
    $StartInfo.RedirectStandardOutput = $true
    $StartInfo.RedirectStandardError = $true
    $StartInfo.CreateNoWindow = $true
    $StartInfo.Arguments = ($ChildArguments -join " ")

    $Process = New-Object System.Diagnostics.Process
    $Process.StartInfo = $StartInfo
    try {
        if (-not $Process.Start()) {
            throw "Native process did not start."
        }
        $StdOutTask = $Process.StandardOutput.ReadToEndAsync()
        $StdErrTask = $Process.StandardError.ReadToEndAsync()
        $Process.WaitForExit()
        $StdOut = $StdOutTask.GetAwaiter().GetResult()
        $StdErr = $StdErrTask.GetAwaiter().GetResult()
        return [PSCustomObject]@{
            ExitCode = $Process.ExitCode
            StdOut = $StdOut
            StdErr = $StdErr
            Executable = $ResolvedPython
        }
    }
    catch {
        return [PSCustomObject]@{
            ExitCode = 9009
            StdOut = ""
            StdErr = $_.Exception.Message
            Executable = $ResolvedPython
        }
    }
    finally {
        $Process.Dispose()
    }
}

function Write-ChildResult {
    param($Result)
    if (-not [string]::IsNullOrEmpty($Result.StdOut)) {
        [Console]::Out.Write($Result.StdOut)
    }
    if (-not [string]::IsNullOrEmpty($Result.StdErr)) {
        [Console]::Error.Write($Result.StdErr)
    }
}

$VersionResult = Invoke-NativePython @("--version")
Write-ChildResult $VersionResult
$VersionLines = @(
    $VersionResult.StdOut -split "\r?\n" |
        Where-Object { -not [string]::IsNullOrWhiteSpace($_) }
)
Write-Output ("RAW_VERSION_COUNT=" + $VersionLines.Count)
Write-Output ("child exit code=" + $VersionResult.ExitCode)
if ($VersionResult.ExitCode -ne 0) {
    [Console]::Error.WriteLine(
        "Python execution failed: executable=$($VersionResult.Executable); " +
        "child exit $($VersionResult.ExitCode); stderr=$($VersionResult.StdErr.Trim())"
    )
    exit 30
}
$PythonVersion = ""
if (
    $VersionLines.Count -eq 1 -and
    $VersionLines[0] -match '^Python (?<Version>[0-9]+\.[0-9]+\.[0-9]+)$'
) {
    $PythonVersion = $Matches.Version
}
if ($PythonVersion -notmatch '^3\.12\.') {
    [Console]::Error.WriteLine(
        "Python 3.12.x is required; actual version: $PythonVersion"
    )
    exit 30
}

if ($Mode -eq "Preflight") {
    exit 0
}

$Commands = @(
    @("-m", "unittest", "tests.test_process_runtime_integration", "-v"),
    @("-m", "unittest", "discover", "-s", "tests", "-v"),
    @("-m", "compileall", "-q", "src", "tests", "tools", "run_backend.py"),
    @("-m", "pip", "check")
)
if ($Mode -eq "Run") {
    $Commands += ,@("tools\simulate_downtime.py")
}

foreach ($Command in $Commands) {
    $Result = Invoke-NativePython $Command
    Write-ChildResult $Result
    if ($Result.ExitCode -ne 0) {
        exit $Result.ExitCode
    }
}

exit 0

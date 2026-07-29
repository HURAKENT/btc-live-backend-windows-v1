param(
    [ValidateSet('Run', 'Preflight')]
    [string]$Mode = 'Run',

    [switch]$NoPause
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$global:LASTEXITCODE = 0

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$CodexRoot = Split-Path -Parent $ProjectRoot
$LogBase = Join-Path $CodexRoot 'task14_manual_logs'
$PythonPath = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
$AcceptanceLauncher = Join-Path `
    $ProjectRoot `
    'scripts\RUN_C1_ACCEPTANCE_SAFE.ps1'
$GitCommand = Get-Command git.exe -ErrorAction SilentlyContinue
if ($null -ne $GitCommand) {
    $GitExecutable = $GitCommand.Source
}
else {
    $GitExecutable = Join-Path $env:ProgramFiles 'Git\cmd\git.exe'
    if (-not (Test-Path -LiteralPath $GitExecutable -PathType Leaf)) {
        throw 'Windows Git executable was not found.'
    }
}

Set-Location -LiteralPath $ProjectRoot

function Invoke-NativeProcess {
    param(
        [string]$Executable,
        [string]$ArgumentString
    )

    $StartInfo = New-Object System.Diagnostics.ProcessStartInfo
    $StartInfo.FileName = $Executable
    $StartInfo.WorkingDirectory = $ProjectRoot
    $StartInfo.UseShellExecute = $false
    $StartInfo.RedirectStandardOutput = $true
    $StartInfo.RedirectStandardError = $true
    $StartInfo.CreateNoWindow = $true
    $StartInfo.Arguments = $ArgumentString

    $Process = New-Object System.Diagnostics.Process
    $Process.StartInfo = $StartInfo
    try {
        if (-not $Process.Start()) {
            throw 'Git process did not start.'
        }
        $StdOutTask = $Process.StandardOutput.ReadToEndAsync()
        $StdErrTask = $Process.StandardError.ReadToEndAsync()
        $Process.WaitForExit()
        return [PSCustomObject]@{
            ExitCode = $Process.ExitCode
            StdOut = $StdOutTask.GetAwaiter().GetResult()
            StdErr = $StdErrTask.GetAwaiter().GetResult()
        }
    }
    finally {
        $Process.Dispose()
    }
}

function Invoke-GitCommand {
    param([string[]]$Arguments)

    foreach ($Argument in $Arguments) {
        if ($Argument -notmatch '^[A-Za-z0-9._/=-]+$') {
            throw "Unsafe Git argument: $Argument"
        }
    }
    return Invoke-NativeProcess `
        -Executable $GitExecutable `
        -ArgumentString ($Arguments -join ' ')
}

function Get-RequiredGitValue {
    param([string[]]$Arguments)

    $Result = Invoke-GitCommand -Arguments $Arguments
    if ($Result.ExitCode -ne 0) {
        throw (
            "Git command failed ($($Result.ExitCode)): " +
            "git $($Arguments -join ' '); $($Result.StdErr.Trim())"
        )
    }
    return $Result.StdOut.Trim()
}

function Test-ManualTask14Preflight {
    $Branch = Get-RequiredGitValue -Arguments @(
        'branch',
        '--show-current'
    )
    $Head = Get-RequiredGitValue -Arguments @('rev-parse', 'HEAD')
    $Origin = Get-RequiredGitValue -Arguments @(
        'rev-parse',
        'origin/codex/c0-c1'
    )
    $StatusResult = Invoke-GitCommand -Arguments @(
        'status',
        '--porcelain=v1',
        '--untracked-files=all'
    )
    if ($StatusResult.ExitCode -ne 0) {
        throw "Git status failed: $($StatusResult.StdErr.Trim())"
    }
    $StatusLines = @(
        $StatusResult.StdOut -split "\r?\n" |
            Where-Object { -not [string]::IsNullOrWhiteSpace($_) }
    )
    $DiffResult = Invoke-GitCommand -Arguments @(
        'diff',
        '--quiet',
        '--exit-code'
    )
    if ($DiffResult.ExitCode -ne 0) {
        throw 'Tracked worktree is not clean.'
    }
    $CachedDiffResult = Invoke-GitCommand -Arguments @(
        'diff',
        '--cached',
        '--quiet',
        '--exit-code'
    )
    if ($CachedDiffResult.ExitCode -ne 0) {
        throw 'Git index is not clean.'
    }
    if ($Branch -ne 'codex/c0-c1') {
        throw "Unexpected branch: $Branch"
    }
    if ($Head -ne $Origin) {
        throw "HEAD does not match origin/codex/c0-c1."
    }
    if ($StatusLines.Count -ne 0) {
        throw "Worktree contains tracked or untracked changes."
    }
    if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) {
        throw "Windows virtual environment is missing: $PythonPath"
    }
    $PythonResult = Invoke-NativeProcess `
        -Executable $PythonPath `
        -ArgumentString '--version'
    $PythonVersion = $PythonResult.StdOut.Trim()
    if ($PythonResult.ExitCode -ne 0) {
        throw "Python version command failed: $($PythonResult.ExitCode)"
    }
    if ($PythonVersion -notmatch '^Python 3\.12\.[0-9]+$') {
        throw "Python 3.12.x is required; actual: $PythonVersion"
    }
    return [PSCustomObject]@{
        Branch = $Branch
        Head = $Head
        Origin = $Origin
        PythonVersion = $PythonVersion
    }
}

if ($Mode -eq 'Preflight') {
    try {
        $Preflight = Test-ManualTask14Preflight
        Write-Output ("BRANCH=" + $Preflight.Branch)
        Write-Output ("HEAD=" + $Preflight.Head)
        Write-Output ("ORIGIN=" + $Preflight.Origin)
        Write-Output $Preflight.PythonVersion
        $PowerShellExecutable = Join-Path `
            $env:SystemRoot `
            'System32\WindowsPowerShell\v1.0\powershell.exe'
        $LauncherArguments = (
            '-NoProfile -ExecutionPolicy Bypass -File "' +
            $AcceptanceLauncher +
            '" -Mode Preflight'
        )
        $LauncherPreflight = Invoke-NativeProcess `
            -Executable $PowerShellExecutable `
            -ArgumentString $LauncherArguments
        if (-not [string]::IsNullOrEmpty($LauncherPreflight.StdOut)) {
            [Console]::Out.Write($LauncherPreflight.StdOut)
        }
        if (-not [string]::IsNullOrEmpty($LauncherPreflight.StdErr)) {
            [Console]::Error.Write($LauncherPreflight.StdErr)
        }
        if ($LauncherPreflight.ExitCode -ne 0) {
            exit $LauncherPreflight.ExitCode
        }
        Write-Output 'TASK14_MANUAL_PREFLIGHT_PASS'
        exit 0
    }
    catch {
        [Console]::Error.WriteLine($_.Exception.ToString())
        exit 1
    }
}

$Preflight = $null
try {
    $Preflight = Test-ManualTask14Preflight
}
catch {
    [Console]::Error.WriteLine($_.Exception.ToString())
    exit 1
}

$Timestamp = [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffZ')
$LogDirectory = Join-Path $LogBase ("manual_task14_" + $Timestamp)
$TranscriptPath = Join-Path `
    $LogDirectory `
    'task14_console_transcript.txt'
New-Item -ItemType Directory -Path $LogDirectory | Out-Null

$StartUtc = [DateTime]::UtcNow
$EndUtc = $StartUtc
$LauncherExitCode = 1
$TranscriptStarted = $false

try {
    Start-Transcript -LiteralPath $TranscriptPath -NoClobber | Out-Null
    $TranscriptStarted = $true

    Write-Output ("BRANCH=" + $Preflight.Branch)
    Write-Output ("HEAD=" + $Preflight.Head)
    Write-Output ("ORIGIN=" + $Preflight.Origin)
    Write-Output 'GIT_STATUS=CLEAN'
    Write-Output $Preflight.PythonVersion
    Write-Output ("TASK14_START_UTC=" + $StartUtc.ToString('o'))
    Write-Output 'EXPECTED_DURATION=at least 11-13 minutes'
    Write-Output (
        'During the 605-second downtime, no console output is expected.'
    )

    & powershell.exe `
        -NoProfile `
        -ExecutionPolicy Bypass `
        -File $AcceptanceLauncher
    $LauncherExitCode = $LASTEXITCODE
}
catch {
    [Console]::Error.WriteLine($_.Exception.ToString())
    if ($LauncherExitCode -eq 0) {
        $LauncherExitCode = 1
    }
}
finally {
    $EndUtc = [DateTime]::UtcNow
    if ($TranscriptStarted) {
        Stop-Transcript | Out-Null
    }
}

$Elapsed = $EndUtc - $StartUtc
Write-Output ("TASK14_START_UTC=" + $StartUtc.ToString('o'))
Write-Output ("TASK14_END_UTC=" + $EndUtc.ToString('o'))
Write-Output ("TASK14_ELAPSED_SECONDS=" + $Elapsed.TotalSeconds)
Write-Output ("TASK14_LAUNCHER_EXIT=" + $LauncherExitCode)
Write-Output ("TASK14_TRANSCRIPT=" + $TranscriptPath)
Write-Output (
    'TASK14_DOWNTIME_REPORT=reports\C1_DOWNTIME_ACCEPTANCE.json'
)
Write-Output 'TASK14_FINAL_REPORT=reports\C1_FINAL_ACCEPTANCE.json'
Write-Output 'TASK14_ACCEPTANCE_PACK=artifacts\C1_ACCEPTANCE_PACK.zip'
if ($LauncherExitCode -eq 0) {
    Write-Output 'TASK14_RESULT=PASS'
}
else {
    Write-Output 'TASK14_RESULT=BLOCKED'
}

if (-not $NoPause) {
    Read-Host 'Нажмите Enter, чтобы закрыть это окно'
}

exit $LauncherExitCode

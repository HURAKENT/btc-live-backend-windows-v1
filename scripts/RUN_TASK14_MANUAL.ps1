param(
    [ValidateSet('Run', 'Preflight')]
    [string]$Mode = 'Run',

    [switch]$NoPause
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$CodexRoot = Split-Path -Parent $ProjectRoot
$LogBase = Join-Path $CodexRoot 'task14_manual_logs'
$PythonPath = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
$AcceptanceLauncher = Join-Path `
    $ProjectRoot `
    'scripts\RUN_C1_ACCEPTANCE_SAFE.ps1'
$PowerShellExecutable = Join-Path `
    $env:SystemRoot `
    'System32\WindowsPowerShell\v1.0\powershell.exe'
$DowntimeReportPath = Join-Path `
    $ProjectRoot `
    'reports\C1_DOWNTIME_ACCEPTANCE.json'
$FinalReportPath = Join-Path `
    $ProjectRoot `
    'reports\C1_FINAL_ACCEPTANCE.json'
$AcceptancePackPath = Join-Path `
    $ProjectRoot `
    'artifacts\C1_ACCEPTANCE_PACK.zip'
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
            throw 'Native process did not start.'
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

function Get-FileSnapshot {
    param([string]$Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return [PSCustomObject]@{
            Exists = $false
            Sha256 = $null
            Size = $null
            LastWriteUtc = $null
        }
    }
    $Item = Get-Item -LiteralPath $Path
    return [PSCustomObject]@{
        Exists = $true
        Sha256 = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
        Size = $Item.Length
        LastWriteUtc = $Item.LastWriteTimeUtc.ToString('o')
    }
}

function Get-ArtifactSnapshot {
    return [PSCustomObject]@{
        Downtime = Get-FileSnapshot -Path $DowntimeReportPath
        Final = Get-FileSnapshot -Path $FinalReportPath
        Pack = Get-FileSnapshot -Path $AcceptancePackPath
    }
}

function Get-RequiredJsonObject {
    param([string]$Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw 'ARTIFACT_MISSING'
    }
    try {
        $Value = Get-Content -LiteralPath $Path -Raw -Encoding UTF8 |
            ConvertFrom-Json
    }
    catch {
        throw 'ARTIFACT_INVALID'
    }
    if ($null -eq $Value -or $Value -is [System.Array]) {
        throw 'ARTIFACT_INVALID'
    }
    return $Value
}

function Get-TerminalJson {
    param([string]$StdOut)

    $SawCandidate = $false
    $Lines = @($StdOut -split "\r?\n")
    [array]::Reverse($Lines)
    foreach ($Line in $Lines) {
        $Candidate = $Line.Trim()
        if (-not ($Candidate.StartsWith('{') -and $Candidate.EndsWith('}'))) {
            continue
        }
        $SawCandidate = $true
        try {
            $Value = $Candidate | ConvertFrom-Json
        }
        catch {
            continue
        }
        $Names = @($Value.PSObject.Properties.Name)
        if (
            $Names -contains 'status' -and
            $Names -contains 'run_id' -and
            $Names -contains 'pack_sha256'
        ) {
            return $Value
        }
    }
    if ($SawCandidate) {
        throw 'TERMINAL_JSON_INVALID'
    }
    throw 'TERMINAL_JSON_MISSING'
}

function Test-ZipReadable {
    param([string]$Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw 'ARTIFACT_MISSING'
    }
    try {
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        $Archive = [System.IO.Compression.ZipFile]::OpenRead($Path)
        try {
            $null = $Archive.Entries.Count
        }
        finally {
            $Archive.Dispose()
        }
    }
    catch {
        throw 'ARTIFACT_INVALID'
    }
}

function Test-ArtifactFreshness {
    param(
        $Before,
        $After
    )

    if (
        $Before.Downtime.Sha256 -eq $After.Downtime.Sha256 -or
        $Before.Final.Sha256 -eq $After.Final.Sha256 -or
        $Before.Pack.Sha256 -eq $After.Pack.Sha256
    ) {
        throw 'STALE_ARTIFACTS'
    }
}

function Test-Task14PassEvidence {
    param(
        $Preflight,
        $BeforeArtifacts,
        $LauncherResult,
        [double]$ElapsedSeconds
    )

    if ($ElapsedSeconds -lt 600.0) {
        throw 'IMPOSSIBLE_PASS_DURATION'
    }
    $Terminal = Get-TerminalJson -StdOut $LauncherResult.StdOut
    if ($Terminal.status -ne 'PASS' -or [string]::IsNullOrWhiteSpace($Terminal.run_id)) {
        throw 'TERMINAL_JSON_INVALID'
    }

    $Downtime = Get-RequiredJsonObject -Path $DowntimeReportPath
    $Final = Get-RequiredJsonObject -Path $FinalReportPath
    Test-ZipReadable -Path $AcceptancePackPath

    if (
        $Downtime.run_id -ne $Terminal.run_id -or
        $Final.run_id -ne $Terminal.run_id
    ) {
        throw 'RUN_ID_MISMATCH'
    }
    if (
        $Downtime.status -ne 'PASS' -or
        $Final.status -ne 'PASS' -or
        $Downtime.gate -ne 'BTC_LIVE_BACKEND_WINDOWS_V1_C1_PASS' -or
        $Final.gate -ne 'BTC_LIVE_BACKEND_WINDOWS_V1_C1_PASS' -or
        $Final.trading_approval -ne $false
    ) {
        throw 'ARTIFACT_INVALID'
    }
    foreach ($Commit in @(
        $Downtime.commit_sha,
        $Downtime.commit_under_test,
        $Downtime.source_commit,
        $Downtime.acceptance_harness_commit,
        $Final.commit_sha,
        $Final.commit_under_test,
        $Final.source_commit,
        $Final.acceptance_harness_commit
    )) {
        if ($Commit -ne $Preflight.Head) {
            throw 'COMMIT_PROVENANCE_MISMATCH'
        }
    }

    $PackSnapshot = Get-FileSnapshot -Path $AcceptancePackPath
    if (
        $PackSnapshot.Sha256 -ne $Terminal.pack_sha256 -or
        $Final.acceptance_pack.sha256 -ne $Terminal.pack_sha256 -or
        $Final.acceptance_pack.size -ne $PackSnapshot.Size
    ) {
        throw 'PACK_HASH_MISMATCH'
    }
    $AfterArtifacts = Get-ArtifactSnapshot
    Test-ArtifactFreshness -Before $BeforeArtifacts -After $AfterArtifacts

    return [PSCustomObject]@{
        Terminal = $Terminal
        Downtime = $Downtime
        Final = $Final
        Artifacts = $AfterArtifacts
    }
}

function Get-ObserverCategory {
    param([string]$Message)

    $Known = @(
        'IMPOSSIBLE_PASS_DURATION',
        'TERMINAL_JSON_MISSING',
        'TERMINAL_JSON_INVALID',
        'RUN_ID_MISMATCH',
        'COMMIT_PROVENANCE_MISMATCH',
        'ARTIFACT_MISSING',
        'ARTIFACT_INVALID',
        'PACK_HASH_MISMATCH',
        'STALE_ARTIFACTS'
    )
    if ($Known -contains $Message) {
        return $Message
    }
    return 'ARTIFACT_INVALID'
}

function Move-AtomicFile {
    param(
        [string]$Source,
        [string]$Destination
    )

    if (Test-Path -LiteralPath $Destination -PathType Leaf) {
        $Backup = $Destination + ".bak.$PID." + [Guid]::NewGuid().ToString('N')
        try {
            [System.IO.File]::Replace(
                $Source,
                $Destination,
                $Backup,
                $true
            )
        }
        finally {
            if (Test-Path -LiteralPath $Backup -PathType Leaf) {
                Remove-Item -LiteralPath $Backup -Force
            }
        }
        return
    }
    [System.IO.File]::Move($Source, $Destination)
}

function Write-JsonAtomic {
    param(
        [string]$Path,
        $Value
    )

    $Parent = Split-Path -Parent $Path
    New-Item -ItemType Directory -Path $Parent -Force | Out-Null
    $TemporaryPath = Join-Path `
        $Parent `
        ((Split-Path -Leaf $Path) + ".tmp.$PID." + [Guid]::NewGuid().ToString('N'))
    $Utf8 = New-Object System.Text.UTF8Encoding($false)
    try {
        $Json = $Value | ConvertTo-Json -Depth 8
        [System.IO.File]::WriteAllText(
            $TemporaryPath,
            $Json + [Environment]::NewLine,
            $Utf8
        )
        $RoundTrip = Get-Content -LiteralPath $TemporaryPath -Raw -Encoding UTF8 |
            ConvertFrom-Json
        if ($RoundTrip.schema_version -ne 1) {
            throw 'INVALID_RECEIPT_SCHEMA'
        }
        Move-AtomicFile -Source $TemporaryPath -Destination $Path
    }
    finally {
        if (Test-Path -LiteralPath $TemporaryPath -PathType Leaf) {
            Remove-Item -LiteralPath $TemporaryPath -Force
        }
    }
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
        throw 'HEAD does not match origin/codex/c0-c1.'
    }
    if ($StatusLines.Count -ne 0) {
        throw 'Worktree contains tracked or untracked changes.'
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
$ReceiptPath = Join-Path $LogDirectory 'task14_launcher_receipt.json'
$LatestReceiptPath = Join-Path `
    $LogBase `
    'LATEST_TASK14_LAUNCHER_RECEIPT.json'
New-Item -ItemType Directory -Path $LogDirectory -Force | Out-Null

$BeforeArtifacts = Get-ArtifactSnapshot
$StartUtc = [DateTime]::UtcNow
$EndUtc = $StartUtc
$Stopwatch = [System.Diagnostics.Stopwatch]::StartNew()
$LauncherExitCode = 1
$RunnerExitCode = 2
$Result = 'BLOCKED'
$ObserverStatus = 'LAUNCHER_NOT_STARTED'
$TranscriptStarted = $false
$TranscriptClosed = $false
$TranscriptSha256 = $null
$Evidence = $null
$RunId = $null
$Gate = $null
$SourceCommit = $null
$HarnessCommit = $null
$DowntimeSha256 = $null
$FinalSha256 = $null
$PackSha256 = $null
$PackSize = $null

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

    $LauncherArguments = (
        '-NoProfile -ExecutionPolicy Bypass -File "' +
        $AcceptanceLauncher +
        '" -Mode Run'
    )
    $LauncherResult = Invoke-NativeProcess `
        -Executable $PowerShellExecutable `
        -ArgumentString $LauncherArguments
    $LauncherExitCode = $LauncherResult.ExitCode
    if (-not [string]::IsNullOrEmpty($LauncherResult.StdOut)) {
        [Console]::Out.Write($LauncherResult.StdOut)
    }
    if (-not [string]::IsNullOrEmpty($LauncherResult.StdErr)) {
        [Console]::Error.Write($LauncherResult.StdErr)
    }

    $Stopwatch.Stop()
    $EndUtc = [DateTime]::UtcNow
    $ElapsedSeconds = $Stopwatch.Elapsed.TotalSeconds

    if ($LauncherExitCode -eq 0) {
        try {
            $Evidence = Test-Task14PassEvidence `
                -Preflight $Preflight `
                -BeforeArtifacts $BeforeArtifacts `
                -LauncherResult $LauncherResult `
                -ElapsedSeconds $ElapsedSeconds
            $ObserverStatus = 'CONFIRMED'
            $Result = 'PASS'
            $RunnerExitCode = 0
            $RunId = $Evidence.Terminal.run_id
            $Gate = $Evidence.Final.gate
            $SourceCommit = $Evidence.Final.source_commit
            $HarnessCommit = $Evidence.Final.acceptance_harness_commit
            $DowntimeSha256 = $Evidence.Artifacts.Downtime.Sha256
            $FinalSha256 = $Evidence.Artifacts.Final.Sha256
            $PackSha256 = $Evidence.Artifacts.Pack.Sha256
            $PackSize = $Evidence.Artifacts.Pack.Size
        }
        catch {
            $ObserverStatus = Get-ObserverCategory `
                -Message $_.Exception.Message
            $Result = 'BLOCKED'
            $RunnerExitCode = 2
        }
    }
    else {
        $ObserverStatus = "CHILD_EXIT_$LauncherExitCode"
        $RunnerExitCode = $LauncherExitCode
    }

    Write-Output ("TASK14_END_UTC=" + $EndUtc.ToString('o'))
    Write-Output ("TASK14_ELAPSED_SECONDS=" + $ElapsedSeconds)
    Write-Output ("TASK14_LAUNCHER_EXIT=" + $LauncherExitCode)
    Write-Output ("TASK14_OBSERVER_STATUS=" + $ObserverStatus)
    Write-Output ("TASK14_RESULT=" + $Result)
    Write-Output (
        'TASK14_DOWNTIME_REPORT=reports\C1_DOWNTIME_ACCEPTANCE.json'
    )
    Write-Output 'TASK14_FINAL_REPORT=reports\C1_FINAL_ACCEPTANCE.json'
    Write-Output 'TASK14_ACCEPTANCE_PACK=artifacts\C1_ACCEPTANCE_PACK.zip'
}
catch {
    if ($Stopwatch.IsRunning) {
        $Stopwatch.Stop()
    }
    $EndUtc = [DateTime]::UtcNow
    $ObserverStatus = 'LOCAL_RUNNER_EXCEPTION'
    $Result = 'BLOCKED'
    $RunnerExitCode = 2
    [Console]::Error.WriteLine($_.Exception.ToString())
}
finally {
    if ($Stopwatch.IsRunning) {
        $Stopwatch.Stop()
    }
    if ($TranscriptStarted) {
        try {
            Stop-Transcript | Out-Null
            $TranscriptClosed = $true
        }
        catch {
            $ObserverStatus = 'TRANSCRIPT_FINALIZATION_FAILED'
            $Result = 'BLOCKED'
            $RunnerExitCode = 2
            [Console]::Error.WriteLine($_.Exception.ToString())
        }
    }
}

if ($TranscriptClosed) {
    try {
        $TranscriptSha256 = (
            Get-FileHash -LiteralPath $TranscriptPath -Algorithm SHA256
        ).Hash.ToLowerInvariant()
    }
    catch {
        $ObserverStatus = 'TRANSCRIPT_FINALIZATION_FAILED'
        $Result = 'BLOCKED'
        $RunnerExitCode = 2
        [Console]::Error.WriteLine($_.Exception.ToString())
    }
}
else {
    $ObserverStatus = 'TRANSCRIPT_FINALIZATION_FAILED'
    $Result = 'BLOCKED'
    $RunnerExitCode = 2
}

if ($null -ne $TranscriptSha256) {
    $Receipt = [ordered]@{
        schema_version = 1
        run_id = $RunId
        gate = $Gate
        branch = $Preflight.Branch
        head = $Preflight.Head
        origin = $Preflight.Origin
        start_utc = $StartUtc.ToString('o')
        end_utc = $EndUtc.ToString('o')
        elapsed_monotonic_seconds = $Stopwatch.Elapsed.TotalSeconds
        launcher_exit_code = $LauncherExitCode
        runner_exit_code = $RunnerExitCode
        result = $Result
        observer_status = $ObserverStatus
        source_commit = $SourceCommit
        acceptance_harness_commit = $HarnessCommit
        transcript_relative_path = (
            (Split-Path -Leaf $LogDirectory) +
            '/task14_console_transcript.txt'
        )
        transcript_sha256 = $TranscriptSha256
        downtime_report_sha256 = $DowntimeSha256
        final_report_sha256 = $FinalSha256
        acceptance_pack_sha256 = $PackSha256
        acceptance_pack_size = $PackSize
        created_by = 'RUN_TASK14_MANUAL.ps1'
        trading_approval = $false
    }
    try {
        Write-JsonAtomic -Path $ReceiptPath -Value $Receipt
    }
    catch {
        $ObserverStatus = 'RECEIPT_WRITE_FAILED'
        $Result = 'BLOCKED'
        $RunnerExitCode = 2
        [Console]::Error.WriteLine($_.Exception.ToString())
    }
    if (Test-Path -LiteralPath $ReceiptPath -PathType Leaf) {
        try {
            $LatestTemporary = Join-Path `
                $LogBase `
                ("LATEST_TASK14_LAUNCHER_RECEIPT.json.tmp.$PID")
            Copy-Item `
                -LiteralPath $ReceiptPath `
                -Destination $LatestTemporary `
                -Force
            Move-AtomicFile `
                -Source $LatestTemporary `
                -Destination $LatestReceiptPath
        }
        catch {
            [Console]::Error.WriteLine(
                "LATEST_RECEIPT_UPDATE_FAILED: " + $_.Exception.Message
            )
        }
    }
}

Write-Output ("TASK14_START_UTC=" + $StartUtc.ToString('o'))
Write-Output ("TASK14_END_UTC=" + $EndUtc.ToString('o'))
Write-Output (
    "TASK14_ELAPSED_SECONDS=" + $Stopwatch.Elapsed.TotalSeconds
)
Write-Output ("TASK14_LAUNCHER_EXIT=" + $LauncherExitCode)
Write-Output ("TASK14_RUNNER_EXIT=" + $RunnerExitCode)
Write-Output ("TASK14_OBSERVER_STATUS=" + $ObserverStatus)
Write-Output ("TASK14_TRANSCRIPT=" + $TranscriptPath)
Write-Output ("TASK14_RECEIPT=" + $ReceiptPath)
Write-Output ("TASK14_RESULT=" + $Result)

if (-not $NoPause) {
    Read-Host 'Нажмите Enter, чтобы закрыть это окно'
}

exit $RunnerExitCode

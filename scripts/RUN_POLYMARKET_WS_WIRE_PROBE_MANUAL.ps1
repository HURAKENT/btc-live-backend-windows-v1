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
$LogBase = Join-Path $CodexRoot 'polymarket_ws_wire_shape_probes'
$PythonPath = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
$ProbeTool = Join-Path `
    $ProjectRoot `
    'tools\polymarket_ws_wire_shape_probe.py'
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
            throw 'Native child process did not start.'
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

function Test-ProbePreflight {
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
    $DiffResult = Invoke-GitCommand -Arguments @(
        'diff',
        '--quiet',
        '--exit-code'
    )
    $CachedDiffResult = Invoke-GitCommand -Arguments @(
        'diff',
        '--cached',
        '--quiet',
        '--exit-code'
    )
    if (
        $StatusResult.ExitCode -ne 0 -or
        $DiffResult.ExitCode -ne 0 -or
        $CachedDiffResult.ExitCode -ne 0
    ) {
        throw 'Git clean-state check failed.'
    }
    $StatusLines = @(
        $StatusResult.StdOut -split "\r?\n" |
            Where-Object { -not [string]::IsNullOrWhiteSpace($_) }
    )
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
    if (-not (Test-Path -LiteralPath $ProbeTool -PathType Leaf)) {
        throw "Probe tool is missing: $ProbeTool"
    }

    $PythonResult = Invoke-NativeProcess `
        -Executable $PythonPath `
        -ArgumentString '--version'
    if ($PythonResult.ExitCode -ne 0) {
        throw "Python version check failed: $($PythonResult.ExitCode)"
    }
    $PythonVersion = $PythonResult.StdOut.Trim()
    if ($PythonVersion -notmatch '^Python 3\.12\.[0-9]+$') {
        throw "Python 3.12.x is required; actual: $PythonVersion"
    }

    $AiohttpResult = Invoke-NativeProcess `
        -Executable $PythonPath `
        -ArgumentString `
            '-c "import aiohttp; print(''aiohttp '' + aiohttp.__version__)"'
    if ($AiohttpResult.ExitCode -ne 0) {
        throw "aiohttp import check failed: $($AiohttpResult.ExitCode)"
    }
    $AiohttpVersion = $AiohttpResult.StdOut.Trim()
    if ($AiohttpVersion -notmatch '^aiohttp 3\.[0-9]+\.[0-9]+$') {
        throw "Unexpected aiohttp version output: $AiohttpVersion"
    }

    $HelpArguments = '"' + $ProbeTool + '" --help'
    $HelpResult = Invoke-NativeProcess `
        -Executable $PythonPath `
        -ArgumentString $HelpArguments
    if ($HelpResult.ExitCode -ne 0) {
        throw "Probe offline help check failed: $($HelpResult.ExitCode)"
    }
    return [PSCustomObject]@{
        Branch = $Branch
        Head = $Head
        Origin = $Origin
        PythonVersion = $PythonVersion
        AiohttpVersion = $AiohttpVersion
    }
}

if ($Mode -eq 'Preflight') {
    try {
        $Preflight = Test-ProbePreflight
        Write-Output ("BRANCH=" + $Preflight.Branch)
        Write-Output ("HEAD=" + $Preflight.Head)
        Write-Output ("ORIGIN=" + $Preflight.Origin)
        Write-Output $Preflight.PythonVersion
        Write-Output $Preflight.AiohttpVersion
        Write-Output 'REAL_EXTERNAL_NETWORK_REQUESTS=0'
        Write-Output 'WEBSOCKET_CONNECTIONS=0'
        Write-Output 'BACKEND_STARTS=0'
        Write-Output 'TASK14_STARTS=0'
        Write-Output 'POLYMARKET_WS_WIRE_PROBE_PREFLIGHT_PASS'
        exit 0
    }
    catch {
        [Console]::Error.WriteLine($_.Exception.ToString())
        exit 1
    }
}

$Preflight = $null
try {
    $Preflight = Test-ProbePreflight
}
catch {
    [Console]::Error.WriteLine($_.Exception.ToString())
    exit 1
}

$Timestamp = [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffZ')
$RunDirectory = Join-Path $LogBase ("manual_probe_" + $Timestamp)
$ReportPath = Join-Path `
    $RunDirectory `
    'polymarket_ws_wire_shape_report.json'
$TranscriptPath = Join-Path `
    $RunDirectory `
    'probe_console_transcript.txt'
New-Item -ItemType Directory -Path $RunDirectory | Out-Null

$StartUtc = [DateTime]::UtcNow
$EndUtc = $StartUtc
$ProbeExitCode = 50
$TranscriptStarted = $false

try {
    Start-Transcript -LiteralPath $TranscriptPath -NoClobber | Out-Null
    $TranscriptStarted = $true
    Write-Output 'PROBE_MODE=Run'
    Write-Output ("BRANCH=" + $Preflight.Branch)
    Write-Output ("HEAD=" + $Preflight.Head)
    Write-Output ("ORIGIN=" + $Preflight.Origin)
    Write-Output $Preflight.PythonVersion
    Write-Output $Preflight.AiohttpVersion
    Write-Output ("PROBE_START_UTC=" + $StartUtc.ToString('o'))
    Write-Output 'MAX_GAMMA_DISCOVERY_SEQUENCES=1'
    Write-Output 'MAX_WEBSOCKET_CONNECTIONS=1'
    Write-Output 'MAX_FRAMES=10'
    Write-Output 'MAX_RUNTIME_SECONDS=30'
    Write-Output 'AUTOMATIC_RETRIES=0'
    Write-Output ("PROBE_REPORT=" + $ReportPath)
    Write-Output ("PROBE_TRANSCRIPT=" + $TranscriptPath)

    $ProbeArguments = (
        '"' + $ProbeTool + '" --output "' + $ReportPath +
        '" --max-frames 10 --timeout-seconds 30'
    )
    $ProbeResult = Invoke-NativeProcess `
        -Executable $PythonPath `
        -ArgumentString $ProbeArguments
    if (-not [string]::IsNullOrEmpty($ProbeResult.StdOut)) {
        [Console]::Out.Write($ProbeResult.StdOut)
    }
    if (-not [string]::IsNullOrEmpty($ProbeResult.StdErr)) {
        [Console]::Error.Write($ProbeResult.StdErr)
    }
    $ProbeExitCode = $ProbeResult.ExitCode
}
catch {
    [Console]::Error.WriteLine($_.Exception.ToString())
}
finally {
    $EndUtc = [DateTime]::UtcNow
    if ($TranscriptStarted) {
        Stop-Transcript | Out-Null
    }
}

$Elapsed = $EndUtc - $StartUtc
Write-Output ("PROBE_START_UTC=" + $StartUtc.ToString('o'))
Write-Output ("PROBE_END_UTC=" + $EndUtc.ToString('o'))
Write-Output ("PROBE_ELAPSED_SECONDS=" + $Elapsed.TotalSeconds)
Write-Output ("PROBE_EXIT=" + $ProbeExitCode)
Write-Output ("PROBE_REPORT=" + $ReportPath)
Write-Output ("PROBE_TRANSCRIPT=" + $TranscriptPath)
if ($ProbeExitCode -eq 0) {
    Write-Output 'PROBE_RESULT=POLYMARKET_WS_WIRE_SHAPE_ESTABLISHED'
}
elseif ($ProbeExitCode -eq 2) {
    Write-Output 'PROBE_RESULT=POLYMARKET_WS_WIRE_SHAPE_INSUFFICIENT'
}
else {
    Write-Output 'PROBE_RESULT=POLYMARKET_WS_WIRE_SHAPE_PROBE_BLOCKED'
}

if (-not $NoPause) {
    Read-Host 'Нажмите Enter, чтобы закрыть это окно'
}

exit $ProbeExitCode

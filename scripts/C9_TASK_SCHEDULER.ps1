param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("Register", "Verify", "Unregister")]
    [string]$Operation,

    [string]$TaskName = "BTC Daily Range Backend V1",

    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),

    [string]$PythonPath
)

$ErrorActionPreference = "Stop"
$TaskPath = "\"

function Write-JsonResult([hashtable]$Result) {
    [Console]::Out.WriteLine(($Result | ConvertTo-Json -Compress))
}

function Fail([string]$Code, [int]$ExitCode) {
    [Console]::Error.WriteLine($Code)
    exit $ExitCode
}

function Get-ExpectedDefinition {
    $ResolvedRoot = [IO.Path]::GetFullPath($ProjectRoot).TrimEnd('\')
    $ResolvedPython = if ([string]::IsNullOrWhiteSpace($PythonPath)) {
        Join-Path $ResolvedRoot ".venv\Scripts\python.exe"
    }
    else {
        [IO.Path]::GetFullPath($PythonPath)
    }
    $LauncherPath = Join-Path $ResolvedRoot "scripts\C9_RUN_BACKEND.ps1"
    $PowerShellPath = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
    $Arguments = '-NoProfile -NonInteractive -ExecutionPolicy RemoteSigned -File "' + $LauncherPath + '" -PythonPath "' + $ResolvedPython + '"'
    $Identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    return [ordered]@{
        execute = $PowerShellPath
        arguments = $Arguments
        working_directory = $ResolvedRoot
        user_id = $Identity.Name
        user_sid = $Identity.User.Value
        python_path = $ResolvedPython
        run_level = "Limited"
        logon_type = "Interactive"
        trigger = "AtLogOn"
        multiple_instances = "IgnoreNew"
        start_when_available = $true
        restart_count = 3
        restart_interval = "PT5M"
    }
}

function Get-DefinitionFingerprint([hashtable]$Definition) {
    $Json = $Definition | ConvertTo-Json -Compress
    $Bytes = [Text.Encoding]::UTF8.GetBytes($Json)
    $Hasher = [Security.Cryptography.SHA256]::Create()
    try {
        return ([BitConverter]::ToString($Hasher.ComputeHash($Bytes))).Replace("-", "").ToLowerInvariant()
    }
    finally {
        $Hasher.Dispose()
    }
}

function Get-ExactTasks {
    return @(Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue)
}

function Assert-ProjectRoot([hashtable]$Expected) {
    if (-not (Test-Path -LiteralPath $Expected.working_directory -PathType Container)) {
        Fail "PROJECT_ROOT_NOT_FOUND" 30
    }
    if (-not (Test-Path -LiteralPath (Join-Path $Expected.working_directory "scripts\C9_RUN_BACKEND.ps1") -PathType Leaf)) {
        Fail "C9_BACKEND_LAUNCHER_NOT_FOUND" 30
    }
    if (-not (Test-Path -LiteralPath $Expected.python_path -PathType Leaf)) {
        Fail "WINDOWS_VENV_NOT_FOUND" 30
    }
}

function Verify-Task([hashtable]$Expected) {
    $Tasks = @(Get-ExactTasks)
    if ($Tasks.Count -eq 0) {
        Fail "TASK_NOT_FOUND" 41
    }
    if ($Tasks.Count -ne 1) {
        Fail "DUPLICATE_TASKS_DETECTED" 41
    }
    $Task = $Tasks[0]
    if ($Task.TaskPath -ne $TaskPath) {
        Fail "TASK_PATH_MISMATCH" 41
    }
    if ($Task.Actions.Count -ne 1 -or $Task.Triggers.Count -ne 1) {
        Fail "TASK_SHAPE_MISMATCH" 41
    }
    $Action = $Task.Actions[0]
    $Trigger = $Task.Triggers[0]
    $ActualRoot = [IO.Path]::GetFullPath($Action.WorkingDirectory).TrimEnd('\')
    $ActualPrincipalSid = (New-Object Security.Principal.NTAccount($Task.Principal.UserId)).Translate([Security.Principal.SecurityIdentifier]).Value
    $Mismatches = @()
    if (-not [string]::Equals($Action.Execute, $Expected.execute, [StringComparison]::OrdinalIgnoreCase)) { $Mismatches += "execute=$($Action.Execute)" }
    if ($Action.Arguments -cne $Expected.arguments) { $Mismatches += "arguments=$($Action.Arguments)" }
    if (-not [string]::Equals($ActualRoot, $Expected.working_directory, [StringComparison]::OrdinalIgnoreCase)) { $Mismatches += "working_directory=$ActualRoot" }
    if ($ActualPrincipalSid -ne $Expected.user_sid) { $Mismatches += "principal_sid=$ActualPrincipalSid" }
    if ($Task.Principal.RunLevel.ToString() -ne $Expected.run_level) { $Mismatches += "run_level=$($Task.Principal.RunLevel)" }
    if ($Task.Principal.LogonType.ToString() -ne $Expected.logon_type) { $Mismatches += "logon_type=$($Task.Principal.LogonType)" }
    if (-not [string]::Equals($Trigger.UserId, $Expected.user_id, [StringComparison]::OrdinalIgnoreCase)) { $Mismatches += "trigger_user=$($Trigger.UserId)" }
    if ($Task.Settings.MultipleInstances.ToString() -ne $Expected.multiple_instances) { $Mismatches += "multiple_instances=$($Task.Settings.MultipleInstances)" }
    if (-not $Task.Settings.StartWhenAvailable) { $Mismatches += "start_when_available=false" }
    if ($Task.Settings.RestartCount -ne $Expected.restart_count) { $Mismatches += "restart_count=$($Task.Settings.RestartCount)" }
    if ($Task.Settings.RestartInterval.ToString() -ne $Expected.restart_interval) { $Mismatches += "restart_interval=$($Task.Settings.RestartInterval)" }
    if ($Mismatches.Count -ne 0) {
        Fail ("TASK_DEFINITION_MISMATCH: " + ($Mismatches -join "; ")) 41
    }
    return $Task
}

if ($TaskName -notmatch '^[A-Za-z0-9 _.\-]{1,200}$') {
    Fail "INVALID_TASK_NAME" 30
}

try {
    Import-Module ScheduledTasks -ErrorAction Stop
    $Expected = Get-ExpectedDefinition
    $Fingerprint = Get-DefinitionFingerprint $Expected

    if ($Operation -eq "Register") {
        Assert-ProjectRoot $Expected
        $Existing = @(Get-ExactTasks)
        if ($Existing.Count -gt 0 -and @($Existing | Where-Object TaskPath -ne $TaskPath).Count -gt 0) {
            Fail "TASK_NAME_COLLISION" 41
        }
        $Action = New-ScheduledTaskAction `
            -Execute $Expected.execute `
            -Argument $Expected.arguments `
            -WorkingDirectory $Expected.working_directory
        $Trigger = New-ScheduledTaskTrigger -AtLogOn -User $Expected.user_id
        $Principal = New-ScheduledTaskPrincipal `
            -UserId $Expected.user_id `
            -LogonType Interactive `
            -RunLevel Limited
        $Settings = New-ScheduledTaskSettingsSet `
            -AllowStartIfOnBatteries `
            -DontStopIfGoingOnBatteries `
            -StartWhenAvailable `
            -MultipleInstances IgnoreNew `
            -RestartCount $Expected.restart_count `
            -RestartInterval ([TimeSpan]::FromMinutes(5)) `
            -ExecutionTimeLimit ([TimeSpan]::Zero)
        $Definition = New-ScheduledTask `
            -Action $Action `
            -Trigger $Trigger `
            -Principal $Principal `
            -Settings $Settings
        Register-ScheduledTask `
            -TaskName $TaskName `
            -TaskPath $TaskPath `
            -InputObject $Definition `
            -Force | Out-Null
        $null = Verify-Task $Expected
        Write-JsonResult @{
            status = "PASS"
            operation = "Register"
            task_name = $TaskName
            task_count = 1
            project_root = $Expected.working_directory
            run_level = $Expected.run_level
            logon_type = $Expected.logon_type
            restart_count = $Expected.restart_count
            restart_interval = $Expected.restart_interval
            definition_fingerprint = $Fingerprint
        }
        exit 0
    }

    if ($Operation -eq "Verify") {
        $null = Verify-Task $Expected
        Write-JsonResult @{
            status = "PASS"
            operation = "Verify"
            task_name = $TaskName
            task_count = 1
            project_root = $Expected.working_directory
            run_level = $Expected.run_level
            logon_type = $Expected.logon_type
            restart_count = $Expected.restart_count
            restart_interval = $Expected.restart_interval
            definition_fingerprint = $Fingerprint
        }
        exit 0
    }

    $RootTask = Get-ScheduledTask `
        -TaskName $TaskName `
        -TaskPath $TaskPath `
        -ErrorAction SilentlyContinue
    if ($null -ne $RootTask) {
        Unregister-ScheduledTask `
            -TaskName $TaskName `
            -TaskPath $TaskPath `
            -Confirm:$false
    }
    Write-JsonResult @{
        status = "PASS"
        operation = "Unregister"
        task_name = $TaskName
        task_count = 0
        project_root = $Expected.working_directory
        run_level = $Expected.run_level
        logon_type = $Expected.logon_type
        restart_count = $Expected.restart_count
        restart_interval = $Expected.restart_interval
        definition_fingerprint = $Fingerprint
    }
    exit 0
}
catch {
    [Console]::Error.WriteLine("TASK_SCHEDULER_OPERATION_FAILED: " + $_.Exception.Message)
    exit 41
}

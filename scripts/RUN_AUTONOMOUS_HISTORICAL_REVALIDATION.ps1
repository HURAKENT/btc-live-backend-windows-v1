[CmdletBinding()]
param(
    [switch]$Smoke,
    [string]$Resume,
    [string]$PyArrowPath
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $ProjectRoot

if ($Smoke -and -not [string]::IsNullOrWhiteSpace($Resume)) {
    throw 'AHR_SMOKE_RESUME_CONFLICT'
}

$Python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    $CanonicalRoot = Split-Path -Parent (Split-Path -Parent $ProjectRoot)
    if ((Split-Path -Leaf (Split-Path -Parent $ProjectRoot)) -eq '.worktrees') {
        $Python = Join-Path $CanonicalRoot '.venv\Scripts\python.exe'
    }
}
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw 'AHR_PYTHON_NOT_FOUND'
}

$UserRoot = [Environment]::GetFolderPath('UserProfile')
$CodexRoot = Join-Path $UserRoot 'Documents\Codex'
$CanonicalArtifacts = @(
    (Join-Path $UserRoot 'Downloads\btc_daily_range_merged_70_v1_10.zip'),
    (Join-Path $UserRoot 'Downloads\btc_daily_range_validation_100_merged_v2_2(1).zip'),
    (Join-Path $CodexRoot 'btc_early_horizon_single_entry_v1_output\checkpoint_matrix_170x11x11.parquet'),
    (Join-Path $CodexRoot 'btc_edge_search_lab_v1\recovery\btc_terminal_distribution_bias_lab_v1\artifacts\actual_bucket_probabilities_170.parquet'),
    (Join-Path $CodexRoot 'btc_edge_search_lab_v1\data\normalized\unified_research_visible_170_v1\markets.parquet'),
    (Join-Path $CodexRoot 'btc_edge_search_lab_v1\data\normalized\unified_research_visible_170_v1\settlements.parquet'),
    (Join-Path $CodexRoot 'btc_no_historical_price_collector_v1\full_runs\merged\checkpoint_coverage.csv'),
    (Join-Path $CodexRoot 'btc_edge_search_lab_v1\recovery\btc_no_strategy_confirmation_136_v1\data\CONFIRMATION_DATES_136.csv'),
    (Join-Path $CodexRoot 'btc_volatility_overlay_audit_v1_output\MODEL_FORECAST_LEDGER.parquet')
)
foreach ($Artifact in $CanonicalArtifacts) {
    Write-Host "AHR_CANONICAL_INPUT=$Artifact"
    if (-not (Test-Path -LiteralPath $Artifact -PathType Leaf)) {
        [Console]::Error.WriteLine("AHR_SOURCE_MISSING=$Artifact")
        exit 10
    }
}

$env:PYTHONNOUSERSITE = '1'
$env:PYTHONDONTWRITEBYTECODE = '1'

$PythonArguments = @('-m', 'src.historical_revalidation')
if ($Smoke) {
    $PythonArguments += '--smoke'
}
if (-not [string]::IsNullOrWhiteSpace($Resume)) {
    $PythonArguments += @('--resume', $Resume)
}
if (-not [string]::IsNullOrWhiteSpace($PyArrowPath)) {
    $PythonArguments += @('--pyarrow-path', ([IO.Path]::GetFullPath($PyArrowPath)))
}

$PythonProcess = Start-Process -FilePath $Python -ArgumentList $PythonArguments -Wait -NoNewWindow -PassThru
exit $PythonProcess.ExitCode

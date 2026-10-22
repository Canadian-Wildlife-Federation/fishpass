param(
    [Parameter(Mandatory=$true)]
    [string]$PlanCode,

    # Optional Azure export, e.g.
    #   .\run.ps1 ns -ExportToAzure -ExportType versioned -Version v1.2
    # Sign in first with `az login` (see README.md).
    [switch]$ExportToAzure,
    [ValidateSet("versioned", "unversioned")]
    [string]$ExportType,
    [string]$Version
)

$env:FISHPASS_HOST = "localhost"
$env:FISHPASS_PORT = "5432"
$env:FISHPASS_DBNAME = "fishpass"
$env:FISHPASS_USER = "fishpass"
$env:FISHPASS_PASSWORD = "changeme"

# Only used with -ExportToAzure.
if (-not $env:AZURE_STORAGE_ACCOUNT_URL) {
    $env:AZURE_STORAGE_ACCOUNT_URL = "https://changeme.blob.core.windows.net"
}

$extraArgs = @()
if ($ExportToAzure) {
    $extraArgs += "--export-to-azure"
    if ($ExportType) { $extraArgs += @("--export-type", $ExportType) }
    if ($Version) { $extraArgs += @("--version", $Version) }
}

python "$PSScriptRoot\scripts\run_model.py" $PlanCode @extraArgs

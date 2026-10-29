param(
    [Parameter(Mandatory=$true)]
    [string]$PlanCode,

    # Optional Azure export, e.g.
    #   .\run.ps1 ns -ExportToAzure -ExportType versioned -ExportVersion v1.2
    # Sign in first with `az login` (see README.md).
    [switch]$ExportToAzure,

    # Optional local export (instead of Azure), e.g.
    #   .\run.ps1 ns -ExportToLocal -LocalExportDirectory C:\temp\exports -ExportType unversioned
    [switch]$ExportToLocal,
    [string]$LocalExportDirectory,

    [ValidateSet("versioned", "unversioned")]
    [string]$ExportType,
    [string]$ExportVersion
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
if ($ExportToAzure) { $extraArgs += "--export-to-azure" }
if ($ExportToLocal) { $extraArgs += "--export-to-local" }
if ($LocalExportDirectory) { $extraArgs += @("--local-export-directory", $LocalExportDirectory) }
if ($ExportToAzure -or $ExportToLocal) {
    if ($ExportType) { $extraArgs += @("--export-type", $ExportType) }
    if ($ExportVersion) { $extraArgs += @("--export-version", $ExportVersion) }
}

python "$PSScriptRoot\scripts\run_model.py" $PlanCode @extraArgs

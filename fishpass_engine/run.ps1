param(
    [Parameter(Mandatory=$true)]
    [string]$PlanCode
)

$env:FISHPASS_HOST = "localhost"
$env:FISHPASS_PORT = "5432"
$env:FISHPASS_DBNAME = "fishpass"
$env:FISHPASS_USER = "fishpass"
$env:FISHPASS_PASSWORD = "changeme"

python "$PSScriptRoot\scripts\run_model.py" $PlanCode

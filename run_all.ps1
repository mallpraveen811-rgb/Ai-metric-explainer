# Full pipeline (Windows PowerShell). Requires psql on PATH and a .env file.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
foreach ($f in "01_order_facts", "02_monthly_metrics", "03_drivers") {
  psql -X -q -v ON_ERROR_STOP=1 -f "sql\$f.sql"; if ($LASTEXITCODE) { throw "SQL failed: $f" }
}
python engine\build_packets.py
python tests\planted_anomalies.py
python engine\explain.py --unusual

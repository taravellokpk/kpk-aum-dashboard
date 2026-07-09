# kpk AUM dashboard - daily refresh runner (invoked by Windows Task Scheduler).
# Runs the pipeline live using the project venv, appends a timestamped log, and
# exits with the pipeline's code so Task Scheduler shows the real last result.
# The API key is read from configurator.json (never leaves this machine).

$ErrorActionPreference = 'Continue'
$root = 'C:\Users\tarav\Desktop\KPK\Dashboards\kpk-aum-dashboard'
$py   = Join-Path $root 'venv\Scripts\python.exe'

$logDir = Join-Path $root 'logs'
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
$log = Join-Path $logDir 'refresh.log'

Set-Location $root
$start = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss')
"==== $start  starting daily refresh ====" | Out-File -FilePath $log -Append -Encoding utf8

# Capture all output and write once with a single UTF-8 encoding (avoids the
# wide-char artifacts from streaming native stderr straight to Out-File).
$out  = & $py -m src.pipeline 2>&1 | Out-String
$code = $LASTEXITCODE
Add-Content -Path $log -Value $out -Encoding UTF8

# On success, rebuild the self-contained shareable file so it always carries
# the latest snapshot.
if ($code -eq 0) {
  $out2 = & $py build_standalone.py 2>&1 | Out-String
  Add-Content -Path $log -Value $out2 -Encoding UTF8
}

$end = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss')
if ($code -eq 0) {
  "==== $end  OK (published) ====`r`n" | Out-File -FilePath $log -Append -Encoding utf8
} else {
  "==== $end  HARD FAILURE exit=$code (held last good snapshot) ====`r`n" | Out-File -FilePath $log -Append -Encoding utf8
}
exit $code

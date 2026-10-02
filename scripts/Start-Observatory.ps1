[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
$taskPython = Join-Path $taskRoot '.venv\Scripts\python.exe'
$taskPort = & $taskPython -c 'from observatory.config import Settings; print(Settings.from_env().port)'
if ($LASTEXITCODE -ne 0) { throw 'Could not read app configuration.' }
$taskUrl = "http://127.0.0.1:$taskPort"
$taskRuntime = Join-Path $taskRoot '.runtime'
$taskPidFile = Join-Path $taskRuntime 'app-process.json'
& (Join-Path $PSScriptRoot 'Start-Postgres.ps1')
if ($LASTEXITCODE -ne 0) { throw 'Database did not start.' }
try {
    $taskHealth = Invoke-RestMethod "$taskUrl/healthz" -TimeoutSec 3
    if ($taskHealth.status -eq 'ok') { Write-Output "Observatory is already running at $taskUrl"; exit 0 }
} catch { }
$taskEnvFile = Join-Path $taskRoot '.env'
$taskEnvText = if (Test-Path -LiteralPath $taskEnvFile) { Get-Content -LiteralPath $taskEnvFile -Raw } else { '' }
if ($taskEnvText -notmatch '(?m)^OBS_COOKIE_SECRET=.+$') {
    $taskSecret = & $taskPython -c 'import secrets; print(secrets.token_hex(32))'
    Add-Content -LiteralPath $taskEnvFile -Value "`nOBS_COOKIE_SECRET=$taskSecret" -Encoding utf8
}
$taskProcess = Start-Process -FilePath $taskPython -ArgumentList @('-X','utf8','-m','observatory.cli','serve') -WorkingDirectory $taskRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $taskRuntime 'app.stdout.log') -RedirectStandardError (Join-Path $taskRuntime 'app.stderr.log')
@{ pid=$taskProcess.Id; started_at=$taskProcess.StartTime.ToUniversalTime().ToString('o'); python=$taskPython } | ConvertTo-Json | Set-Content -LiteralPath $taskPidFile -Encoding utf8
for ($taskAttempt=0; $taskAttempt -lt 20; $taskAttempt++) {
    Start-Sleep -Milliseconds 500
    try {
        $taskHealth=Invoke-RestMethod "$taskUrl/healthz" -TimeoutSec 2
        if ($taskHealth.status -eq 'ok') { Write-Output "Observatory started at $taskUrl"; exit 0 }
    } catch { }
    if ($taskProcess.HasExited) { throw 'App exited before becoming healthy. Inspect .runtime/app.stderr.log locally.' }
}
throw 'App did not become healthy; inspect project logs before retrying.'

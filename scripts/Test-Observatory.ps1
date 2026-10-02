[CmdletBinding()]
param()
$ErrorActionPreference='Stop'
$taskRoot=Split-Path -Parent $PSScriptRoot
$taskPort=& (Join-Path $taskRoot '.venv\Scripts\python.exe') -c 'from observatory.config import Settings; print(Settings.from_env().port)'
if ($LASTEXITCODE -ne 0) { throw 'Could not read app configuration.' }
$taskResult=Invoke-RestMethod "http://127.0.0.1:$taskPort/healthz" -TimeoutSec 10
$taskResult | ConvertTo-Json -Depth 5
if ($taskResult.status -ne 'ok') { throw 'Observatory is not healthy.' }

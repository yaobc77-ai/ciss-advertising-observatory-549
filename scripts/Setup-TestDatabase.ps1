[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
& (Join-Path $taskRoot '.venv\Scripts\python.exe') -X utf8 (Join-Path $PSScriptRoot 'local_postgres.py') setup-test-db
if ($LASTEXITCODE -ne 0) { throw 'Project test database setup failed.' }

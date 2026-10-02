[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
& (Join-Path $taskRoot '.venv\Scripts\python.exe') -X utf8 (Join-Path $PSScriptRoot 'local_postgres.py') start
if ($LASTEXITCODE -ne 0) { throw 'Project PostgreSQL start failed.' }

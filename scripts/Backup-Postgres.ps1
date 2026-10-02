[CmdletBinding()]
param([string]$OutputFile)
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
$taskArgs = @('-X', 'utf8', (Join-Path $PSScriptRoot 'local_postgres.py'), 'backup')
if ($OutputFile) { $taskArgs += @('--output-file', $OutputFile) }
& (Join-Path $taskRoot '.venv\Scripts\python.exe') @taskArgs
if ($LASTEXITCODE -ne 0) { throw 'Project PostgreSQL backup failed.' }

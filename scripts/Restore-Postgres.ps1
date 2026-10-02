[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$BackupFile,
    [Parameter(Mandatory = $true)][ValidatePattern('^[a-z][a-z0-9_]{0,62}$')][string]$Database
)
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
& (Join-Path $taskRoot '.venv\Scripts\python.exe') -X utf8 (Join-Path $PSScriptRoot 'local_postgres.py') restore --backup-file $BackupFile --database $Database
if ($LASTEXITCODE -ne 0) { throw 'Project PostgreSQL restore failed.' }

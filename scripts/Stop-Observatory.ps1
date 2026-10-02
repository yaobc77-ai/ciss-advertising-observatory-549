[CmdletBinding(SupportsShouldProcess=$true)]
param()
$ErrorActionPreference='Stop'
$taskRoot=Split-Path -Parent $PSScriptRoot
$taskPidFile=Join-Path $taskRoot '.runtime\app-process.json'
if (!(Test-Path -LiteralPath $taskPidFile)) { Write-Output 'No project-managed app process is recorded.'; exit 0 }
$taskState=Get-Content -LiteralPath $taskPidFile -Raw | ConvertFrom-Json
$taskProcess=Get-Process -Id $taskState.pid -ErrorAction SilentlyContinue
if (!$taskProcess) { Write-Output 'Recorded app process is already stopped.'; exit 0 }
if ($taskProcess.StartTime.ToUniversalTime().Ticks -ne ([datetime]$taskState.started_at).ToUniversalTime().Ticks) { throw 'PID was reused; refusing to stop a different process.' }
$taskCommand=Get-CimInstance Win32_Process -Filter "ProcessId=$($taskState.pid)"
if ($taskCommand.CommandLine -notmatch 'observatory\.cli.+serve') { throw 'Process command does not match the project app; refusing to stop it.' }
if ($taskCommand.ExecutablePath -ne $taskState.python) { throw 'Process executable does not match the recorded project runtime.' }
# Windows virtual-environment launchers may spawn the interpreter that actually
# serves requests. Stop only children of this verified launcher with the same
# app command, not every Python process or every process using the port.
$taskChildren=@(Get-CimInstance Win32_Process -Filter "ParentProcessId=$($taskState.pid)" |
    Where-Object { $_.CommandLine -match 'observatory\.cli\s+serve\s*$' })
foreach ($taskChild in $taskChildren) {
    $taskChildProcess=Get-Process -Id $taskChild.ProcessId -ErrorAction SilentlyContinue
    if (!$taskChildProcess -or $taskChildProcess.StartTime -lt $taskProcess.StartTime) { throw 'App child process identity could not be verified.' }
    if ($PSCmdlet.ShouldProcess("Project app child PID $($taskChild.ProcessId)", 'Stop')) {
        Stop-Process -Id $taskChild.ProcessId -ErrorAction Stop
        Wait-Process -Id $taskChild.ProcessId -Timeout 10 -ErrorAction SilentlyContinue
    }
}
$taskProcess=Get-Process -Id $taskState.pid -ErrorAction SilentlyContinue
if ($taskProcess -and $PSCmdlet.ShouldProcess("Project app launcher PID $($taskState.pid)", 'Stop')) {
    Stop-Process -Id $taskState.pid -ErrorAction Stop
    Wait-Process -Id $taskState.pid -Timeout 10 -ErrorAction SilentlyContinue
}
if (!$WhatIfPreference) { Write-Output 'Project app stopped. PostgreSQL remains available.' }

[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
$taskRuntime = Join-Path $taskRoot '.runtime'
$taskPython = Join-Path $taskRoot '.venv\Scripts\python.exe'
$taskPgRoot = Join-Path $taskRuntime 'postgres18\Library'
$taskPostgres = Join-Path $taskPgRoot 'bin\postgres.exe'
$taskArchive = Join-Path $taskRuntime 'downloads\pgvector-v0.8.6.tar.gz'
$taskExpectedHash = '10bf9938906e5d643bbc4a7eea104b6f57ba4898e5b76b20e60484ea1d5a7f8f'
$taskReceipt = Join-Path $taskRuntime 'postgres18\pgvector-build.json'
$taskDll = Join-Path $taskPgRoot 'lib\vector.dll'
$taskControl = Join-Path $taskPgRoot 'share\extension\vector.control'
$taskSql = Join-Path $taskPgRoot 'share\extension\vector--0.8.6.sql'
if (-not (Test-Path -LiteralPath $taskPython) -or -not (Test-Path -LiteralPath $taskPostgres)) {
    throw 'Prepare the project Python environment and PostgreSQL 18 runtime before building pgvector.'
}
$taskVersion = & $taskPostgres --version
if ($LASTEXITCODE -ne 0 -or $taskVersion -notmatch '\bPostgreSQL\)?\s+18(?:\.\d+)*\b') {
    throw 'pgvector must be built against this project PostgreSQL 18 runtime.'
}
$taskPostgresHash = (Get-FileHash -LiteralPath $taskPostgres -Algorithm SHA256).Hash.ToLowerInvariant()
if ((Test-Path -LiteralPath $taskReceipt) -and (Test-Path -LiteralPath $taskDll) -and (Test-Path -LiteralPath $taskControl) -and (Test-Path -LiteralPath $taskSql)) {
    $taskExisting = Get-Content -LiteralPath $taskReceipt -Raw | ConvertFrom-Json
    if ($taskExisting.pgvector -eq '0.8.6' -and $taskExisting.postgresql_major -eq 18 -and $taskExisting.source_sha256 -eq $taskExpectedHash -and $taskExisting.postgres_sha256 -eq $taskPostgresHash -and
        $taskExisting.dll_sha256 -eq (Get-FileHash -LiteralPath $taskDll -Algorithm SHA256).Hash.ToLowerInvariant() -and
        $taskExisting.control_sha256 -eq (Get-FileHash -LiteralPath $taskControl -Algorithm SHA256).Hash.ToLowerInvariant() -and
        $taskExisting.sql_sha256 -eq (Get-FileHash -LiteralPath $taskSql -Algorithm SHA256).Hash.ToLowerInvariant()) {
        Write-Output 'pgvector 0.8.6 is already built and installed for this PostgreSQL 18 runtime.'
        return
    }
}
New-Item -ItemType Directory -Path (Split-Path -Parent $taskArchive) -Force | Out-Null
if (-not (Test-Path -LiteralPath $taskArchive)) {
    Invoke-WebRequest -Uri 'https://github.com/pgvector/pgvector/archive/refs/tags/v0.8.6.tar.gz' -OutFile $taskArchive
}
if ((Get-FileHash -LiteralPath $taskArchive -Algorithm SHA256).Hash.ToLowerInvariant() -ne $taskExpectedHash) {
    throw 'pgvector source archive checksum mismatch; nothing was built or installed.'
}
$taskVsWhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
if (-not (Test-Path -LiteralPath $taskVsWhere)) {
    throw 'Install Visual Studio 2022 C++ Build Tools (x64) before building pgvector.'
}
$taskVsRoot = & $taskVsWhere -latest -version '[17.0,18.0)' -products '*' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
if ($LASTEXITCODE -ne 0 -or -not $taskVsRoot) { throw 'Visual Studio 2022 x64 C++ tools were not found.' }
$taskVcVars = Join-Path $taskVsRoot 'VC\Auxiliary\Build\vcvars64.bat'
if (-not (Test-Path -LiteralPath $taskVcVars) -or $taskVcVars -match '["&|<>^%]') {
    throw 'The Visual Studio compiler environment path could not be validated.'
}
$taskCompilerEnvironment = & $env:ComSpec /d /s /c ('call "{0}" >nul && set' -f $taskVcVars)
if ($LASTEXITCODE -ne 0) { throw 'Visual Studio compiler environment initialization failed.' }
# Build in a fresh project-owned directory; never delete or reuse another build.
$taskBuildRoot = Join-Path $taskRuntime ('build\pgvector-pg18-' + [guid]::NewGuid().ToString('N'))
@'
import sys, tarfile
from pathlib import Path, PurePosixPath
destination = Path(sys.argv[2]).resolve()
destination.mkdir(parents=True, exist_ok=False)
with tarfile.open(sys.argv[1], 'r:gz') as archive:
    members = archive.getmembers()
    for member in members:
        path = PurePosixPath(member.name)
        if (path.is_absolute() or '..' in path.parts or '\\' in member.name
                or ':' in member.name or not path.parts or path.parts[0] != 'pgvector-0.8.6'
                or not (member.isfile() or member.isdir())):
            raise RuntimeError('Unsafe or unexpected pgvector source archive member')
        if not (destination / member.name).resolve().is_relative_to(destination):
            raise RuntimeError('Source archive member escapes the project build directory')
    for member in members:
        output = destination / member.name
        if member.isdir():
            output.mkdir(parents=True, exist_ok=True)
        else:
            output.parent.mkdir(parents=True, exist_ok=True)
            with output.open('xb') as stream:
                stream.write(archive.extractfile(member).read())
'@ | & $taskPython -X utf8 - $taskArchive $taskBuildRoot
if ($LASTEXITCODE -ne 0) { throw 'pgvector source extraction failed.' }
$taskSource = Join-Path $taskBuildRoot 'pgvector-0.8.6'
$taskOriginalEnvironment = @{}
Get-ChildItem Env: | ForEach-Object { $taskOriginalEnvironment[$_.Name] = $_.Value }
try {
    foreach ($taskLine in $taskCompilerEnvironment) {
        if ($taskLine -match '^([^=]+)=(.*)$') {
            [Environment]::SetEnvironmentVariable($Matches[1], $Matches[2], 'Process')
        }
    }
    $taskNmake = (Get-Command nmake.exe -ErrorAction Stop).Source
    Push-Location -LiteralPath $taskSource
    try {
        & $taskNmake /NOLOGO /F Makefile.win "PGROOT=$taskPgRoot"
        if ($LASTEXITCODE -ne 0) { throw 'pgvector PostgreSQL 18 compilation failed; the database was not changed.' }
        & $taskNmake /NOLOGO /F Makefile.win "PGROOT=$taskPgRoot" install
        if ($LASTEXITCODE -ne 0) { throw 'pgvector installation failed. Stop PostgreSQL 18 before rebuilding a loaded extension DLL.' }
    } finally {
        Pop-Location
    }
} finally {
    Get-ChildItem Env: | ForEach-Object {
        if (-not $taskOriginalEnvironment.ContainsKey($_.Name)) { [Environment]::SetEnvironmentVariable($_.Name, $null, 'Process') }
    }
    foreach ($taskKey in $taskOriginalEnvironment.Keys) {
        [Environment]::SetEnvironmentVariable($taskKey, $taskOriginalEnvironment[$taskKey], 'Process')
    }
}
$taskBuildReceipt = [ordered]@{
    pgvector = '0.8.6'
    postgresql_major = 18
    postgresql = $taskVersion
    source_url = 'https://github.com/pgvector/pgvector/archive/refs/tags/v0.8.6.tar.gz'
    source_sha256 = $taskExpectedHash
    postgres_sha256 = $taskPostgresHash
    dll_sha256 = (Get-FileHash -LiteralPath $taskDll -Algorithm SHA256).Hash.ToLowerInvariant()
    control_sha256 = (Get-FileHash -LiteralPath $taskControl -Algorithm SHA256).Hash.ToLowerInvariant()
    sql_sha256 = (Get-FileHash -LiteralPath $taskSql -Algorithm SHA256).Hash.ToLowerInvariant()
    compiler = 'Visual Studio 2022 x64 C++ tools; upstream Makefile.win'
    installed_at = [DateTime]::UtcNow.ToString('o')
}
[System.IO.File]::WriteAllText($taskReceipt, ($taskBuildReceipt | ConvertTo-Json) + [Environment]::NewLine, [System.Text.UTF8Encoding]::new($false))
Write-Output 'Built and installed pgvector 0.8.6 for the project PostgreSQL 18 runtime. No database was modified.'

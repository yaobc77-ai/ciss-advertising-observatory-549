[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
$taskRuntime = Join-Path $taskRoot '.runtime'
$taskPython = Join-Path $taskRoot '.venv\Scripts\python.exe'
$taskMamba = Join-Path $taskRuntime 'tools\micromamba.exe'
$taskPrefix = Join-Path $taskRuntime 'postgres18'
$taskArchive = Join-Path $taskRuntime 'downloads\micromamba-2.9.0-0.tar.bz2'
$taskExpectedHash = '97a336f4ab794bd96a6a4da5e6ed63e75a1d31830414a182419b23d3b36f3fe0'
if (-not (Test-Path -LiteralPath $taskPython)) {
    throw 'Create the project .venv and install its dependencies (including psycopg) before setting up PostgreSQL.'
}
# Resolve Windows junctions just as the Python cluster operator does.
@'
import json, sys
from pathlib import Path
runtime = Path(sys.argv[1]).resolve()
data = runtime / 'pgdata18'
marker = runtime / 'postgres-cluster.json'
if marker.exists():
    cluster = json.loads(marker.read_text(encoding='utf-8'))
    if not cluster.get('data_directory') or Path(cluster['data_directory']).resolve() != data.resolve():
        sys.exit('An existing project cluster requires the reviewed PostgreSQL 18 migration. Setup does not migrate, replace, or start the old cluster.')
elif (runtime / 'pgdata' / 'PG_VERSION').exists():
    sys.exit('An existing PostgreSQL data directory requires the reviewed PostgreSQL 18 migration. Setup will not initialize a second empty main database.')
if (data / 'PG_VERSION').exists() and (data / 'PG_VERSION').read_text(encoding='ascii').strip() != '18':
    sys.exit('The target data directory is not PostgreSQL 18; existing data was retained.')
'@ | & $taskPython -X utf8 - $taskRuntime
if ($LASTEXITCODE -ne 0) { throw 'Project PostgreSQL 18 setup preflight failed; no runtime or database was changed.' }
foreach ($taskDirectory in @('tools', 'downloads', 'mamba')) {
    New-Item -ItemType Directory -Path (Join-Path $taskRuntime $taskDirectory) -Force | Out-Null
}
if (-not (Test-Path -LiteralPath $taskMamba)) {
    if (-not (Test-Path -LiteralPath $taskArchive)) {
        Invoke-WebRequest -Uri 'https://api.anaconda.org/download/conda-forge/micromamba/2.9.0/win-64/micromamba-2.9.0-0.tar.bz2' -OutFile $taskArchive
    }
    $taskActualHash = (Get-FileHash -LiteralPath $taskArchive -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($taskActualHash -ne $taskExpectedHash) { throw 'Micromamba archive checksum mismatch; nothing was installed.' }
    @'
import sys, tarfile
from pathlib import Path
with tarfile.open(sys.argv[1], 'r:bz2') as archive:
    matches = [item for item in archive.getmembers() if item.isfile() and item.name.endswith('/micromamba.exe')]
    if len(matches) != 1:
        raise RuntimeError('Unexpected micromamba archive layout')
    with Path(sys.argv[2]).open('xb') as destination:
        destination.write(archive.extractfile(matches[0]).read())
'@ | & $taskPython -X utf8 - $taskArchive $taskMamba
    if ($LASTEXITCODE -ne 0) { throw 'Micromamba extraction failed.' }
}
if (-not (Test-Path -LiteralPath (Join-Path $taskPrefix 'Library\bin\postgres.exe'))) {
    if ((Test-Path -LiteralPath $taskPrefix) -and @(Get-ChildItem -LiteralPath $taskPrefix -Force).Count -gt 0) {
        throw 'The runtime prefix exists but PostgreSQL is missing. Inspect it manually; setup does not replace nonempty environments.'
    }
    & $taskMamba create --yes --no-rc --root-prefix (Join-Path $taskRuntime 'mamba') --prefix $taskPrefix --file (Join-Path $PSScriptRoot 'postgres-win64.lock.txt')
    if ($LASTEXITCODE -ne 0) { throw 'Project PostgreSQL runtime installation failed.' }
}
$taskVersion = & (Join-Path $taskPrefix 'Library\bin\postgres.exe') --version
if ($LASTEXITCODE -ne 0) { throw 'Project PostgreSQL binary could not run.' }
if ($taskVersion -notmatch '\bPostgreSQL\)?\s+18(?:\.\d+)*\b') { throw 'The project runtime is not PostgreSQL 18; no cluster was started.' }
Write-Output $taskVersion
& (Join-Path $PSScriptRoot 'Install-Pgvector.ps1')
& (Join-Path $PSScriptRoot 'Start-Postgres.ps1')
& (Join-Path $PSScriptRoot 'Test-Postgres.ps1')

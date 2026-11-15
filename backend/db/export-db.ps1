#requires -Version 7.3
<#
.SYNOPSIS
Exports the complete application database to a portable PostgreSQL custom archive.
.EXAMPLE
.\backend\db\export-db.ps1
.EXAMPLE
.\backend\db\export-db.ps1 -OutputPath C:\transfer\archive.dump
#>
[CmdletBinding()]
param([string]$OutputPath)

. (Join-Path $PSScriptRoot 'db-tools.ps1')
Initialize-DbTools

if (!$OutputPath) {
    $OutputPath = Join-Path $DbRepoRoot ".backups/archive-$(Get-Date -Format 'yyyyMMdd-HHmmss-fff').dump"
}
$OutputPath = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($OutputPath)
if (Test-Path -LiteralPath $OutputPath) { throw "Backup already exists: $OutputPath" }
[IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($OutputPath)) | Out-Null

$token = [guid]::NewGuid().ToString('N')
$containerDump = "/tmp/archive-export-$token.dump"
$partialPath = "$OutputPath.$token.partial"
$writers = @(Get-DbRunningWriters)
try {
    Invoke-DbCompose -Arguments @('up', '-d', '--wait', 'postgres') | Out-Host
    if ($writers.Count) {
        Write-Host 'Stopping API and worker for the database snapshot...'
        Invoke-DbCompose -Arguments (@('stop') + $writers) | Out-Host
    }
    $summary = Get-DbSummary
    Invoke-DbPostgres -Command 'exec pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom --no-owner --no-privileges --file="$1"' -Arguments @($containerDump) | Out-Host
    Invoke-DbPostgres -Command 'exec pg_restore --list "$1"' -Arguments @($containerDump) | Out-Null
    # Copy the binary file directly; PowerShell text redirection can corrupt archives.
    Invoke-DbCompose -Arguments @('cp', "postgres:$containerDump", $partialPath) | Out-Host
    if ((Get-Item -LiteralPath $partialPath).Length -eq 0) { throw 'The exported backup is empty.' }
    [IO.File]::Move($partialPath, $OutputPath)
    Write-Host "Backup: $OutputPath"
    Write-Host "SHA256: $((Get-FileHash -LiteralPath $OutputPath -Algorithm SHA256).Hash)"
    Write-Host "Database snapshot: $summary"
}
finally {
    Remove-DbTemporaryDump -Path $containerDump
    if (Test-Path -LiteralPath $partialPath) { Remove-Item -LiteralPath $partialPath }
    if ($writers.Count) {
        Invoke-DbCompose -Arguments (@('start', '--wait') + $writers) | Out-Host
    }
}

#requires -Version 7.3
<#
.SYNOPSIS
Restores an archive on another computer, leaving ingestion paused.
.EXAMPLE
.\backend\db\restore-db.ps1 -BackupPath .\archive.dump
.EXAMPLE
.\backend\db\restore-db.ps1 -BackupPath .\archive.dump -ReplaceExisting -NoStart
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$BackupPath,
    [switch]$ReplaceExisting,
    [switch]$NoStart
)

. (Join-Path $PSScriptRoot 'db-tools.ps1')
Initialize-DbTools
$BackupPath = (Resolve-Path -LiteralPath $BackupPath).ProviderPath
if (!(Test-Path -LiteralPath $BackupPath -PathType Leaf) -or (Get-Item -LiteralPath $BackupPath).Length -eq 0) {
    throw 'BackupPath must point to a nonempty PostgreSQL custom archive.'
}
Write-Host "Backup SHA256: $((Get-FileHash -LiteralPath $BackupPath -Algorithm SHA256).Hash)"

Invoke-DbCompose -Arguments @('up', '-d', '--wait', 'postgres') | Out-Host
$target = (Invoke-DbSql -Sql 'SELECT current_database();').Trim()
if ($target -in @('postgres', 'template0', 'template1')) { throw 'Use a dedicated POSTGRES_DB for the application.' }
$occupiedSql = @'
SELECT EXISTS (
    SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname NOT IN ('pg_catalog', 'information_schema') AND n.nspname NOT LIKE 'pg_toast%'
) OR EXISTS (SELECT 1 FROM pg_extension WHERE extname <> 'plpgsql')
  OR EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
             WHERE n.nspname = 'public')
  OR EXISTS (SELECT 1 FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
             WHERE n.nspname = 'public')
  OR EXISTS (SELECT 1 FROM pg_namespace WHERE nspname NOT IN ('public', 'pg_catalog', 'information_schema')
             AND nspname NOT LIKE 'pg_toast%' AND nspname NOT LIKE 'pg_temp%');
'@
if ((Invoke-DbSql -Sql $occupiedSql).Trim() -eq 't' -and !$ReplaceExisting) {
    throw 'The database is not empty. Use -ReplaceExisting explicitly to replace it; the old database will be retained under another name.'
}

$token = [guid]::NewGuid().ToString('N')
$staging = "archive_restore_$token"
$previous = "archive_previous_$token"
$containerDump = "/tmp/archive-restore-$token.dump"
$writers = @(Get-DbRunningWriters)
$stagingCreated = $false
$promoted = $false
$stopped = $false
try {
    Invoke-DbCompose -Arguments @('cp', $BackupPath, "postgres:$containerDump") | Out-Host
    Invoke-DbPostgres -Command 'exec pg_restore --list "$1"' -Arguments @($containerDump) | Out-Null
    Invoke-DbPostgres -Command 'exec createdb -U "$POSTGRES_USER" --template=template0 -- "$1"' -Arguments @($staging) | Out-Host
    $stagingCreated = $true
    Write-Host 'Restoring into a temporary database before switching the application...'
    Invoke-DbPostgres -Command 'exec pg_restore -U "$POSTGRES_USER" -d "$1" --no-owner --no-privileges --exit-on-error --single-transaction "$2"' -Arguments @($staging, $containerDump) | Out-Host
    $summary = Get-DbSummary -Database $staging
    $revision = ($summary | ConvertFrom-Json).schema_version
    $knownRevisions = @(Get-ChildItem -LiteralPath (Join-Path $DbRepoRoot 'backend/migrations/versions') -Filter '*.py' | ForEach-Object {
        $match = [regex]::Match([IO.File]::ReadAllText($_.FullName), '(?m)^revision\s*=\s*["'']([^"'']+)["'']')
        if ($match.Success) { $match.Groups[1].Value }
    })
    if ($revision -notin $knownRevisions) {
        throw "Backup schema '$revision' is not supported by this checkout. Use the matching repository version; see the README baseline instructions for old databases."
    }
    $config = ((Invoke-DbCompose -Arguments @('config', '--format', 'json')) -join "`n") | ConvertFrom-Json
    $dimensionSetting = $config.services.api.environment.PSObject.Properties['EMBEDDING_DIMENSIONS']
    $dimensions = if ($null -ne $dimensionSetting) { [int]$dimensionSetting.Value } else { 4096 }
    $snapshot = $summary | ConvertFrom-Json
    if ($snapshot.catalog_vector_type -ne "vector($dimensions)" -or $snapshot.vision_vector_type -ne "vector($dimensions)") {
        throw "Backup vector dimensions do not match EMBEDDING_DIMENSIONS=$dimensions. Match .env to the original computer before restoring."
    }
    # Preserve task history; the worker recovers any interrupted claims when started.
    Invoke-DbSql -Database $staging -Sql "UPDATE ingestion_jobs SET status = 'paused' WHERE status NOT IN ('completed', 'completed_with_errors');" | Out-Host

    $stopped = $true
    Invoke-DbCompose -Arguments @('stop', 'worker', 'api') | Out-Host
    # Recheck after stopping writers in case an empty database was populated during import.
    if ((Invoke-DbSql -Sql $occupiedSql).Trim() -eq 't' -and !$ReplaceExisting) {
        throw 'The database was populated during import. Retry with -ReplaceExisting to replace it explicitly.'
    }
    # Quote names as SQL identifiers/literals, including custom POSTGRES_DB values.
    $targetIdentifier = '"' + $target.Replace('"', '""') + '"'
    $targetLiteral = "'" + $target.Replace("'", "''") + "'"
    $switchSql = @"
SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = $targetLiteral AND pid <> pg_backend_pid();
BEGIN;
ALTER DATABASE $targetIdentifier RENAME TO $previous;
ALTER DATABASE $staging RENAME TO $targetIdentifier;
COMMIT;
"@
    # The two renames commit together. No DROP DATABASE or volume deletion is used.
    Invoke-DbSql -Database 'postgres' -Sql $switchSql | Out-Null
    $promoted = $true
    Write-Host "Restored database: $summary"
    Write-Host "Previous database retained as: $previous"
    Write-Host 'Ingestion is paused. Keep the original embedding provider, model, and dimensions in .env.'
    if ($NoStart) {
        Write-Host 'API and worker remain stopped. Start with: docker compose up -d --build --wait'
    }
    else {
        Invoke-DbCompose -Arguments @('up', '-d', '--build', '--wait') | Out-Host
        Write-Host 'Restore complete. Open the app and click Resume when ready to continue ingestion.'
    }
}
finally {
    Remove-DbTemporaryDump -Path $containerDump
    if ($stagingCreated -and !$promoted) {
        try {
            Invoke-DbPostgres -Command 'exec dropdb -U "$POSTGRES_USER" --if-exists -- "$1"' -Arguments @($staging) | Out-Host
        }
        catch { Write-Warning "Temporary database could not be removed: $staging" }
    }
    if ($stopped -and !$promoted -and $writers.Count) {
        Invoke-DbCompose -Arguments (@('start', '--wait') + $writers) | Out-Host
    }
}

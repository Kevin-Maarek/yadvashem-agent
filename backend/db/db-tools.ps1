# Shared Docker/PostgreSQL helpers for export-db.ps1 and restore-db.ps1.
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$DbRepoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../..'))

function Initialize-DbTools {
    if (!(Get-Command docker -ErrorAction SilentlyContinue)) {
        throw 'Docker is required. Install and start Docker Desktop first.'
    }
    if (!(Test-Path -LiteralPath (Join-Path $DbRepoRoot '.env') -PathType Leaf)) {
        throw 'Create .env from .env.example in the repository root first.'
    }
}

function Invoke-DbCompose {
    param([string[]]$Arguments)
    # Docker writes progress to stderr even on success.
    $ErrorActionPreference = 'Continue'
    $PSNativeCommandUseErrorActionPreference = $false
    $PSNativeCommandArgumentPassing = 'Standard'
    $prefix = @('compose', '--project-directory', $DbRepoRoot,
        '--env-file', (Join-Path $DbRepoRoot '.env'), '-f', (Join-Path $DbRepoRoot 'compose.yaml'))
    & docker @prefix @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Docker Compose command failed (exit $LASTEXITCODE): $($Arguments[0])"
    }
}

function Invoke-DbPostgres {
    param([string]$Command, [string[]]$Arguments = @())
    Invoke-DbCompose -Arguments (@('exec', '-T', 'postgres', 'sh', '-c', $Command, 'db-tools') + $Arguments)
}

function Invoke-DbSql {
    param([string]$Sql, [string]$Database = '')
    Invoke-DbPostgres -Command 'exec psql -X -qAt -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "${1:-$POSTGRES_DB}" -c "$2"' -Arguments @($Database, $Sql)
}

function Get-DbRunningWriters {
    # Some Compose versions emit a blank line when neither service is running.
    Invoke-DbCompose -Arguments @('ps', '--status', 'running', '--services', 'api', 'worker') |
        ForEach-Object { $_.Trim() } | Where-Object { $_ -in @('api', 'worker') }
}

function Get-DbSummary {
    param([string]$Database = '')
    $sql = @'
SELECT json_build_object(
    'schema_version', (SELECT version_num FROM alembic_version),
    'records', (SELECT count(*) FROM archival_records),
    'images', (SELECT count(*) FROM logical_images),
    'descriptions', (SELECT count(*) FROM vision_descriptions),
    'catalog_vectors', (SELECT count(*) FROM catalog_embeddings),
    'vision_vectors', (SELECT count(*) FROM vision_embeddings),
    'conversations', (SELECT count(*) FROM chat_conversations),
    'chat_turns', (SELECT count(*) FROM chat_turns),
    'catalog_vector_type', (SELECT format_type(atttypid, atttypmod) FROM pg_attribute
        WHERE attrelid = 'catalog_embeddings'::regclass AND attname = 'vector' AND NOT attisdropped),
    'vision_vector_type', (SELECT format_type(atttypid, atttypmod) FROM pg_attribute
        WHERE attrelid = 'vision_embeddings'::regclass AND attname = 'vector' AND NOT attisdropped),
    'embedding_models', (SELECT json_agg(model) FROM
        (SELECT model FROM catalog_embeddings UNION SELECT model FROM vision_embeddings) models)
);
'@
    Invoke-DbSql -Database $Database -Sql $sql
}

function Remove-DbTemporaryDump {
    param([string]$Path)
    try {
        Invoke-DbPostgres -Command 'rm -f -- "$1"' -Arguments @($Path) | Out-Host
    }
    catch { Write-Warning "Could not remove temporary container file: $Path" }
}

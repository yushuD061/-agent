param(
    [string]$EnvFile = (Join-Path $PSScriptRoot ".env")
)

$ErrorActionPreference = "Stop"

function New-RandomSecret {
    param([int]$ByteCount = 36)
    $bytes = New-Object byte[] $ByteCount
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
    return [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
}

$required = [ordered]@{
    DOCKER_ELASTIC_PASSWORD = { New-RandomSecret }
    DOCKER_MILVUS_ROOT_PASSWORD = { New-RandomSecret }
    DOCKER_MILVUS_APP_USER = { "nanoclaw_rag" }
    DOCKER_MILVUS_APP_PASSWORD = { New-RandomSecret }
}

$lines = [System.Collections.Generic.List[string]]::new()
if (Test-Path -LiteralPath $EnvFile) {
    foreach ($line in (Get-Content -LiteralPath $EnvFile -Encoding UTF8)) { $lines.Add([string]$line) }
}
$existing = @{}
foreach ($line in $lines) {
    if ($line -match '^([A-Za-z_][A-Za-z0-9_]*)=') { $existing[$Matches[1]] = $true }
}
foreach ($entry in $required.GetEnumerator()) {
    if (-not $existing.ContainsKey($entry.Key)) {
        $lines.Add("$($entry.Key)=$(& $entry.Value)")
    }
}
[System.IO.File]::WriteAllLines($EnvFile, $lines, [System.Text.UTF8Encoding]::new($false))
Write-Output "Docker data-service secrets are present in the ignored env file. Values were not printed."

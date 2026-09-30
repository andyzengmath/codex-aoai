[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [Alias("Model")]
    [string[]]$Models
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSCommandPath
$buildHome = Join-Path $root ".catalog-build"
$catalogPath = Join-Path $root "model-catalog.json"
$previousCodexHome = $env:CODEX_HOME

if (Test-Path -LiteralPath $buildHome) {
    Remove-Item -LiteralPath $buildHome -Recurse -Force
}
New-Item -ItemType Directory -Path $buildHome | Out-Null

try {
    $env:CODEX_HOME = $buildHome
    $rawCatalog = & codex debug models
    if ($LASTEXITCODE -ne 0) {
        throw "codex debug models failed with exit code $LASTEXITCODE"
    }

    $catalog = $rawCatalog | ConvertFrom-Json
    $selectedModels = foreach ($model in $Models) {
        $matches = @($catalog.models | Where-Object slug -eq $model)
        if ($matches.Count -ne 1) {
            throw "Expected one model named '$model'; found $($matches.Count)"
        }
        $matches[0]
    }

    $json = @{ models = @($selectedModels) } | ConvertTo-Json -Depth 100 -Compress
    [IO.File]::WriteAllText(
        $catalogPath,
        $json,
        [Text.UTF8Encoding]::new($false)
    )
    Write-Host "Wrote $catalogPath for $($Models -join ', ')"
}
finally {
    if ($null -eq $previousCodexHome) {
        Remove-Item Env:CODEX_HOME -ErrorAction SilentlyContinue
    }
    else {
        $env:CODEX_HOME = $previousCodexHome
    }

    if (Test-Path -LiteralPath $buildHome) {
        Remove-Item -LiteralPath $buildHome -Recurse -Force
    }
}

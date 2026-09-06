[CmdletBinding()]
param(
    [switch]$Deploy
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
Push-Location $repoRoot
try {
    $dirty = git status --porcelain --untracked-files=normal
    if ($LASTEXITCODE -ne 0) { throw "Unable to inspect Git working tree." }
    if ($dirty) {
        throw "Production release refused: Git working tree is dirty.`n$dirty"
    }

    $sha = (git rev-parse HEAD).Trim()
    if ($LASTEXITCODE -ne 0 -or -not $sha) { throw "Unable to resolve release Git SHA." }
    $buildDate = [DateTime]::UtcNow.ToString("yyyy-MM-ddTHH:mm:ssZ")
    $env:RELEASE_GIT_SHA = $sha
    $env:RELEASE_BUILD_DATE = $buildDate

    docker compose -f docker-compose.yml -f docker-compose.release.yml build bot
    if ($LASTEXITCODE -ne 0) { throw "Release image build failed." }

    $label = docker image inspect "reels_bot:$sha" --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}'
    if ($LASTEXITCODE -ne 0 -or $label.Trim() -ne $sha) {
        throw "Image provenance verification failed: expected $sha, got $label"
    }

    if ($Deploy) {
        docker compose -f docker-compose.yml -f docker-compose.release.yml up -d --force-recreate bot worker
        if ($LASTEXITCODE -ne 0) { throw "Release deployment failed." }
    }

    Write-Output "RELEASE_GIT_SHA=$sha"
    Write-Output "RELEASE_BUILD_DATE=$buildDate"
    Write-Output "IMAGE=reels_bot:$sha"
    Write-Output "DEPLOYED=$($Deploy.IsPresent)"
}
finally {
    Pop-Location
}

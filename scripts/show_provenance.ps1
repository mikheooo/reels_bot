$ErrorActionPreference = "Stop"

foreach ($service in @("bot", "worker")) {
    $containerId = docker compose ps -q $service
    if (-not $containerId) {
        Write-Output "$service=NOT_RUNNING"
        continue
    }
    $imageId = docker inspect $containerId --format '{{.Image}}'
    $imageSha = docker image inspect $imageId --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}'
    $runtime = docker exec $containerId python -m app.core.runtime
    Write-Output "$service container=$containerId image=$imageId label_sha=$imageSha runtime=$runtime"
}

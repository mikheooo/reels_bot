# Release operations

Production releases use Python 3.13 and must be built from a clean Git tree.
Direct ad-hoc production builds are unsupported; use `scripts/release.ps1`.

## Offline release gates

```powershell
python -m pip install -r requirements-dev.txt
ruff check .
pytest -m "not integration" -q
```

CI installs `requirements-dev.txt` and runs the last two commands on Python 3.13.
Integration tests are opt-in and may access configured services.

## PostgreSQL backup

The Compose volume is explicitly named `reels_bot_postgres_data`. Before any
volume migration or destructive database maintenance:

```powershell
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
New-Item -ItemType Directory -Force backups | Out-Null
docker compose exec -T postgres pg_dump -U user -d reels_db -Fc -f "/tmp/reels_db-$stamp.dump"
docker cp "reels_bot-postgres-1:/tmp/reels_db-$stamp.dump" "backups/reels_db-$stamp.dump"
docker compose exec -T postgres rm -f "/tmp/reels_db-$stamp.dump"
Get-FileHash -Algorithm SHA256 "backups/reels_db-$stamp.dump"
docker run --rm --mount "type=bind,source=$((Resolve-Path "backups/reels_db-$stamp.dump")),target=/backup.dump,readonly" postgres:15-alpine pg_restore --list /backup.dump
```

Backups are ignored by Git. Keep an additional copy outside this workstation.

## Controlled restore verification

Never test a restore against canonical production. Restore into a disposable
PostgreSQL 15 container, compare aggregate counts, then remove only that
temporary container:

```powershell
$backup = (Resolve-Path "backups/reels_db-YYYYMMDD-HHMMSS.dump").Path
$name = "reels-bot-restore-verify"
docker run -d --name $name --tmpfs /var/lib/postgresql/data -e POSTGRES_USER=user -e POSTGRES_PASSWORD=password -e POSTGRES_DB=reels_db postgres:15-alpine
docker exec $name pg_isready -U user -d reels_db
docker cp $backup "${name}:/tmp/backup.dump"
docker exec $name pg_restore -U user -d reels_db --clean --if-exists /tmp/backup.dump
docker exec $name psql -U user -d reels_db -c "SELECT status, count(*) FROM jobs GROUP BY status ORDER BY status;" -c "SELECT count(*) FROM tasks;"
docker rm -f $name
```

## Build, deploy and provenance

```powershell
pwsh -File scripts/release.ps1
pwsh -File scripts/release.ps1 -Deploy
pwsh -File scripts/show_provenance.ps1
docker compose logs --since 5m bot worker
```

The release script refuses a dirty tree, passes the exact HEAD SHA and build
timestamp into the image, verifies the OCI revision label, and optionally
recreates bot and worker. Runtime startup logs contain the same identity.

## Completion and audit policy

- `DONE`: all applicable delivery steps succeeded, or channel delivery was an
  idempotent duplicate skip.
- `PARTIAL`: analysis reached the user, but channel, plan or Task persistence
  failed. `delivery_status` records each step.
- `ERROR`: the required analysis/user-delivery pipeline failed.
- `REVIEW_REQUIRED`: strict evidence policy blocked publication and task creation.

Post-Publish Audit is deferred. New jobs are not scheduled. Historical due
timestamps are preserved and marked `DEFERRED_LEGACY`; they are not executed
until a future explicitly scoped stage implements an idempotent scheduler.

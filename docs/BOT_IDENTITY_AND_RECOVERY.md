# Bot identity and recovery

## Why the incident happened

The Telegram token for `@Reeelsanalyzerbot` was running in the `reels_bot` Compose project. That project contained the legacy reels pipeline, so the analyzer token received the wrong implementation. The wrong formatter then emitted a raw transcript instead of the structured analyzer report.

The two stacks are separate directories:

- `C:/Users/Misha/reels_bot` — Reels Analyzer (`@Reeelsanalyzerbot`)
- `C:/Users/Misha/reels2action` — Reels2Action (`@Reels2ActionBot`)

Never copy `.env` files between these directories.

## Startup guard

`reels_bot` now verifies the Telegram identity with `getMe` before polling. Compose passes `EXPECTED_BOT_USERNAME=Reeelsanalyzerbot`. If a wrong token is placed in this project, the bot exits with an identity-mismatch error instead of silently processing messages with the wrong bot.

## Recovery

From `C:/Users/Misha/reels_bot`:

```bash
docker compose build bot worker
docker compose up -d --force-recreate bot worker
docker compose logs --since 2m bot worker
```

Expected startup evidence includes:

```text
Telegram identity verified: @Reeelsanalyzerbot
Run polling for bot @Reeelsanalyzerbot
Worker starting up...
```

Before changing code, inspect the repository state:

```bash
git status --short
git log --oneline -5
```

Do not use `reels2action`'s Compose project to start the analyzer and do not use the analyzer token in `reels2action/.env`.

# ROADMAP REELS ANALYZER

## CURRENT RELEASE BOUNDARY

Production is the reactive Telegram analysis pipeline documented in
`README.md`. Content Router, hybrid transcription, fact-checking and delivery
semantics are current capabilities. Prioritization is connected to Router policy
and controls only usefulness-oriented optional actions; it cannot relax safety
requirements or hide the user result. Multilingual analysis has an explicit
source-preserving contract and offline equivalence evaluation. Multiple output
variants contract (`TLDR`, `TELEGRAM_LONG`, `X_POST`, `THREADS_POST`, `YOUTUBE_COMMUNITY`)
is deterministically rendered, validated against platform constraints, and persisted.
Content Factory Delivery, External Platform Connectors (Level B publication boundary
for X, Threads, YouTube Community), and Post-Publish Audit & Telemetry v2 (decaying cadence
monitoring, observational Telegram edit telemetry, and strict occurrence idempotency) are
verified and complete.

Current stage is **Priority 5: Automatic Publication — Publication Orchestrator Core & Controlled X API v2 Connector Path — COMPLETE**.
The pipeline implements the unified `PublicationOrchestrator`, exact `OwnerApproval` binding, a 9-state publication state machine, crash consistency boundaries A-E, per-platform isolation, deterministic idempotency, and an offline replay suite with 7 safety gates. Autonomous publishing without approval remains prohibited.

## PRIORITY 1: Мультиязычный анализ — COMPLETE
**Цель:** Научить пайплайн автоматически определять язык ролика и корректно работать с английским, русским, тайским, испанским, китайским, японским.
**Включает:** Whisper (или нативный анализ Gemini), фактчекинг, QA, генерацию постов, перевод при необходимости.

**Baseline 2026-09-06:** typed language context, deterministic detection,
source preservation, Russian output policy, multilingual claim/search metadata
and offline equivalence gates implemented. Live calibration breadth and an
optional user-selectable output language remain future scopes.

## PRIORITY 2: Приоритизация новостей — COMPLETE
**Цель:** Не публиковать все подряд. Добавить AI-модуль оценки новости.
**Оценивать:** важность, вирусность, новизну, вероятность набора просмотров, ценность для аудитории. Публиковать только материалы выше заданного порога.

**Baseline 2026-09-06:** typed PriorityScore, deterministic Router-Priority bridge (router_priority_v1),
immutable safety floor preservation, and offline evaluation suite implemented.

## PRIORITY 3: Несколько вариантов постов — COMPLETE
**Цель:** После анализа автоматически генерировать: короткий пост, подробный пост, Telegram, X, Threads, YouTube Community. AI должен выбирать лучший вариант.

**Baseline 2026-09-06:** typed CanonicalContentResult, deterministic renderers for TLDR, TELEGRAM_LONG, X_POST, THREADS_POST, and YOUTUBE_COMMUNITY, declarative platform constraints (VARIANT_CONSTRAINTS), budget overflow handling without blind slicing, persistence under jobs.qa_reasons.output_variants, and offline evaluation replay suite implemented.

## COMPLETED SUBSYSTEMS (PRIORITY 5 PREREQUISITES):
- **Content Factory Delivery — COMPLETE:** ContentPackageModel, DistributionTarget, ContentDeliveryModel, PublicationIntentModel, and owner approval lifecycle.
- **External Platform Connectors — COMPLETE:** PublicationConnector base, XConnector (OAuth 1.0a / OAuth 2.0 PKCE / API v2), ThreadsConnector (Graph API), YouTubeCommunityConnector (manual export boundary), rate limiting, retry backoff, and connector evaluation suite.
- **Post-Publish Audit & Telemetry v2 — COMPLETE:** AuditTargetModel, AuditSnapshotModel, AuditEventModel, multi-phase decaying cadence (15m, 2h, 12h, 24h, 3d, 7d), observational edit telemetry, scheduled occurrence idempotency, and offline audit replay suite.

## PRIORITY 4: Обучение на собственной статистике — COMPLETE (Shadow Calibration Mode v1)
**Цель:** После публикации автоматически собирать: просмотры, CTR, удержание, лайки, комментарии, репосты. Использовать для улучшения выбора тем и стиля публикаций.
**Scope v1 (COMPLETE):** Typed OutcomeObservation contracts, fixed horizons (15m, 2h, 12h, 24h, 3d, 7d), platform-specific normalization without fake zeros, data quality taxonomy, data sufficiency policies, outlier guards, shadow calibration engine, and offline replay suite without automatic production policy mutation.

## PRIORITY 5: Автоматическая публикация — IN PROGRESS (Slice 1 Core & Slice 2 Multi-Platform Expansion COMPLETE)
**Цель:** После подтверждения автоматически публиковать в Telegram, YouTube Shorts, Instagram, TikTok, X, Threads (Единый пайплайн).
**Slice 1 Baseline (2026-09-07):** Publication Orchestrator Core (`PublicationOrchestrator`), `OwnerApproval` cryptographic binding, 9-state publication state machine, crash consistency across Boundaries A-E, per-platform isolation, deterministic idempotency (`publication_key` & `attempt_key`), X API v2 controlled connector validation, and deterministic evaluation replay suite (15 scenarios, 7 safety gates). Zero autonomous publishing without owner approval.
**Slice 2 Baseline (2026-09-07):** Multi-Platform Publication Expansion & Connector Parity (`TargetPlatform.THREADS` official Graph API integration with two-phase container publishing, user timeline reconciliation `GET /{user-id}/threads?limit=5`, capability declaration model, target-set binding on `OwnerApproval`, deterministic `PublicationPlanStatus` 8-state aggregation, preflight payload capability validation, 34/34 orchestration tests, and 19 replay scenarios across all 7 mandatory safety gates). Autonomous publishing remains strictly disabled.

## PRIORITY 6: Самообучающийся фактчекинг
**Цель:** Если Post-Publish Audit регулярно находит ошибки: определить причину, понять где ошиблась модель, автоматически улучшать промпты, снижать количество ложных подтверждений.

## PRIORITY 7: Интеллектуальный поиск источников
**Цель:** Динамический выбор источника вместо фиксированного Exa (официальный сайт, GitHub, документация, пресс-релиз, научная статья, регулятор, новостные агентства) в зависимости от типа новости.

## PRIORITY 8: Оценка доверия
**Цель:** Каждая публикация получает Trust Score (например, 92/100) с объяснением (официальный источник, 3 подтверждения, свежая информация, QA согласен, аудит пройден).

## PRIORITY 9: Полностью автономный режим
**Цель:** End-to-end пайплайн: поиск новостей → выбор лучших → проверка → QA → публикация → аудит через неделю → исправления → обучение на результатах (без участия человека).

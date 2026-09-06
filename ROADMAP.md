# ROADMAP REELS ANALYZER

## CURRENT RELEASE BOUNDARY

Production is the reactive Telegram analysis pipeline documented in
`README.md`. Content Router, hybrid transcription, fact-checking and delivery
semantics are current capabilities. Prioritization is connected to Router policy
and controls only usefulness-oriented optional actions; it cannot relax safety
requirements or hide the user result. Multilingual analysis has an explicit
source-preserving contract and offline equivalence evaluation. Multiple output
variants contract (`TLDR`, `TELEGRAM_LONG`, `X_POST`, `THREADS_POST`, `YOUTUBE_COMMUNITY`)
is deterministically rendered, validated against platform constraints, and persisted in
the database with offline evaluation replay gating. External auto-publishing, Post-Publish
Audit, and the content-factory design remain deferred and must not be described as
active production capabilities.

## PRIORITY 1: Мультиязычный анализ
**Цель:** Научить пайплайн автоматически определять язык ролика и корректно работать с английским, русским, тайским, испанским, китайским, японским.
**Включает:** Whisper (или нативный анализ Gemini), фактчекинг, QA, генерацию постов, перевод при необходимости.

**Baseline 2026-09-06:** typed language context, deterministic detection,
source preservation, Russian output policy, multilingual claim/search metadata
and offline equivalence gates implemented. Live calibration breadth and an
optional user-selectable output language remain future scopes.

## PRIORITY 2: Приоритизация новостей
**Цель:** Не публиковать все подряд. Добавить AI-модуль оценки новости.
**Оценивать:** важность, вирусность, новизну, вероятность набора просмотров, ценность для аудитории. Публиковать только материалы выше заданного порога.

## PRIORITY 3: Несколько вариантов постов
**Цель:** После анализа автоматически генерировать: короткий пост, подробный пост, Telegram, X, Threads, YouTube Community. AI должен выбирать лучший вариант.

**Baseline 2026-09-06:** typed CanonicalContentResult, deterministic renderers for TLDR, TELEGRAM_LONG, X_POST, THREADS_POST, and YOUTUBE_COMMUNITY, declarative platform constraints (VARIANT_CONSTRAINTS), budget overflow handling without blind slicing, persistence under jobs.qa_reasons.output_variants, and offline evaluation replay suite implemented. External auto-publishing remains manual/deferred.

## PRIORITY 4: Обучение на собственной статистике
**Цель:** После публикации автоматически собирать: просмотры, CTR, удержание, лайки, комментарии, репосты. Использовать для улучшения выбора тем и стиля публикаций.

## PRIORITY 5: Автоматическая публикация
**Цель:** После подтверждения автоматически публиковать в Telegram, YouTube Shorts, Instagram, TikTok, X, Threads (Единый пайплайн).

## PRIORITY 6: Самообучающийся фактчекинг
**Цель:** Если Post-Publish Audit регулярно находит ошибки: определить причину, понять где ошиблась модель, автоматически улучшать промпты, снижать количество ложных подтверждений.

## PRIORITY 7: Интеллектуальный поиск источников
**Цель:** Динамический выбор источника вместо фиксированного Exa (официальный сайт, GitHub, документация, пресс-релиз, научная статья, регулятор, новостные агентства) в зависимости от типа новости.

## PRIORITY 8: Оценка доверия
**Цель:** Каждая публикация получает Trust Score (например, 92/100) с объяснением (официальный источник, 3 подтверждения, свежая информация, QA согласен, аудит пройден).

## PRIORITY 9: Полностью автономный режим
**Цель:** End-to-end пайплайн: поиск новостей → выбор лучших → проверка → QA → публикация → аудит через неделю → исправления → обучение на результатах (без участия человека).

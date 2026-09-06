# Telegram analysis UX: real-world readability validation v1

Validation date: 2026-09-07 ICT

## Method

- 12 real production Reel analyses were reviewed.
- Two Kwork samples use persisted `TELEGRAM_LONG` output from production jobs.
- Ten samples were rendered without delivery or database writes from existing production transcripts and structured fact/business results through the deployed Router, Priority, specialized-analysis, canonical, and `TELEGRAM_LONG` path.
- No synthetic golden content was counted as a real-world sample.
- Available production history covered business/income, how-to, products/services, finance claims, education, job search, informational content, business-check/no-business-check, LOW/MEDIUM risk, confirmed facts, and unverified claims. No suitable typed health/wellness or travel/lifestyle output was available; those categories are an explicit coverage gap rather than simulated evidence.

## Findings before code changes

| Job/sample | Content type | Result | Exact problematic phrase | UX impact | Deterministic correction rule |
|---|---|---|---|---|---|
| `dcec0b41-dd28-404d-b818-adf78b025c77` | Business/income, MEDIUM, business check | ISSUE | `...DeepSeek для написания` | Title ends with an unfinished phrase and the conclusion starts with the same wording. | Prefer a complete clause; remove trailing dependent words; keep the full explanation in the conclusion. |
| `c263d729-960d-48d1-b02f-ce42b57342b7` | How-to, MEDIUM, no business check | ISSUE | `Видеоинструкция по поиску заказов...` repeated at the start of `Короткий вывод` | Heading and first paragraph read as duplicated summary. | Make the title a shorter subject phrase while preserving `what_it_is` in the conclusion. |
| `2570e155-b0c2-46da-92bb-96526b20b915` | Job-search how-to, MEDIUM | ISSUE | `...под ATS-фильтры и попытке`; `Ролик в основном сообщает информацию.` | Dangling title ending; neutral intent is presented as a reason for risk. | Trim incomplete title tails and suppress neutral intents from risk reasons. |
| `bb980309-2f47-41be-8f87-f9500c8e3033` | YouTube automation product, MEDIUM | ISSUE | `...контент-плана, сценариев`; `иначе пропустить` | Title stops inside a list; recommendation still tells the user to skip the material. | Prefer the first complete clause and remove skip-only suffixes. |
| `a76c6ba6-605d-432b-a4e6-90db92fbda15` | Integration/plugin how-to, LOW | ISSUE | `...к ChatGPT через`; four intent lines under LOW risk | Title ends on a preposition; LOW-risk block makes neutral/persuasion intents look like hazards. | Remove trailing connectors and omit causal intent bullets for LOW risk. |
| `9a3513d0-2501-427b-8833-43a3e064bb92` | Remotion product/how-to, LOW | ISSUE | `...Remotion, синтеза`; English fact statements in Russian output | Incomplete enumeration and unnecessary language switch reduce mobile readability. | Prefer a complete clause; use existing Russian `analysis_statement` when present. |
| `163c860c-9b2b-4c01-a4d0-87c15acf1a48` | Education/certifications, LOW | ISSUE | `...GitHub Foundations`; heading `Что здесь за бизнес-модель` for `EDUCATIONAL` | Title cuts a parenthesized list; educational explanation is mislabeled as a business model. | Drop incomplete parenthetical tails; use `Что здесь за смысл` for non-commercial categories. |
| `7eec579a-a38b-49ad-ae53-e3d70955eaf4` | Finance-related software, MEDIUM | ISSUE | `...которая токенизирует сырые`; `Ролик в основном сообщает информацию.` | Title ends before its object; neutral intent dilutes the actual warning. | Trim dependent tail and suppress `INFORM` in risk reasons. |
| `ad7abb2b-d406-4683-8641-1698c5d23211` | Job platforms/service, MEDIUM | ISSUE | `Ролик в основном сообщает информацию.` | This does not explain why the risk is medium. | Keep only actionable/risk-relevant intents in MEDIUM/HIGH reasons. |
| `5c9d9146-96a0-445e-bfdd-786a58e4c930` | Editing skill/plugin, LOW | ISSUE | Four intent bullets under `Риск: низкий` | The block visually overstates risk despite a LOW classification. | For LOW risk show level and guidance without causal bullets. |
| `a6065088-4d0c-4bef-ac46-c02ee8b374d7` | Donkey Cut product, LOW | ISSUE | `Donkey Cut is a free CapCut alternative...` | Russian message unexpectedly switches to English although normalized Russian claim text exists. | Select `analysis_statement` for presentation; retain original claim fields internally. |
| `fbfd3102-b544-43ea-9d0f-9bcc84ce02ea` | Income/business funnel, MEDIUM | ISSUE | `...675 заказов за 3 месяца) и` | Title ends on a conjunction. | Remove unfinished conjunction/dependent tail without changing canonical data. |

## Confirmed patch scope

1. Improve deterministic title clause selection and dangling-tail cleanup.
2. Keep LOW risk compact and remove neutral `INFORM`/`ENTERTAIN` reasons from MEDIUM/HIGH presentation.
3. Prefer the existing normalized Russian `analysis_statement` in canonical user-facing claim summaries.
4. Render `EDUCATIONAL` and `UNCLEAR` business results under `Что здесь за смысл`.
5. Remove deterministic `иначе пропустить`/`пропустить материал` recommendation fragments wherever they occur.

Router, Priority, risk classification/thresholds, Business classifier, Fact Check semantics, persistence, lifecycle, delivery, audit, and telemetry are outside this patch.

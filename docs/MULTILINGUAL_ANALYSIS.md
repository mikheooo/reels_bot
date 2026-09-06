# Multilingual analysis contract

## Audited behavior before this stage

The Gemini 3.5 transcription path returned verbatim text only. Its metadata
contained model, fallback, completeness status, duration, latency and character
count, but no language. The primary transcription prompt was English and the
legacy prompt Russian; neither established a typed language result.

The canonical transcript was already persisted without rewriting. Router,
prioritization, structured/specialized analysis, visual descriptions,
fact-check, business-check, Telegram rendering and Task/Hermes text used mainly
Russian prompts. Models could translate implicitly, but no result recorded the
source, output language, mixed-language state or translation decision.
`Claim.statement` could represent either original or normalized text, and Exa
received only that one query. Mixed-language and non-Latin input was passed to
models without explicit policy. No multilingual equivalence suite existed.

## Canonical flow

```text
media
  -> verbatim canonical transcript + transcription metadata
  -> LanguageContext(transcript + provider metadata if present + visible text)
  -> Content Router
  -> prioritization
  -> combined deterministic policy
  -> analysis / claims / fact-check / business-check
  -> Russian user response and Russian channel result
```

`LanguageContext` is defined in `app/worker/language.py` and persisted under
`jobs.qa_reasons.language`. It records dominant language/code, confidence,
mixed status, source languages, analysis/user/channel languages, translation
policy, detector/fallback information and a SHA-256 of the original transcript.

## Detection policy

Detection order:

1. normalized transcription-provider metadata, when present;
2. local deterministic script/lexical detection over the transcript, augmented
   at lower weight by verbatim `text_read` from visual evidence;
3. `unknown` fallback.

Provider metadata currently does not expose a language, so production normally
uses step 2. Codes normalize BCP-47-like values to their base code. The tested
minimum is `en`, `ru`, `th`, `uk`, `es`, and `unknown`; the contract can also
represent other codes and includes basic CJK script recognition. Telegram
locale is never used as source-language evidence.

The detector is bounded by `LANGUAGE_DETECTION_TIMEOUT_SECONDS` (default 2).
It does not call an external translation or detection API.

## Source, analysis and output policy

- `jobs.full_transcript` remains the exact selected transcription result in the
  source language. It is never replaced by a translation.
- Canonical internal analysis language is Russian (`ru`). Every downstream
  prompt receives an explicit language directive.
- User Telegram output is Russian. There is no persisted user-language
  preference in the current product, so no new preference store was added.
- Channel output is independently fixed to Russian. Language policy does not
  merge user delivery with priority/channel publication decisions.
- Translation uses the existing Gemini analysis calls through explicit prompt
  policy. There is no separate translated-transcript artifact or external API.

For a Russian-only source, `translation_required=false`. A non-Russian or mixed
source sets it true. Unknown language uses Russian fallback analysis without
claiming that a source translation succeeded.

## Mixed language

Mixed content is first class. `detected_language_code` is the dominant language
and `source_languages` retains meaningful secondary language signals. Russian
speech with English technical terms and Thai speech/text with English product
names are covered by offline fixtures. Visible subtitle/text languages can add
a secondary source language without overriding stronger speech evidence.

## Claims, translation and evidence

Each new extracted `Claim` can retain:

- `original_statement` and `original_language_code`;
- Russian `analysis_statement`/`statement` as a separate representation;
- `translation_applied` and `translation_status`;
- typed `search_queries` with language and purpose;
- `source_quote`, which points to the original Reel wording.

Web `exact_quote` remains verbatim source evidence and is never treated as a
translation. Claim validation carries original-language metadata forward even
when it produces a Russian assessment.

Fact-check searches the original-language query first. For a non-English claim,
the extraction model may provide a separate English coverage query; when it is
present, Exa searches both and deduplicates URLs. Results record the query
language. Source authority is determined by provenance/domain, not Englishness;
using an English query does not upgrade source quality.

## Failure semantics

- Unsupported or weak text becomes `UNKNOWN` with a fallback reason; safe
  analysis continues and does not become `ERROR` merely because of language.
- Detector exception/timeout becomes an observable `UNKNOWN` context with
  `failure_code`. `delivery_status.language=FAILED_FALLBACK:<type>`, so a
  completed delivery is `PARTIAL`, not a false `DONE`.
- Missing claim translation uses `FALLBACK_ORIGINAL`; original evidence and
  original-language search remain available.
- Failures in translated structured analysis follow the existing bounded
  downstream fallback/error semantics. They cannot relax Router risk floors.
- HIGH-risk requirements are derived from Router semantics and are unchanged by
  language, translation convenience or priority.

## Offline evaluation

`tests/fixtures/multilingual_eval.json` contains 13 cases across seven semantic
groups: HOW_TO in English/Russian/Thai; a HIGH-risk medical claim in
English/Russian/Ukrainian; BUSINESS in English/Thai; low-value entertainment in
English/Russian; Russian+English; Thai+English; and an unknown poor transcript.

The replay checks language/mixed detection, original transcript hash, fixed
output languages, Router/risk equivalence, priority-band equivalence, combined
policy equivalence, score drift and zero language-induced risk-floor violations.
Recorded outputs make the release gate deterministic and network-free; they do
not claim live-model quality or complete linguistic coverage.

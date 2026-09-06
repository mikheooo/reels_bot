# Multiple output variants contract & offline evaluation

## Overview and motivation

Prior to this stage, `reels_bot` generated delivery artifacts strictly for Telegram:
- A user summary message (MarkdownV2 or HTML formatted);
- An optional channel post for high-priority / verified content.

As the pipeline evolved to support multi-platform distribution (Telegram, X/Twitter, Meta Threads, YouTube Community), formatting logic cannot be scattered or coupled to downstream publication clients. Moreover, naive string slicing (e.g. `text[:280]`) introduces critical failures:
1. Truncating sentences mid-word or mid-claim alters meaning;
2. Truncating mandatory risk disclaimers (e.g., medical or financial warnings) violates safety floors;
3. Incomplete formatting breaks markup parsers.

This stage introduces a typed, deterministic rendering contract that produces 5 platform-specific output variants from a single **Canonical Content Representation** (`CanonicalContentResult`), validates each variant against declarative platform constraints, and records render metrics without performing external API calls or auto-publishing.

---

## Canonical flow

```text
media analysis
  -> verbatim canonical transcript (source language)
  -> LanguageContext (jobs.qa_reasons.language)
  -> Content Router (primary_label, secondary_labels, risk_level)
  -> Prioritization (priority_score, priority_decision)
  -> Fact-check & business-check results
  -> CanonicalContentResult (normalized epistemic & structural content)
  -> Deterministic Variant Renderers (TLDR, TELEGRAM_LONG, X_POST, THREADS_POST, YOUTUBE_COMMUNITY)
  -> Declarative Validator (against VARIANT_CONSTRAINTS)
  -> OutputVariantsBundle (jobs.qa_reasons.output_variants, version="variants_v1")
  -> jobs.delivery_status["output_variants"] = "SUCCEEDED"
```

### Production Telegram delivery integration (Option A)

Telegram user delivery is directly bound to the `TELEGRAM_LONG` output variant:
1. `resolve_telegram_delivery_payload(payload, legacy_analysis)` evaluates variant rendering status.
2. If `TELEGRAM_LONG` is successfully rendered (`status == "RENDERED"`), its text is delivered to the user as `user_delivery_text`, and persisted as `jobs.analysis_text`.
3. If `TELEGRAM_LONG` fails to render (`FAILED` or `NOT_RENDERABLE`), user delivery gracefully falls back to legacy analysis text, marks `delivery_status["user"] = "SUCCEEDED_FALLBACK"`, and `delivery_status["output_variants"] = "FAILED:TELEGRAM_LONG_..."`. The job terminal completion status resolves to `PARTIAL` (never a false `DONE`).
4. Failures in optional external variants (`X_POST`, `THREADS_POST`, `YOUTUBE_COMMUNITY`) do not degrade user delivery or block `DONE`.

---

## Architecture and data contracts

The contract is implemented in `app/worker/output_variants.py`.

### 1. `CanonicalContentResult`

Represents the verified semantic essence of the video, independent of platform presentation:

```python
class CanonicalContentResult(BaseModel):
    title: str
    core_takeaway: str
    detailed_points: list[str] = Field(default_factory=list)
    actionable_steps: list[str] = Field(default_factory=list)
    primary_category: str  # HOW_TO, BUSINESS_IDEA, etc.
    risk_level: str        # LOW, MEDIUM, HIGH, CRITICAL
    epistemic_status: str  # VERIFIED, PLAUSIBLE, DISPUTED, UNVERIFIED
    verified_claims: list[str] = Field(default_factory=list)
    disputed_claims: list[str] = Field(default_factory=list)
    mandatory_disclaimers: list[str] = Field(default_factory=list)
    language_code: str = "ru"
    source_url: str | None = None
```

Normalization helper:
`build_canonical_content_result(...)` derives this representation from the job's analysis text, structured analysis JSON, fact-check evaluation, router result, priority result, and language context.

### 2. Declarative platform constraints (`VARIANT_CONSTRAINTS`)

Each variant type is bound by explicit constraints registered in `VARIANT_CONSTRAINTS`:

| Variant Type | Max Chars | Min Chars | Markup Allowed | Max Hashtags | Truncation Policy |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `TLDR` | 280 | 20 | None | 0 | Deterministic / typed failure |
| `TELEGRAM_LONG` | 4096 | 100 | HTML | 5 | Structural pagination (not sliced) |
| `X_POST` | 280 | 10 | None | 2 | **NO blind slicing**; fails if budget exceeded |
| `THREADS_POST` | 500 | 20 | None | 3 | Deterministic / typed failure |
| `YOUTUBE_COMMUNITY` | 2000 | 50 | None | 3 | Structural summary |

### 3. Epistemic preservation and safety invariants

1. **Certainty Preservation**: Disputed claims are never rendered as verified facts across any variant.
2. **Mandatory Risk Disclaimers**: For `HIGH` or `CRITICAL` risk categories (e.g. medical, health, high-stakes finance), disclaimers (e.g. `⚠️ Внимание: Требуется консультация специалиста.`) are unconditionally prepended or appended.
3. **No Blind Truncation**: If a post (such as `X_POST`) cannot fit its core takeaway and mandatory disclaimers within the platform character budget, it **MUST NOT** be blindly sliced (`text[:280]`). Instead, the renderer sets:
   - `status = VariantRenderStatus.NOT_RENDERABLE`
   - `error_reason = "BUDGET_EXCEEDED"`
   - `text = ""`

### 4. `OutputVariant`

```python
class OutputVariant(BaseModel):
    variant_type: OutputVariantType
    text: str
    char_count: int
    status: VariantRenderStatus  # RENDERED, NOT_RENDERABLE
    violations: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
```

---

## Variant renderers

1. **`TLDR`**:
   - Ultra-compact summary for quick reading.
   - Core takeaway + 1 key actionable step or risk disclaimer.
   - Strictly <= 280 characters.
2. **`TELEGRAM_LONG`**:
   - Comprehensive multi-section post formatted in clean HTML.
   - Header with category tag and epistemic badge.
   - Core takeaway, detailed analysis, step-by-step instructions, and verified fact-check sources.
   - Risk disclaimer block where applicable.
3. **`X_POST`**:
   - Punchy post optimized for microblogging.
   - If takeaway + disclaimer <= 280 chars, renders cleanly with category hashtag.
   - If takeaway exceeds character budget even after concise reformulation, returns `NOT_RENDERABLE` (`BUDGET_EXCEEDED`).
4. **`THREADS_POST`**:
   - Conversational, narrative post with hook, 2-3 key bullet points, and discussion question.
   - Strictly <= 500 characters.
5. **`YOUTUBE_COMMUNITY`**:
   - Video companion format designed for subscriber engagement.
   - Title, insights, actionable tips, discussion question for comments, and source Reel link.
   - Strictly <= 2000 characters.

---

## Persistence and failure semantics

Output variants are computed in `app/worker/tasks.py` during `process_video`:

```python
# Both normal completion and REVIEW_REQUIRED execute variant generation:
variants_bundle = generate_all_variants(canonical_content)
qa_reasons_data["output_variants"] = variants_bundle.to_dict()
delivery_status["output_variants"] = "SUCCEEDED"
```

- **Persistence**: Persisted in `jobs.qa_reasons["output_variants"]` with schema version `variants_v1`.
- **Delivery Status**: Recorded in `jobs.delivery_status["output_variants"] = "SUCCEEDED"` (or `"FAILED"` if an unexpected runtime exception occurs).
- **Graceful Degradation**: If an individual variant fails validation or budget checks, its status is `NOT_RENDERABLE` with a typed reason; the overall bundle still persists and valid variants remain usable.
- **Independence**: Generation of output variants does not interfere with Telegram user delivery or Telegram channel publishing.

---

## Publishing boundary

> [!IMPORTANT]
> **NO external publishing is implemented or enabled in this stage.**
> Publishing to X (Twitter), Meta Threads, or YouTube Community requires platform developer credentials, OAuth2 token management, webhook infrastructure, and rate limit handling.
> This stage strictly defines and verifies the **content contracts, deterministic rendering, validation, and storage**.

---

## Offline evaluation replay suite

The offline evaluation suite verifies rendering determinism, constraint adherence, and epistemic preservation without network calls or LLM variability.

### Evaluation fixtures (`tests/fixtures/output_variants_eval.json`)

Contains 8 diverse test cases:
1. `how_to_git_rebase`: Standard technical how-to tutorial.
2. `how_to_bread_making`: Step-by-step culinary how-to.
3. `health_fasting_cure`: HIGH-risk medical content with disputed cancer cure claims; verifies mandatory disclaimer and non-endorsement of disputed claims.
4. `business_saas_micro`: Entrepreneurial business idea with actionable steps.
5. `entertainment_comedy`: Low-risk entertainment short with no steps.
6. `finance_crypto_arbitrage`: Disputed high-risk crypto scheme; verifies disclaimer and dispute preservation.
7. `multilingual_thai_tech`: Mixed Thai/English technical tutorial rendered into Russian variants.
8. `budget_exceeded_x_post`: Long single-sentence claim that cannot fit within 280 characters with disclaimer; verifies `NOT_RENDERABLE` (`BUDGET_EXCEEDED`) without blind slicing.

### Replay runner (`app/worker/variant_evaluation.py`)

Run via command:
```powershell
python -m app.worker.variant_evaluation
```

Metrics tracked across all 40 evaluations (8 cases × 5 variants):
- **Constraint Compliance Rate**: `1.0` (40/40 passed)
- **Epistemic Preservation Score**: `1.0` (0 disputed claims asserted, all high-risk disclaimers present)
- **Budget Compliance Rate**: `1.0` (0 budget overruns, clean `NOT_RENDERABLE` on overflow)
- **Deterministic Match Rate**: `1.0` (100% reproducible)

Automated tests:
```powershell
pytest tests/test_output_variants.py -v
```

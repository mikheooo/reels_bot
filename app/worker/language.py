"""Deterministic multilingual context and output policy."""

from __future__ import annotations

import asyncio
import hashlib
import re
from typing import Literal

from pydantic import BaseModel, Field

LanguageCode = str
DetectionMethod = Literal[
    "provider_metadata",
    "deterministic_script_lexical_v1",
    "fallback_unknown",
]

LANGUAGE_NAMES = {
    "en": "English",
    "ru": "Russian",
    "th": "Thai",
    "uk": "Ukrainian",
    "es": "Spanish",
    "zh": "Chinese",
    "ja": "Japanese",
    "ko": "Korean",
    "unknown": "Unknown",
}
ALIASES = {
    "eng": "en", "english": "en", "rus": "ru", "russian": "ru",
    "tha": "th", "thai": "th", "ukr": "uk", "ukrainian": "uk",
    "spa": "es", "spanish": "es", "und": "unknown", "unknown": "unknown",
}
EN_WORDS = {
    "the", "and", "this", "that", "how", "to", "is", "are", "with", "for",
    "you", "your", "can", "github", "actions", "deployment", "workflow", "pipeline",
}
ES_WORDS = {"el", "la", "los", "las", "y", "que", "como", "para", "con", "una", "un", "es", "puede"}
UK_WORDS = {"це", "що", "як", "для", "та", "але", "можна", "потрібно", "який", "вона", "вони"}


class LanguageContext(BaseModel):
    detected_language: str
    detected_language_code: LanguageCode
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    is_mixed_language: bool
    source_languages: list[LanguageCode]
    analysis_language: str = "Russian"
    analysis_language_code: LanguageCode = "ru"
    user_output_language: str = "Russian"
    user_output_language_code: LanguageCode = "ru"
    channel_output_language: str = "Russian"
    channel_output_language_code: LanguageCode = "ru"
    translation_required: bool
    translation_mode: Literal["none", "model_prompted", "fallback"]
    detection_method: DetectionMethod
    fallback_reason: str | None = None
    failure_code: str | None = None
    original_transcript_sha256: str
    contract_version: Literal["multilingual_v1"] = "multilingual_v1"


def normalize_language_code(value: str | None) -> str:
    if not value:
        return "unknown"
    code = value.strip().lower().replace("_", "-")
    code = ALIASES.get(code, code.split("-", 1)[0])
    return code if re.fullmatch(r"[a-z]{2,3}", code) else "unknown"


def _visible_text(visual_evidence: str | dict | None) -> str:
    if not isinstance(visual_evidence, dict):
        return ""
    return " ".join(
        str(item.get("text_read") or "")
        for item in visual_evidence.get("evidence", [])
        if isinstance(item, dict)
    )


def _language_signals(text: str) -> dict[str, float]:
    signals: dict[str, float] = {}
    thai = len(re.findall(r"[\u0E00-\u0E7F]", text))
    han = len(re.findall(r"[\u4E00-\u9FFF]", text))
    japanese = len(re.findall(r"[\u3040-\u30FF]", text))
    korean = len(re.findall(r"[\uAC00-\uD7AF]", text))
    if thai:
        signals["th"] = float(thai) * 2.0
    if han:
        signals["zh"] = float(han)
    if japanese:
        signals["ja"] = float(japanese + han)
    if korean:
        signals["ko"] = float(korean)

    cyrillic = re.findall(r"[А-Яа-яЁёІіЇїЄєҐґ]", text)
    if cyrillic:
        lowered = text.lower()
        uk_unique = len(re.findall(r"[іїєґ]", lowered))
        uk_words = sum(
            1 for word in re.findall(r"[а-яіїєґ]+", lowered) if word in UK_WORDS
        )
        code = "uk" if uk_unique or uk_words >= 2 else "ru"
        signals[code] = signals.get(code, 0.0) + len(cyrillic) * 2.0

    latin_words = re.findall(r"[A-Za-zÀ-ÿ]+", text.lower())
    if latin_words:
        en_hits = sum(word in EN_WORDS for word in latin_words)
        es_hits = sum(word in ES_WORDS for word in latin_words)
        spanish_marks = len(re.findall(r"[áéíóúñü¿¡]", text.lower()))
        if es_hits + spanish_marks > en_hits and es_hits + spanish_marks >= 1:
            latin_code = "es"
        elif en_hits >= 1 or any(code in signals for code in ("ru", "uk", "th")):
            latin_code = "en"
        else:
            latin_code = "unknown"
        if latin_code != "unknown":
            signals[latin_code] = signals.get(latin_code, 0.0) + sum(
                len(word) for word in latin_words
            )
    return signals


def detect_language_context(
    transcript: str,
    transcription_meta: dict | None = None,
    visual_evidence: str | dict | None = None,
) -> LanguageContext:
    transcript = transcript or ""
    digest = hashlib.sha256(transcript.encode("utf-8")).hexdigest()
    provider_code = normalize_language_code(
        (transcription_meta or {}).get("language_code")
        or (transcription_meta or {}).get("language")
    )
    signals = _language_signals(transcript)
    for code, weight in _language_signals(_visible_text(visual_evidence)).items():
        signals[code] = signals.get(code, 0.0) + weight * 0.25

    method: DetectionMethod = "deterministic_script_lexical_v1"
    if provider_code != "unknown":
        signals[provider_code] = signals.get(provider_code, 0.0) + max(
            sum(signals.values()) + 1.0, 20.0
        )
        method = "provider_metadata"

    if not signals:
        return unknown_language_context(
            transcript,
            "No reliable script or lexical signal in transcript/visible text.",
        )

    ordered = sorted(signals.items(), key=lambda item: item[1], reverse=True)
    dominant, dominant_weight = ordered[0]
    total = sum(signals.values())
    source_languages = [dominant]
    for code, weight in ordered[1:]:
        if weight >= 4 and weight / total >= 0.08:
            source_languages.append(code)
    mixed = len(source_languages) > 1
    translation_required = any(code != "ru" for code in source_languages)
    return LanguageContext(
        detected_language=LANGUAGE_NAMES.get(dominant, dominant),
        detected_language_code=dominant,
        confidence=round(dominant_weight / total, 4),
        is_mixed_language=mixed,
        source_languages=source_languages,
        translation_required=translation_required,
        translation_mode="model_prompted" if translation_required else "none",
        detection_method=method,
        original_transcript_sha256=digest,
    )


def unknown_language_context(
    transcript: str, reason: str, failure: BaseException | None = None
) -> LanguageContext:
    return LanguageContext(
        detected_language="Unknown",
        detected_language_code="unknown",
        confidence=None,
        is_mixed_language=False,
        source_languages=["unknown"],
        translation_required=False,
        translation_mode="fallback",
        detection_method="fallback_unknown",
        fallback_reason=reason,
        failure_code=type(failure).__name__ if failure else None,
        original_transcript_sha256=hashlib.sha256(
            (transcript or "").encode("utf-8")
        ).hexdigest(),
    )


async def resolve_language_context(
    transcript: str,
    transcription_meta: dict | None = None,
    visual_evidence: str | dict | None = None,
    timeout_seconds: float = 2.0,
) -> LanguageContext:
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(
                detect_language_context,
                transcript,
                transcription_meta,
                visual_evidence,
            ),
            timeout=timeout_seconds,
        )
    except Exception as exc:
        return unknown_language_context(
            transcript, "Language detection failed; Russian output fallback applied.", exc
        )


def language_prompt(context: LanguageContext | None) -> str:
    if context is None:
        return "Ответ и structured fields формируй на русском языке."
    return (
        "LANGUAGE CONTRACT: source languages="
        f"{context.source_languages}; dominant={context.detected_language_code}. "
        "Canonical transcript is immutable original evidence. Perform analysis "
        "and all explanatory/structured output in Russian. Preserve direct source "
        "quotes verbatim; put translations only in separate analysis fields."
    )


def language_delivery_outcome(context: LanguageContext) -> str:
    if context.failure_code:
        return f"FAILED_FALLBACK:{context.failure_code}"
    if context.detected_language_code == "unknown":
        return "SUCCEEDED_UNKNOWN_FALLBACK"
    return "SUCCEEDED"

from types import SimpleNamespace

from app.worker.schemas import Claim, VideoAnalysis
from app.worker.structured_analysis import REPORT_PROMPT
from app.worker.tasks import (
    _compose_analysis_output,
    _extract_summary,
    extract_tasks_from_analysis,
)


def _analysis_with_independent_layers() -> VideoAnalysis:
    claim = Claim(
        statement="Инструмент показан в видео",
        claim_type="fact",
        status="подтверждено",
        source_type="official",
        source_url="https://example.test/source",
    )
    business_check = SimpleNamespace(
        offer=SimpleNamespace(type="tool", description="Тестовый инструмент"),
        monetization_hypothesis=SimpleNamespace(type="unknown", reason="Не установлено"),
        cta=SimpleNamespace(detected=False, type="none", destination="none"),
        commercial_interest=SimpleNamespace(level="UNKNOWN", reason="Недостаточно данных"),
        reproducibility=SimpleNamespace(level="UNKNOWN", reason="Недостаточно данных"),
        promises=[],
        missing_economics=[],
        alternatives=SimpleNamespace(status="NOT_ENOUGH_DATA", items=[]),
        verdict=SimpleNamespace(category="UNCLEAR", assessment="NOT_ENOUGH_DATA", summary="Недостаточно данных"),
    )
    return VideoAnalysis.model_construct(
        claims=[claim],
        viable_idea=False,
        task_description=None,
        business_check=business_check,
    )


def test_structured_prompt_contract():
    required = [
        "КРАТКО ДЛЯ КАНАЛА",
        "О ЧЁМ ВИДЕО",
        "МЕХАНИКА И DATA FLOW",
        "Data Flow",
        "ФАКТ VS ВЫМЫСЕЛ",
        "Техническая сборка / реализация",
        "Легализация доступа к API",
        "Риски серого пути / блокировок",
        "ЗАДАЧА ДЛЯ МИХАИЛА",
        "Практический план интеграции",
        "КРИТЕРИИ ГОТОВНОСТИ",
        "📺 [В ВИДЕО]",
        "🧠 [КОНТЕКСТ ИИ]",
        "⚠️ [ПРЕДПОЛОЖЕНИЕ]",
        "не показано",
        "не подтверждено",
        "неизвестно",
        "Не выдумывай",
        "Не навязывай n8n, Hermes, Python",
    ]
    for marker in required:
        assert marker in REPORT_PROMPT


def test_optional_better_block_is_not_canonical_requirement():
    assert "### 🚀 МОЖНО ЛУЧШЕ" not in REPORT_PROMPT
    assert "Опциональный блок **🚀 МОЖНО ЛУЧШЕ**" in REPORT_PROMPT
    assert "Если такой альтернативы нет, блок не выводи вообще" in REPORT_PROMPT


def test_evidence_bound_plan_does_not_assume_unshown_installation():
    assert "если установка/настройка не показана" in REPORT_PROMPT
    assert "Проверить возможность" in REPORT_PROMPT
    assert "нельзя писать" in REPORT_PROMPT
    for verb in ("«установить»", "«настроить»", "«внедрить»", "«активировать»"):
        assert verb in REPORT_PROMPT


def test_evidence_bound_dod_cannot_turn_mentions_into_completed_work():
    assert "Не превращай упоминание инструмента" in REPORT_PROMPT
    assert "Подтверждено наличие" in REPORT_PROMPT
    assert "не может требовать установить, настроить, внедрить или активировать" in REPORT_PROMPT


def test_claims_must_not_be_strengthened_or_concretized():
    assert "Не усиливай claims" in REPORT_PROMPT
    assert "практически неограниченное использование" in REPORT_PROMPT
    assert "неограниченные лимиты" in REPORT_PROMPT
    assert "бесплатные API" in REPORT_PROMPT
    assert "затраты до нуля" in REPORT_PROMPT
    assert "Не добавляй конкретные причины" in REPORT_PROMPT
    assert "мусорным кодом" in REPORT_PROMPT


def test_mechanics_plan_and_dod_share_one_evidence_level():
    assert "МЕХАНИКА, Практический план интеграции и КРИТЕРИИ ГОТОВНОСТИ" in REPORT_PROMPT
    assert "не может внезапно стать обязательным выполненным этапом" in REPORT_PROMPT


def test_summary_is_bound_to_mechanics_evidence_status():
    normalized = " ".join(REPORT_PROMPT.split())
    assert "КРАТКО ДЛЯ КАНАЛА" in REPORT_PROMPT
    assert "SUMMARY" in REPORT_PROMPT
    assert "МЕХАНИКЕ" in REPORT_PROMPT
    assert "не показано" in REPORT_PROMPT
    assert "не подтверждено" in REPORT_PROMPT
    assert "неизвестно" in REPORT_PROMPT
    assert "не описывай его в summary как фактическую механику" in normalized


def test_summary_does_not_turn_unshown_installation_into_mechanics():
    normalized = " ".join(REPORT_PROMPT.split())
    forbidden_summary_phrases = (
        "механика сводится к установке",
        "работа заключается в установке",
        "нужно установить",
        "практическая механика — установка",
    )
    for phrase in forbidden_summary_phrases:
        assert phrase in normalized
    assert "не грубый запрет слова «установка»" in normalized


def test_summary_may_describe_an_explicitly_shown_installation():
    normalized = " ".join(REPORT_PROMPT.split())
    assert "если установка явно показана" in normalized
    assert "можно описать её как показанный процесс" in normalized


def test_structured_success_keeps_independent_layers():
    result = _compose_analysis_output(
        "### CANONICAL STRUCTURED REPORT\n### 4. ФАКТ VS ВЫМЫСЕЛ",
        _analysis_with_independent_layers(),
        "source transcript",
    )
    assert result.startswith("### CANONICAL STRUCTURED REPORT")
    assert "НЕЗАВИСИМАЯ ПРОВЕРКА УТВЕРЖДЕНИЙ" in result
    assert "Инструмент показан в видео" in result
    assert "Бизнес-анализ механики" in result
    assert result.index("CANONICAL STRUCTURED REPORT") < result.index("НЕЗАВИСИМАЯ")


def test_structured_failure_marks_transcript_as_source_material():
    result = _compose_analysis_output(None, _analysis_with_independent_layers(), "raw transcript")
    assert "STRUCTURED ANALYSIS UNAVAILABLE" in result
    assert "ДОСТУПНЫЙ МАТЕРИАЛ ВИДЕО" in result
    assert "Raw transcript is source material, not reconstructed mechanics." in result
    assert "Сырой Транскрипт (Механика)" not in result
    assert "НЕЗАВИСИМАЯ ПРОВЕРКА УТВЕРЖДЕНИЙ" in result
    assert "Бизнес-анализ механики" in result


def test_summary_and_task_contracts_remain_available():
    analysis = """**КРАТКО ДЛЯ КАНАЛА:**
Короткое описание механики видео.

---

### 6. ЗАДАЧА ДЛЯ МИХАИЛА
* **Инженерная суть:** Повторить показанный шаг.

#### КРИТЕРИИ ГОТОВНОСТИ:
1. Шаг воспроизведён.
"""
    assert "Короткое описание механики видео." in _extract_summary(analysis)
    tasks = extract_tasks_from_analysis(analysis, url="https://example.test/reel")
    assert len(tasks) == 1
    assert "Повторить показанный шаг" in tasks[0]["description"]

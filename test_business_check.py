import json
from unittest.mock import MagicMock, patch

import pytest

from app.worker.business_check import (
    format_business_check_markdown,
    run_business_check,
)
from app.worker.schemas import (
    BusinessCheckResult,
    Claim,
    VideoAnalysis,
)


# Mock BusinessCheckResult helper generator
def _make_mock_bc_result(
    offer_type="tool",
    offer_desc="Тестовый инструмент для автоматизации",
    monetization_type="saas",
    monetization_reason="Подписка на SaaS сервис",
    cta_detected=True,
    cta_type="telegram",
    cta_dest="t.me/testchannel",
    cta_prompt="Переходи в Телеграм канал",
    promises=None,
    missing_econ=None,
    repro_level="MEDIUM",
    repro_reason="Требуются базовые навыки настройки API",
    alt_status="FOUND",
    alt_items=None,
    comm_level="POSSIBLE",
    comm_reason="Ссылка на собственное сообщество",
    verdict_cat="LEAD_GENERATION",
    verdict_assess="POTENTIALLY_USEFUL",
    verdict_sum="Полезный обзор инструмента, но основной целью является привлечение подписчиков."
) -> dict:
    if promises is None:
        promises = [
            {
                "claim": "Сэкономит 80% времени при рутинных задачах",
                "target_audience": "Разработчики",
                "expected_result": "Экономия времени",
                "timeframe": "сразу",
                "conditions": "при правильной настройке",
                "has_concrete_metrics": True,
                "evidence_status": "VERIFIED"
            }
        ]
    if missing_econ is None:
        missing_econ = [
            {
                "item": "API ключи OpenAI",
                "status": "NOT_STATED",
                "description": "Автор не упомянул необходимость платной подписки на API"
            }
        ]
    if alt_items is None and alt_status == "FOUND":
        alt_items = ["Открытый скрипт на Python", "Бесплатный аналог в GitHub"]
    elif alt_items is None:
        alt_items = []

    return {
        "offer": {
            "type": offer_type,
            "description": offer_desc
        },
        "monetization_hypothesis": {
            "type": monetization_type,
            "reason": monetization_reason
        },
        "cta": {
            "detected": cta_detected,
            "type": cta_type,
            "destination": cta_dest,
            "action_prompt": cta_prompt
        },
        "promises": promises,
        "missing_economics": missing_econ,
        "reproducibility": {
            "level": repro_level,
            "reason": repro_reason
        },
        "alternatives": {
            "status": alt_status,
            "items": alt_items
        },
        "commercial_interest": {
            "level": comm_level,
            "reason": comm_reason
        },
        "verdict": {
            "category": verdict_cat,
            "assessment": verdict_assess,
            "summary": verdict_sum
        }
    }


def _mock_call_gemini(return_dict):
    """Returns a patch context manager for call_gemini_api."""
    mock_resp = {
        "candidates": [{
            "content": {
                "parts": [{
                    "text": json.dumps(return_dict)
                }]
            }
        }]
    }
    return patch("app.worker.business_check.call_gemini_api", return_value=mock_resp)


@pytest.mark.asyncio
async def test_scenario_1_no_cta():
    """1. Reel без CTA."""
    data = _make_mock_bc_result(
        cta_detected=False,
        cta_type="none",
        cta_dest="none",
        cta_prompt=None,
        comm_level="NONE_DETECTED",
        comm_reason="Призывов и коммерческих ссылок не обнаружено",
        verdict_cat="EDUCATIONAL",
        verdict_assess="GOOD_VALUE"
    )
    with _mock_call_gemini(data):
        res = await run_business_check("Обзор функции Python без ссылок")
        assert res.cta.detected is False
        assert res.cta.type == "none"
        assert res.commercial_interest.level == "NONE_DETECTED"
        assert res.verdict.category == "EDUCATIONAL"


@pytest.mark.asyncio
async def test_scenario_2_telegram_cta():
    """2. Reel с Telegram CTA."""
    data = _make_mock_bc_result(
        cta_detected=True,
        cta_type="telegram",
        cta_dest="t.me/mychannel",
        cta_prompt="Забирай промпт в закрепленном посте канала",
        verdict_cat="AUDIENCE_GROWTH",
        verdict_assess="MARKETING_HEAVY"
    )
    with _mock_call_gemini(data):
        res = await run_business_check("Видео про промпты для AI, переходи в канал")
        assert res.cta.detected is True
        assert res.cta.type == "telegram"
        assert res.cta.destination == "t.me/mychannel"
        assert res.verdict.category == "AUDIENCE_GROWTH"


@pytest.mark.asyncio
async def test_scenario_3_product_sale():
    """3. Reel с продажей продукта."""
    data = _make_mock_bc_result(
        offer_type="software",
        offer_desc="Платная утилита для обработки видео",
        monetization_type="product_sales",
        monetization_reason="Прямая продажа лицензии на софт",
        cta_type="purchase",
        cta_dest="buy.example.com",
        comm_level="CLEAR",
        comm_reason="Автор является разработчиком и продает продукт",
        verdict_cat="PRODUCT_PROMOTION",
        verdict_assess="POTENTIALLY_USEFUL"
    )
    with _mock_call_gemini(data):
        res = await run_business_check("Показываю как мой софт монтирует видео за 10 сек")
        assert res.offer.type == "software"
        assert res.monetization_hypothesis.type == "product_sales"
        assert res.commercial_interest.level == "CLEAR"
        assert res.verdict.category == "PRODUCT_PROMOTION"


@pytest.mark.asyncio
async def test_scenario_4_income_promise():
    """4. Reel с обещанием заработка."""
    promises = [
        {
            "claim": "Заработаешь $5,000 за первую неделю на арбитраже",
            "target_audience": "Новички",
            "expected_result": "$5,000",
            "timeframe": "1 неделя",
            "conditions": "без вложений",
            "has_concrete_metrics": True,
            "evidence_status": "UNVERIFIED"
        }
    ]
    data = _make_mock_bc_result(
        promises=promises,
        verdict_assess="MARKETING_HEAVY",
        verdict_sum="Агрессивный маркетинг с нереалистичными обещаниями доходности."
    )
    with _mock_call_gemini(data):
        res = await run_business_check("Секретная схема: как делать $5k в неделю")
        assert len(res.promises) == 1
        assert res.promises[0].has_concrete_metrics is True
        assert "$5,000" in res.promises[0].claim
        assert res.promises[0].evidence_status == "UNVERIFIED"


@pytest.mark.asyncio
async def test_scenario_5_concrete_metrics():
    """5. Reel с конкретной цифрой."""
    promises = [
        {
            "claim": "Ускоряет сборку проекта на 300% за 3 простых шага",
            "target_audience": "DevOps инженеры",
            "expected_result": "Ускорение на 300%",
            "timeframe": "сразу",
            "conditions": "использование Docker cache",
            "has_concrete_metrics": True,
            "evidence_status": "VERIFIED"
        }
    ]
    data = _make_mock_bc_result(promises=promises)
    with _mock_call_gemini(data):
        res = await run_business_check("3 шага для ускорения Docker на 300%")
        assert res.promises[0].has_concrete_metrics is True
        assert "300%" in res.promises[0].claim


@pytest.mark.asyncio
async def test_scenario_6_unknown_commercial_interest():
    """6. Reel, где коммерческий интерес неизвестен."""
    data = _make_mock_bc_result(
        comm_level="UNKNOWN",
        comm_reason="Недостаточно данных для определения аффилированности автора с проектом",
        verdict_cat="UNCLEAR",
        verdict_assess="INSUFFICIENT_EVIDENCE"
    )
    with _mock_call_gemini(data):
        res = await run_business_check("Упоминание нескольких опенсорс проектов без явных ссылок")
        assert res.commercial_interest.level == "UNKNOWN"
        assert res.verdict.category == "UNCLEAR"


@pytest.mark.asyncio
async def test_scenario_7_not_enough_data():
    """7. Reel с недостатком данных."""
    data = _make_mock_bc_result(
        alt_status="NOT_ENOUGH_DATA",
        alt_items=[],
        verdict_assess="NOT_ENOUGH_DATA",
        verdict_sum="Короткий фрагмент текста без деталей."
    )
    with _mock_call_gemini(data):
        res = await run_business_check("Очень короткое видео без контекста")
        assert res.alternatives.status == "NOT_ENOUGH_DATA"
        assert len(res.alternatives.items) == 0
        assert res.verdict.assessment == "NOT_ENOUGH_DATA"


@pytest.mark.asyncio
async def test_scenario_8_business_check_isolation():
    """8. Проверка, что Business Check не меняет существующий Fact Check."""
    c1 = Claim(statement="Python 3.12 вышел в 2023 году", claim_type="fact", status="подтверждено")
    c2 = Claim(statement="Python самый быстрый язык в мире", claim_type="opinion", status="пропущено")

    factcheck_analysis = VideoAnalysis(
        claims=[c1, c2],
        viable_idea=True,
        task_description="Изучить паттерны match/case в Python"
    )

    bc_data = _make_mock_bc_result()
    with _mock_call_gemini(bc_data):
        bc_res = await run_business_check(
            transcript="Видео про функции Python",
            claims=[c1, c2],
            factcheck_analysis=factcheck_analysis
        )
        factcheck_analysis.business_check = bc_res

        # Verify factcheck fields are completely preserved
        assert len(factcheck_analysis.claims) == 2
        assert factcheck_analysis.claims[0].statement == "Python 3.12 вышел в 2023 году"
        assert factcheck_analysis.claims[0].status == "подтверждено"
        assert factcheck_analysis.claims[1].status == "пропущено"
        assert factcheck_analysis.viable_idea is True
        assert factcheck_analysis.task_description == "Изучить паттерны match/case в Python"

        # Verify business_check was attached cleanly
        assert factcheck_analysis.business_check is not None
        assert factcheck_analysis.business_check.offer.type == "tool"


@pytest.mark.asyncio
async def test_scenario_markdown_formatter():
    """Verify Markdown formatting function produces required sections."""
    bc_dict = _make_mock_bc_result()
    bc = BusinessCheckResult(**bc_dict)
    md = format_business_check_markdown(bc)

    assert "Бизнес-анализ механики (Business Check)" in md
    assert "Предложение (Offer):" in md
    assert "Модель монетизации (гипотеза):" in md
    assert "Призыв к действию (CTA):" in md
    assert "Воспроизводимость:" in md
    assert "Бизнес-вердикт:" in md

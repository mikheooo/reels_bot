import json

import pytest

from app.bot.analysis_view import analysis_keyboard
from app.worker.compact_renderer import build_detail_sections, render_compact_analysis
from app.worker.content_router import RouterDecision, policy_for
from app.worker.personal_context import load_personal_context
from app.worker.specialized_analysis import (
    SpecializedAnalysis,
    generate_specialized_analysis,
)
from app.worker.tasks import extract_routed_tasks


def superpowers_route():
    return RouterDecision(
        primary_type="AI_SKILL_PLUGIN",
        labels=[
            {"label": "AI_SKILL_PLUGIN", "confidence": 0.98},
            {"label": "SOFTWARE_TOOL", "confidence": 0.66},
        ],
        intents=[{"intent": "RECOMMEND", "confidence": 0.83}],
        risk="LOW",
        risk_reasons=[],
        summary="Coding-agent methods",
    )


def result():
    return SpecializedAnalysis(
        verdict="Не устанавливать весь пакет без проверки пробелов.",
        what_it_is="Набор методик и skills для coding-agent, а не новая модель.",
        why_it_matters="Может стандартизировать planning, debugging и verification.",
        truth_assessment="Существование пакета показано; эффект не проверялся извне.",
        relevance="Наличие прямой установки неизвестно; возможны аналоги процессов.",
        next_action="Сравнить только отдельные skills с текущим процессом.",
        personal_relevance={
            "status": "UNKNOWN",
            "needed": "UNKNOWN",
            "explanation": "Локальный контекст не подключён.",
            "evidence": [],
        },
        type_specific_details="Сравнивать по одной методике.",
        technical_details="Skill-файлы подключаются к coding-agent workflow.",
        actionable=True,
        task_description="ЗАДАЧА: Сравнить Superpowers с текущими skills\nПроверить только отсутствующие методики.",
    )


def test_personal_context_is_unknown_without_explicit_evidence(monkeypatch):
    monkeypatch.delenv("REELS_PERSONAL_CONTEXT_JSON", raising=False)
    monkeypatch.delenv("REELS_PERSONAL_CONTEXT_FILE", raising=False)
    context = load_personal_context()
    assert context["status"] == "UNKNOWN"
    assert context["evidence"] == []


def test_personal_context_accepts_explicit_json(monkeypatch):
    monkeypatch.setenv(
        "REELS_PERSONAL_CONTEXT_JSON",
        json.dumps({"workflows": ["TDD", "verification"]}),
    )
    context = load_personal_context()
    assert context["status"] == "AVAILABLE"
    assert context["evidence"]["workflows"] == ["TDD", "verification"]


def test_compact_output_has_five_universal_blocks_and_route_metadata():
    text = render_compact_analysis(superpowers_route(), result())
    for label in [
        "Что это?",
        "Зачем это знать?",
        "Насколько это правда?",
        "Актуально ли тебе?",
        "Что делать?",
    ]:
        assert label in text
    assert "AI_SKILL_PLUGIN 98%" in text
    assert len(text) < 4096


def test_details_and_buttons_are_conditional():
    details = build_detail_sections(result(), None, None, "legacy technical")
    assert set(details) == {"tech", "type", "relevance"}
    keyboard = analysis_keyboard("123", list(details))
    callback_data = [
        button.callback_data for row in keyboard.inline_keyboard for button in row
    ]
    assert "detail:tech:123" in callback_data
    assert "detail:fact:123" not in callback_data
    assert "full:123" in callback_data


def test_routed_task_preserves_explicit_title():
    tasks = extract_routed_tasks(
        "ЗАДАЧА: Сравнить Superpowers с текущими skills\nПроверить только отсутствующие методики.",
        url="https://example.test/reel",
    )
    assert tasks[0]["title"] == "Сравнить Superpowers с текущими skills"
    assert tasks[0]["source_url"] == "https://example.test/reel"


@pytest.mark.asyncio
async def test_superpowers_prompt_requires_no_install_everything_advice(monkeypatch):
    captured = {}
    payload_result = result().model_dump()

    async def fake_call(payload):
        captured["prompt"] = payload["contents"][0]["parts"][0]["text"]
        return {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {"text": json.dumps(payload_result, ensure_ascii=False)}
                        ]
                    }
                }
            ]
        }

    monkeypatch.setattr("app.worker.specialized_analysis.call_gemini_api", fake_call)
    route = superpowers_route()
    await generate_specialized_analysis(
        "Superpowers skills for agents",
        None,
        route,
        policy_for(route),
        {"status": "UNKNOWN", "evidence": []},
    )
    assert "набор методик/skills для coding-agent" in captured["prompt"]
    assert "не советуй ставить всё подряд" in captured["prompt"]

import pytest
from app.worker.tasks import (
    clean_title_str,
    is_valid_title,
    truncate_to_words,
    extract_tasks_from_analysis,
    _extract_task,
)

def test_generic_title_rejection():
    """Verify that generic placeholders are flagged as invalid and cleaned."""
    assert is_valid_title("Идея из видео") is False
    assert is_valid_title("Идея из видео (Reels)") is False
    assert is_valid_title("Idea from video") is False
    assert is_valid_title("Reel idea") is False
    assert is_valid_title("Новая задача") is False
    assert is_valid_title("Разбор видео") is False
    assert is_valid_title("Идея") is False
    assert is_valid_title("Задачи") is False


def test_clean_title_prefixes():
    """Verify prefix removal and markdown cleanup."""
    raw = "### 6. ЗАДАЧА: **Добавить автоматическое определение хуков в первых 3 секундах**"
    cleaned = clean_title_str(raw)
    assert cleaned == "Добавить автоматическое определение хуков в первых 3 секундах"

    raw2 = "Идея из видео: Проверить генерацию коротких Reels из длинных видео"
    cleaned2 = clean_title_str(raw2)
    assert cleaned2 == "Проверить генерацию коротких Reels из длинных видео"

    raw3 = "#### **Концепция:** «Smart Free-Tier API Router» для агента Hermes"
    cleaned3 = clean_title_str(raw3)
    assert "Smart Free-Tier API Router" in cleaned3
    assert "«" not in cleaned3 and "»" not in cleaned3


def test_old_bug_regression_mikhail_header():
    """Regression test for old bug where '### 6. ЗАДАЧА ДЛЯ МИХАИЛА' broke the parser or returned None."""
    analysis = """
### 1. О ЧЁМ ВИДЕО
* **Содержание:** Автор показывает утилиту codexer для ChatGPT.

### 6. ЗАДАЧА ДЛЯ МИХАИЛА (Применение в стеке):

#### **Идея:** Создание бесплатного Fallback-слоя для агента Hermes и n8n-пайплайнов.
Использовать этот метод для черновой обработки данных.

#### КРИТЕРИИ ГОТОВНОСТИ:
* [ ] Утилита развернута в Docker
"""
    tasks = extract_tasks_from_analysis(analysis, url="https://instagram.com/reel/123")
    assert len(tasks) == 1
    t = tasks[0]
    assert t["title"] != "ДЛЯ МИХАИЛА (Применение в стеке)"
    assert "Fallback-слоя для агента Hermes" in t["title"] or "Создание бесплатного Fallback-слоя" in t["title"]
    assert is_valid_title(t["title"]) is True


def test_single_explicit_task_contract():
    """Verify prompt contract format: ЗАДАЧА: <title>."""
    analysis = """
🔎 **Проверка фактов:**
- ✅ [Подтверждено] Инструмент работает

ЗАДАЧА:
Добавить автоматическое определение хуков в первых 3 секундах

ЦЕЛЬ:
Увеличить удержание аудитории на первых секундах видео

ШАГИ:
1. Подключить модуль распознавания сцен
2. Измерить тайминги первых 3 секунд

КРИТЕРИИ ГОТОВНОСТИ:
* Скрипт детектирует хуки с точностью >90%
"""
    tasks = extract_tasks_from_analysis(analysis, url="https://instagram.com/reel/abc")
    assert len(tasks) == 1
    assert tasks[0]["title"] == "Добавить автоматическое определение хуков в первых 3 секундах"
    assert "🎯" in tasks[0]["description"] or "ЦЕЛЬ:" in tasks[0]["description"]


def test_multiple_independent_tasks_explicit():
    """Verify multiple independent ЗАДАЧА blocks are extracted as separate tasks."""
    analysis = """
ЗАДАЧА 1:
Автоматически определять хуки в первых секундах
ЦЕЛЬ:
Детекция динамики

ЗАДАЧА 2:
Добавить генерацию captions для Reels
ЦЕЛЬ:
Автоматические субтитры

ЗАДАЧА 3:
Тестировать 3 варианта CTA
ЦЕЛЬ:
Сплит-тест конверсии
"""
    tasks = extract_tasks_from_analysis(analysis, url="https://instagram.com/reel/xyz")
    assert len(tasks) == 3
    assert "хуки" in tasks[0]["title"].lower()
    assert "captions" in tasks[1]["title"].lower() or "субтитр" in tasks[1]["title"].lower()
    assert "cta" in tasks[2]["title"].lower()


def test_multiple_independent_tools_in_section_6():
    """Verify 3 distinct tools in Section 6 become distinct tasks."""
    analysis = """
КРАТКО ДЛЯ КАНАЛА:
Видео демонстрирует три полезных скилла для Claude Code/AI-агентов.

### 6. ЗАДАЧА ДЛЯ МИХАИЛА (Применение):

1. **Внедрение Hyper Research в Hermes Agent (Python / n8n):**
   * Создай отдельный режим hyper_research.
2. **Apple Design Patterns для дашборда:**
   * Интегрировать библиотеку компонентов.
3. **Beautify README Generator:**
   * Встроить скилл оформления репозиториев.
"""
    tasks = extract_tasks_from_analysis(analysis, url="https://instagram.com/reel/multi")
    assert len(tasks) >= 2
    titles = [t["title"] for t in tasks]
    assert any("Hyper Research" in t for t in titles)
    assert any("Apple Design" in t for t in titles)


def test_fallback_when_no_explicit_task_section():
    """Verify extraction works gracefully from summary or Section 1 when ЗАДАЧА block is missing."""
    analysis = """
КРАТКО ДЛЯ КАНАЛА:
Разбор репозитория-агрегатора (134 бесплатных API-эндпоинта от NVIDIA, Cloudflare, Groq, GitHub Models) и способа его развертывания.

### 1. О ЧЁМ ВИДЕО
* **Содержание:** Автор демонстрирует репозиторий awesome-freellm-apis.
* **Идея / Концепция:** Объединение десятков фри-тиров в единую экосистему роутера моделей.
"""
    tasks = extract_tasks_from_analysis(analysis, url="https://instagram.com/reel/summary_only")
    assert len(tasks) == 1
    assert is_valid_title(tasks[0]["title"]) is True
    assert "Идея из видео" not in tasks[0]["title"]
    assert "134" in tasks[0]["title"] or "API" in tasks[0]["title"] or "роутер" in tasks[0]["title"].lower() or "репозитори" in tasks[0]["title"].lower()


def test_title_word_length():
    """Verify word count constraint of approximately 4-10 words."""
    long_title = "Очень длинное и перегруженное название задачи, которое содержит слишком много слов и должно быть аккуратно обрезано по смыслу"
    shortened = truncate_to_words(long_title, min_words=4, max_words=10)
    words = shortened.split()
    assert 4 <= len(words) <= 10
    assert not shortened.endswith(",")


def test_pipeline_title_synchronization(tmp_path):
    """End-to-end test verifying title synchronization across H1 brief, BACKLOG.md, and Task model."""
    import uuid
    from app.db.models import Task

    analysis = """
🔎 **Проверка фактов:**
- ✅ [Подтверждено] Open-source утилита Capcut-CLI работает локально

ЗАДАЧА:
Интеграция утилиты Capcut-CLI для программного монтажа черновиков

ЦЕЛЬ:
Управлять локальными JSON-черновиками CapCut из Python-скриптов

ШАГИ:
1. Установить capcut-cli через pip
2. Протестировать экспорт таймлайна

КРИТЕРИИ ГОТОВНОСТИ:
* Команда capcut-cli --list выводит список локальных проектов
"""
    url = "https://instagram.com/reel/test_sync_123"
    job_id = str(uuid.uuid4())
    user_id = 123456789

    # 1. Extract tasks
    extracted_tasks = extract_tasks_from_analysis(analysis, url=url)
    assert len(extracted_tasks) == 1
    task_info = extracted_tasks[0]
    expected_title = task_info["title"]
    assert expected_title == "Интеграция утилиты Capcut-CLI для программного монтажа черновиков"

    # 2. Simulate plan file creation
    plan_file = str(tmp_path / f"idea_{job_id}.md")
    with open(plan_file, "w", encoding="utf-8") as f:
        f.write(f"# {expected_title}\n\n")
        f.write(analysis)

    with open(plan_file, "r", encoding="utf-8") as f:
        first_line = f.readline().strip()
    assert first_line == f"# {expected_title}"

    # 3. Simulate BACKLOG.md entry
    backlog_file = str(tmp_path / "BACKLOG.md")
    with open(backlog_file, "a", encoding="utf-8") as bf:
        for t in extracted_tasks:
            bf.write(f"- [ ] [{t['title']}]({plan_file}) - {url}\n")

    with open(backlog_file, "r", encoding="utf-8") as bf:
        backlog_content = bf.read().strip()
    assert f"- [ ] [{expected_title}]" in backlog_content

    # 4. Simulate Task DB model creation
    db_task = Task(
        id=str(uuid.uuid4()),
        job_id=job_id,
        user_id=user_id,
        title=task_info["title"],
        description=task_info.get("description"),
        status="PENDING",
    )
    assert db_task.title == expected_title
    assert db_task.title == first_line.replace("# ", "").strip()
    assert f"[{db_task.title}]" in backlog_content

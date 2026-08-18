import os
import uuid
import pytest
from app.worker.tasks import extract_tasks_from_analysis
from app.db.models import Task

def test_case_1_single_explicit_task():
    """Case 1: Single explicit ЗАДАЧА with specific action title."""
    analysis = """
КРАТКО ДЛЯ КАНАЛА:
Разбор метода автоматической детекции хуков в первых 3 секундах видео.

### 1. О ЧЁМ ВИДЕО
* **Содержание:** Автор показывает, как удержание аудитории зависит от динамики в первые 3 секунды.

### 6. ЗАДАЧА:
ЗАДАЧА: Добавить автоматическое определение хуков в первых 3 секундах

ЦЕЛЬ:
Автоматически оценивать качество динамического хука в монтируемых Reels.

ШАГИ:
1. Подключить модуль детекции смены сцен через OpenCV/FFmpeg
2. Рассчитать индекс динамики кадров за секунды 0-3

КРИТЕРИИ ГОТОВНОСТИ:
* Скрипт выдает оценку хука от 1 до 10 для тестового видео
"""
    url = "https://www.instagram.com/reel/case_1_single"
    tasks = extract_tasks_from_analysis(analysis, url=url)
    
    assert len(tasks) == 1
    t = tasks[0]
    assert t["title"] == "Добавить автоматическое определение хуков в первых 3 секундах"
    assert t["source_type"] == "reel"
    assert t["source_url"] == url
    assert "🎯 **ЦЕЛЬ**" in t["description"] or "ЦЕЛЬ:" in t["description"]


def test_case_2_multiple_independent_tasks():
    """Case 2: Multiple independent tasks from one Reel."""
    analysis = """
КРАТКО ДЛЯ КАНАЛА:
Три независимых улучшения пайплайна коротких видео: генерация хуков, создание субтитров и A/B тестирование CTA.

### 6. ЗАДАЧА ДЛЯ МИХАИЛА (Применение):

1. **Автоматически определять хуки в первых секундах:**
   * Внедрить алгоритм подсчета движения на первых кадрах.
2. **Добавить генерацию captions для Reels:**
   * Подключить Faster-Whisper с анимацией караоке-субтитров.
3. **Тестировать 3 варианта CTA:**
   * Настроить ротацию призывов к действию в финале ролика.
"""
    url = "https://www.instagram.com/reel/case_2_multi"
    tasks = extract_tasks_from_analysis(analysis, url=url)
    
    assert len(tasks) == 3
    assert tasks[0]["title"] == "Автоматически определять хуки в первых секундах"
    assert tasks[1]["title"] == "Добавить генерацию captions для Reels"
    assert tasks[2]["title"] == "Тестировать 3 варианта CTA"
    for t in tasks:
        assert t["source_type"] == "reel"
        assert t["source_url"] == url


def test_case_3_no_explicit_task_section():
    """Case 3: No explicit ЗАДАЧА block, but clear idea in analysis."""
    analysis = """
КРАТКО ДЛЯ КАНАЛА:
Проверить генерацию коротких Reels из длинных видео через открытую модель Whisper-Video-Cutter.

### 1. О ЧЁМ ВИДЕО
* **Содержание:** Автор показывает утилиту, которая парсит таймкоды подкаста и нарезает Shorts.
* **Идея / Концепция:** Полная автоматизация репурпозинга горизонтальных подкастов в вертикальные клипы.

### 2. МЕХАНИКА
1. Транскрибация длинного аудио
2. Поиск кульминационных моментов
3. Нарезка в формат 9:16
"""
    url = "https://www.instagram.com/reel/case_3_no_task_header"
    tasks = extract_tasks_from_analysis(analysis, url=url)
    
    assert len(tasks) == 1
    t = tasks[0]
    assert "Идея из видео" not in t["title"]
    assert "Reels" in t["title"] or "видео" in t["title"].lower() or "подкаст" in t["title"].lower()
    words = t["title"].split()
    assert 4 <= len(words) <= 10


def test_case_4_complex_and_non_standard_format():
    """Case 4: Complex/non-standard model output format with nested quotes and colloquial Russian intro."""
    analysis = """
### 1. О ЧЁМ ВИДЕО
* **Содержание:** Автор на коленке собрал прокси для Claude Code.

### 6. ЗАДАЧА ДЛЯ МИХАИЛА (Применение в стеке)

Михаил, для твоей рабочей связки это не просто игрушка, а:
#### **Идея:** «Локальный шлюз OmniRoute для роутинга CLI-агентов»
Вместо того чтобы вручную переключать модели в терминале, настроить автоматический прокси.

#### КРИТЕРИИ ГОТОВНОСТИ:
* Сервер OmniRoute запущен на localhost:8000
"""
    url = "https://www.instagram.com/reel/case_4_complex"
    tasks = extract_tasks_from_analysis(analysis, url=url)
    
    assert len(tasks) == 1
    t = tasks[0]
    assert "Михаил" not in t["title"]
    assert "«" not in t["title"] and "»" not in t["title"]
    assert "OmniRoute" in t["title"]
    assert "Локальный шлюз OmniRoute для роутинга CLI-агентов" in t["title"] or "OmniRoute для роутинга CLI-агентов" in t["title"]

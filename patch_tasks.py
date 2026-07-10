import re

with open("app/worker/tasks.py", "r", encoding="utf-8") as f:
    code = f.read()

new_analyze = """
async def analyze_video(file_path: str) -> str:
    keys = []
    for i in range(1, 10):
        k = os.getenv(f"GEMINI_API_KEY_{i}")
        if k: keys.append(k)
    if not keys:
        keys.append(settings.gemini_api_key)
        
    last_error = None
    for key in keys:
        genai.configure(api_key=key)
        logger.info(f"Trying Gemini API key ending in ...{key[-4:] if key else 'None'}")
        
        try:
            def _upload_and_analyze():
                return genai.upload_file(path=file_path)

            video_file = await asyncio.to_thread(_upload_and_analyze)
            
            while video_file.state.name == "PROCESSING":
                await asyncio.sleep(3)
                video_file = await asyncio.to_thread(genai.get_file, video_file.name)
                
            if video_file.state.name == "FAILED":
                raise Exception("Gemini video processing failed.")
                
            def _generate():
                # Возвращаем flash, так как у pro жесткий лимит 2 RPM
                model = genai.GenerativeModel(model_name="gemini-1.5-flash")
                prompt = \"\"\"Проанализируй это короткое видео подробно:

1. О чём оно? Опиши содержание (что показано, что говорят, какая идея/концепция)
2. Что за техника/трюк/решение демонстрируется?
3. Насколько это реально реализовать? Оцени от 1 до 10 с обоснованием
4. Раздели: факт vs вымысел/преувеличение

5. Оцени реализуемость с учётом УЖЕ СУЩЕСТВУЮЩЕЙ инфраструктуры:

ТЕКУЩИЙ СТЕК (уже работает):
- Hermes Agent: AI-агент с инструментами (terminal, web, file, code, cron, skills), Telegram Gateway
- n8n: локально на http://localhost:5678, webhook-автоматизация
- Python: скрипты, парсинг, API-интеграции
- Telegram-боты: aiogram (этот reels-бот), Telethon (автопостинг)
- Telegram-каналы: @hermesaigm (AI-новости, cron 21:00), @remotejobd (вакансии, cron 10:00), @savemyreels (этот канал)
- Agent Reach: парсинг Twitter/X, YouTube, веб-контента
- Контент-пайплайн: GDrive → Whisper (транскрипция) → LLM → Telegram + YouTube (unlisted)
- LLM-провайдеры: Gemini, Claude, GPT, GLM-5.2 (z.ai), OpenRouter, ZenMux, GonkaGate
- Google Workspace: Gmail, Drive, Calendar через gws CLI
- Cobalt API: скачивание видео (reels/tiktok/youtube)
- computer_use: управление рабочим столом (Windows), browser CDP
- Docker: Postgres + Redis + ARQ воркеры для очередей
- Windows 10 хост (Паттайя, удалённая работа)

Можно ли повторить идею из видео? Что УЖЕ есть, а что нужно добавить? Оцени сложность.

6. Сформируй конкретную задачу для Hermes Agent в формате:
   ЗАДАЧА: [краткое название]
   ЦЕЛЬ: [что должно получиться]
   ИСПОЛЬЗУЕТ: [какие существующие компоненты задействовать]
   ДОБАВИТЬ: [что нового нужно создать]
   ШАГИ: [пронумерованный план]
   КРИТЕРИИ ГОТОВНОСТИ: [как проверим что работает]
   
   Если реализация невозможна или нерациональна — напиши почему и предложи альтернативу.\"\"\"
                response = model.generate_content([video_file, prompt])
                return response.text
                
            result = await asyncio.to_thread(_generate)
            await asyncio.to_thread(genai.delete_file, video_file.name)
            return result
            
        except Exception as e:
            error_msg = str(e).lower()
            if "429" in error_msg or "quota" in error_msg or "exhausted" in error_msg:
                logger.warning(f"Key rate limited/exhausted. Switching to next. Error: {e}")
                last_error = e
                continue # переходим к следующему ключу
            else:
                raise e
                
    raise last_error or Exception("All API keys failed")
"""

# Найти старую функцию и заменить
pattern = re.compile(r'async def analyze_video\(file_path: str\) -> str:.*?return result', re.DOTALL)
new_code = pattern.sub(new_analyze.strip(), code)

with open("app/worker/tasks.py", "w", encoding="utf-8") as f:
    f.write(new_code)
print("tasks.py updated!")

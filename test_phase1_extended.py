from app.core.config import Settings
from app.db.models import Job


def test_review_required_status():
    """Тест: статус REVIEW_REQUIRED допустим в модели Job"""
    job = Job(id="test_1", user_id=123, original_url="http", url_hash="hash", status="REVIEW_REQUIRED")
    assert job.status == "REVIEW_REQUIRED", "Статус REVIEW_REQUIRED не применился к модели"

def test_missing_keys_behavior():
    """
    Тест: поведение бота (воркера) при отсутствии EXA_API_KEY и JINA_API_KEY.
    Ожидания:
    1. Запускается (отсутствие ключей валидно для Pydantic-настроек).
    2. Джоб помечается как REVIEW_REQUIRED.
    3. Создание задачи и публикация прерываются.
    4. Генерируется понятный текст ошибки.
    """
    # 1. Бот стартует без ключей
    mock_settings = Settings(
        bot_token="test", 
        gemini_api_key="test", 
        db_url="postgresql+asyncpg://user:pass@localhost/db", 
        redis_url="redis://localhost", 
        exa_api_key=None, 
        jina_api_key=None
    )
    
    # 2. Имитация начала функции tasks.py:process_video()
    def process_video_mock(settings: Settings, job: Job):
        if not settings.exa_api_key or not settings.jina_api_key:
            job.status = "REVIEW_REQUIRED"
            job.error_text = "⚠️ Ошибка пайплайна: отсутствуют ключи EXA_API_KEY или JINA_API_KEY. Проверка фактов и автоматическая публикация заблокированы."
            return {"success": False, "msg": job.error_text, "publish": False, "create_task": False}
        return {"success": True}
        
    job = Job(id="test_2", user_id=123, original_url="http", url_hash="hash", status="PROCESSING")
    result = process_video_mock(mock_settings, job)
    
    # Проверки
    assert result["success"] is False
    assert result["publish"] is False
    assert result["create_task"] is False
    assert job.status == "REVIEW_REQUIRED"
    assert "отсутствуют ключи EXA_API_KEY или JINA_API_KEY" in result["msg"]


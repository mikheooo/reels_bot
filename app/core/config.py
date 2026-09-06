from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    bot_token: str
    gemini_api_key: str
    db_url: str
    redis_url: str
    channel_chat_id: str | None = None
    expected_bot_username: str | None = None
    exa_api_key: str | None = None
    jina_api_key: str | None = None
    publish_threshold: float = 0.6
    deprioritize_threshold: float = 0.4
    prioritization_timeout_seconds: float = 120.0
    language_detection_timeout_seconds: float = 2.0
    # Was hardcoded as "gemini-3.7-flash" in tasks.py and factcheck.py.
    # Override via GEMINI_MODEL in .env to switch models without touching code.
    gemini_model: str = "gemini-3.7-flash"

    # Dedicated transcription configuration (never reuses GEMINI_MODEL).
    transcription_primary_model: str = "gemini-3.5-transcribe"
    transcription_fallback_model: str = "gemini-3.7-flash"
    transcription_primary_enabled: bool = True
    transcription_min_chars_per_min: int = 400

    # External Platform Connectors (X / Twitter)
    x_api_key: str | None = None
    x_api_secret: str | None = None
    x_access_token: str | None = None
    x_access_token_secret: str | None = None
    x_bearer_token: str | None = None
    x_client_id: str | None = None
    x_client_secret: str | None = None
    expected_x_user_id: str | None = None
    expected_x_username: str | None = None

    # External Platform Connectors (Threads)
    threads_access_token: str | None = None
    threads_user_id: str | None = None
    expected_threads_user_id: str | None = None

    # Publication Retry Orchestration
    publish_max_retries: int = 3
    publish_retry_backoff_seconds: list[int] = [30, 120, 300]


    class Config:
        env_file = ".env"
        extra = "allow"

settings = Settings()

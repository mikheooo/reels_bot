from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    bot_token: str
    gemini_api_key: str
    db_url: str
    redis_url: str

    class Config:
        env_file = ".env"

settings = Settings()

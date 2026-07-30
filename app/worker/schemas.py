from typing import Literal

from pydantic import BaseModel, Field


class SearchResult(BaseModel):
    url: str
    title: str | None
    domain: str
    source_type: Literal["official", "authoritative_secondary", "other"]
    published_date: str | None
    text_snippet: str
    retrieved_at: str

class Claim(BaseModel):
    statement: str = Field(description="Само утверждение из видео")
    claim_type: Literal["fact", "opinion"] = Field(description="fact - проверяемый факт; opinion - мнение, совет, оценка")
    status: Literal["подтверждено", "опровергнуто", "не проверено", "пропущено"] = Field(description="Вердикт проверки")
    
    checked_at: str | None = Field(default=None, description="Дата и время проверки (ISO)")
    source_name: str | None = Field(default=None, description="Название сайта/домена (источник)")
    source_url: str | None = Field(default=None, description="Прямая ссылка на источник, подтверждающая цитату")
    source_type: Literal["official", "authoritative_secondary", "other", "none"] = Field(default="none", description="Тип источника")
    exact_quote: str | None = Field(default=None, description="Точная цитата из источника. Обязательно должна сопровождаться source_url.")
    confidence_level: Literal["high", "medium", "low", "none"] = Field(default="none", description="Уровень уверенности")
    unverified_reason: str | None = Field(default=None, description="Причина, почему статус 'не проверено'")

class VideoAnalysis(BaseModel):
    claims: list[Claim] = Field(description="Список проверенных утверждений")
    viable_idea: bool = Field(description="True, если есть конкретная, выполнимая и практически полезная идея, основанная ТОЛЬКО НА ПОДТВЕРЖДЕННЫХ фактах.")
    task_description: str | None = Field(default=None, description="СУТЬ ИДЕИ (задача) для инженера")

class QAResult(BaseModel):
    approved: bool = Field(description="True, если все факты корректно подтверждены или опровергнуты со ссылками")
    reasons: list[str] | None = Field(default=None, description="Список ошибок, если approved=False")

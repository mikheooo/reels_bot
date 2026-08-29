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
    
    semantic_category: Literal[
        "ACTIONABLE_CLAIM",
        "PRODUCT_FEATURE",
        "NUMERIC_CLAIM",
        "PERFORMANCE_CLAIM",
        "FINANCIAL_CLAIM",
        "MARKETING_CLAIM",
        "OPINION",
        "VISUAL_DESCRIPTION",
        "AUDIO_DESCRIPTION",
        "CTA",
        "OTHER"
    ] = Field(default="ACTIONABLE_CLAIM", description="Смысловая категория утверждения")
    relevance_score: float = Field(default=1.0, ge=0.0, le=1.0, description="Смысловая ценность для разбора (0.0=шум, 1.0=суть)")
    nature: Literal[
        "SUPPORTED",
        "CONTRADICTED",
        "PARTIALLY_SUPPORTED",
        "UNVERIFIED_PUBLIC",
        "PRIVATE_CLAIM",
        "OPINION",
        "MARKETING_CLAIM",
        "NOT_FACTCHECKABLE"
    ] = Field(default="NOT_FACTCHECKABLE", description="Природа утверждения для корректного коммента")

    checked_at: str | None = Field(default=None, description="Дата и время проверки (ISO)")
    source_name: str | None = Field(default=None, description="Название сайта/домена (источник)")
    source_url: str | None = Field(default=None, description="Прямая ссылка на источник, подтверждающая цитату")
    source_type: Literal["official", "authoritative_secondary", "other", "none"] = Field(default="none", description="Тип источника")
    exact_quote: str | None = Field(default=None, description="Точная цитата из источника. Обязательно должна сопровождаться source_url.")
    confidence_level: Literal["high", "medium", "low", "none"] = Field(default="none", description="Уровень уверенности")
    unverified_reason: str | None = Field(default=None, description="Причина, почему статус 'не проверено'")
    source_start: float | None = Field(default=None, description="Начало фрагмента в исходном видео (в секундах)")
    source_end: float | None = Field(default=None, description="Конец фрагмента в исходном видео (в секундах)")
    source_quote: str | None = Field(default=None, description="Цитата или фраза из исходного видео")
    source_context: str | None = Field(default=None, description="Контекст исходного видео")


# --- BUSINESS CHECK SCHEMAS ---

class OfferInfo(BaseModel):
    type: Literal[
        "tool",
        "service",
        "course",
        "subscription",
        "community",
        "telegram_channel",
        "affiliate_product",
        "consulting",
        "software",
        "lead_magnet",
        "audience_growth",
        "other"
    ] = Field(description="Тип предложения автора")
    description: str = Field(description="Описание того, что автор фактически предлагает зрителю")


class MonetizationHypothesis(BaseModel):
    type: Literal[
        "product_sales",
        "subscription",
        "affiliate",
        "ads",
        "lead_generation",
        "paid_community",
        "consulting",
        "course_sale",
        "saas",
        "audience_growth",
        "unknown"
    ] = Field(description="Предполагаемая модель заработка автора")
    reason: str = Field(description="Обоснование гипотезы (это гипотеза, а не утверждение о намерениях)")


class CTAInfo(BaseModel):
    detected: bool = Field(description="Обнаружен ли призыв к действию (CTA)")
    type: Literal[
        "link_click",
        "telegram",
        "website",
        "promo_code",
        "registration",
        "purchase",
        "subscription",
        "download",
        "none",
        "other"
    ] = Field(description="Тип призыва к действию")
    destination: str = Field(default="available_from_source", description="Назначение ссылки/куда ведет CTA (без выдумывания URL)")
    action_prompt: str | None = Field(default=None, description="Что именно автор просит сделать")


class PromiseItem(BaseModel):
    claim: str = Field(description="Формулировка обещания")
    target_audience: str | None = Field(default=None, description="Кому адресовано")
    expected_result: str | None = Field(default=None, description="Какой результат обещан")
    timeframe: str | None = Field(default=None, description="За какой срок")
    conditions: str | None = Field(default=None, description="При каких условиях")
    has_concrete_metrics: bool = Field(default=False, description="Есть ли конкретные цифры ($X, X%, X дней, работает у всех, без навыков)")
    evidence_status: Literal["VERIFIED", "UNVERIFIED", "REFUTED", "NOT_APPLICABLE"] = Field(default="UNVERIFIED", description="Статус подтверждения обещания")


class MissingEconomicsItem(BaseModel):
    item: str = Field(description="Скрытая или неоговоренная статья расходов/ограничение (подписки, API, реклама, оборудование, время, аудитория, навыки и т.д.)")
    status: Literal["NOT_STATED", "REQUIRES_VERIFICATION", "PROBABLE_LIMITATION"] = Field(
        description="Статус: NOT_STATED ('не указано'), REQUIRES_VERIFICATION ('требует проверки'), PROBABLE_LIMITATION ('вероятное ограничение')"
    )
    description: str = Field(description="Пояснение, почему этот расход или ресурс необходим, но не упомянут")


class ReproducibilityInfo(BaseModel):
    level: Literal["HIGH", "MEDIUM", "LOW", "UNKNOWN"] = Field(description="Оценка воспроизводимости результата обычным зрителем")
    reason: str = Field(description="Объяснение, что потребуется обычному человеку для повторения результата")


class AlternativesInfo(BaseModel):
    status: Literal["FOUND", "NOT_ENOUGH_DATA"] = Field(description="Найдены ли альтернативы или недостаточно данных")
    items: list[str] = Field(default_factory=list, description="Альтернативные инструменты, бесплатные/дешевые варианты, более простые варианты или ручной способ. Пусто при NOT_ENOUGH_DATA.")


class CommercialInterestInfo(BaseModel):
    level: Literal["NONE_DETECTED", "POSSIBLE", "CLEAR", "UNKNOWN"] = Field(description="Уровень коммерческого интереса автора")
    reason: str = Field(description="Обоснование. Не делать вывод 'автор мошенник' только из-за ссылки или продажи.")


class BusinessVerdict(BaseModel):
    category: Literal[
        "EDUCATIONAL",
        "PRODUCT_PROMOTION",
        "LEAD_GENERATION",
        "AFFILIATE_PROMOTION",
        "AUDIENCE_GROWTH",
        "MIXED",
        "UNCLEAR"
    ] = Field(description="Категория бизнес-вердикта")
    assessment: Literal[
        "GOOD_VALUE",
        "POTENTIALLY_USEFUL",
        "MARKETING_HEAVY",
        "INSUFFICIENT_EVIDENCE",
        "NOT_ENOUGH_DATA"
    ] = Field(description="Общая оценка предложения для зрителя")
    summary: str = Field(description="Итоговый вывод: что реально полезно зрителю, что является маркетингом, какие условия скрыты и есть ли основания считать предложение выгодным")


class BusinessCheckResult(BaseModel):
    offer: OfferInfo
    monetization_hypothesis: MonetizationHypothesis
    cta: CTAInfo
    promises: list[PromiseItem] = Field(default_factory=list)
    missing_economics: list[MissingEconomicsItem] = Field(default_factory=list)
    reproducibility: ReproducibilityInfo
    alternatives: AlternativesInfo
    commercial_interest: CommercialInterestInfo
    verdict: BusinessVerdict


# --- MAIN VIDEO ANALYSIS SCHEMA ---

class VideoAnalysis(BaseModel):
    claims: list[Claim] = Field(description="Список проверенных утверждений")
    viable_idea: bool = Field(description="True, если есть конкретная, выполнимая и практически полезная идея, основанная ТОЛЬКО НА ПОДТВЕРЖДЕННЫХ фактах.")
    task_description: str | None = Field(default=None, description="СУТЬ ИДЕИ (задача) для инженера")
    business_check: BusinessCheckResult | None = Field(default=None, description="Результат бизнес-анализа (Business Check)")


class QAResult(BaseModel):
    approved: bool = Field(description="True, если все факты корректно подтверждены или опровергнуты со ссылками")
    reasons: list[str] | None = Field(default=None, description="Список ошибок, если approved=False")


class PriorityScore(BaseModel):
    importance: float = Field(ge=0.0, le=1.0, description="Важность темы для аудитории (0.0-1.0)")
    virality: float = Field(ge=0.0, le=1.0, description="Вирусный потенциал ролика (0.0-1.0)")
    novelty: float = Field(ge=0.0, le=1.0, description="Новизна информации (0.0-1.0)")
    views_potential: float = Field(ge=0.0, le=1.0, description="Вероятность просмотров (0.0-1.0)")
    audience_value: float = Field(ge=0.0, le=1.0, description="Ценность для аудитории (0.0-1.0)")
    overall: float = Field(description="Взвешенная сумма критериев")
    publish: bool = Field(description="True, если материал стоит публиковать (overall >= порога)")
    reasons: list[str] = Field(default_factory=list, description="Обоснование оценки")

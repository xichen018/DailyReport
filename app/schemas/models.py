from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TaskStatus(StrEnum):
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"


class Impact(StrEnum):
    POSITIVE = "positive"
    NEGATIVE = "negative"
    NEUTRAL = "neutral"


class CheckStatus(StrEnum):
    COMPLETED = "completed"
    NO_MATERIAL_FINDING = "no_material_finding"
    NOT_TRIGGERED = "not_triggered"
    DATA_UNAVAILABLE = "data_unavailable"


class Window(StrictModel):
    timezone: str
    start_at: datetime
    end_at: datetime

    @model_validator(mode="after")
    def ordered(self) -> "Window":
        if self.start_at >= self.end_at:
            raise ValueError("window start_at must precede end_at")
        return self


class MarketDates(StrictModel):
    calendar: str
    latest_trading_day: date
    previous_trading_day: date


class RunContext(StrictModel):
    schema_version: str = "1.0.0"
    run_id: str
    timezone: str
    scheduled_for: datetime
    window: Window
    market_dates: dict[str, MarketDates]


class Source(StrictModel):
    source_id: str
    provider: str
    publisher: str
    url: HttpUrl
    published_at: datetime | None = None
    retrieved_at: datetime


class PricePoint(StrictModel):
    kind: str
    value: Decimal = Field(gt=0)
    currency: str
    as_of: datetime
    previous_value: Decimal | None = Field(default=None, gt=0)
    change_value: Decimal | None = None
    change_pct: Decimal | None = None
    source_ids: list[str] = Field(min_length=1)


class NewsItem(StrictModel):
    headline: str = Field(min_length=1)
    published_at: datetime
    summary_zh: str = Field(min_length=1)
    impact: Impact
    rationale_zh: str = Field(min_length=1)
    source_ids: list[str] = Field(min_length=1)
    outside_window: bool = False


class InstrumentResult(StrictModel):
    instrument_id: str
    symbol: str
    name: str
    asset_class: str
    exchange: str
    currency: str
    trading_date: date | None = None
    prices: list[PricePoint] = Field(default_factory=list)
    news: list[NewsItem] = Field(default_factory=list)

    @field_validator("trading_date", mode="before")
    @classmethod
    def normalize_trading_date(cls, value: object) -> object:
        if isinstance(value, str) and "T" in value:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
        if isinstance(value, datetime):
            return value.date()
        return value


class MacroObservation(StrictModel):
    metric_id: str
    label: str
    value: Decimal | str
    unit: str
    period: str
    actual: Decimal | None = None
    consensus: Decimal | None = None
    prior: Decimal | None = None
    source_ids: list[str] = Field(min_length=1)


class RelativeObservation(StrictModel):
    label: str
    as_of: date
    numerator_value: Decimal
    denominator_value: Decimal
    ratio: Decimal

    @field_validator("as_of", mode="before")
    @classmethod
    def normalize_as_of_date(cls, value: object) -> object:
        if isinstance(value, str) and "T" in value:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
        if isinstance(value, datetime):
            return value.date()
        return value


class RelativeMetric(StrictModel):
    metric_id: str
    numerator: str
    denominator: str
    observations: list[RelativeObservation] = Field(min_length=1)
    interpretation_zh: str
    source_ids: list[str] = Field(min_length=1)


class NoMajorNews(StrictModel):
    instrument_id: str
    checked_at: datetime
    reason_zh: str


class ResearchCheck(StrictModel):
    check_id: str
    requirement_type: str
    scope_id: str
    requirement_zh: str
    status: CheckStatus
    conclusion_zh: str
    source_ids: list[str] = Field(default_factory=list)


class TaskWarning(StrictModel):
    code: str
    message_zh: str
    field_path: str | None = None


class TaskError(StrictModel):
    code: str
    stage: str
    message_zh: str
    retryable: bool


class ResearchTaskResult(StrictModel):
    schema_version: str = "1.0.0"
    run_id: str
    request_id: str
    task_id: str
    title_zh: str
    status: TaskStatus
    window: Window
    instruments: list[InstrumentResult] = Field(default_factory=list)
    section_news: list[NewsItem] = Field(default_factory=list)
    macro_observations: list[MacroObservation] = Field(default_factory=list)
    relative_metrics: list[RelativeMetric] = Field(default_factory=list)
    research_checks: list[ResearchCheck] = Field(default_factory=list)
    sources: list[Source] = Field(default_factory=list)
    no_major_news: list[NoMajorNews] = Field(default_factory=list)
    warnings: list[TaskWarning] = Field(default_factory=list)
    errors: list[TaskError] = Field(default_factory=list)

    @model_validator(mode="after")
    def failure_has_error(self) -> "ResearchTaskResult":
        if self.status == TaskStatus.FAILED and not self.errors:
            raise ValueError("failed task must contain at least one error")
        return self

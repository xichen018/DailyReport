from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Protocol

from app.modules.loader import ModuleConfig


class MarketDataProvider(Protocol):
    def get_task_data(self, module: ModuleConfig, as_of: datetime) -> dict[str, Any]: ...


class NewsProvider(Protocol):
    def get_task_data(self, module: ModuleConfig, start_at: datetime, end_at: datetime) -> dict[str, Any]: ...


class MacroDataProvider(Protocol):
    def get_task_data(self, module: ModuleConfig, start_at: datetime, end_at: datetime) -> dict[str, Any]: ...


class TradingCalendarProvider(Protocol):
    def latest_closed_session(self, calendar: str, as_of: datetime) -> date: ...
    def previous_session(self, calendar: str, session: date) -> date: ...


@dataclass(frozen=True)
class ProviderBundle:
    market: MarketDataProvider
    news: NewsProvider
    macro: MacroDataProvider
    calendar: TradingCalendarProvider


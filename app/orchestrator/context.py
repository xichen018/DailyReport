from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.providers.base import TradingCalendarProvider
from app.schemas.models import MarketDates, RunContext, Window


HKT = ZoneInfo("Asia/Hong_Kong")


def build_run_context(
    calendar: TradingCalendarProvider,
    scheduled_for: datetime | None = None,
    window_hours: int = 30,
) -> RunContext:
    now = scheduled_for or datetime.now(HKT)
    if now.tzinfo is None:
        now = now.replace(tzinfo=HKT)
    else:
        now = now.astimezone(HKT)
    run_id = now.strftime("%Y%m%dT%H%M%S%z")
    market_dates: dict[str, MarketDates] = {}
    for calendar_name in ("HKEX", "US", "NYMEX"):
        latest = calendar.latest_closed_session(calendar_name, now)
        market_dates[calendar_name] = MarketDates(
            calendar=calendar_name,
            latest_trading_day=latest,
            previous_trading_day=calendar.previous_session(calendar_name, latest),
        )
    return RunContext(
        run_id=run_id,
        timezone="Asia/Hong_Kong",
        scheduled_for=now,
        window=Window(
            timezone="Asia/Hong_Kong",
            start_at=now - timedelta(hours=window_hours),
            end_at=now,
        ),
        market_dates=market_dates,
    )


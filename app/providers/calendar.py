from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo


MARKET_RULES = {
    "HKEX": (ZoneInfo("Asia/Hong_Kong"), time(16, 0)),
    "US": (ZoneInfo("America/New_York"), time(16, 0)),
    "NYMEX": (ZoneInfo("America/New_York"), time(17, 0)),
}


class SimpleTradingCalendar:
    """Deterministic phase-two calendar with injectable holidays.

    Production replaces this provider with an authoritative exchange calendar.
    """

    def __init__(self, holidays: dict[str, set[date]] | None = None) -> None:
        self.holidays = holidays or {}

    def _is_session(self, calendar: str, candidate: date) -> bool:
        return candidate.weekday() < 5 and candidate not in self.holidays.get(calendar, set())

    def _at_or_before_session(self, calendar: str, candidate: date) -> date:
        while not self._is_session(calendar, candidate):
            candidate -= timedelta(days=1)
        return candidate

    def latest_closed_session(self, calendar: str, as_of: datetime) -> date:
        timezone, close_time = MARKET_RULES[calendar]
        local = as_of.astimezone(timezone)
        candidate = local.date()
        if local.time() < close_time:
            candidate -= timedelta(days=1)
        return self._at_or_before_session(calendar, candidate)

    def previous_session(self, calendar: str, session: date) -> date:
        return self._at_or_before_session(calendar, session - timedelta(days=1))


from __future__ import annotations

import unittest
from datetime import date, datetime
from zoneinfo import ZoneInfo

from app.orchestrator.context import build_run_context
from app.providers.calendar import SimpleTradingCalendar


class RunContextTests(unittest.TestCase):
    def test_hkt_monday_morning_uses_previous_closed_sessions(self) -> None:
        context = build_run_context(
            SimpleTradingCalendar(),
            scheduled_for=datetime(2026, 8, 17, 8, 15, tzinfo=ZoneInfo("Asia/Hong_Kong")),
        )
        self.assertEqual(context.window.start_at.isoformat(), "2026-08-16T02:15:00+08:00")
        self.assertEqual(context.market_dates["HKEX"].latest_trading_day, date(2026, 8, 14))
        self.assertEqual(context.market_dates["US"].latest_trading_day, date(2026, 8, 14))

    def test_injected_holiday_is_skipped(self) -> None:
        calendar = SimpleTradingCalendar({"HKEX": {date(2026, 8, 17)}})
        context = build_run_context(
            calendar,
            scheduled_for=datetime(2026, 8, 18, 8, 15, tzinfo=ZoneInfo("Asia/Hong_Kong")),
        )
        self.assertEqual(context.market_dates["HKEX"].latest_trading_day, date(2026, 8, 14))


if __name__ == "__main__":
    unittest.main()


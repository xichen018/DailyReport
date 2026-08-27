from __future__ import annotations

import json
import os
import unittest
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from app.integrations.secrets import load_secrets
from app.integrations.openai_responses import _gateway_compatible_strict_schema
from app.modules.loader import load_module_configs
from app.providers.http import FredMacroDataProvider, FreeMarketDataProvider, FreeNewsProvider, _iso_published_at
from app.settings import Settings
from app.schemas.models import InstrumentResult


ROOT = Path(__file__).resolve().parents[1]


class RealIntegrationContractTests(unittest.TestCase):
    def test_model_datetime_is_normalized_for_trading_date(self) -> None:
        result = InstrumentResult.model_validate(
            {
                "instrument_id": "bitcoin_binance",
                "symbol": "BTCUSDT",
                "name": "Bitcoin / Tether",
                "asset_class": "crypto",
                "exchange": "BINANCE",
                "currency": "USDT",
                "trading_date": "2026-08-18T23:39:27+08:00",
                "prices": [],
                "news": [],
            }
        )
        self.assertEqual(result.trading_date.isoformat(), "2026-08-18")

    def test_rss_datetime_is_normalized_to_iso_8601(self) -> None:
        self.assertEqual(
            _iso_published_at("Tue, 18 Aug 2026 11:40:48 GMT"),
            "2026-08-18T11:40:48+00:00",
        )

    def test_gdelt_datetime_is_normalized_to_iso_8601(self) -> None:
        self.assertEqual(
            _iso_published_at("20260818T114048Z"),
            "2026-08-18T11:40:48+00:00",
        )

    def test_gateway_schema_is_strict_without_unsupported_formats(self) -> None:
        schema = _gateway_compatible_strict_schema()
        serialized = json.dumps(schema)
        for keyword in ("format", "pattern", "minLength", "maxLength", "minItems", "exclusiveMinimum", "default"):
            self.assertNotIn(f'"{keyword}"', serialized)
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(set(schema["required"]), set(schema["properties"]))

    def test_settings_support_compatible_base_url_without_storing_key(self) -> None:
        with patch.dict(os.environ, {"OPENAI_BASE_URL": "https://gateway.example/v1", "OPENAI_API_KEY": "test-secret"}, clear=True):
            settings = Settings.from_env("real")
            secrets = load_secrets(settings)
        self.assertEqual(settings.openai_base_url, "https://gateway.example/v1")
        self.assertEqual(secrets["openai_api_key"], "test-secret")
        self.assertNotIn("test-secret", repr(settings))

    def test_binance_response_is_normalized(self) -> None:
        payload = json.dumps({"lastPrice": "65000", "prevClosePrice": "64000", "priceChangePercent": "1.5625"}).encode()
        with patch("app.providers.http._get", return_value=payload):
            record = FreeMarketDataProvider()._binance()[0]
        self.assertEqual(record["symbol"], "BTCUSDT")
        self.assertEqual(record["provider"], "binance")
        self.assertEqual(record["value"], "65000")

    def test_sec_companyfacts_produces_comparable_quarterly_fundamentals(self) -> None:
        def fact(entries: list[dict[str, object]]) -> dict[str, object]:
            return {"units": {"USD": entries}}

        prior = {"start": "2024-04-01", "end": "2024-06-30", "filed": "2024-08-01", "form": "10-Q", "frame": "CY2024Q2", "accn": "0001-24-000001"}
        latest = {"start": "2025-04-01", "end": "2025-06-30", "filed": "2025-08-01", "form": "10-Q", "frame": "CY2025Q2", "accn": "0001-25-000001"}
        payload = {
            "facts": {"us-gaap": {
                "RevenueFromContractWithCustomerExcludingAssessedTax": fact([{**prior, "val": 800}, {**latest, "val": 1000}]),
                "OperatingIncomeLoss": fact([{**prior, "val": 120}, {**latest, "val": 200}]),
                "NetCashProvidedByUsedInOperatingActivities": fact([{**prior, "val": 180}, {**latest, "val": 250}]),
                "PaymentsToAcquirePropertyPlantAndEquipment": fact([{**prior, "val": 80}, {**latest, "val": 100}]),
                "Assets": fact([
                    {"end": "2025-06-30", "filed": "2025-08-01", "form": "10-Q", "frame": "CY2025Q2I", "accn": "0001-25-000001", "val": 5000},
                ]),
            }}
        }

        with patch("app.providers.http._get", return_value=json.dumps(payload).encode()):
            observations = FreeMarketDataProvider()._sec_fundamentals(
                "alphabet_c", "GOOG", datetime(2025, 8, 10, tzinfo=timezone.utc)
            )

        by_id = {item["metric_id"]: item for item in observations}
        self.assertEqual(by_id["sec_revenue"]["change_pct"], "25.00")
        self.assertEqual(by_id["sec_revenue"]["period_end"], "2025-06-30")
        self.assertEqual(by_id["sec_revenue"]["form"], "10-Q")
        self.assertEqual(by_id["sec_operating_margin"]["value"], "20.00")
        self.assertEqual(by_id["sec_free_cash_flow"]["value"], "150")
        self.assertEqual(by_id["sec_assets"]["value"], "5000")
        self.assertTrue(by_id["sec_revenue"]["source_url"].startswith("https://data.sec.gov/"))

    def test_sec_companyfacts_rejects_cumulative_and_future_facts(self) -> None:
        units = {"USD": [
            {"start": "2025-01-01", "end": "2025-06-30", "filed": "2025-08-01", "form": "10-Q", "frame": "CY2025Q2", "val": 200},
            {"start": "2025-04-01", "end": "2025-06-30", "filed": "2025-09-01", "form": "10-Q", "frame": "CY2025Q2", "val": 100},
        ]}

        entries = FreeMarketDataProvider._sec_quarterly_entries(
            units, instant=False, as_of=datetime(2025, 8, 10, tzinfo=timezone.utc)
        )

        self.assertEqual(entries, [])

    def test_sec_companyfacts_merges_tags_when_reporting_tag_changes(self) -> None:
        def fact(entries: list[dict[str, object]]) -> dict[str, object]:
            return {"units": {"USD": entries}}

        old = {"start": "2024-04-01", "end": "2024-06-30", "filed": "2024-08-01", "form": "10-Q", "frame": "CY2024Q2", "accn": "old", "val": 800}
        new = {"start": "2025-04-01", "end": "2025-06-30", "filed": "2025-08-01", "form": "10-Q", "frame": "CY2025Q2", "accn": "new", "val": 1000}
        payload = {"facts": {"us-gaap": {
            "RevenueFromContractWithCustomerExcludingAssessedTax": fact([old]),
            "Revenues": fact([new]),
        }}}

        with patch("app.providers.http._get", return_value=json.dumps(payload).encode()):
            observations = FreeMarketDataProvider()._sec_fundamentals(
                "alphabet_c", "GOOG", datetime(2025, 8, 10, tzinfo=timezone.utc)
            )

        revenue = next(item for item in observations if item["metric_id"] == "sec_revenue")
        self.assertEqual(revenue["period_end"], "2025-06-30")
        self.assertEqual(revenue["tag"], "Revenues")
        self.assertEqual(revenue["change_pct"], "25.00")

    def test_sec_companyfacts_accepts_unframed_fiscal_quarter(self) -> None:
        units = {"USD": [{
            "start": "2025-02-01", "end": "2025-04-30", "filed": "2025-06-01",
            "form": "10-Q", "fy": 2026, "fp": "Q1", "accn": "fiscal-q1", "val": 500,
        }]}

        entries = FreeMarketDataProvider._sec_quarterly_entries(
            units, instant=False, as_of=datetime(2025, 6, 10, tzinfo=timezone.utc)
        )

        self.assertEqual(entries[0]["frame"], "FY2026Q1")
        self.assertEqual(entries[0]["period_basis"], "reported_quarter_fiscal")

    def test_sec_companyfacts_derives_matching_q2_cash_flow_from_ytd(self) -> None:
        def fact(entries: list[dict[str, object]]) -> dict[str, object]:
            return {"units": {"USD": entries}}

        q1 = {"start": "2025-01-01", "end": "2025-03-31", "filed": "2025-05-01", "form": "10-Q", "frame": "CY2025Q1", "fy": 2025, "fp": "Q1", "accn": "q1"}
        q2_ytd = {"start": "2025-01-01", "end": "2025-06-30", "filed": "2025-08-01", "form": "10-Q", "fy": 2025, "fp": "Q2", "accn": "q2"}
        payload = {"facts": {"us-gaap": {
            "NetCashProvidedByUsedInOperatingActivities": fact([{**q1, "val": 100}, {**q2_ytd, "val": 260}]),
            "PaymentsToAcquirePropertyPlantAndEquipment": fact([{**q1, "val": 40}, {**q2_ytd, "val": 110}]),
        }}}

        with patch("app.providers.http._get", return_value=json.dumps(payload).encode()):
            observations = FreeMarketDataProvider()._sec_fundamentals(
                "alphabet_c", "GOOG", datetime(2025, 8, 10, tzinfo=timezone.utc)
            )

        by_id = {item["metric_id"]: item for item in observations}
        self.assertEqual(by_id["sec_operating_cash_flow"]["value"], "160")
        self.assertEqual(by_id["sec_operating_cash_flow"]["period_basis"], "derived_quarter_from_ytd")
        self.assertEqual(by_id["sec_operating_cash_flow"]["derived_from"], ["q1", "q2"])
        self.assertEqual(by_id["sec_free_cash_flow"]["value"], "90")
        self.assertEqual(by_id["sec_free_cash_flow"]["period_basis"], "derived_quarter_from_ytd")

    def test_sec_companyfacts_does_not_derive_fcf_from_mismatched_basis(self) -> None:
        def fact(entries: list[dict[str, object]]) -> dict[str, object]:
            return {"units": {"USD": entries}}

        direct = {"start": "2025-04-01", "end": "2025-06-30", "filed": "2025-08-01", "form": "10-Q", "frame": "CY2025Q2", "accn": "direct", "val": 160}
        q1 = {"start": "2025-01-01", "end": "2025-03-31", "filed": "2025-05-01", "form": "10-Q", "frame": "CY2025Q1", "fy": 2025, "fp": "Q1", "accn": "q1", "val": 40}
        q2_ytd = {"start": "2025-01-01", "end": "2025-06-30", "filed": "2025-08-01", "form": "10-Q", "fy": 2025, "fp": "Q2", "accn": "q2", "val": 110}
        payload = {"facts": {"us-gaap": {
            "NetCashProvidedByUsedInOperatingActivities": fact([direct]),
            "PaymentsToAcquirePropertyPlantAndEquipment": fact([q1, q2_ytd]),
        }}}

        with patch("app.providers.http._get", return_value=json.dumps(payload).encode()):
            observations = FreeMarketDataProvider()._sec_fundamentals(
                "alphabet_c", "GOOG", datetime(2025, 8, 10, tzinfo=timezone.utc)
            )

        self.assertNotIn("sec_free_cash_flow", {item["metric_id"] for item in observations})

    def test_binance_structure_calculates_trend_momentum_and_volume(self) -> None:
        rows = []
        for index in range(220):
            close = 100 + index
            rows.append([index, str(close - 1), str(close + 1), str(close - 2), str(close), str(1000 + index), index, "0", 0, "0", "0", "0"])
        with patch("app.providers.http._get", return_value=json.dumps(rows).encode()):
            signals = FreeMarketDataProvider()._binance_structure()

        by_id = {item["metric_id"]: item for item in signals}
        self.assertEqual(by_id["btc_sma_20d"]["value"], "309.50")
        self.assertEqual(by_id["btc_sma_200d"]["value"], "219.50")
        self.assertEqual(by_id["btc_rsi_14d"]["value"], "100.00")
        self.assertIn("btc_volume_vs_20d", by_id)

    def test_binance_structure_excludes_unfinished_daily_bar(self) -> None:
        rows = []
        for index in range(202):
            close = 100 + index
            close_time = index
            rows.append([index, str(close), str(close), str(close), str(close), "1000", close_time, "0", 0, "0", "0", "0"])
        rows[-1][4] = "99999"
        rows[-1][6] = 9999999999999
        with patch("app.providers.http._get", return_value=json.dumps(rows).encode()):
            signals = FreeMarketDataProvider()._binance_structure()

        by_id = {item["metric_id"]: item for item in signals}
        self.assertNotEqual(by_id["btc_30d_high"]["value"], "99999.00")
        self.assertEqual(by_id["btc_30d_high"]["value"], "300.00")

    def test_structural_levels_stay_on_the_correct_side_of_price(self) -> None:
        closes = [Decimal("100")] * 70
        highs = [Decimal("101")] * 70
        lows = [Decimal("99")] * 70
        highs[60] = Decimal("110")
        lows[55] = Decimal("90")

        supports, resistances = FreeMarketDataProvider._structural_levels(closes, highs, lows)

        self.assertTrue(supports)
        self.assertTrue(resistances)
        self.assertTrue(all(value < closes[-1] for value in supports))
        self.assertTrue(all(value > closes[-1] for value in resistances))
        self.assertLessEqual(len(supports), 2)
        self.assertLessEqual(len(resistances), 2)

    def test_eia_wpsr_parses_complete_stock_section_with_weekly_and_yoy_comparisons(self) -> None:
        payload = b'''title \x96 official note\nSTUB_1,8/14/26,8/7/26,Difference,Percent Change,8/15/25,Difference,Percent Change\nCommercial (Excluding SPR),"428,815",424.410,4.405,1.000,420.684,8.130,1.900\nTotal Motor Gasoline,209.378,208.690,0.688,0.300,223.570,-14.192,-6.300\nDistillate Fuel Oil,105.619,107.149,-1.530,-1.400,116.028,-10.409,-9.000\nSTUB_1,STUB_2,STUB_3\nCommercial (Excluding SPR),999,999,0,0,999,0,0\n'''
        with patch("app.providers.http._get", return_value=payload):
            observations = FreeMarketDataProvider()._eia_petroleum_stocks()

        by_id = {item["metric_id"]: item for item in observations}
        crude = by_id["eia_commercial_crude_stocks"]
        self.assertEqual(len(observations), 3)
        self.assertEqual(crude["value"], "428815")
        self.assertEqual(crude["report_week"], "2026-08-14")
        self.assertEqual(crude["yoy_change_pct"], "1.900")
        self.assertEqual(by_id["eia_distillate_stocks"]["change_value"], "-1.530")

    def test_eia_wpsr_rejects_incomplete_or_mixed_stock_sections(self) -> None:
        payload = b'''STUB_1,8/14/26,8/7/26,Difference,Percent Change,8/15/25,Difference,Percent Change\nCommercial (Excluding SPR),428.815,424.410,4.405,1.000,420.684,8.130,1.900\nTotal Motor Gasoline,209.378,208.690,0.688,0.300,223.570,-14.192,-6.300\nSTUB_1,STUB_2,STUB_3\nDistillate Fuel Oil,105.619,107.149,-1.530,-1.400,116.028,-10.409,-9.000\n'''
        with patch("app.providers.http._get", return_value=payload):
            with self.assertRaisesRegex(ValueError, "incomplete EIA WPSR stock section"):
                FreeMarketDataProvider()._eia_petroleum_stocks()

    def test_cross_asset_fetches_eia_stocks_once(self) -> None:
        module = next(item for item in load_module_configs(ROOT / "app" / "modules") if item.task_id == "cross_asset")
        eia_signal = {
            "metric_id": "eia_commercial_crude_stocks", "instrument_id": "wti_front_month",
            "label": "EIA美国商业原油库存（不含SPR）", "value": "428.815", "unit": "million_barrels",
            "as_of": "2026-08-14T00:00:00+00:00", "source_url": "https://ir.eia.gov/wpsr/table1.csv",
            "provider": "eia-wpsr",
        }
        provider = FreeMarketDataProvider(calendar=MagicMock())
        provider.calendar.latest_closed_session.return_value = date(2026, 8, 25)
        with (
            patch.object(provider, "_eia_petroleum_stocks", return_value=[eia_signal]) as eia,
            patch.object(provider, "_binance", return_value=[]),
            patch.object(provider, "_binance_30h", return_value=[]),
            patch.object(provider, "_coingecko", return_value=[]),
            patch.object(provider, "_binance_structure", return_value=[]),
            patch.object(provider, "_binance_derivatives", return_value=[]),
            patch.object(provider, "_yahoo", return_value=[]),
            patch.object(provider, "_yahoo_structure", return_value=[]),
            patch.object(provider, "_stooq", return_value=[]),
        ):
            result = provider.get_task_data(module, datetime(2026, 8, 26, tzinfo=timezone.utc))

        eia.assert_called_once_with()
        self.assertIn(eia_signal, result["signals"])

    def test_tencent_hk_response_is_normalized(self) -> None:
        fields = [""] * 33
        fields[3], fields[4], fields[30], fields[32] = "39.000", "40.080", "2026/08/18 16:08:06", "-2.69"
        payload = f'v_hk01772="{"~".join(fields)}";'.encode("gb18030")
        with patch("app.providers.http._get", return_value=payload):
            record = FreeMarketDataProvider()._tencent_hk("ganfeng_h", "1772.HK")[0]
        self.assertEqual(record["provider"], "tencent-quote")
        self.assertEqual(record["value"], "39.000")
        self.assertEqual(record["previous_value"], "40.080")

    def test_yahoo_includes_previous_trading_timestamp(self) -> None:
        payload = json.dumps(
            {
                "chart": {
                    "result": [{
                        "timestamp": [1787101200, 1787187600],
                        "indicators": {"quote": [{"close": [100.0, 102.0]}]},
                    }]
                }
            }
        ).encode()
        with patch("app.providers.http._get", return_value=payload):
            record = FreeMarketDataProvider()._yahoo("alphabet_c", "GOOG", "USD")[0]
        self.assertEqual(record["previous_value"], "100.0")
        self.assertNotEqual(record["previous_as_of"], record["as_of"])

    def test_yahoo_excludes_unfinished_daily_bar(self) -> None:
        timestamps = [
            int(datetime(2026, 8, day, 13, 30, tzinfo=timezone.utc).timestamp())
            for day in (19, 20, 21)
        ]
        payload = json.dumps({
            "chart": {"result": [{
                "timestamp": timestamps,
                "indicators": {"quote": [{"close": [100.0, 102.0, 99.0]}]},
            }]}
        }).encode()
        with patch("app.providers.http._get", return_value=payload):
            record = FreeMarketDataProvider()._yahoo(
                "alphabet_c", "GOOG", "USD", datetime(2026, 8, 20).date(), ZoneInfo("America/New_York")
            )[0]
        self.assertEqual(record["value"], "102.0")
        self.assertEqual(record["previous_value"], "100.0")
        self.assertEqual(record["session_date"], "2026-08-20")

    def test_yahoo_structure_calculates_asset_specific_signals(self) -> None:
        timestamps = [
            int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()) + index * 86400
            for index in range(220)
        ]
        closes = [100.0 + index for index in range(220)]
        volumes = [1000 + index for index in range(220)]
        payload = json.dumps({
            "chart": {"result": [{
                "timestamp": timestamps,
                "indicators": {"quote": [{"close": closes, "volume": volumes}]},
            }]}
        }).encode()
        with patch("app.providers.http._get", return_value=payload):
            signals = FreeMarketDataProvider()._yahoo_structure(
                "alphabet_c", "GOOG", "USD", date(2026, 8, 8), ZoneInfo("America/New_York")
            )

        by_id = {item["metric_id"]: item for item in signals}
        self.assertEqual(by_id["alphabet_c_sma_20d"]["instrument_id"], "alphabet_c")
        self.assertEqual(by_id["alphabet_c_sma_20d"]["value"], "309.50")
        self.assertEqual(by_id["alphabet_c_rsi_14d"]["value"], "100.00")

    def test_google_news_uses_separate_chinese_and_english_locales(self) -> None:
        module = next(item for item in load_module_configs(ROOT / "app" / "modules") if item.task_id == "cybersecurity")
        requested_urls: list[str] = []

        def fake_get(url: str, _: float) -> bytes:
            requested_urls.append(url)
            if "gdeltproject.org" in url:
                return b'{"articles": []}'
            return b"<rss><channel></channel></rss>"

        with patch("app.providers.http._get", side_effect=fake_get):
            result = FreeNewsProvider(None).get_task_data(
                module,
                datetime(2026, 8, 17, tzinfo=ZoneInfo("Asia/Hong_Kong")),
                datetime(2026, 8, 18, tzinfo=ZoneInfo("Asia/Hong_Kong")),
            )

        google_queries = [item for item in result["queries"] if item["provider"] == "google-news-rss"]
        self.assertEqual({item["language"] for item in google_queries}, {"zh", "en"})
        self.assertTrue(any("hl=en-US" in url and "ceid=US%3Aen" in url for url in requested_urls))
        self.assertTrue(any("hl=zh-CN" in url and "ceid=HK%3Azh-Hans" in url for url in requested_urls))

    def test_only_exact_upcoming_date_is_structured_from_authoritative_headline(self) -> None:
        module = next(item for item in load_module_configs(ROOT / "app" / "modules") if item.task_id == "macro_market")
        rss = b"""<rss><channel><item>
        <title>Federal Reserve public event August 25 at 10:00 AM ET</title>
        <description>Official event calendar</description>
        <pubDate>Fri, 21 Aug 2026 16:32:09 GMT</pubDate>
        <source>Federal Reserve</source><link>https://www.federalreserve.gov/calendar.htm</link>
        </item></channel></rss>"""

        def fake_get(url: str, _: float) -> bytes:
            return b'{"articles": []}' if "gdeltproject.org" in url else rss

        with patch("app.providers.http._get", side_effect=fake_get):
            result = FreeNewsProvider(None).get_task_data(
                module,
                datetime(2026, 8, 21, 16, 0, tzinfo=ZoneInfo("Asia/Hong_Kong")),
                datetime(2026, 8, 22, 22, 0, tzinfo=ZoneInfo("Asia/Hong_Kong")),
            )

        event = result["upcoming_events"][0]
        self.assertEqual(event["event_at"], "2026-08-25T22:00:00+08:00")
        self.assertIsNone(event["event_end_at"])
        self.assertEqual(event["original_timezone"], "America/New_York")
        self.assertEqual(event["confirmation_status"], "confirmed")

    def test_weekly_date_range_is_not_an_event(self) -> None:
        module = next(item for item in load_module_configs(ROOT / "app" / "modules") if item.task_id == "macro_market")
        rss = b"""<rss><channel><item>
        <title>Economic Data This Week August 24-28</title>
        <description>Weekly overview</description><pubDate>Fri, 21 Aug 2026 16:32:09 GMT</pubDate>
        <source>Federal Reserve</source><link>https://www.federalreserve.gov/calendar.htm</link>
        </item></channel></rss>"""
        with patch("app.providers.http._get", side_effect=lambda url, _: b'{"articles": []}' if "gdeltproject.org" in url else rss):
            result = FreeNewsProvider(None).get_task_data(
                module,
                datetime(2026, 8, 21, 16, 0, tzinfo=ZoneInfo("Asia/Hong_Kong")),
                datetime(2026, 8, 22, 22, 0, tzinfo=ZoneInfo("Asia/Hong_Kong")),
            )
        self.assertEqual(result["upcoming_events"], [])

    def test_bea_official_calendar_returns_exact_hkt_events(self) -> None:
        html = b"""<table><tbody><tr class='scheduled-releases-type-press'>
        <td class='scheduled-date no-wrap'><div class='release-date'>August 26</div><small>8:30 AM</small></td>
        <td class='release-title views-field'>GDP (Second Estimate), 2nd Quarter 2026</td>
        </tr></tbody></table>"""
        provider = FreeNewsProvider(None)
        end_at = datetime(2026, 8, 23, 8, 0, tzinfo=ZoneInfo("Asia/Hong_Kong"))
        with patch("app.providers.http._get", return_value=html):
            events, articles = provider._bea_calendar(end_at)
        self.assertEqual(events[0]["event_at"], "2026-08-26T20:30:00+08:00")
        self.assertEqual(events[0]["confirmation_status"], "confirmed")
        self.assertEqual(events[0]["original_time_label"], "2026-08-26 08:30 EDT")
        self.assertEqual(articles[0]["source_tier"], "primary")

    def test_crowdstrike_official_ir_earnings_event_is_in_calendar(self) -> None:
        module = next(item for item in load_module_configs(ROOT / "app" / "modules") if item.task_id == "cybersecurity")
        with patch("app.providers.http._get", side_effect=lambda url, _: b'{"articles": []}' if "gdeltproject.org" in url else b"<rss><channel></channel></rss>"):
            result = FreeNewsProvider(None).get_task_data(
                module,
                datetime(2026, 8, 24, 5, 0, tzinfo=ZoneInfo("Asia/Hong_Kong")),
                datetime(2026, 8, 25, 11, 0, tzinfo=ZoneInfo("Asia/Hong_Kong")),
            )
        event = next(item for item in result["upcoming_events"] if item["provider"] == "company-ir-calendar")
        self.assertEqual(event["event_at"], "2026-08-27T05:00:00+08:00")
        self.assertEqual(event["original_time_label"], "2026-08-26 17:00 EDT")
        self.assertEqual(event["confirmation_status"], "confirmed")

    def test_macro_ratio_is_calculated_by_provider_for_all_periods(self) -> None:
        module = next(item for item in load_module_configs(ROOT / "app" / "modules") if item.task_id == "macro_market")
        end_at = datetime(2026, 8, 18, tzinfo=ZoneInfo("Asia/Hong_Kong"))
        timestamps = [
            int(datetime(2026, month, day, tzinfo=timezone.utc).timestamp())
            for month, day in ((1, 2), (5, 20), (7, 20), (8, 17), (8, 18))
        ]

        def fake_history(symbol: str):
            values = {
                "^IXIC": [20000, 21000, 22000, 23000, 23100],
                "^SOX": [5000, 5400, 5800, 6000, 6060],
                "^NDX": [21000, 22000, 23000, 24000, 24240],
            }[symbol]
            return list(zip(timestamps, values)), f"https://example.test/{symbol}"

        csv_payload = b"observation_date,VIXCLS,CPIAUCSL,DFF\n2026-08-17,15.0,300.0,4.0\n"
        with patch("app.providers.http._get", return_value=csv_payload), patch.object(
            FredMacroDataProvider, "_index_history", side_effect=fake_history
        ):
            result = FredMacroDataProvider().get_task_data(module, end_at.replace(day=17), end_at)

        observations = result["relative_metrics"][0]["observations"]
        self.assertEqual([item["label"] for item in observations], ["current", "one_month_ago", "three_months_ago", "year_start"])
        self.assertTrue(all("ratio" in item for item in observations))
        self.assertEqual(observations[0]["ratio"], "0.2500")
        self.assertEqual(observations[-1]["as_of"], "2026-01-02")
        self.assertEqual(observations[-1]["numerator_value"], "5000")

    def test_cross_asset_receives_fred_liquidity_series(self) -> None:
        module = next(item for item in load_module_configs(ROOT / "app" / "modules") if item.task_id == "cross_asset")
        payloads = {
            "DFF": b"observation_date,DFF\n2026-08-21,4.25\n",
            "DGS10": b"observation_date,DGS10\n2026-08-21,4.10\n",
            "DTWEXBGS": b"observation_date,DTWEXBGS\n2026-08-21,118.2\n",
        }

        def fake_get(url: str, _: float) -> bytes:
            return next(payload for series, payload in payloads.items() if f"id={series}" in url)

        end_at = datetime(2026, 8, 22, tzinfo=ZoneInfo("Asia/Hong_Kong"))
        with patch("app.providers.http._get", side_effect=fake_get):
            result = FredMacroDataProvider().get_task_data(module, end_at.replace(day=21), end_at)

        self.assertEqual({item["metric_id"] for item in result["observations"]}, {"dff", "dgs10", "dtwexbgs"})
        self.assertEqual(result["relative_metrics"], [])

    def test_marketaux_token_is_not_returned_in_provider_bundle(self) -> None:
        module = next(item for item in load_module_configs(ROOT / "app" / "modules") if item.task_id == "cybersecurity")
        payloads = [json.dumps({"data": []}).encode(), b"<rss><channel></channel></rss>", b"<rss><channel></channel></rss>", b"<rss><channel></channel></rss>"]
        with patch("app.providers.http._get", side_effect=payloads):
            result = FreeNewsProvider("private-token").get_task_data(
                module,
                datetime(2026, 8, 17, tzinfo=ZoneInfo("Asia/Hong_Kong")),
                datetime(2026, 8, 18, tzinfo=ZoneInfo("Asia/Hong_Kong")),
            )
        self.assertNotIn("private-token", json.dumps(result))
        self.assertEqual(result["queries"][0], {"provider": "marketaux", "symbol": "CRWD", "status": "success", "returned": 0})


if __name__ == "__main__":
    unittest.main()

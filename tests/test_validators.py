from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from app.integrations.mock_openai import MockResponsesClient
from app.modules.loader import load_module_configs
from app.orchestrator.context import build_run_context
from app.prompts.builder import PromptBuilder
from app.providers.mock import MockProviderBundle
from app.schemas.models import CheckStatus, EventStatus, ResearchTaskResult, UpcomingEvent
from app.validators.result import ValidationFailure, canonical_url, validate_result


ROOT = Path(__file__).resolve().parents[1]


class ValidatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.providers = MockProviderBundle()
        self.context = build_run_context(
            self.providers.calendar,
            datetime(2026, 8, 18, 8, 15, tzinfo=ZoneInfo("Asia/Hong_Kong")),
        )
        self.module = {item.task_id: item for item in load_module_configs(ROOT / "app" / "modules")}["hk_equities"]
        provider_data = {
            "market": self.providers.market.get_task_data(self.module, self.context.scheduled_for),
            "news": self.providers.news.get_task_data(self.module, self.context.window.start_at, self.context.window.end_at),
            "macro": self.providers.macro.get_task_data(self.module, self.context.window.start_at, self.context.window.end_at),
        }
        prompt = PromptBuilder(ROOT / "app" / "prompts").build(self.module, self.context)
        self.raw = MockResponsesClient().create(self.module, prompt, provider_data)
        self.provider_data = provider_data

    def test_price_and_duplicate_news_validation(self) -> None:
        result = validate_result(ResearchTaskResult.model_validate(self.raw), self.module)
        self.assertEqual(len(result.instruments[0].news), 1)
        self.assertTrue(any(item.code == "DUPLICATE_NEWS_REMOVED" for item in result.warnings))
        self.assertEqual(canonical_url("https://EXAMPLE.test/a/?utm_source=x&b=2"), "https://example.test/a?b=2")

    def test_bad_price_change_is_rejected(self) -> None:
        broken = ResearchTaskResult.model_validate(self.raw)
        broken.instruments[0].prices[0].change_pct = Decimal("99")
        with self.assertRaises(ValidationFailure):
            validate_result(broken, self.module)

    def test_small_rounding_difference_is_normalized(self) -> None:
        result = ResearchTaskResult.model_validate(self.raw)
        price = result.instruments[0].prices[0]
        price.change_value += Decimal("0.004")
        price.change_pct += Decimal("0.004")
        validated = validate_result(result, self.module)
        self.assertTrue(any(item.code == "PRICE_CHANGE_RECALCULATED" for item in validated.warnings))

    def test_relative_metric_is_normalized_from_provider_values(self) -> None:
        module = {item.task_id: item for item in load_module_configs(ROOT / "app" / "modules")}["macro_market"]
        provider_data = {
            "market": self.providers.market.get_task_data(module, self.context.scheduled_for),
            "news": self.providers.news.get_task_data(module, self.context.window.start_at, self.context.window.end_at),
            "macro": self.providers.macro.get_task_data(module, self.context.window.start_at, self.context.window.end_at),
        }
        prompt = PromptBuilder(ROOT / "app" / "prompts").build(module, self.context)
        result = ResearchTaskResult.model_validate(MockResponsesClient().create(module, prompt, provider_data))
        result.relative_metrics[0].observations[0].numerator_value += Decimal("100")
        result.relative_metrics[0].observations[0].ratio = Decimal("9")

        validated = validate_result(result, module, provider_data)

        expected = provider_data["macro"]["relative_metrics"][0]["observations"][0]
        actual = validated.relative_metrics[0].observations[0]
        self.assertEqual(actual.numerator_value, Decimal(expected["numerator_value"]))
        self.assertEqual(actual.ratio, (Decimal(expected["numerator_value"]) / Decimal(expected["denominator_value"])).quantize(Decimal("0.0001")))
        self.assertTrue(any(item.code == "RELATIVE_METRIC_PROVIDER_NORMALIZED" for item in validated.warnings))

    def test_fixed_data_check_cannot_be_not_triggered(self) -> None:
        module = {item.task_id: item for item in load_module_configs(ROOT / "app" / "modules")}["macro_market"]
        provider_data = {
            "market": self.providers.market.get_task_data(module, self.context.scheduled_for),
            "news": self.providers.news.get_task_data(module, self.context.window.start_at, self.context.window.end_at),
            "macro": self.providers.macro.get_task_data(module, self.context.window.start_at, self.context.window.end_at),
        }
        prompt = PromptBuilder(ROOT / "app" / "prompts").build(module, self.context)
        result = ResearchTaskResult.model_validate(MockResponsesClient().create(module, prompt, provider_data))
        check = next(item for item in result.research_checks if item.requirement_type == "data")
        check.status = CheckStatus.NOT_TRIGGERED
        check.conclusion_zh = "没有触发。"

        validated = validate_result(result, module, provider_data)

        self.assertEqual(check.status, CheckStatus.DATA_UNAVAILABLE)
        self.assertIn("未能获取精确数据", check.conclusion_zh)
        self.assertEqual(validated.status.value, "partial")

    def test_news_outside_window_is_rejected(self) -> None:
        broken = ResearchTaskResult.model_validate(self.raw)
        broken.instruments[0].news[0].published_at = self.context.window.end_at + timedelta(hours=1)
        with self.assertRaises(ValidationFailure):
            validate_result(broken, self.module)

    def test_upcoming_event_must_be_within_seven_days_and_source_backed(self) -> None:
        result = ResearchTaskResult.model_validate(self.raw)
        news_source_id = result.instruments[0].news[0].source_ids[0]
        result.upcoming_events = [UpcomingEvent(
            event_at=self.context.window.end_at + timedelta(days=3),
            event_end_at=self.context.window.end_at + timedelta(days=5),
            title_zh="公司已公告的投资者活动",
            affected_assets_zh=["港股"],
            why_it_matters_zh="活动可能提供经营指引更新。",
            source_ids=[news_source_id],
        )]
        source_url = next(item for item in result.sources if item.source_id == news_source_id).url
        self.provider_data["news"]["upcoming_events"] = [{
            "title": "公司已公告的投资者活动",
            "event_at": (self.context.window.end_at + timedelta(days=3)).isoformat(),
            "event_end_at": (self.context.window.end_at + timedelta(days=5)).isoformat(),
            "confirmation_status": "confirmed",
            "last_verified_at": self.context.window.end_at.isoformat(),
            "url": str(source_url),
        }]

        validated = validate_result(result, self.module, self.provider_data)

        self.assertEqual(len(validated.upcoming_events), 1)
        self.assertIs(validated.upcoming_events[0].confirmation_status, EventStatus.CONFIRMED)
        self.assertIsInstance(validated.upcoming_events[0].last_verified_at, datetime)
        result = ResearchTaskResult.model_validate(self.raw)
        result.upcoming_events = [UpcomingEvent(
            event_at=self.context.window.end_at + timedelta(days=8),
            title_zh="超出前瞻窗口的活动",
            affected_assets_zh=["港股"],
            why_it_matters_zh="不应进入本期报告。",
            source_ids=[news_source_id],
        )]
        with self.assertRaisesRegex(ValidationFailure, "outside next-seven-day window"):
            validate_result(result, self.module, self.provider_data)

    def test_upcoming_event_restores_unique_shared_official_source(self) -> None:
        result = ResearchTaskResult.model_validate(self.raw)
        event_at = self.context.window.end_at + timedelta(days=3)
        result.upcoming_events = [UpcomingEvent(
            event_at=event_at,
            original_timezone="America/New_York",
            title_zh="美国二季度GDP第二次估算与企业利润",
            affected_assets_zh=["美股"],
            why_it_matters_zh="数据将改变盈利与利率预期。",
            source_ids=["source_bea_calendar"],
        )]
        self.provider_data["shared_context"] = {"news": {
            "articles": [{
                "headline": "Gross Domestic Product, 2nd Estimate",
                "published_at": self.context.window.end_at.isoformat(),
                "publisher": "U.S. Bureau of Economic Analysis",
                "provider": "bea-official-calendar",
                "url": "https://www.bea.gov/news/schedule",
            }],
            "upcoming_events": [{
                "title": "Gross Domestic Product, 2nd Estimate",
                "event_at": event_at.isoformat(),
                "event_end_at": None,
                "original_timezone": "America/New_York",
                "all_day": False,
                "confirmation_status": "confirmed",
                "last_verified_at": self.context.window.end_at.isoformat(),
                "publisher": "U.S. Bureau of Economic Analysis",
                "provider": "bea-official-calendar",
                "url": "https://www.bea.gov/news/schedule",
            }],
        }}

        validated = validate_result(result, self.module, self.provider_data)

        restored = next(source for source in validated.sources if source.source_id == "source_bea_calendar")
        self.assertEqual(str(restored.url), "https://www.bea.gov/news/schedule")
        self.assertTrue(any(warning.code == "EVENT_SOURCE_METADATA_RESTORED" for warning in validated.warnings))

    def test_missing_mandatory_research_check_is_rejected(self) -> None:
        broken = ResearchTaskResult.model_validate(self.raw)
        broken.research_checks.pop()
        with self.assertRaisesRegex(ValidationFailure, "required research checks missing"):
            validate_result(broken, self.module)

    def test_price_must_match_provider_bundle(self) -> None:
        broken = ResearchTaskResult.model_validate(self.raw)
        broken.instruments[0].prices[0].value += Decimal("10")
        broken.instruments[0].prices[0].change_value += Decimal("10")
        previous = broken.instruments[0].prices[0].previous_value
        broken.instruments[0].prices[0].change_pct = ((broken.instruments[0].prices[0].value - previous) / previous * Decimal("100")).quantize(Decimal("0.01"))
        with self.assertRaisesRegex(ValidationFailure, "price not found in provider data"):
            validate_result(broken, self.module, self.provider_data)

    def test_previous_close_matches_provider_previous_value(self) -> None:
        result = ResearchTaskResult.model_validate(self.raw)
        price = result.instruments[0].prices[0]
        provider_record = self.provider_data["market"]["records"][0]
        price.kind = "previous_close"
        price.value = Decimal(str(provider_record["previous_value"]))
        price.previous_value = None
        price.change_value = None
        price.change_pct = None
        validated = validate_result(result, self.module, self.provider_data)
        self.assertEqual(validated.instruments[0].prices[0].value, Decimal(str(provider_record["previous_value"])))

    def test_chinese_previous_close_kind_is_normalized(self) -> None:
        result = ResearchTaskResult.model_validate(self.raw)
        price = result.instruments[0].prices[0]
        provider_record = self.provider_data["market"]["records"][0]
        price.kind = "上一交易日收盘价"
        price.value = Decimal(str(provider_record["previous_value"])).quantize(Decimal("0.01"))
        price.previous_value = None
        price.change_value = None
        price.change_pct = None

        validated = validate_result(result, self.module, self.provider_data)

        self.assertEqual(validated.instruments[0].prices[0].kind, "previous_close")
        self.assertEqual(validated.instruments[0].prices[0].value, Decimal(str(provider_record["previous_value"])))

    def test_mislabeled_previous_close_is_identified_by_value_and_timestamp(self) -> None:
        result = ResearchTaskResult.model_validate(self.raw)
        price = result.instruments[0].prices[0]
        provider_record = self.provider_data["market"]["records"][0]
        provider_record["previous_as_of"] = "2026-08-17T08:00:00+00:00"
        price.kind = "close"
        price.value = Decimal(str(provider_record["previous_value"])).quantize(Decimal("0.01"))
        price.as_of = datetime.fromisoformat(provider_record["previous_as_of"])
        price.previous_value = None
        price.change_value = None
        price.change_pct = None

        validated = validate_result(result, self.module, self.provider_data)

        self.assertEqual(validated.instruments[0].prices[0].kind, "previous_close")
        self.assertEqual(validated.instruments[0].prices[0].value, Decimal(str(provider_record["previous_value"])))

    def test_missing_previous_close_is_added_from_yahoo_provider(self) -> None:
        result = ResearchTaskResult.model_validate(self.raw)
        result.instruments[0].prices = result.instruments[0].prices[:1]
        provider_record = self.provider_data["market"]["records"][0]
        provider_record["provider"] = "yahoo-chart"
        provider_record["previous_as_of"] = "2026-08-17T08:00:00+00:00"
        source = next(item for item in result.sources if item.source_id in result.instruments[0].prices[0].source_ids)
        provider_record["source_url"] = str(source.url)

        validated = validate_result(result, self.module, self.provider_data)

        previous = next(price for price in validated.instruments[0].prices if price.kind == "previous_close")
        self.assertEqual(previous.value, Decimal(str(provider_record["previous_value"])))
        self.assertEqual(previous.as_of.isoformat(), "2026-08-17T08:00:00+00:00")
        self.assertTrue(any(item.code == "PREVIOUS_CLOSE_ADDED_FROM_PROVIDER" for item in validated.warnings))

    def test_unknown_research_check_source_is_removed_with_warning(self) -> None:
        result = ResearchTaskResult.model_validate(self.raw)
        result.research_checks[0].source_ids.append("unknown_source")

        validated = validate_result(result, self.module, self.provider_data)

        self.assertNotIn("unknown_source", validated.research_checks[0].source_ids)
        self.assertTrue(any(item.code == "UNKNOWN_CHECK_SOURCE_REMOVED" for item in validated.warnings))

    def test_missing_news_source_metadata_is_restored_by_unique_timestamp(self) -> None:
        result = ResearchTaskResult.model_validate(self.raw)
        news = result.instruments[0].news[0]
        original_source_id = news.source_ids[0]
        result.sources = [source for source in result.sources if source.source_id != original_source_id]
        self.provider_data["news"]["articles"][0]["published_at"] = news.published_at.isoformat()
        for offset, article in enumerate(self.provider_data["news"]["articles"][1:], start=1):
            article["published_at"] = (news.published_at - timedelta(hours=offset)).isoformat()

        validated = validate_result(result, self.module, self.provider_data)

        restored = next(source for source in validated.sources if source.source_id == original_source_id)
        self.assertEqual(restored.published_at, news.published_at)
        self.assertTrue(any(item.code == "NEWS_SOURCE_METADATA_RESTORED" for item in validated.warnings))

    def test_provider_precision_is_restored_before_change_recalculation(self) -> None:
        result = ResearchTaskResult.model_validate(self.raw)
        price = result.instruments[0].prices[0]
        provider_record = self.provider_data["market"]["records"][0]
        provider_record["value"] = 8.085000038146973
        provider_record["previous_value"] = 8.100000381469727
        price.value = Decimal("8.09")
        price.previous_value = Decimal("8.10")
        price.change_value = Decimal("-0.02")
        price.change_pct = Decimal("-0.19")

        validated = validate_result(result, self.module, self.provider_data)
        normalized = validated.instruments[0].prices[0]
        self.assertEqual(normalized.value, Decimal("8.085000038146973"))
        self.assertEqual(normalized.previous_value, Decimal("8.100000381469727"))
        self.assertEqual(normalized.change_value, Decimal("-0.02"))
        self.assertEqual(normalized.change_pct, Decimal("-0.19"))
        self.assertTrue(any(item.code == "PRICE_PROVIDER_NORMALIZED" for item in validated.warnings))

    def test_unexpected_research_check_is_rejected(self) -> None:
        broken = ResearchTaskResult.model_validate(self.raw)
        extra = broken.research_checks[0].model_copy(
            update={"check_id": "extra", "requirement_zh": "未配置检查"}
        )
        broken.research_checks.append(extra)
        with self.assertRaisesRegex(ValidationFailure, "unexpected research checks"):
            validate_result(broken, self.module)

    def test_successful_news_search_normalizes_no_news_to_no_material_finding(self) -> None:
        result = ResearchTaskResult.model_validate(self.raw)
        news_check = next(item for item in result.research_checks if item.requirement_type == "news")
        news_check.status = CheckStatus.DATA_UNAVAILABLE
        self.provider_data["news"]["queries"] = [
            {"provider": "google-news-rss", "query": "test", "status": "success", "returned": 0}
        ]
        validated = validate_result(result, self.module, self.provider_data)
        self.assertEqual(news_check.status, CheckStatus.NO_MATERIAL_FINDING)
        self.assertFalse(any(item.code == "NO_NEWS_IS_NOT_MISSING_DATA" for item in validated.warnings))

    def test_unreported_provider_news_article_is_allowed(self) -> None:
        result = ResearchTaskResult.model_validate(self.raw)
        self.provider_data["news"]["articles"].append(
            {
                "instrument_id": None,
                "headline": "Unreported provider article",
                "description": "test",
                "published_at": self.context.window.end_at.isoformat(),
                "publisher": "Test News",
                "url": "https://example.test/news/unreported",
                "provider": "test-news",
            }
        )
        validated = validate_result(result, self.module, self.provider_data)
        self.assertEqual(len(validated.instruments[0].news), len(result.instruments[0].news))

    def test_truncated_google_news_url_is_restored_from_provider(self) -> None:
        result = ResearchTaskResult.model_validate(self.raw)
        source = next(item for item in result.sources if item.provider == "mock-news")
        source.provider = "google-news-rss"
        full_url = "https://news.google.com/rss/articles/ABCDEF123456?oc=5"
        source.url = "https://news.google.com/rss/articles/ABCDEF?oc=5"
        self.provider_data["news"]["articles"][0]["url"] = full_url

        validated = validate_result(result, self.module, self.provider_data)

        restored = next(item for item in validated.sources if item.source_id == source.source_id)
        self.assertEqual(str(restored.url), full_url)
        self.assertTrue(any(item.code == "NEWS_SOURCE_URL_RESTORED" for item in validated.warnings))


if __name__ == "__main__":
    unittest.main()

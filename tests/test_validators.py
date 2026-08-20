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
from app.schemas.models import CheckStatus, ResearchTaskResult
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

    def test_news_outside_window_is_rejected(self) -> None:
        broken = ResearchTaskResult.model_validate(self.raw)
        broken.instruments[0].news[0].published_at = self.context.window.end_at + timedelta(hours=1)
        with self.assertRaises(ValidationFailure):
            validate_result(broken, self.module)

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

    def test_every_provider_news_article_must_be_summarized(self) -> None:
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
        with self.assertRaisesRegex(ValidationFailure, "provider news articles were not summarized"):
            validate_result(result, self.module, self.provider_data)


if __name__ == "__main__":
    unittest.main()

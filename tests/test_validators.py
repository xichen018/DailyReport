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
from app.schemas.models import ResearchTaskResult
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


if __name__ == "__main__":
    unittest.main()

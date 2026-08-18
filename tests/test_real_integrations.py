from __future__ import annotations

import json
import os
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from app.integrations.secrets import load_secrets
from app.modules.loader import load_module_configs
from app.providers.http import FreeMarketDataProvider, FreeNewsProvider
from app.settings import Settings


ROOT = Path(__file__).resolve().parents[1]


class RealIntegrationContractTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()

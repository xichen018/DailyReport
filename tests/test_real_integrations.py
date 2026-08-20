from __future__ import annotations

import json
import os
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from app.integrations.secrets import load_secrets
from app.integrations.openai_responses import _gateway_compatible_strict_schema
from app.modules.loader import load_module_configs
from app.providers.http import FreeMarketDataProvider, FreeNewsProvider, _iso_published_at
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

    def test_tencent_hk_response_is_normalized(self) -> None:
        fields = [""] * 33
        fields[3], fields[4], fields[30], fields[32] = "39.000", "40.080", "2026/08/18 16:08:06", "-2.69"
        payload = f'v_hk01772="{"~".join(fields)}";'.encode("gb18030")
        with patch("app.providers.http._get", return_value=payload):
            record = FreeMarketDataProvider()._tencent_hk("ganfeng_h", "1772.HK")[0]
        self.assertEqual(record["provider"], "tencent-quote")
        self.assertEqual(record["value"], "39.000")
        self.assertEqual(record["previous_value"], "40.080")

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

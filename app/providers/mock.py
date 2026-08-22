from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from app.modules.loader import ModuleConfig
from app.providers.base import ProviderBundle
from app.providers.calendar import SimpleTradingCalendar


def _seed(symbol: str) -> Decimal:
    value = int(hashlib.sha256(symbol.encode("utf-8")).hexdigest()[:6], 16)
    return Decimal(value % 40000 + 1000) / Decimal("100")


class MockMarketDataProvider:
    name = "mock-market"

    def get_task_data(self, module: ModuleConfig, as_of: datetime) -> dict[str, Any]:
        records = []
        for index, instrument in enumerate(module.instruments):
            current = _seed(instrument.symbol)
            previous = (current / (Decimal("1.012") if index % 2 == 0 else Decimal("0.993"))).quantize(Decimal("0.01"))
            records.append(
                {
                    "instrument_id": instrument.instrument_id,
                    "symbol": instrument.symbol,
                    "value": str(current),
                    "previous_value": str(previous),
                    "currency": instrument.currency,
                    "as_of": as_of.isoformat(),
                    "source_url": f"https://example.test/market/{instrument.symbol}",
                }
            )
        signals = []
        for instrument in module.instruments:
            metric_rows = (
                (f"{instrument.instrument_id}_sma_20d", f"{instrument.symbol} 20日简单移动均线", "72.00", instrument.currency),
                (f"{instrument.instrument_id}_rsi_14d", f"{instrument.symbol} 14日 RSI", "58.00", "index"),
            )
            if instrument.symbol == "BTCUSDT":
                metric_rows = (
                    ("btc_sma_20d", "BTC 20日简单移动均线", "72.00", "USDT"),
                    ("btc_rsi_14d", "BTC 14日 RSI", "58.00", "index"),
                    ("btc_perp_funding", "BTCUSDT 永续资金费率", "0.0100", "%"),
                )
            for metric_id, label, value, unit in metric_rows:
                signals.append({"metric_id": metric_id, "instrument_id": instrument.instrument_id, "label": label, "value": value, "unit": unit, "as_of": as_of.isoformat(), "source_url": f"https://example.test/market/{metric_id}"})
        return {"provider": self.name, "records": records, "signals": signals}


class MockNewsProvider:
    name = "mock-news"

    def get_task_data(self, module: ModuleConfig, start_at: datetime, end_at: datetime) -> dict[str, Any]:
        articles = []
        for index, instrument in enumerate(module.instruments):
            if index == 1 and module.task_id in {"hk_equities", "us_semis_optics"}:
                continue
            published = end_at - timedelta(hours=3 + index)
            articles.append(
                {
                    "instrument_id": instrument.instrument_id,
                    "headline": f"{instrument.name} 发布模拟业务更新",
                    "published_at": published.isoformat(),
                    "summary_zh": "公司披露的模拟经营信息符合当前板块研究主题。",
                    "impact": "positive" if index % 2 == 0 else "neutral",
                    "rationale_zh": "该信息可能改善收入可见度，但仍需后续数据验证。",
                    "publisher": "Mock Financial News",
                    "url": f"https://example.test/news/{module.task_id}/{instrument.instrument_id}",
                }
            )
            if index == 0:
                duplicate = dict(articles[-1])
                duplicate["url"] += "?utm_source=duplicate"
                articles.append(duplicate)
        return {"provider": self.name, "articles": articles}


class MockMacroDataProvider:
    name = "mock-macro"

    def get_task_data(self, module: ModuleConfig, start_at: datetime, end_at: datetime) -> dict[str, Any]:
        if module.task_id != "macro_market":
            return {"provider": self.name, "observations": [], "relative_metrics": []}
        observation_time = end_at - timedelta(hours=4)
        return {
            "provider": self.name,
            "observations": [
                {
                    "metric_id": "us_cpi_yoy",
                    "label": "美国 CPI 同比（模拟）",
                    "value": "2.8",
                    "unit": "%",
                    "period": observation_time.date().isoformat(),
                    "actual": "2.8",
                    "consensus": "2.9",
                    "prior": "3.0",
                    "url": "https://example.test/macro/us-cpi",
                },
                {
                    "metric_id": "vix_close",
                    "label": "VIX 收盘（模拟）",
                    "value": "16.4",
                    "unit": "index",
                    "period": observation_time.date().isoformat(),
                    "url": "https://example.test/market/vix",
                },
            ],
            "relative_metrics": [
                {
                    "metric_id": "sox_ndx_ratio",
                    "numerator": "SOX",
                    "denominator": "NDX",
                    "observations": [
                        {"label": "current", "as_of": end_at.date().isoformat(), "numerator_value": "7200", "denominator_value": "25000"},
                        {"label": "one_month_ago", "as_of": (end_at - timedelta(days=30)).date().isoformat(), "numerator_value": "6800", "denominator_value": "24500"},
                    ],
                    "interpretation_zh": "模拟数据表明半导体相对强弱较一个月前上升。",
                    "url": "https://example.test/market/sox-ndx",
                }
            ],
        }


class MockProviderBundle(ProviderBundle):
    def __init__(self) -> None:
        super().__init__(
            market=MockMarketDataProvider(),
            news=MockNewsProvider(),
            macro=MockMacroDataProvider(),
            calendar=SimpleTradingCalendar(),
        )

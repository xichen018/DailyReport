from __future__ import annotations

import csv
import io
import json
import re
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from app.modules.loader import ModuleConfig
from app.providers.base import ProviderBundle
from app.providers.calendar import SimpleTradingCalendar


USER_AGENT = "DailyReport/0.2 (+financial-research; contact=operator)"
YAHOO_SYMBOLS = {"BTCUSDT": "BTC-USD", "CL1": "CL=F"}
STOOQ_SYMBOLS = {
    "MU": "mu.us", "COHR": "cohr.us", "GOOG": "goog.us", "DJT": "djt.us",
    "CRWD": "crwd.us", "1772.HK": "1772.hk", "6166.HK": "6166.hk", "CL1": "cl.f",
}


def _get(url: str, timeout: float) -> bytes:
    transport = httpx.HTTPTransport(retries=2)
    with httpx.Client(transport=transport, timeout=timeout, follow_redirects=True) as client:
        response = client.get(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json,text/csv,application/rss+xml,*/*"})
        response.raise_for_status()
        return response.content


def _error(provider: str, exc: Exception) -> dict[str, str]:
    return {"provider": provider, "error_type": type(exc).__name__, "message": str(exc).split("api_token=")[0]}


class FreeMarketDataProvider:
    name = "free-market-multiprovider"

    def __init__(self, timeout: float = 25.0) -> None:
        self.timeout = timeout

    def _binance(self) -> list[dict[str, Any]]:
        urls = [f"https://api{suffix}.binance.com/api/v3/ticker/24hr?symbol=BTCUSDT" for suffix in ("", "1", "2", "3")]
        last_error: Exception | None = None
        for url in urls:
            try:
                data = json.loads(_get(url, self.timeout))
                break
            except Exception as exc:
                last_error = exc
        else:
            raise RuntimeError("all Binance public endpoints failed") from last_error
        return [{
            "instrument_id": "bitcoin_binance", "symbol": "BTCUSDT", "kind": "latest_24h",
            "value": data["lastPrice"], "previous_value": data["prevClosePrice"], "change_pct": data["priceChangePercent"],
            "currency": "USDT", "as_of": datetime.now(timezone.utc).isoformat(),
            "source_url": url, "provider": "binance",
        }]

    def _yahoo(self, instrument_id: str, symbol: str, currency: str) -> list[dict[str, Any]]:
        remote = YAHOO_SYMBOLS.get(symbol, symbol)
        encoded = urllib.parse.quote(remote, safe="")
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{encoded}?range=5d&interval=1d&events=history"
        result = json.loads(_get(url, self.timeout))["chart"]["result"][0]
        timestamps = result["timestamp"]
        closes = result["indicators"]["quote"][0]["close"]
        valid = [(ts, value) for ts, value in zip(timestamps, closes) if value is not None]
        if len(valid) < 2:
            raise ValueError(f"insufficient Yahoo closes for {symbol}")
        previous, current = valid[-2], valid[-1]
        return [{
            "instrument_id": instrument_id, "symbol": symbol, "kind": "close",
            "value": str(current[1]), "previous_value": str(previous[1]), "currency": currency,
            "as_of": datetime.fromtimestamp(current[0], timezone.utc).isoformat(), "source_url": url, "provider": "yahoo-chart",
        }]

    def _stooq(self, instrument_id: str, symbol: str, currency: str) -> list[dict[str, Any]]:
        remote = STOOQ_SYMBOLS[symbol]
        url = f"https://stooq.com/q/d/l/?s={urllib.parse.quote(remote)}&i=d"
        rows = list(csv.DictReader(io.StringIO(_get(url, self.timeout).decode("utf-8"))))
        valid = [row for row in rows if row.get("Close") not in {None, "", "N/D"}]
        if len(valid) < 2:
            raise ValueError(f"insufficient Stooq closes for {symbol}")
        previous, current = valid[-2], valid[-1]
        return [{
            "instrument_id": instrument_id, "symbol": symbol, "kind": "close_crosscheck",
            "value": current["Close"], "previous_value": previous["Close"], "currency": currency,
            "as_of": f"{current['Date']}T00:00:00+00:00", "source_url": url, "provider": "stooq",
        }]

    def _tencent_hk(self, instrument_id: str, symbol: str) -> list[dict[str, Any]]:
        numeric = symbol.split(".", 1)[0].zfill(5)
        url = f"https://qt.gtimg.cn/q=hk{numeric}"
        text = _get(url, self.timeout).decode("gb18030", errors="replace")
        match = re.search(r'="([^"]+)"', text)
        if match is None:
            raise ValueError(f"invalid Tencent quote for {symbol}")
        fields = match.group(1).split("~")
        if len(fields) < 33:
            raise ValueError(f"incomplete Tencent quote for {symbol}")
        as_of = datetime.strptime(fields[30], "%Y/%m/%d %H:%M:%S").replace(tzinfo=ZoneInfo("Asia/Hong_Kong"))
        return [{
            "instrument_id": instrument_id, "symbol": symbol, "kind": "close_crosscheck",
            "value": fields[3], "previous_value": fields[4], "change_pct": fields[32], "currency": "HKD",
            "as_of": as_of.isoformat(), "source_url": url, "provider": "tencent-quote",
        }]

    def get_task_data(self, module: ModuleConfig, as_of: datetime) -> dict[str, Any]:
        records: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        for instrument in module.instruments:
            if instrument.symbol == "BTCUSDT":
                try:
                    records.extend(self._binance())
                except Exception as exc:
                    errors.append(_error("binance", exc))
            try:
                records.extend(self._yahoo(instrument.instrument_id, instrument.symbol, instrument.currency))
            except Exception as exc:
                errors.append(_error("yahoo-chart", exc))
            if instrument.exchange == "HKEX":
                try:
                    records.extend(self._tencent_hk(instrument.instrument_id, instrument.symbol))
                except Exception as exc:
                    errors.append(_error("tencent-quote", exc))
            if instrument.symbol in STOOQ_SYMBOLS:
                try:
                    records.extend(self._stooq(instrument.instrument_id, instrument.symbol, instrument.currency))
                except Exception as exc:
                    errors.append(_error("stooq", exc))
        return {"provider": self.name, "records": records, "errors": errors, "as_of": as_of.isoformat()}


class FreeNewsProvider:
    name = "marketaux-google-news"

    def __init__(self, marketaux_token: str | None, timeout: float = 25.0) -> None:
        self.marketaux_token = marketaux_token
        self.timeout = timeout

    def get_task_data(self, module: ModuleConfig, start_at: datetime, end_at: datetime) -> dict[str, Any]:
        articles: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        if module.instruments and self.marketaux_token:
            for instrument in module.instruments:
                query = urllib.parse.urlencode({
                    "symbols": instrument.symbol, "filter_entities": "true", "language": "en", "limit": 3,
                    "published_after": start_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
                    "api_token": self.marketaux_token,
                })
                try:
                    data = json.loads(_get(f"https://api.marketaux.com/v1/news/all?{query}", self.timeout))
                    for item in data.get("data", []):
                        articles.append({
                            "instrument_id": instrument.instrument_id, "headline": item.get("title"),
                            "description": item.get("description"), "published_at": item.get("published_at"),
                            "publisher": item.get("source"), "url": item.get("url"), "provider": "marketaux",
                            "entities": item.get("entities", []),
                        })
                except Exception as exc:
                    errors.append(_error("marketaux", exc))
        elif module.instruments:
            errors.append({"provider": "marketaux", "error_type": "MissingSecret", "message": "marketaux_api_token unavailable"})

        queries = list(module.search_terms_zh) + list(module.search_terms_en)
        for query_text in queries:
            url = "https://news.google.com/rss/search?" + urllib.parse.urlencode({"q": query_text, "hl": "zh-CN", "gl": "HK", "ceid": "HK:zh-Hans"})
            try:
                root = ET.fromstring(_get(url, self.timeout))
                for item in root.findall("./channel/item")[:5]:
                    articles.append({
                        "instrument_id": None, "headline": item.findtext("title"), "description": item.findtext("description"),
                        "published_at": item.findtext("pubDate"), "publisher": item.findtext("source") or "Google News",
                        "url": item.findtext("link"), "provider": "google-news-rss", "query": query_text,
                    })
            except Exception as exc:
                errors.append(_error("google-news-rss", exc))
        return {"provider": self.name, "articles": articles, "errors": errors, "window": {"start_at": start_at.isoformat(), "end_at": end_at.isoformat()}}


class FredMacroDataProvider:
    name = "fred-public"
    SERIES = {"VIXCLS": "VIX", "CPIAUCSL": "美国 CPI", "DFF": "联邦基金有效利率"}

    def __init__(self, timeout: float = 25.0) -> None:
        self.timeout = timeout

    def get_task_data(self, module: ModuleConfig, start_at: datetime, end_at: datetime) -> dict[str, Any]:
        if module.task_id != "macro_market":
            return {"provider": self.name, "observations": [], "relative_metrics": [], "errors": []}
        observations: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        for series_id, label in self.SERIES.items():
            start_date = (end_at - timedelta(days=400)).date().isoformat()
            url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}&cosd={start_date}"
            try:
                rows = list(csv.DictReader(io.StringIO(_get(url, self.timeout).decode("utf-8"))))
                current = next(row for row in reversed(rows) if row.get(series_id) not in {None, "", "."})
                observations.append({"metric_id": series_id.lower(), "label": label, "value": current[series_id], "period": current["DATE"], "url": url, "provider": "fred"})
            except Exception as exc:
                errors.append(_error("fred", exc))
        return {"provider": self.name, "observations": observations, "relative_metrics": [], "errors": errors}


def build_free_provider_bundle(marketaux_token: str | None, timeout: float = 25.0) -> ProviderBundle:
    return ProviderBundle(
        market=FreeMarketDataProvider(timeout),
        news=FreeNewsProvider(marketaux_token, timeout),
        macro=FredMacroDataProvider(timeout),
        calendar=SimpleTradingCalendar(),
    )

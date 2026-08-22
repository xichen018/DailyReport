from __future__ import annotations

import csv
import io
import json
import re
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from email.utils import parsedate_to_datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from app.modules.loader import ModuleConfig
from app.providers.base import ProviderBundle
from app.providers.calendar import MARKET_RULES, SimpleTradingCalendar


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


def _iso_published_at(value: str | None) -> str | None:
    if not value:
        return None
    try:
        if re.fullmatch(r"\d{8}T\d{6}Z", value):
            parsed = datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        else:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        parsed = parsedate_to_datetime(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


class FreeMarketDataProvider:
    name = "free-market-multiprovider"

    def __init__(self, timeout: float = 25.0, calendar: SimpleTradingCalendar | None = None) -> None:
        self.timeout = timeout
        self.calendar = calendar or SimpleTradingCalendar()

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

    def _binance_30h(self) -> list[dict[str, Any]]:
        url = "https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1h&limit=31"
        rows = json.loads(_get(url, self.timeout))
        if len(rows) < 31:
            raise ValueError("insufficient Binance hourly bars for 30h change")
        return [{
            "instrument_id": "bitcoin_binance", "symbol": "BTCUSDT", "kind": "rolling_30h",
            "value": rows[-1][4], "previous_value": rows[0][4], "currency": "USDT",
            "as_of": datetime.fromtimestamp(rows[-1][6] / 1000, timezone.utc).isoformat(),
            "source_url": url, "provider": "binance",
        }]

    @staticmethod
    def _rsi(closes: list[Decimal], period: int = 14) -> Decimal:
        changes = [current - previous for previous, current in zip(closes[-period - 1:-1], closes[-period:])]
        gains = sum((change for change in changes if change > 0), Decimal("0")) / period
        losses = sum((-change for change in changes if change < 0), Decimal("0")) / period
        if losses == 0:
            return Decimal("100")
        return Decimal("100") - Decimal("100") / (Decimal("1") + gains / losses)

    @staticmethod
    def _structural_levels(
        closes: list[Decimal],
        highs: list[Decimal],
        lows: list[Decimal],
    ) -> tuple[list[Decimal], list[Decimal]]:
        """Return nearest confirmed support and resistance candidates."""
        current = closes[-1]
        start = max(2, len(closes) - 62)
        pivot_highs = [
            highs[index]
            for index in range(start, len(closes) - 2)
            if highs[index] == max(highs[index - 2:index + 3])
        ]
        pivot_lows = [
            lows[index]
            for index in range(start, len(closes) - 2)
            if lows[index] == min(lows[index - 2:index + 3])
        ]
        averages = [sum(closes[-period:]) / period for period in (20, 50) if len(closes) >= period]
        supports = [*pivot_lows, min(lows[-30:]), *(value for value in averages if value < current)]
        resistances = [*pivot_highs, max(highs[-30:]), *(value for value in averages if value > current)]

        def nearest(values: list[Decimal], reverse: bool) -> list[Decimal]:
            ordered = sorted(
                {value for value in values if (value < current if reverse else value > current)},
                reverse=reverse,
            )
            selected: list[Decimal] = []
            for value in ordered:
                if not selected or abs(value - selected[-1]) / current >= Decimal("0.005"):
                    selected.append(value)
                if len(selected) == 2:
                    break
            return selected

        return nearest(supports, True), nearest(resistances, False)

    def _binance_structure(self) -> list[dict[str, Any]]:
        url = "https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1d&limit=220"
        rows = json.loads(_get(url, self.timeout))
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        rows = [row for row in rows if int(row[6]) <= now_ms]
        if len(rows) < 201:
            raise ValueError("insufficient Binance daily bars for BTC structure")
        closes = [Decimal(str(row[4])) for row in rows]
        highs = [Decimal(str(row[2])) for row in rows]
        lows = [Decimal(str(row[3])) for row in rows]
        volumes = [Decimal(str(row[5])) for row in rows]
        as_of = datetime.fromtimestamp(rows[-1][6] / 1000, timezone.utc).isoformat()
        signals = []

        def add(metric_id: str, label: str, value: Decimal, unit: str) -> None:
            signals.append({
                "metric_id": metric_id, "instrument_id": "bitcoin_binance", "label": label,
                "value": str(value.quantize(Decimal("0.01"))), "unit": unit, "as_of": as_of,
                "source_url": url, "provider": "binance-klines",
            })

        for period in (20, 50, 200):
            add(f"btc_sma_{period}d", f"BTC {period}日简单移动均线", sum(closes[-period:]) / period, "USDT")
        add("btc_rsi_14d", "BTC 14日 RSI", self._rsi(closes), "index")
        add("btc_30d_high", "BTC 近30日最高收盘", max(closes[-30:]), "USDT")
        add("btc_30d_low", "BTC 近30日最低收盘", min(closes[-30:]), "USDT")
        average_volume = sum(volumes[-21:-1]) / 20
        add("btc_volume_vs_20d", "BTC 当日成交量相对20日均量", volumes[-1] / average_volume, "ratio")
        supports, resistances = self._structural_levels(closes, highs, lows)
        for index, value in enumerate(supports, start=1):
            add(f"btc_structural_support_{index}", f"BTC 结构支撑 {index}", value, "USDT")
        for index, value in enumerate(resistances, start=1):
            add(f"btc_structural_resistance_{index}", f"BTC 结构压力 {index}", value, "USDT")
        return signals

    def _binance_derivatives(self) -> list[dict[str, Any]]:
        funding_url = "https://fapi.binance.com/fapi/v1/fundingRate?symbol=BTCUSDT&limit=3"
        open_interest_url = "https://fapi.binance.com/fapi/v1/openInterest?symbol=BTCUSDT"
        funding = json.loads(_get(funding_url, self.timeout))
        open_interest = json.loads(_get(open_interest_url, self.timeout))
        latest = funding[-1]
        return [
            {
                "metric_id": "btc_perp_funding", "instrument_id": "bitcoin_binance", "label": "BTCUSDT 永续资金费率",
                "value": str((Decimal(str(latest["fundingRate"])) * 100).quantize(Decimal("0.0001"))), "unit": "%",
                "as_of": datetime.fromtimestamp(latest["fundingTime"] / 1000, timezone.utc).isoformat(),
                "source_url": funding_url, "provider": "binance-futures",
            },
            {
                "metric_id": "btc_perp_open_interest", "instrument_id": "bitcoin_binance", "label": "BTCUSDT 永续未平仓量",
                "value": str(Decimal(str(open_interest["openInterest"]))), "unit": "BTC",
                "as_of": datetime.fromtimestamp(open_interest["time"] / 1000, timezone.utc).isoformat(),
                "source_url": open_interest_url, "provider": "binance-futures",
            },
        ]

    def _coingecko(self) -> list[dict[str, Any]]:
        url = "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd&include_24hr_change=true&include_last_updated_at=true"
        item = json.loads(_get(url, self.timeout))["bitcoin"]
        value = float(item["usd"])
        change_pct = float(item["usd_24h_change"])
        previous = value / (1 + change_pct / 100)
        return [{
            "instrument_id": "bitcoin_binance", "symbol": "BTCUSDT", "kind": "crosscheck_24h",
            "value": str(value), "previous_value": str(previous), "change_pct": str(change_pct), "currency": "USD",
            "as_of": datetime.fromtimestamp(item["last_updated_at"], timezone.utc).isoformat(),
            "source_url": url, "provider": "coingecko",
        }]

    def _yahoo(
        self,
        instrument_id: str,
        symbol: str,
        currency: str,
        closed_session: date | None = None,
        market_timezone: ZoneInfo | None = None,
    ) -> list[dict[str, Any]]:
        remote = YAHOO_SYMBOLS.get(symbol, symbol)
        encoded = urllib.parse.quote(remote, safe="")
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{encoded}?range=5d&interval=1d&events=history"
        result = json.loads(_get(url, self.timeout))["chart"]["result"][0]
        timestamps = result["timestamp"]
        closes = result["indicators"]["quote"][0]["close"]
        valid = [(ts, value) for ts, value in zip(timestamps, closes) if value is not None]
        if closed_session is not None and market_timezone is not None:
            valid = [
                (ts, value)
                for ts, value in valid
                if datetime.fromtimestamp(ts, timezone.utc).astimezone(market_timezone).date() <= closed_session
            ]
        if len(valid) < 2:
            raise ValueError(f"insufficient Yahoo closes for {symbol}")
        previous, current = valid[-2], valid[-1]
        current_at = datetime.fromtimestamp(current[0], timezone.utc)
        previous_at = datetime.fromtimestamp(previous[0], timezone.utc)
        session_timezone = market_timezone or timezone.utc
        return [{
            "instrument_id": instrument_id, "symbol": symbol, "kind": "close",
            "value": str(current[1]), "previous_value": str(previous[1]), "currency": currency,
            "as_of": current_at.isoformat(), "session_date": current_at.astimezone(session_timezone).date().isoformat(),
            "previous_as_of": previous_at.isoformat(), "previous_session_date": previous_at.astimezone(session_timezone).date().isoformat(),
            "source_url": url, "provider": "yahoo-chart",
        }]

    def _yahoo_structure(
        self,
        instrument_id: str,
        symbol: str,
        currency: str,
        closed_session: date,
        market_timezone: ZoneInfo,
    ) -> list[dict[str, Any]]:
        remote = YAHOO_SYMBOLS.get(symbol, symbol)
        encoded = urllib.parse.quote(remote, safe="")
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{encoded}?range=1y&interval=1d&events=history"
        result = json.loads(_get(url, self.timeout))["chart"]["result"][0]
        quotes = result["indicators"]["quote"][0]
        quote_highs = quotes.get("high") or quotes["close"]
        quote_lows = quotes.get("low") or quotes["close"]
        bars = [
            (ts, Decimal(str(close)), Decimal(str(high)), Decimal(str(low)), Decimal(str(volume or 0)))
            for ts, close, high, low, volume in zip(
                result["timestamp"], quotes["close"], quote_highs, quote_lows, quotes.get("volume", [])
            )
            if close is not None and high is not None and low is not None
            and datetime.fromtimestamp(ts, timezone.utc).astimezone(market_timezone).date() <= closed_session
        ]
        if len(bars) < 31:
            raise ValueError(f"insufficient Yahoo daily bars for structure: {symbol}")
        closes = [item[1] for item in bars]
        highs = [item[2] for item in bars]
        lows = [item[3] for item in bars]
        volumes = [item[4] for item in bars]
        as_of = datetime.fromtimestamp(bars[-1][0], timezone.utc).isoformat()
        signals: list[dict[str, Any]] = []

        def add(suffix: str, label: str, value: Decimal, unit: str) -> None:
            signals.append({
                "metric_id": f"{instrument_id}_{suffix}", "instrument_id": instrument_id, "label": label,
                "value": str(value.quantize(Decimal("0.01"))), "unit": unit, "as_of": as_of,
                "source_url": url, "provider": "yahoo-chart-structure",
            })

        for period in (20, 50, 200):
            if len(closes) >= period:
                add(f"sma_{period}d", f"{symbol} {period}日简单移动均线", sum(closes[-period:]) / period, currency)
        add("rsi_14d", f"{symbol} 14日 RSI", self._rsi(closes), "index")
        add("30d_high", f"{symbol} 近30日最高收盘", max(closes[-30:]), currency)
        add("30d_low", f"{symbol} 近30日最低收盘", min(closes[-30:]), currency)
        average_volume = sum(volumes[-21:-1]) / 20
        if average_volume > 0:
            add("volume_vs_20d", f"{symbol} 当日成交量相对20日均量", volumes[-1] / average_volume, "ratio")
        supports, resistances = self._structural_levels(closes, highs, lows)
        for index, value in enumerate(supports, start=1):
            add(f"structural_support_{index}", f"{symbol} 结构支撑 {index}", value, currency)
        for index, value in enumerate(resistances, start=1):
            add(f"structural_resistance_{index}", f"{symbol} 结构压力 {index}", value, currency)
        return signals

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
        signals: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        for instrument in module.instruments:
            if instrument.symbol == "BTCUSDT":
                try:
                    records.extend(self._binance())
                except Exception as exc:
                    errors.append(_error("binance", exc))
                try:
                    records.extend(self._binance_30h())
                except Exception as exc:
                    errors.append(_error("binance-30h", exc))
                try:
                    records.extend(self._coingecko())
                except Exception as exc:
                    errors.append(_error("coingecko", exc))
                try:
                    signals.extend(self._binance_structure())
                except Exception as exc:
                    errors.append(_error("binance-structure", exc))
                try:
                    signals.extend(self._binance_derivatives())
                except Exception as exc:
                    errors.append(_error("binance-derivatives", exc))
            try:
                calendar_name = "HKEX" if instrument.exchange == "HKEX" else "NYMEX" if instrument.exchange in {"NYMEX", "COMEX"} else "US"
                closed_session = None if instrument.asset_class == "crypto" else self.calendar.latest_closed_session(calendar_name, as_of)
                market_timezone = None if instrument.asset_class == "crypto" else MARKET_RULES[calendar_name][0]
                records.extend(self._yahoo(
                    instrument.instrument_id,
                    instrument.symbol,
                    instrument.currency,
                    closed_session,
                    market_timezone,
                ))
                if instrument.asset_class != "crypto" and closed_session is not None and market_timezone is not None:
                    signals.extend(self._yahoo_structure(
                        instrument.instrument_id,
                        instrument.symbol,
                        instrument.currency,
                        closed_session,
                        market_timezone,
                    ))
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
        return {"provider": self.name, "records": records, "signals": signals, "errors": errors, "as_of": as_of.isoformat()}


class FreeNewsProvider:
    name = "google-news-gdelt-marketaux"

    def __init__(self, marketaux_token: str | None, timeout: float = 25.0) -> None:
        self.marketaux_token = marketaux_token
        self.timeout = timeout

    def get_task_data(self, module: ModuleConfig, start_at: datetime, end_at: datetime) -> dict[str, Any]:
        articles: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        optional_errors: list[dict[str, str]] = []
        queries: list[dict[str, Any]] = []
        if module.instruments and self.marketaux_token:
            for instrument in module.instruments:
                query = urllib.parse.urlencode({
                    "symbols": instrument.symbol, "filter_entities": "true", "language": "en", "limit": 3,
                    "published_after": start_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
                    "api_token": self.marketaux_token,
                })
                try:
                    data = json.loads(_get(f"https://api.marketaux.com/v1/news/all?{query}", self.timeout))
                    returned = data.get("data", [])
                    queries.append({"provider": "marketaux", "symbol": instrument.symbol, "status": "success", "returned": len(returned)})
                    for item in returned:
                        articles.append({
                            "instrument_id": instrument.instrument_id, "headline": item.get("title"),
                            "description": item.get("description"), "published_at": _iso_published_at(item.get("published_at")),
                            "publisher": item.get("source"), "url": item.get("url"), "provider": "marketaux",
                            "entities": item.get("entities", []),
                        })
                except Exception as exc:
                    optional_errors.append(_error("marketaux", exc))
                    queries.append({"provider": "marketaux", "symbol": instrument.symbol, "status": "failed", "returned": 0})
        elif module.instruments:
            queries.append({"provider": "marketaux", "status": "skipped", "reason": "token unavailable"})

        if module.instruments:
            terms = [
                (instrument.aliases[0] if instrument.aliases else instrument.name)
                for instrument in module.instruments
            ]
            query_text = " OR ".join(f'\"{term}\"' for term in terms)
            query = urllib.parse.urlencode({
                "query": query_text,
                "mode": "ArtList",
                "maxrecords": 25,
                "format": "json",
                "sort": "DateDesc",
                "startdatetime": start_at.astimezone(timezone.utc).strftime("%Y%m%d%H%M%S"),
                "enddatetime": end_at.astimezone(timezone.utc).strftime("%Y%m%d%H%M%S"),
            })
            url = f"https://api.gdeltproject.org/api/v2/doc/doc?{query}"
            try:
                returned = json.loads(_get(url, self.timeout)).get("articles", [])
                queries.append({"provider": "gdelt", "scope": module.task_id, "status": "success", "returned": len(returned)})
                for item in returned:
                    articles.append({
                        "instrument_id": None,
                        "headline": item.get("title"),
                        "description": None,
                        "published_at": _iso_published_at(item.get("seendate")),
                        "publisher": item.get("domain") or "GDELT",
                        "url": item.get("url"),
                        "provider": "gdelt-doc-2",
                        "language": item.get("language"),
                    })
            except Exception as exc:
                optional_errors.append(_error("gdelt", exc))
                queries.append({"provider": "gdelt", "scope": module.task_id, "status": "failed", "returned": 0})

        search_queries = [
            *((query_text, "zh", False, False) for query_text in module.search_terms_zh),
            *((query_text, "en", False, False) for query_text in module.search_terms_en),
            *((query_text, "zh", True, False) for query_text in module.background_search_terms_zh),
            *((query_text, "en", True, False) for query_text in module.background_search_terms_en),
            *((query_text, "zh", False, True) for query_text in module.upcoming_event_terms_zh),
            *((query_text, "en", False, True) for query_text in module.upcoming_event_terms_en),
        ]
        for query_text, language, background_candidate, upcoming_candidate in search_queries:
            lookback_days = 14 if background_candidate or upcoming_candidate else 2
            windowed_query = f"({query_text}) when:{lookback_days}d"
            locale = (
                {"hl": "en-US", "gl": "US", "ceid": "US:en"}
                if language == "en"
                else {"hl": "zh-CN", "gl": "HK", "ceid": "HK:zh-Hans"}
            )
            url = "https://news.google.com/rss/search?" + urllib.parse.urlencode({"q": windowed_query, **locale})
            try:
                root = ET.fromstring(_get(url, self.timeout))
                returned = root.findall("./channel/item")[:15]
                queries.append({
                    "provider": "google-news-rss", "query": query_text, "language": language,
                    "background": background_candidate, "upcoming": upcoming_candidate,
                    "status": "success", "returned": len(returned),
                })
                for item in returned:
                    articles.append({
                        "instrument_id": None, "headline": item.findtext("title"), "description": item.findtext("description"),
                        "published_at": _iso_published_at(item.findtext("pubDate")), "publisher": item.findtext("source") or "Google News",
                        "url": item.findtext("link"), "provider": "google-news-rss", "query": query_text,
                        "language": language, "background_candidate": background_candidate,
                        "upcoming_candidate": upcoming_candidate,
                    })
            except Exception as exc:
                errors.append(_error("google-news-rss", exc))
                queries.append({
                    "provider": "google-news-rss", "query": query_text, "language": language,
                    "background": background_candidate, "upcoming": upcoming_candidate,
                    "status": "failed", "returned": 0,
                })
        window_articles = []
        for article in articles:
            published_at = article.get("published_at")
            if not published_at:
                continue
            published = datetime.fromisoformat(str(published_at).replace("Z", "+00:00"))
            article_start = end_at - timedelta(days=14) if article.get("background_candidate") or article.get("upcoming_candidate") else start_at
            if article_start <= published.astimezone(start_at.tzinfo) <= end_at:
                window_articles.append(article)
        return {
            "provider": self.name,
            "articles": window_articles,
            "queries": queries,
            "errors": errors,
            "optional_errors": optional_errors,
            "window": {"start_at": start_at.isoformat(), "end_at": end_at.isoformat()},
        }


class FredMacroDataProvider:
    name = "fred-public"
    SERIES = {"VIXCLS": "VIX", "CPIAUCSL": "美国 CPI", "DFF": "联邦基金有效利率"}
    BTC_LIQUIDITY_SERIES = {"DFF": "联邦基金有效利率", "DGS10": "美国10年期国债收益率", "DTWEXBGS": "广义美元指数"}
    SERIES_UNITS = {"VIXCLS": "index", "CPIAUCSL": "index", "DFF": "%", "DGS10": "%", "DTWEXBGS": "index"}

    def __init__(self, timeout: float = 25.0) -> None:
        self.timeout = timeout

    def _index_history(self, symbol: str) -> tuple[list[tuple[int, float]], str]:
        encoded = urllib.parse.quote(symbol, safe="")
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{encoded}?range=1y&interval=1d&events=history"
        result = json.loads(_get(url, self.timeout))["chart"]["result"][0]
        closes = result["indicators"]["quote"][0]["close"]
        values = [(ts, float(value)) for ts, value in zip(result["timestamp"], closes) if value is not None]
        if len(values) < 2:
            raise ValueError(f"insufficient index history for {symbol}")
        return values, url

    @staticmethod
    def _nearest(values: list[tuple[int, float]], target: datetime) -> tuple[int, float]:
        target_ts = int(target.timestamp())
        return min(values, key=lambda item: abs(item[0] - target_ts))

    def get_task_data(self, module: ModuleConfig, start_at: datetime, end_at: datetime) -> dict[str, Any]:
        if module.task_id not in {"macro_market", "cross_asset"}:
            return {"provider": self.name, "observations": [], "relative_metrics": [], "errors": []}
        observations: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        series = self.SERIES if module.task_id == "macro_market" else self.BTC_LIQUIDITY_SERIES
        for series_id, label in series.items():
            start_date = (end_at - timedelta(days=400)).date().isoformat()
            url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}&cosd={start_date}"
            try:
                rows = list(csv.DictReader(io.StringIO(_get(url, self.timeout).decode("utf-8"))))
                current = next(row for row in reversed(rows) if row.get(series_id) not in {None, "", "."})
                date_field = "observation_date" if "observation_date" in current else "DATE"
                observations.append({"metric_id": series_id.lower(), "label": label, "value": current[series_id], "unit": self.SERIES_UNITS[series_id], "period": current[date_field], "url": url, "provider": "fred"})
            except Exception as exc:
                errors.append(_error("fred", exc))

        relative_metrics: list[dict[str, Any]] = []
        if module.task_id == "cross_asset":
            return {"provider": self.name, "observations": observations, "relative_metrics": [], "errors": errors}
        try:
            histories = {}
            urls = {}
            for symbol in ("^IXIC", "^SOX", "^NDX"):
                histories[symbol], urls[symbol] = self._index_history(symbol)
            for symbol, metric_id, label in (
                ("^IXIC", "nasdaq_daily_change", "纳斯达克综合指数日涨跌幅"),
                ("^SOX", "sox_daily_change", "费城半导体指数日涨跌幅"),
            ):
                previous, current = histories[symbol][-2], histories[symbol][-1]
                change_pct = (current[1] - previous[1]) / previous[1] * 100
                observations.append({
                    "metric_id": metric_id, "label": label, "value": str(round(change_pct, 4)), "unit": "%",
                    "period": datetime.fromtimestamp(current[0], timezone.utc).date().isoformat(),
                    "url": urls[symbol], "provider": "yahoo-chart",
                })
            labels = [
                ("current", end_at),
                ("one_month_ago", end_at - timedelta(days=30)),
                ("three_months_ago", end_at - timedelta(days=90)),
                ("year_start", datetime(end_at.year, 1, 1, tzinfo=end_at.tzinfo)),
            ]
            ratio_observations = []
            for label, target in labels:
                if label == "current":
                    sox = histories["^SOX"][-1]
                    ndx = histories["^NDX"][-1]
                elif label == "year_start":
                    first_session = date(end_at.year, 1, 1)
                    sox = next(
                        item for item in histories["^SOX"]
                        if datetime.fromtimestamp(item[0], timezone.utc).date() >= first_session
                    )
                    ndx = next(
                        item for item in histories["^NDX"]
                        if datetime.fromtimestamp(item[0], timezone.utc).date() >= first_session
                    )
                else:
                    sox = self._nearest(histories["^SOX"], target)
                    ndx = self._nearest(histories["^NDX"], target)
                ratio_observations.append({
                    "label": label,
                    "as_of": datetime.fromtimestamp(sox[0], timezone.utc).date().isoformat(),
                    "numerator_value": str(sox[1]),
                    "denominator_value": str(ndx[1]),
                    "ratio": str((Decimal(str(sox[1])) / Decimal(str(ndx[1]))).quantize(Decimal("0.0001"))),
                })
            relative_metrics.append({
                "metric_id": "sox_ndx_ratio", "numerator": "SOX", "denominator": "NDX",
                "observations": ratio_observations,
                "url": urls["^SOX"], "secondary_url": urls["^NDX"], "provider": "yahoo-chart",
            })
        except Exception as exc:
            errors.append(_error("yahoo-index-history", exc))
        return {"provider": self.name, "observations": observations, "relative_metrics": relative_metrics, "errors": errors}


def build_free_provider_bundle(marketaux_token: str | None, timeout: float = 25.0) -> ProviderBundle:
    calendar = SimpleTradingCalendar()
    return ProviderBundle(
        market=FreeMarketDataProvider(timeout, calendar),
        news=FreeNewsProvider(marketaux_token, timeout),
        macro=FredMacroDataProvider(timeout),
        calendar=calendar,
    )

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
from html.parser import HTMLParser
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from app.modules.loader import ModuleConfig
from app.providers.base import ProviderBundle
from app.providers.calendar import MARKET_RULES, SimpleTradingCalendar
from app.research.sources import source_policy


USER_AGENT = "DailyReport/0.2 (+financial-research; contact=operator)"
YAHOO_SYMBOLS = {"BTCUSDT": "BTC-USD", "CL1": "CL=F"}
STOOQ_SYMBOLS = {
    "MU": "mu.us", "COHR": "cohr.us", "GOOG": "goog.us", "DJT": "djt.us",
    "CRWD": "crwd.us", "1772.HK": "1772.hk", "6166.HK": "6166.hk", "CL1": "cl.f",
}
SEC_CIKS = {
    "CRWD": "0001535527",
    "GOOG": "0001652044",
    "DJT": "0001849635",
    "MU": "0000723125",
    "COHR": "0000820318",
}
SEC_FACTS = {
    "revenue": ("收入", ("RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "SalesRevenueNet")),
    "gross_profit": ("毛利润", ("GrossProfit",)),
    "operating_income": ("营业利润", ("OperatingIncomeLoss",)),
    "net_income": ("净利润", ("NetIncomeLoss",)),
    "operating_cash_flow": ("经营现金流", ("NetCashProvidedByUsedInOperatingActivities",)),
    "capex": ("资本开支", ("PaymentsToAcquirePropertyPlantAndEquipment",)),
    "assets": ("总资产", ("Assets",)),
    "equity": ("股东权益", ("StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest")),
}
SEC_INSTANT_METRICS = {"assets", "equity"}
OFFICIAL_COMPANY_EVENTS = ({
    "task_id": "cybersecurity",
    "instrument_id": "crowdstrike",
    "title": "CrowdStrike Fiscal Second Quarter 2027 Results and Conference Call",
    "published_at": "2026-08-04T00:00:00-04:00",
    "event_at": "2026-08-27T05:00:00+08:00",
    "original_timezone": "America/New_York",
    "original_time_label": "2026-08-26 17:00 EDT",
    "publisher": "CrowdStrike Investor Relations",
    "provider": "company-ir-calendar",
    "url": "https://ir.crowdstrike.com/news-releases/news-release-details/crowdstrike-announces-date-fiscal-second-quarter-2027-financial",
},)
EIA_WPSR_TABLE1_URL = "https://ir.eia.gov/wpsr/table1.csv"
EIA_STOCK_ROWS = {
    "Commercial (Excluding SPR)": ("eia_commercial_crude_stocks", "EIA美国商业原油库存（不含SPR）"),
    "Total Motor Gasoline": ("eia_motor_gasoline_stocks", "EIA美国车用汽油库存"),
    "Distillate Fuel Oil": ("eia_distillate_stocks", "EIA美国馏分油库存"),
}


class _ScheduleTableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.rows: list[list[tuple[str, str]]] = []
        self._row: list[tuple[str, str]] | None = None
        self._cell_class = ""
        self._cell_parts: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "tr":
            self._row = []
        elif tag == "td" and self._row is not None:
            self._cell_class = attributes.get("class") or ""
            self._cell_parts = []

    def handle_data(self, data: str) -> None:
        if self._cell_parts is not None and data.strip():
            self._cell_parts.append(data.strip())

    def handle_endtag(self, tag: str) -> None:
        if tag == "td" and self._row is not None and self._cell_parts is not None:
            self._row.append((self._cell_class, " ".join(self._cell_parts)))
            self._cell_parts = None
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None


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

    def _eia_petroleum_stocks(self) -> list[dict[str, Any]]:
        rows = csv.reader(io.StringIO(_get(EIA_WPSR_TABLE1_URL, self.timeout).decode("cp1252")))
        header: list[str] | None = None
        selected: dict[str, list[str]] = {}
        for row in rows:
            if not row:
                continue
            if row[0].strip() == "STUB_1":
                if header is not None:
                    break
                if len(row) < 8 or not re.fullmatch(r"\d{1,2}/\d{1,2}/\d{2}", row[1].strip()):
                    continue
                header = [item.strip() for item in row]
                continue
            if header is not None and row[0].strip() in EIA_STOCK_ROWS:
                selected[row[0].strip()] = row

        missing = set(EIA_STOCK_ROWS) - set(selected)
        if header is None or missing:
            raise ValueError(f"incomplete EIA WPSR stock section: {sorted(missing)}")
        report_week = datetime.strptime(header[1], "%m/%d/%y").date().isoformat()
        observations: list[dict[str, Any]] = []
        for row_name, (metric_id, label) in EIA_STOCK_ROWS.items():
            row = selected[row_name]
            if len(row) < 8:
                raise ValueError(f"incomplete EIA WPSR row: {row_name}")
            values = [item.strip().replace(",", "") for item in row[1:8]]
            try:
                for value in values:
                    Decimal(value)
            except Exception as exc:
                raise ValueError(f"invalid EIA WPSR value: {row_name}") from exc
            observations.append({
                "metric_id": metric_id,
                "instrument_id": "wti_front_month",
                "label": label,
                "value": values[0],
                "previous_value": values[1],
                "change_value": values[2],
                "change_pct": values[3],
                "prior_year_value": values[4],
                "yoy_change_value": values[5],
                "yoy_change_pct": values[6],
                "unit": "million_barrels",
                "as_of": f"{report_week}T00:00:00+00:00",
                "report_week": report_week,
                "source_url": EIA_WPSR_TABLE1_URL,
                "provider": "eia-wpsr",
            })
        return observations

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

    @staticmethod
    def _sec_quarterly_entries(
        units: dict[str, list[dict[str, Any]]],
        *,
        instant: bool,
        as_of: datetime,
    ) -> list[dict[str, Any]]:
        entries = units.get("USD", [])
        selected: dict[tuple[str, str], dict[str, Any]] = {}
        frame_pattern = re.compile(r"^CY\d{4}Q[1-4]I$" if instant else r"^CY\d{4}Q[1-4]$")
        for entry in entries:
            if entry.get("form") not in {"10-Q", "10-K"}:
                continue
            filed = str(entry.get("filed") or "")
            if not filed or date.fromisoformat(filed) > as_of.date():
                continue
            normalized = dict(entry)
            if instant:
                if not frame_pattern.fullmatch(str(entry.get("frame") or "")):
                    continue
                normalized["period_basis"] = "instant"
            else:
                start, end = entry.get("start"), entry.get("end")
                if not start or not end:
                    continue
                duration = (date.fromisoformat(str(end)) - date.fromisoformat(str(start))).days
                if not 60 <= duration <= 120:
                    continue
                frame = str(entry.get("frame") or "")
                if frame_pattern.fullmatch(frame):
                    normalized["period_basis"] = "reported_quarter"
                elif entry.get("form") == "10-Q" and entry.get("fy") is not None and entry.get("fp") in {"Q1", "Q2", "Q3"}:
                    normalized["frame"] = f"FY{entry.get('fy')}{entry.get('fp')}"
                    normalized["period_basis"] = "reported_quarter_fiscal"
                else:
                    continue
            key = (str(normalized.get("frame")), str(normalized.get("end")))
            current = selected.get(key)
            if current is None or (filed, str(entry.get("accn") or "")) > (
                str(current.get("filed") or ""), str(current.get("accn") or "")
            ):
                selected[key] = normalized
        return sorted(selected.values(), key=lambda item: (str(item.get("end")), str(item.get("filed"))))

    @staticmethod
    def _sec_derived_quarterly_entries(
        units: dict[str, list[dict[str, Any]]],
        *,
        as_of: datetime,
    ) -> list[dict[str, Any]]:
        eligible = []
        for entry in units.get("USD", []):
            if entry.get("form") != "10-Q" or entry.get("fp") not in {"Q2", "Q3"}:
                continue
            filed, start, end = str(entry.get("filed") or ""), entry.get("start"), entry.get("end")
            if not filed or not start or not end or date.fromisoformat(filed) > as_of.date():
                continue
            duration = (date.fromisoformat(str(end)) - date.fromisoformat(str(start))).days
            if duration <= 120:
                continue
            eligible.append(entry)

        derived: list[dict[str, Any]] = []
        all_entries = units.get("USD", [])
        for cumulative in eligible:
            candidates = []
            for prior in all_entries:
                if (
                    prior.get("start") != cumulative.get("start")
                    or prior.get("fy") != cumulative.get("fy")
                    or prior.get("form") != "10-Q"
                    or str(prior.get("end") or "") >= str(cumulative.get("end") or "")
                    or date.fromisoformat(str(prior.get("filed") or "9999-12-31")) > as_of.date()
                ):
                    continue
                gap = (date.fromisoformat(str(cumulative["end"])) - date.fromisoformat(str(prior["end"]))).days
                if 60 <= gap <= 120:
                    candidates.append(prior)
            if not candidates:
                continue
            prior = max(candidates, key=lambda item: (str(item.get("end")), str(item.get("filed") or "")))
            item = dict(cumulative)
            item["val"] = Decimal(str(cumulative["val"])) - Decimal(str(prior["val"]))
            item["start"] = (date.fromisoformat(str(prior["end"])) + timedelta(days=1)).isoformat()
            item["frame"] = str(cumulative.get("frame") or f"FY{cumulative.get('fy')}{cumulative.get('fp')}")
            item["period_basis"] = "derived_quarter_from_ytd"
            item["derived_from"] = [str(prior.get("accn") or ""), str(cumulative.get("accn") or "")]
            derived.append(item)
        return derived

    def _sec_fundamentals(
        self,
        instrument_id: str,
        symbol: str,
        as_of: datetime,
    ) -> list[dict[str, Any]]:
        cik = SEC_CIKS[symbol]
        url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
        payload = json.loads(_get(url, self.timeout))
        us_gaap = payload.get("facts", {}).get("us-gaap", {})
        observations: list[dict[str, Any]] = []
        latest_by_metric: dict[str, dict[str, Any]] = {}

        for metric_id, (label, tags) in SEC_FACTS.items():
            entries: list[dict[str, Any]] = []
            for tag in tags:
                fact = us_gaap.get(tag)
                if not fact:
                    continue
                tag_entries = self._sec_quarterly_entries(
                    fact.get("units", {}),
                    instant=metric_id in SEC_INSTANT_METRICS,
                    as_of=as_of,
                )
                if metric_id in {"operating_cash_flow", "capex"}:
                    tag_entries += self._sec_derived_quarterly_entries(fact.get("units", {}), as_of=as_of)
                for entry in tag_entries:
                    entries.append({**entry, "_tag": tag})
            if not entries:
                continue
            deduplicated: dict[tuple[str, str], dict[str, Any]] = {}
            for entry in entries:
                key = (str(entry.get("frame")), str(entry.get("end")))
                current = deduplicated.get(key)
                if current is None or (str(entry.get("filed") or ""), str(entry.get("accn") or "")) > (
                    str(current.get("filed") or ""), str(current.get("accn") or "")
                ):
                    deduplicated[key] = entry
            entries = sorted(deduplicated.values(), key=lambda item: (str(item.get("end")), str(item.get("filed"))))
            latest = entries[-1]
            latest_frame = str(latest["frame"])
            year_match = re.search(r"CY(\d{4})Q([1-4])", latest_frame)
            prior = None
            if year_match:
                suffix = "I" if metric_id in SEC_INSTANT_METRICS else ""
                prior_frame = f"CY{int(year_match.group(1)) - 1}Q{year_match.group(2)}{suffix}"
                prior = next((entry for entry in reversed(entries) if entry.get("frame") == prior_frame), None)
            elif latest.get("fy") is not None and latest.get("fp"):
                prior = next((
                    entry for entry in reversed(entries)
                    if str(entry.get("fy")) == str(int(latest.get("fy")) - 1)
                    and entry.get("fp") == latest.get("fp")
                    and entry.get("period_basis") == latest.get("period_basis")
                ), None)
            value = Decimal(str(latest["val"]))
            prior_value = Decimal(str(prior["val"])) if prior is not None else None
            change_pct = None
            if prior_value not in {None, Decimal("0")}:
                change_pct = ((value - prior_value) / abs(prior_value) * 100).quantize(Decimal("0.01"))
            filing_url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{str(latest.get('accn', '')).replace('-', '')}/"
            observation = {
                "metric_id": f"sec_{metric_id}", "instrument_id": instrument_id, "symbol": symbol,
                "label": label, "value": str(value), "unit": "USD", "period_start": latest.get("start"),
                "period_end": latest.get("end"), "frame": latest_frame, "form": latest.get("form"),
                "fiscal_year": latest.get("fy"), "fiscal_period": latest.get("fp"),
                "filed_at": latest.get("filed"), "accession": latest.get("accn"), "tag": latest.get("_tag"),
                "period_basis": latest.get("period_basis"), "derived_from": latest.get("derived_from", []),
                "prior_year_value": str(prior_value) if prior_value is not None else None,
                "prior_year_period_end": prior.get("end") if prior is not None else None,
                "change_pct": str(change_pct) if change_pct is not None else None,
                "source_url": url, "filing_url": filing_url, "provider": "sec-companyfacts",
            }
            observations.append(observation)
            latest_by_metric[metric_id] = observation

        revenue = latest_by_metric.get("revenue")
        operating_income = latest_by_metric.get("operating_income")
        if revenue and operating_income and revenue["period_end"] == operating_income["period_end"] and revenue["period_basis"] == operating_income["period_basis"]:
            revenue_value = Decimal(revenue["value"])
            if revenue_value:
                observations.append({
                    "metric_id": "sec_operating_margin", "instrument_id": instrument_id, "symbol": symbol,
                    "label": "营业利润率", "value": str((Decimal(operating_income["value"]) / revenue_value * 100).quantize(Decimal("0.01"))),
                    "unit": "%", "period_end": revenue["period_end"], "form": revenue["form"],
                    "filed_at": revenue["filed_at"], "source_url": url, "filing_url": revenue["filing_url"],
                    "provider": "sec-companyfacts-derived", "derived_from": ["sec_revenue", "sec_operating_income"],
                    "period_basis": revenue["period_basis"],
                })
        operating_cash_flow = latest_by_metric.get("operating_cash_flow")
        capex = latest_by_metric.get("capex")
        if operating_cash_flow and capex and operating_cash_flow["period_end"] == capex["period_end"] and operating_cash_flow["period_basis"] == capex["period_basis"]:
            observations.append({
                "metric_id": "sec_free_cash_flow", "instrument_id": instrument_id, "symbol": symbol,
                "label": "自由现金流", "value": str(Decimal(operating_cash_flow["value"]) - Decimal(capex["value"])),
                "unit": "USD", "period_end": operating_cash_flow["period_end"], "form": operating_cash_flow["form"],
                "filed_at": operating_cash_flow["filed_at"], "source_url": url,
                "filing_url": operating_cash_flow["filing_url"], "provider": "sec-companyfacts-derived",
                "derived_from": ["sec_operating_cash_flow", "sec_capex"],
                "period_basis": operating_cash_flow["period_basis"],
            })
        return observations

    def get_task_data(self, module: ModuleConfig, as_of: datetime) -> dict[str, Any]:
        records: list[dict[str, Any]] = []
        signals: list[dict[str, Any]] = []
        fundamentals: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        if any(instrument.symbol == "CL1" for instrument in module.instruments):
            try:
                signals.extend(self._eia_petroleum_stocks())
            except Exception as exc:
                errors.append(_error("eia-wpsr", exc))
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
            if instrument.symbol in SEC_CIKS:
                try:
                    fundamentals.extend(self._sec_fundamentals(instrument.instrument_id, instrument.symbol, as_of))
                except Exception as exc:
                    errors.append(_error("sec-companyfacts", exc))
            if instrument.symbol in STOOQ_SYMBOLS:
                try:
                    records.extend(self._stooq(instrument.instrument_id, instrument.symbol, instrument.currency))
                except Exception as exc:
                    errors.append(_error("stooq", exc))
        return {"provider": self.name, "records": records, "signals": signals, "fundamentals": fundamentals, "errors": errors, "as_of": as_of.isoformat()}


class FreeNewsProvider:
    name = "google-news-gdelt-marketaux"

    def __init__(self, marketaux_token: str | None, timeout: float = 25.0) -> None:
        self.marketaux_token = marketaux_token
        self.timeout = timeout

    def _bea_calendar(self, end_at: datetime) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        url = "https://www.bea.gov/news/schedule"
        parser = _ScheduleTableParser()
        parser.feed(_get(url, self.timeout).decode("utf-8", errors="replace"))
        events: list[dict[str, Any]] = []
        articles: list[dict[str, Any]] = []
        eastern = ZoneInfo("America/New_York")
        for row in parser.rows:
            date_text = next((text for css, text in row if "scheduled-date" in css), "")
            title = next((text for css, text in row if "release-title" in css), "")
            match = re.search(
                r"(" + "|".join(("January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"))
                + r")\s+(\d{1,2})\s+(\d{1,2}):(\d{2})\s+(AM|PM)",
                date_text,
                re.IGNORECASE,
            )
            if match is None or not title:
                continue
            parsed = datetime.strptime(
                f"{match.group(1)} {match.group(2)} {end_at.year} {match.group(3)}:{match.group(4)} {match.group(5)}",
                "%B %d %Y %I:%M %p",
            ).replace(tzinfo=eastern)
            event_at = parsed.astimezone(end_at.tzinfo)
            if not end_at < event_at <= end_at + timedelta(days=7):
                continue
            events.append({
                "title": title, "event_at": event_at.isoformat(), "event_end_at": None,
                "original_timezone": "America/New_York", "original_time_label": parsed.strftime("%Y-%m-%d %H:%M %Z"),
                "all_day": False, "confirmation_status": "confirmed", "last_verified_at": end_at.isoformat(),
                "publisher": "U.S. Bureau of Economic Analysis", "url": url, "provider": "bea-official-calendar",
                "consensus": None, "prior": None, "actual": None,
            })
            articles.append({
                "instrument_id": None, "headline": title, "description": f"Official BEA release scheduled for {date_text}",
                "published_at": None, "publisher": "U.S. Bureau of Economic Analysis", "url": url,
                "provider": "bea-official-calendar", "language": "en", "upcoming_candidate": True,
                "source_tier": "primary", "content_access": "public", "evidence_role": "confirmed_fact", "author": None,
            })
        return events, articles

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
        professional_domains = (
            ["ft.com", "wsj.com"]
            if module.task_id == "macro_market"
            else ["thedefiant.io", "chainfeeds.xyz", "ft.com", "wsj.com"]
            if module.task_id == "cross_asset"
            else ["ft.com", "wsj.com"]
        )
        for query_text in module.search_terms_en[:2]:
            for domain in professional_domains:
                search_queries.append((f"{query_text} site:{domain}", "en", False, False))
        official_event_domains = {
            "macro_market": ["bls.gov", "bea.gov", "census.gov", "federalreserve.gov"],
            "cross_asset": ["congress.gov", "senate.gov", "house.gov", "sec.gov", "eia.gov", "opec.org", "federalreserve.gov"],
            "hk_equities": ["hkexnews.hk"],
            "us_semis_optics": ["sec.gov"],
            "us_platform_media": ["sec.gov"],
            "cybersecurity": ["sec.gov"],
        }.get(module.task_id, [])
        for query_text in module.upcoming_event_terms_en:
            for domain in official_event_domains:
                search_queries.append((f"{query_text} site:{domain}", "en", False, True))
        for query_text, language, background_candidate, upcoming_candidate in search_queries:
            lookback_days = 45 if upcoming_candidate else 14 if background_candidate else 2
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
                    publisher = item.findtext("source") or "Google News"
                    link = item.findtext("link") or ""
                    policy = source_policy(link, publisher)
                    articles.append({
                        "instrument_id": None, "headline": item.findtext("title"), "description": item.findtext("description"),
                        "published_at": _iso_published_at(item.findtext("pubDate")), "publisher": publisher,
                        "url": link, "provider": "google-news-rss", "query": query_text,
                        "language": language, "background_candidate": background_candidate,
                        "upcoming_candidate": upcoming_candidate,
                        "source_tier": policy.tier, "content_access": policy.content_access,
                        "evidence_role": policy.role, "author": None,
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
        upcoming_events = []
        month_numbers = {
            name: number for number, name in enumerate(
                ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"),
                start=1,
            )
        }
        exact_pattern = re.compile(
            r"\b(" + "|".join(month_numbers) + r")\s+(\d{1,2})(?:,?\s+(\d{4}))?"
            r"(?:\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)?\s*(ET|EST|EDT|UTC|GMT))?\b",
            re.IGNORECASE,
        )
        timezone_map = {"ET": "America/New_York", "EST": "America/New_York", "EDT": "America/New_York", "UTC": "UTC", "GMT": "UTC"}
        for article in window_articles:
            if not article.get("upcoming_candidate"):
                continue
            headline = str(article.get("headline") or "")
            if re.search(r"\d{1,2}\s*[-–]\s*\d{1,2}", headline):
                continue
            match = exact_pattern.search(headline)
            if match is None:
                continue
            month = month_numbers[match.group(1).title()]
            day = int(match.group(2))
            year = int(match.group(3) or end_at.year)
            hour = int(match.group(4) or 0)
            minute = int(match.group(5) or 0)
            meridiem = (match.group(6) or "").lower()
            if meridiem.startswith("p") and hour < 12:
                hour += 12
            if meridiem.startswith("a") and hour == 12:
                hour = 0
            original_zone = timezone_map.get((match.group(7) or "").upper(), "Asia/Hong_Kong")
            event_local = datetime(year, month, day, hour, minute, tzinfo=ZoneInfo(original_zone))
            event_start = event_local.astimezone(end_at.tzinfo)
            if match.group(3) is None and event_start <= end_at and month < end_at.month:
                event_local = event_local.replace(year=year + 1)
                event_start = event_local.astimezone(end_at.tzinfo)
            policy = source_policy(str(article.get("url") or ""), str(article.get("publisher") or ""))
            if end_at < event_start <= end_at + timedelta(days=7):
                upcoming_events.append({
                    "title": headline,
                    "event_at": event_start.isoformat(),
                    "event_end_at": None,
                    "original_timezone": original_zone,
                    "original_time_label": event_local.strftime("%Y-%m-%d %H:%M %Z"),
                    "all_day": match.group(4) is None,
                    "confirmation_status": "confirmed" if policy.tier == "primary" else "tentative",
                    "last_verified_at": end_at.isoformat(),
                    "publisher": article.get("publisher"),
                    "url": article.get("url"),
                    "provider": article.get("provider"),
                })
        if module.task_id == "macro_market":
            try:
                official_events, official_articles = self._bea_calendar(end_at)
                upcoming_events.extend(official_events)
                window_articles.extend(official_articles)
                queries.append({"provider": "bea-official-calendar", "status": "success", "returned": len(official_events)})
            except Exception as exc:
                errors.append(_error("bea-official-calendar", exc))
                queries.append({"provider": "bea-official-calendar", "status": "failed", "returned": 0})
        for item in OFFICIAL_COMPANY_EVENTS:
            event_at = datetime.fromisoformat(item["event_at"])
            if item["task_id"] != module.task_id or not end_at < event_at <= end_at + timedelta(days=7):
                continue
            policy = source_policy(item["url"], item["publisher"])
            window_articles.append({
                "instrument_id": item["instrument_id"], "headline": item["title"], "description": "",
                "published_at": item["published_at"], "publisher": item["publisher"], "url": item["url"],
                "provider": item["provider"], "language": "en", "background_candidate": True,
                "upcoming_candidate": True, "source_tier": policy.tier,
                "content_access": policy.content_access, "evidence_role": policy.role, "author": None,
            })
            upcoming_events.append({
                "title": item["title"], "event_at": item["event_at"], "event_end_at": None,
                "original_timezone": item["original_timezone"], "original_time_label": item["original_time_label"],
                "all_day": False, "confirmation_status": "confirmed", "last_verified_at": end_at.isoformat(),
                "publisher": item["publisher"], "url": item["url"], "provider": item["provider"],
            })
            queries.append({"provider": item["provider"], "status": "success", "returned": 1})
        return {
            "provider": self.name,
            "articles": window_articles,
            "upcoming_events": upcoming_events,
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

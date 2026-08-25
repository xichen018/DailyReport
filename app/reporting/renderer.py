from __future__ import annotations

import html
import json
import re
from collections import Counter
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFError, TTFont
from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from app.schemas.models import ResearchTaskResult, RunContext, TaskStatus


STATUS_ZH = {
    TaskStatus.SUCCESS: "已更新",
    TaskStatus.PARTIAL: "部分数据待补",
    TaskStatus.FAILED: "数据待补",
}
IMPACT_ZH = {"positive": "利好", "negative": "利空", "neutral": "中性"}
ASSET_CLASS_ZH = {"equity": "股票", "crypto": "加密资产", "future": "期货", "index": "指数"}
PRICE_KIND_ZH = {
    "close": "最新收盘",
    "previous_close": "上一交易日收盘",
    "latest_24h": "最新价 / 24小时",
    "rolling_30h": "30小时滚动",
    "crosscheck_24h": "24小时交叉核验",
    "close_crosscheck": "收盘价交叉核验",
    "yahoo_reference_close_non_official": "Yahoo 近月参考收盘（非官方结算）",
}

NON_DECISION_EVIDENCE_PHRASES = ("不能单独证明", "不能证明", "不能据此", "不足以判断", "不能单独说明")

RATIONALE_LABELS = ("影响判断：", "核心变化：", "传导分析：", "关键验证：")
HKT = ZoneInfo("Asia/Hong_Kong")
ISO_DATETIME_RE = re.compile(
    r"(?<!\d)(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2}))(?!\d)"
)


def _reader_datetime_text(value: str) -> str:
    def replace(match: re.Match[str]) -> str:
        parsed = datetime.fromisoformat(match.group(1).replace("Z", "+00:00"))
        return parsed.astimezone(HKT).strftime("%m月%d日 %H:%M HKT")

    return ISO_DATETIME_RE.sub(replace, value)


def _reader_html(value: str) -> str:
    return html.escape(_reader_datetime_text(value))


def _duplicated_catalysts(results: list[ResearchTaskResult]) -> set[str]:
    values = [
        analysis.catalysts_zh.strip()
        for result in results
        for analysis in result.investment_analyses
        if analysis.catalysts_zh.strip()
    ]
    return {value for value, count in Counter(values).items() if count > 1}


def _rationale_sections(value: str) -> list[tuple[str, str]]:
    pattern = "(" + "|".join(re.escape(label) for label in RATIONALE_LABELS) + ")"
    parts = re.split(pattern, value.strip())
    if len(parts) == 1:
        return [("", value.strip())]
    sections: list[tuple[str, str]] = []
    prefix = parts[0].strip()
    if prefix:
        sections.append(("", prefix))
    for index in range(1, len(parts), 2):
        label = parts[index]
        content = parts[index + 1].strip() if index + 1 < len(parts) else ""
        sections.append((label, content))
    return sections


def _html_rationale(value: str) -> str:
    sections = _rationale_sections(value)
    if len(sections) == 1 and not sections[0][0]:
        return f"<p class='rationale'><strong>投资含义：</strong>{html.escape(sections[0][1])}</p>"
    return "<div class='rationale rationale-structured'>" + "".join(
        f"<p><strong>{html.escape(label)}</strong>{html.escape(content)}</p>"
        for label, content in sections
    ) + "</div>"


def _reader_text(value: str, limit: int = 130) -> str:
    compact = re.sub(r"\s+", " ", value).strip()
    if len(compact) <= limit:
        return compact
    cut = max(compact.rfind(mark, 0, limit + 1) for mark in ("。", "；", "，"))
    if cut < limit // 2:
        cut = limit
    return compact[:cut].rstrip("，；。 ") + "。"


def _format_number(value: object, places: int = 2) -> str:
    if value is None:
        return "-"
    try:
        number = Decimal(str(value))
    except Exception:
        return str(value)
    if number == 0:
        return "0.00"
    if abs(number) < Decimal("0.01"):
        places = 4
    return f"{number:,.{places}f}"


def _source_labels(result: ResearchTaskResult) -> dict[str, str]:
    labels: dict[str, str] = {}
    for source in result.sources:
        publisher = source.publisher.strip()
        provider = source.provider.strip()
        label = publisher if publisher and publisher.lower() not in {"unknown", "n/a"} else provider
        labels[source.source_id] = label
    return labels


def _source_refs(source_ids: list[str], labels: dict[str, str] | None = None) -> str:
    display = labels or {}
    return " · ".join(display.get(source_id, source_id) for source_id in source_ids)


def _html_source_links(result: ResearchTaskResult, source_ids: list[str]) -> str:
    source_map = {source.source_id: source for source in result.sources}
    links: list[str] = []
    for source_id in source_ids:
        source = source_map.get(source_id)
        if source is None:
            continue
        publisher = source.publisher.strip()
        provider = source.provider.strip()
        label = publisher if publisher and publisher.lower() not in {"unknown", "n/a"} else provider
        links.append(
            f"<a href='{html.escape(str(source.url), quote=True)}' target='_blank' rel='noopener noreferrer'>"
            f"{html.escape(label)} · 查看原文</a>"
        )
    return "<span aria-hidden='true'> · </span>".join(links)


def _pdf_source_links(result: ResearchTaskResult, source_ids: list[str]) -> str:
    source_map = {source.source_id: source for source in result.sources}
    links: list[str] = []
    for source_id in source_ids:
        source = source_map.get(source_id)
        if source is None:
            continue
        publisher = source.publisher.strip()
        provider = source.provider.strip()
        label = publisher if publisher and publisher.lower() not in {"unknown", "n/a"} else provider
        safe_url = html.escape(str(source.url), quote=True)
        links.append(f'<link href="{safe_url}" color="#175CD3">{html.escape(label)} · 查看原文</link>')
    return " · ".join(links)


def _change_class(value: object) -> str:
    if value is None:
        return "flat"
    numeric = float(value)
    return "up" if numeric > 0 else "down" if numeric < 0 else "flat"


def _format_percent(value: object) -> str:
    return "-" if value is None else f"{_format_number(value)}%"


def _price_kind_label(kind: str) -> str:
    if kind in PRICE_KIND_ZH:
        return PRICE_KIND_ZH[kind]
    if "上一" in kind and "收盘" in kind:
        return PRICE_KIND_ZH["previous_close"]
    if "收盘" in kind:
        return PRICE_KIND_ZH["close"]
    return kind


def _display_prices(prices: list[object]) -> list[object]:
    return [price for price in prices if not _price_kind_label(price.kind) == PRICE_KIND_ZH["previous_close"]]


def _reader_evidence(items: list[str]) -> list[str]:
    return [item for item in items if not any(phrase in item for phrase in NON_DECISION_EVIDENCE_PHRASES)]


def _previous_close(price: object, prices: list[object]) -> object | None:
    if price.previous_value is not None:
        return price.previous_value
    previous = next(
        (item for item in prices if _price_kind_label(item.kind) == PRICE_KIND_ZH["previous_close"]),
        None,
    )
    return previous.value if previous is not None else None


def _upcoming_event_entries(results: list[ResearchTaskResult]) -> list[tuple[ResearchTaskResult, object]]:
    entries: list[tuple[ResearchTaskResult, object]] = []
    seen: set[tuple[str, str]] = set()
    macro_results = [result for result in results if result.task_id == "macro_market" and result.upcoming_events]
    event_results = macro_results or results
    for result in event_results:
        for event in result.upcoming_events:
            if event.confirmation_status.value != "confirmed":
                continue
            key = (event.event_at.isoformat(), re.sub(r"\W+", "", event.title_zh).casefold())
            if key in seen:
                continue
            seen.add(key)
            entries.append((result, event))
    return sorted(entries, key=lambda item: item[1].event_at)


def _event_date_label(event: object) -> str:
    hkt = event.event_at.strftime("%m月%d日") if event.all_day else event.event_at.strftime("%m月%d日 %H:%M HKT")
    if event.event_end_at is not None:
        hkt = f"{event.event_at.strftime('%m月%d日')}-{event.event_end_at.strftime('%m月%d日')}"
    if event.original_time_label and event.original_timezone != "Asia/Hong_Kong":
        return f"{hkt}（{event.original_time_label}）"
    return hkt


def _html_report(context: RunContext, results: Iterable[ResearchTaskResult], mode: str) -> str:
    result_list = list(results)
    instruments = [instrument for result in result_list for instrument in result.instruments]
    news_items = [item for instrument in instruments for item in instrument.news] + [
        item for result in result_list for item in result.section_news
    ]
    upcoming_entries = _upcoming_event_entries(result_list)
    duplicated_catalysts = _duplicated_catalysts(result_list)
    regime = next((item.market_regime_zh for item in result_list if item.market_regime_zh), "")
    portfolio_note = next((item.portfolio_implications_zh for item in result_list if item.portfolio_implications_zh), "")
    top_views = [
        analysis.investment_view_zh
        for result in result_list for analysis in result.investment_analyses
        if analysis.investment_view_zh
    ][:3]
    snapshot_rows: list[str] = []
    for instrument in instruments:
        if not instrument.prices:
            snapshot_rows.append(
                "<tr>"
                f"<td><strong>{html.escape(instrument.symbol)}</strong> · {html.escape(instrument.name)}</td>"
                "<td class='numeric flat'>待补</td><td class='numeric flat'>-</td>"
                f"<td>{html.escape(instrument.trading_date.isoformat() if instrument.trading_date else '-')}</td>"
                "<td class='source-ref'>-</td></tr>"
            )
            continue
        price = instrument.prices[0]
        owner = next(result for result in result_list if instrument in result.instruments)
        labels = _source_labels(owner)
        direction = _change_class(price.change_pct)
        snapshot_rows.append(
            "<tr>"
            f"<td><strong>{html.escape(instrument.symbol)}</strong> · {html.escape(instrument.name)}</td>"
            f"<td class='numeric'>{_format_number(price.value)} <small>{html.escape(price.currency)}</small></td>"
            f"<td class='numeric {direction}'>{_format_percent(price.change_pct)}</td>"
            f"<td>{html.escape(instrument.trading_date.isoformat() if instrument.trading_date else price.as_of.date().isoformat())}</td>"
            f"<td class='source-ref'>{html.escape(_source_refs(price.source_ids, labels))}</td></tr>"
        )
    sections: list[str] = []
    for section_number, result in enumerate(result_list, start=1):
        status_class = "pending" if result.status != TaskStatus.SUCCESS else "ok"
        source_labels = _source_labels(result)
        body: list[str] = []
        if result.errors:
            body.append("<div class='data-note'><strong>本节数据待补</strong><p>当前资料不足以形成可靠结论，本节不作推断。</p></div>")
        if result.research_checks:
            unavailable = [item for item in result.research_checks if item.status.value == "data_unavailable"]
            rows = "".join(
                "<tr>"
                f"<td>{html.escape(item.scope_id)}</td><td>{html.escape(item.requirement_zh)}</td>"
                f"<td>{html.escape(item.status.value)}</td><td>{html.escape(item.conclusion_zh)}</td></tr>"
                for item in result.research_checks
            )
            body.append(
                f"<div class='coverage'><strong>研究要求覆盖：已核查 {len(result.research_checks) - len(unavailable)}/{len(result.research_checks)} 项</strong>"
                f"<span>受限 {len(unavailable)} 项</span></div>"
                "<details class='quality'><summary>查看逐项研究清单</summary>"
                "<table class='data-table'><thead><tr><th>范围</th><th>必查项</th><th>状态</th><th>结论</th></tr></thead>"
                f"<tbody>{rows}</tbody></table></details>"
            )
        analyses_by_instrument = {item.instrument_id: item for item in result.investment_analyses}
        for instrument_number, instrument in enumerate(result.instruments, start=1):
            body.append(
                "<div class='instrument'>"
                f"<div class='instrument-head'><div><span class='instrument-index'>{section_number}.{instrument_number}</span>"
                f"<h3>{html.escape(instrument.name)}</h3>"
                f"<strong>{html.escape(instrument.symbol)}</strong></div>"
                f"<span>{ASSET_CLASS_ZH.get(instrument.asset_class, instrument.asset_class)} · "
                f"{html.escape(instrument.exchange)} · {html.escape(instrument.currency)}</span></div>"
            )
            if instrument.prices:
                display_prices = _display_prices(instrument.prices)
                show_kind = len(display_prices) > 1
                rows = "".join(
                    "<tr>"
                    + (f"<td>{html.escape(_price_kind_label(price.kind))}</td>" if show_kind else "")
                    + f"<td class='numeric'>{_format_number(price.value)} {html.escape(price.currency)}</td>"
                    f"<td class='numeric {_change_class(price.change_pct)}'>{_format_percent(price.change_pct)}</td>"
                    f"<td>{html.escape(str(instrument.trading_date or price.as_of.date()))}</td><td class='source-ref'>{html.escape(_source_refs(price.source_ids, source_labels))}</td></tr>"
                    for price in display_prices
                )
                kind_header = "<th>口径</th>" if show_kind else ""
                body.append("<table class='data-table'><thead><tr>" + kind_header + "<th>最新收盘</th><th>涨跌幅</th><th>交易日</th><th>来源</th></tr></thead><tbody>" + rows + "</tbody></table>")
            analysis = analyses_by_instrument.get(instrument.instrument_id)
            if analysis:
                source_links = _html_source_links(result, analysis.source_ids)
                evidence_items = _reader_evidence(analysis.key_evidence_zh)
                evidence = "".join(f"<li>{html.escape(item)}</li>" for item in evidence_items)
                evidence_block = (
                    f"<h4>证据依据</h4><ol class='analysis-evidence'>{evidence}</ol>"
                    if evidence_items else ""
                )
                analysis_parts = [
                    "<div class='asset-analysis'><h4><span>A</span>当前判断</h4>",
                    f"<p class='scenario-lead'>{_reader_html(analysis.investment_view_zh)}</p>",
                ]
                for label, title, value in (
                    ("B", "市场正在定价什么", analysis.market_pricing_zh),
                    ("C", "关键分歧与增量信息", analysis.variant_view_zh),
                    ("D", "未来催化剂", analysis.catalysts_zh),
                    ("E", "关键价位与应对", analysis.levels_and_actions_zh or analysis.key_variable_zh),
                ):
                    if label == "D" and value.strip() in duplicated_catalysts:
                        continue
                    if value:
                        analysis_parts.append(f"<h4><span>{label}</span>{title}</h4><p>{_reader_html(value)}</p>")
                analysis_parts.extend((evidence_block, f"<p class='news-source'>观点来源：{source_links}</p></div>"))
                body.append("".join(analysis_parts))
            if instrument.news:
                event_label = "F" if analysis else "A"
                body.append(f"<h4 class='asset-subhead'><span>{event_label}</span>重要事件</h4><div class='news'>")
                for news_number, item in enumerate(instrument.news, start=1):
                    source_links = _html_source_links(result, item.source_ids)
                    body.append(
                        f"<article><div class='event-line'><span class='news-index'>{event_label}.{news_number}</span>"
                        f"<span class='impact {html.escape(item.impact.value)}'>{IMPACT_ZH[item.impact.value]}</span>"
                        f"<h4>{html.escape(item.headline)}</h4></div>"
                        f"<div class='news-meta'><time>{html.escape(item.published_at.strftime('%Y-%m-%d %H:%M %Z'))}</time>"
                        f"<span class='news-source'>来源：{source_links}</span></div>"
                        f"<p>{html.escape(item.summary_zh)}</p>{_html_rationale(item.rationale_zh)}</article>"
                    )
                body.append("</div>")
            elif not analysis:
                body.append("<p class='no-news'>研究窗口内未发现达到披露阈值的重大新闻。</p>")
            body.append("</div>")
        if result.section_news:
            section_news_group = len(result.instruments) + 1
            body.append(
                f"<div class='instrument-head'><div><span class='instrument-index'>{section_number}.{section_news_group}</span>"
                "<h3>板块与行业新闻</h3></div><span>重大动态</span></div><div class='news'>"
            )
            for news_number, item in enumerate(result.section_news, start=1):
                source_links = _html_source_links(result, item.source_ids)
                body.append(
                    f"<article><div class='event-line'><span class='news-index'>{section_number}.{section_news_group}.{news_number}</span>"
                    f"<span class='impact {html.escape(item.impact.value)}'>{IMPACT_ZH[item.impact.value]}</span>"
                    f"<h4>{html.escape(item.headline)}</h4></div>"
                    f"<div class='news-meta'><time>{html.escape(item.published_at.strftime('%Y-%m-%d %H:%M %Z'))}</time>"
                    f"<span class='news-source'>来源：{source_links}</span></div>"
                    f"<p>{html.escape(item.summary_zh)}</p>{_html_rationale(item.rationale_zh)}</article>"
                )
            body.append("</div>")
        if result.macro_observations:
            rows = "".join(
                f"<tr><td>{html.escape(item.label)}</td><td>{html.escape(_format_number(item.value))}</td><td>{html.escape(item.unit)}</td><td>{_reader_html(item.period)}</td><td>{html.escape(_source_refs(item.source_ids, source_labels))}</td></tr>"
                for item in result.macro_observations
            )
            body.append("<table class='data-table'><thead><tr><th>指标</th><th>数值</th><th>单位</th><th>期间</th><th>来源</th></tr></thead><tbody>" + rows + "</tbody></table>")
        for metric in result.relative_metrics:
            body.append(f"<div class='metric-note'><strong>{html.escape(metric.numerator)}/{html.escape(metric.denominator)}</strong><p>{_reader_html(metric.interpretation_zh)} <span class='source-ref'>{html.escape(_source_refs(metric.source_ids, source_labels))}</span></p></div>")
        if result.warnings:
            body.append("<details class='quality'><summary>数据质量记录（{count}）</summary><ul>{items}</ul></details>".format(count=len(result.warnings), items="".join(f"<li>{html.escape(item.message_zh)}</li>" for item in result.warnings)))
        if result.sources:
            body.append("<details class='sources'><summary>展开完整来源审计记录</summary><ol>")
            for source in result.sources:
                published = f"，发布时间 {html.escape(source.published_at.isoformat())}" if source.published_at else ""
                body.append(
                    f"<li id='{html.escape(result.task_id)}-{html.escape(source.source_id)}'><strong>{html.escape(source.publisher)}</strong> "
                    f"<span class='provider'>{html.escape(source.provider)}</span>{published}："
                    f"<a href='{html.escape(str(source.url), quote=True)}' rel='noopener noreferrer'>{html.escape(str(source.url))}</a></li>"
                )
            body.append("</ol></details>")
        sections.append(
            f"<section><div class='section-head'><div><span class='section-no'>{section_number}</span><h2>{html.escape(result.title_zh)}</h2></div>"
            + (f"<span class='status {status_class}'>{STATUS_ZH[result.status]}</span>" if result.status != TaskStatus.SUCCESS else "")
            + f"</div>{''.join(body)}</section>"
        )

    upcoming_body: list[str] = []
    upcoming_section_number = len(result_list) + 1
    for index, (owner, event) in enumerate(upcoming_entries, start=1):
        affected = "、".join(event.affected_assets_zh)
        source_links = _html_source_links(owner, event.source_ids)
        date_label = _event_date_label(event)
        values = " / ".join(item for item in (
            f"预期 {event.consensus}" if event.consensus else "",
            f"前值 {event.prior}" if event.prior else "",
            f"实际 {event.actual}" if event.actual else "",
        ) if item)
        upcoming_body.append(
            f"<article><div class='event-line'><span class='news-index'>{upcoming_section_number}.{index}</span>"
            f"<h4>{html.escape(date_label)} | {html.escape(event.title_zh)}</h4></div>"
            f"<div class='news-meta'><span>影响：{html.escape(affected)}</span><span class='news-source'>来源：{source_links}</span></div>"
            + (f"<p><strong>数据：</strong>{html.escape(values)}</p>" if values else "")
            + (f"<p><strong>传导变量：</strong>{html.escape(event.transmission_variable_zh)}</p>" if event.transmission_variable_zh else "")
            + f"<p>{html.escape(event.why_it_matters_zh)}</p></article>"
        )
    if not upcoming_body:
        upcoming_body.append("<p class='no-news'>未来七天暂未取得日期与来源均可核实的重大事件。</p>")
    sections.append(
        f"<section><div class='section-head'><div><span class='section-no'>{upcoming_section_number}</span><h2>未来一周关键事件</h2></div></div>"
        f"<div class='news'>{''.join(upcoming_body)}</div></section>"
    )

    mode_label = "正式研究" if mode == "real" else "设计预览"
    footer_label = "结构化投资研究 · 数据截至报告所示时间" if mode == "real" else "版式与流程预览 · 非实时投资依据"
    return f"""<!doctype html>
<html lang="zh-Hans"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>金融日报 {context.scheduled_for.date().isoformat()}</title>
<style>
:root{{--ink:#182230;--muted:#667085;--line:#dfe3e8;--soft:#f6f8fa;--accent:#24466f;--accent-soft:#eef2f6;--up:#067647;--down:#b42318;--amber:#9c6500;--paper:#fff}}
*{{box-sizing:border-box}} html{{background:#eef1f3}} body{{margin:0;color:var(--ink);font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;font-variant-numeric:tabular-nums;letter-spacing:0}}
main{{max-width:1040px;margin:24px auto;background:var(--paper);padding:46px 54px 64px;box-shadow:0 1px 4px rgba(16,24,40,.08)}}
.masthead{{border-top:5px solid var(--ink);padding-top:22px;border-bottom:1px solid var(--ink);padding-bottom:18px}} .brand-row{{display:flex;justify-content:space-between;align-items:flex-start;gap:24px}}
.eyebrow{{font-size:11px;font-weight:700;color:var(--accent);text-transform:uppercase}} h1{{font-size:32px;line-height:1.15;margin:5px 0 0;font-weight:700}} .edition{{text-align:right;font-size:12px;color:var(--muted);line-height:1.7}}
.mode{{display:inline-block;margin-top:8px;padding:2px 7px;border:1px solid var(--amber);color:var(--amber);font-weight:700}} .meta-strip{{display:flex;gap:28px;margin-top:16px;font-size:12px;color:var(--muted)}} .meta-strip strong{{color:var(--ink)}}
.brief{{padding:22px 0 26px;border-bottom:2px solid var(--ink)}} .brief h2{{font-size:15px;margin:0 0 14px;text-transform:uppercase}} .brief-grid{{display:grid;grid-template-columns:repeat(3,1fr);border:1px solid var(--line)}} .brief-item{{padding:12px 14px;border-right:1px solid var(--line)}} .brief-item:last-child{{border:0}} .brief-item span{{display:block;font-size:11px;color:var(--muted)}} .brief-item strong{{display:block;font-size:20px;margin-top:4px}}
.snapshot{{margin-top:20px}} .snapshot h3{{font-size:13px;margin:0 0 8px}} section{{padding:30px 0;border-bottom:1px solid var(--line)}}
.section-head{{display:flex;align-items:center;justify-content:space-between;gap:16px;margin-bottom:18px}} .section-head>div{{display:flex;align-items:baseline;gap:12px}} .section-no{{font-size:12px;color:var(--accent);font-weight:700}} h2{{font-size:22px;margin:0}} .status{{font-size:11px;padding:2px 7px;border:1px solid var(--accent);color:var(--accent);font-weight:700}} .status.pending{{border-color:var(--amber);color:var(--amber)}}
.instrument{{margin:22px 0 28px;border-top:2px solid var(--ink);padding-top:0}} .instrument-head{{display:flex;align-items:center;justify-content:space-between;gap:16px;margin:0;padding:11px 0 10px;border-bottom:1px solid var(--line)}} .instrument-head>div{{display:flex;align-items:baseline;gap:10px;min-width:0}} .instrument-head h3{{font-size:17px;margin:0}} .instrument-head strong{{font-size:13px;color:var(--accent)}} .instrument-head span{{font-size:11px;color:var(--muted);font-weight:700;white-space:nowrap}} .instrument-head .instrument-index{{display:inline-flex;align-items:center;justify-content:center;min-width:34px;padding:2px 5px;background:var(--ink);color:#fff;font-size:10px}}
.coverage{{display:flex;justify-content:space-between;gap:12px;padding:9px 11px;background:var(--accent-soft);font-size:12px;margin-bottom:12px}}
table{{width:100%;border-collapse:collapse;font-size:12px;table-layout:fixed}} th,td{{padding:8px 9px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top;overflow-wrap:anywhere}} th{{background:var(--soft);font-size:10px;color:#475467;text-transform:uppercase}} td:first-child{{font-weight:500}} td span{{display:block;font-size:10px;color:var(--muted);font-weight:400;margin-top:2px}} .numeric{{text-align:right;white-space:nowrap}} .up{{color:var(--up);font-weight:700}} .down{{color:var(--down);font-weight:700}} .flat{{color:var(--muted)}} small{{font-size:9px;color:var(--muted)}}
.data-table{{margin-bottom:13px}} .news{{border-top:0}} article{{padding:15px 0;border-bottom:1px solid var(--line)}} .event-line{{display:flex;align-items:flex-start;gap:9px}} h4{{font-size:14px;line-height:1.45;margin:0;flex:1}} .news-index{{flex:0 0 auto;color:var(--muted);font-size:10px;font-weight:700;padding-top:3px;min-width:42px}} .impact{{flex:0 0 auto;font-size:10px;font-weight:700;padding:2px 6px;border:1px solid;margin-top:1px}} .impact.positive{{color:var(--up)}} .impact.negative{{color:var(--down)}} .impact.neutral{{color:var(--muted)}}
.news-meta{{display:flex;align-items:center;flex-wrap:wrap;gap:7px 14px;margin:6px 0 7px;padding-left:44px;color:var(--muted);font-size:11px}} time{{display:inline;margin:0}} .news-source a{{color:#175cd3;font-weight:700;text-decoration:none}} .news-source a:hover{{text-decoration:underline}} article p{{margin:4px 0;line-height:1.62;font-size:13px}} .rationale{{color:#344054}} .rationale-structured{{border-left:2px solid #d0d5dd;padding-left:12px;margin-top:8px}} .rationale-structured p{{margin:0 0 6px}} .rationale-structured p:last-child{{margin-bottom:0}} .source-ref{{color:var(--accent);font-size:10px;font-weight:700}} .no-news{{font-size:12px;color:var(--muted);padding:12px 0;margin:0}}
.metric-note{{border-left:3px solid var(--accent);padding:8px 12px;margin:12px 0;background:var(--soft)}} .metric-note p{{margin:4px 0;font-size:13px}} .data-note{{border-left:3px solid var(--amber);background:#fffaeb;padding:12px 14px;color:#7a4d00}} .data-note p{{margin:4px 0 0}}
.asset-analysis{{margin-top:12px;padding:12px 14px;background:var(--soft);border-left:3px solid var(--accent)}} .asset-analysis h4,.asset-subhead,.view-update h4{{display:flex;align-items:center;gap:7px;margin:8px 0 5px;font-size:12px;color:var(--ink)}} .asset-analysis h4:first-child{{margin-top:0}} .asset-analysis h4 span,.asset-subhead span,.view-update h4 span{{display:inline-flex;align-items:center;justify-content:center;width:19px;height:19px;background:var(--accent);color:#fff;font-size:10px;font-weight:700}} .scenario-lead{{font-size:13px!important;line-height:1.65;color:var(--ink)}} .analysis-evidence{{margin:4px 0 8px;padding-left:19px}} .analysis-evidence li{{margin:4px 0;font-size:12px;line-height:1.55}} .asset-subhead{{margin-top:13px}} .view-update{{margin:10px 0 4px;padding-top:8px;border-top:1px solid var(--line)}} .view-update p{{font-size:12px;line-height:1.6;margin:4px 0}} .asset-analysis .news-source{{display:block;margin-top:8px;font-size:10px}}
.quality{{margin-top:14px;color:var(--muted);font-size:11px}} .quality summary{{cursor:pointer}} .sources{{margin-top:18px;border-top:1px solid var(--line);padding-top:10px;color:var(--muted);font-size:10px}} .sources summary{{cursor:pointer;font-weight:700}} .sources ol{{padding-left:20px;margin:10px 0 0}} .sources li{{margin:6px 0;line-height:1.45;overflow-wrap:anywhere}} .sources a{{color:#175cd3}} .provider{{display:inline;margin-left:5px;padding:1px 4px;background:var(--soft);color:var(--muted)}}
footer{{margin-top:28px;padding-top:14px;border-top:1px solid var(--ink);display:flex;justify-content:space-between;color:var(--muted);font-size:10px}}
@media(max-width:700px){{html{{background:#fff}} main{{margin:0;padding:26px 18px;box-shadow:none}} .brand-row,.meta-strip,footer{{align-items:flex-start;flex-direction:column;gap:8px}} .edition{{text-align:left}} .brief-grid{{grid-template-columns:1fr}} .brief-item{{border-right:0;border-bottom:1px solid var(--line)}} .section-head,.instrument-head{{align-items:flex-start}} .instrument-head{{flex-direction:column;gap:4px}} .news-meta{{padding-left:0}} .snapshot,.data-table{{overflow-x:auto}} table{{min-width:690px}} h1{{font-size:27px}}}}
@media print{{html{{background:#fff}} main{{margin:0;max-width:none;box-shadow:none;padding:0}} a{{color:inherit;text-decoration:none}} .quality{{display:none}}}}
</style></head><body><main>
<header class="masthead"><div class="brand-row"><div><div class="eyebrow">Daily Investment Intelligence</div><h1>金融市场日报</h1></div><div class="edition">{context.scheduled_for.strftime('%Y年%m月%d日')}<br>香港时间 {context.scheduled_for.strftime('%H:%M')}<br><span class="mode">{mode_label}</span></div></div>
<div class="meta-strip"><span><strong>研究窗口</strong> {context.window.start_at.strftime('%m-%d %H:%M')} - {context.window.end_at.strftime('%m-%d %H:%M')} HKT</span><span><strong>覆盖</strong> 港股 · 美股 · 加密资产 · 能源 · 宏观</span></div></header>
<div class="brief"><h2>PM Brief</h2><div class="brief-grid"><div class="brief-item"><span>研究覆盖</span><strong>{len(result_list)} 个板块</strong></div><div class="brief-item"><span>覆盖资产</span><strong>{len(instruments)} 项</strong></div><div class="brief-item"><span>未来催化</span><strong>{len(upcoming_entries)} 项</strong></div></div>
{f'<div class="metric-note"><strong>市场环境</strong><p>{html.escape(regime)}</p></div>' if regime else ''}
{''.join(f'<div class="metric-note"><strong>组合判断 {index}</strong><p>{html.escape(view)}</p></div>' for index, view in enumerate(top_views, start=1))}
{f'<div class="metric-note"><strong>联动与集中风险</strong><p>{html.escape(portfolio_note)}</p></div>' if portfolio_note else ''}
<div class="snapshot"><h3>市场快照</h3><table><thead><tr><th>标的</th><th>价格</th><th>涨跌幅</th><th>交易日</th><th>来源</th></tr></thead><tbody>{''.join(snapshot_rows)}</tbody></table></div></div>
{''.join(sections)}
<footer><span>{footer_label}</span><span>生成时间 {context.scheduled_for.isoformat()}</span></footer>
</main></body></html>"""


def _register_pdf_font() -> str:
    font_name = "DailyReportCJK"
    if font_name in pdfmetrics.getRegisteredFontNames():
        return font_name
    candidates = [
        Path("/System/Library/Fonts/Supplemental/Arial Unicode.ttf"),
        Path("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"),
        Path("/usr/share/fonts/truetype/arphic/uming.ttc"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"),
    ]
    incompatible: list[str] = []
    for font_path in candidates:
        if not font_path.is_file():
            continue
        try:
            pdfmetrics.registerFont(TTFont(font_name, str(font_path), subfontIndex=0))
            return font_name
        except TTFError:
            incompatible.append(str(font_path))
    detail = f"; incompatible fonts: {', '.join(incompatible)}" if incompatible else ""
    raise RuntimeError(f"No ReportLab-compatible CJK TrueType font found; install fonts-wqy-zenhei{detail}")


def _pdf_styles() -> tuple[str, dict[str, ParagraphStyle]]:
    font_name = _register_pdf_font()
    base = getSampleStyleSheet()
    return font_name, {
        "eyebrow": ParagraphStyle("Eyebrow", parent=base["BodyText"], fontName=font_name, fontSize=7.5, leading=10, textColor=colors.HexColor("#24466F"), spaceAfter=3),
        "title": ParagraphStyle("ChineseTitle", parent=base["Title"], fontName=font_name, fontSize=24, leading=29, textColor=colors.HexColor("#182230"), alignment=TA_LEFT, spaceAfter=9),
        "meta": ParagraphStyle("ChineseMeta", parent=base["BodyText"], fontName=font_name, fontSize=8.5, leading=13, textColor=colors.HexColor("#667085")),
        "h2": ParagraphStyle("ChineseH2", parent=base["Heading2"], fontName=font_name, fontSize=15, leading=20, textColor=colors.HexColor("#24466F"), spaceBefore=12, spaceAfter=8),
        "h3": ParagraphStyle("ChineseH3", parent=base["Heading3"], fontName=font_name, fontSize=11, leading=15, spaceBefore=9, spaceAfter=6, keepWithNext=True, backColor=colors.HexColor("#F2F4F7"), borderPadding=(5, 7, 5, 7)),
        "h4": ParagraphStyle("ChineseH4", parent=base["Heading4"], fontName=font_name, fontSize=9, leading=13, textColor=colors.HexColor("#24466F"), spaceBefore=7, spaceAfter=4, keepWithNext=True),
        "body": ParagraphStyle("ChineseBody", parent=base["BodyText"], fontName=font_name, fontSize=9, leading=14, spaceAfter=5),
        "rationale": ParagraphStyle("ChineseRationale", parent=base["BodyText"], fontName=font_name, fontSize=8.5, leading=13, leftIndent=8, borderColor=colors.HexColor("#D0D5DD"), borderWidth=0, borderLeftWidth=1.5, borderPadding=(0, 0, 0, 7), textColor=colors.HexColor("#344054"), spaceAfter=4),
        "small": ParagraphStyle("ChineseSmall", parent=base["BodyText"], fontName=font_name, fontSize=7.5, leading=11, textColor=colors.HexColor("#667085"), spaceAfter=3),
        "positive": ParagraphStyle("Positive", parent=base["BodyText"], fontName=font_name, fontSize=8.5, leading=13, textColor=colors.HexColor("#067647"), spaceAfter=4),
        "negative": ParagraphStyle("Negative", parent=base["BodyText"], fontName=font_name, fontSize=8.5, leading=13, textColor=colors.HexColor("#B42318"), spaceAfter=4),
        "neutral": ParagraphStyle("Neutral", parent=base["BodyText"], fontName=font_name, fontSize=8.5, leading=13, textColor=colors.HexColor("#475467"), spaceAfter=4),
        "error": ParagraphStyle("ChineseError", parent=base["BodyText"], fontName=font_name, fontSize=9, leading=14, textColor=colors.HexColor("#B42318")),
    }


def _pdf_rationale(value: str, styles: dict[str, ParagraphStyle]) -> list[Paragraph]:
    sections = _rationale_sections(value)
    if len(sections) == 1 and not sections[0][0]:
        return [Paragraph(f"<b>投资含义：</b>{html.escape(sections[0][1])}", styles["body"])]
    return [
        Paragraph(f"<b>{html.escape(label)}</b>{html.escape(content)}", styles["rationale"])
        for label, content in sections
    ]


def _pdf_report(path: Path, context: RunContext, results: Iterable[ResearchTaskResult], mode: str) -> None:
    font_name, styles = _pdf_styles()
    result_list = list(results)
    instruments = [instrument for result in result_list for instrument in result.instruments]
    upcoming_entries = _upcoming_event_entries(result_list)
    duplicated_catalysts = _duplicated_catalysts(result_list)
    doc = SimpleDocTemplate(str(path), pagesize=A4, leftMargin=17 * mm, rightMargin=17 * mm, topMargin=20 * mm, bottomMargin=17 * mm, title="金融市场日报")

    def decorate_page(canvas: object, document: object) -> None:
        canvas.saveState()
        canvas.setFont(font_name, 7)
        canvas.setFillColor(colors.HexColor("#667085"))
        canvas.drawString(17 * mm, A4[1] - 11 * mm, f"FINANCIAL MARKETS DAILY  |  {context.scheduled_for.date().isoformat()}")
        canvas.setStrokeColor(colors.HexColor("#182230"))
        canvas.setLineWidth(0.6)
        canvas.line(17 * mm, A4[1] - 13 * mm, A4[0] - 17 * mm, A4[1] - 13 * mm)
        canvas.setFillColor(colors.HexColor("#667085"))
        footer = "结构化投资研究 · 数据截至报告所示时间" if mode == "real" else "版式与流程预览 · 非实时投资依据"
        canvas.drawString(17 * mm, 9 * mm, footer)
        canvas.drawRightString(A4[0] - 17 * mm, 9 * mm, f"{document.page}")
        canvas.restoreState()

    story: list[object] = [
        Paragraph("DAILY INVESTMENT INTELLIGENCE", styles["eyebrow"]),
        Paragraph("金融市场日报", styles["title"]),
        Paragraph(f"{context.scheduled_for.strftime('%Y年%m月%d日')} · 香港时间 {context.scheduled_for.strftime('%H:%M')} · 研究窗口 {context.window.start_at.strftime('%m-%d %H:%M')} 至 {context.window.end_at.strftime('%m-%d %H:%M')}", styles["meta"]),
        Spacer(1, 5 * mm),
    ]
    brief_data = [["研究覆盖", "覆盖资产", "未来催化"], [f"{len(result_list)} 个板块", f"{len(instruments)} 项", f"{len(upcoming_entries)} 项"]]
    brief = Table(brief_data, colWidths=[53 * mm, 53 * mm, 53 * mm])
    brief.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), font_name), ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("FONTSIZE", (0, 0), (-1, 0), 7.5), ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#667085")),
        ("FONTSIZE", (0, 1), (-1, 1), 15), ("TEXTCOLOR", (0, 1), (-1, 1), colors.HexColor("#182230")),
        ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#D0D5DD")), ("INNERGRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#D0D5DD")),
        ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    story.extend([brief, Spacer(1, 3 * mm)])
    regime = next((item.market_regime_zh for item in result_list if item.market_regime_zh), "")
    portfolio_note = next((item.portfolio_implications_zh for item in result_list if item.portfolio_implications_zh), "")
    if regime:
        story.extend((Paragraph("市场环境", styles["h4"]), Paragraph(_reader_html(regime), styles["body"])))
    top_views = [analysis.investment_view_zh for result in result_list for analysis in result.investment_analyses if analysis.investment_view_zh][:3]
    for index, view in enumerate(top_views, start=1):
        story.extend((Paragraph(f"组合判断 {index}", styles["h4"]), Paragraph(_reader_html(view), styles["body"])))
    if portfolio_note:
        story.extend((Paragraph("联动与集中风险", styles["h4"]), Paragraph(_reader_html(portfolio_note), styles["body"])))
    story.extend([Spacer(1, 3 * mm), Paragraph("市场快照", styles["h3"])])
    snapshot = [["标的", "价格", "涨跌幅", "交易日", "来源"]]
    for instrument in instruments:
        identity = Paragraph(f"<b>{html.escape(instrument.symbol)}</b> · {html.escape(instrument.name)}", styles["small"])
        if not instrument.prices:
            snapshot.append([identity, "待补", "-", str(instrument.trading_date or "-"), "-"])
            continue
        price = instrument.prices[0]
        owner = next(result for result in result_list if instrument in result.instruments)
        labels = _source_labels(owner)
        snapshot.append([
            identity,
            f"{_format_number(price.value)} {price.currency}",
            _format_percent(price.change_pct),
            str(instrument.trading_date or price.as_of.date()),
            Paragraph(html.escape(_source_refs(price.source_ids, labels)), styles["small"]),
        ])
    snapshot_table = Table(snapshot, colWidths=[47 * mm, 30 * mm, 20 * mm, 30 * mm, 32 * mm], repeatRows=1)
    snapshot_table.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), font_name), ("FONTSIZE", (0, 0), (-1, -1), 7.3),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F2F4F7")), ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#475467")),
        ("LINEBELOW", (0, 0), (-1, -1), 0.35, colors.HexColor("#D0D5DD")), ("ALIGN", (1, 1), (2, -1), "RIGHT"),
        ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.extend([snapshot_table, Spacer(1, 5 * mm)])

    for section_number, result in enumerate(result_list, start=1):
        status_suffix = f" · {STATUS_ZH[result.status]}" if result.status != TaskStatus.SUCCESS else ""
        source_labels = _source_labels(result)
        story.append(Paragraph(f"{section_number}  {result.title_zh}{status_suffix}", styles["h2"]))
        if result.errors:
            story.append(Paragraph("本节数据待补：当前资料不足以形成可靠结论，本节不作推断。", styles["meta"]))
        if result.research_checks:
            unavailable = sum(item.status.value == "data_unavailable" for item in result.research_checks)
            covered = len(result.research_checks) - unavailable
            coverage_text = f"研究覆盖：{covered}/{len(result.research_checks)}"
            if unavailable:
                coverage_text += f"；待补数据：{unavailable} 项"
            story.append(Paragraph(coverage_text, styles["meta"]))
        analyses_by_instrument = {item.instrument_id: item for item in result.investment_analyses}
        for instrument_number, instrument in enumerate(result.instruments, start=1):
            instrument_heading = Paragraph(
                f"{section_number}.{instrument_number}  |  {html.escape(instrument.name)}  |  {html.escape(instrument.symbol)}  |  "
                f"{ASSET_CLASS_ZH.get(instrument.asset_class, instrument.asset_class)} · "
                f"{html.escape(instrument.exchange)} · {html.escape(instrument.currency)}",
                styles["h3"],
            )
            if instrument.prices:
                display_prices = _display_prices(instrument.prices)
                show_kind = len(display_prices) > 1
                data = [[*(["口径"] if show_kind else []), "最新收盘", "涨跌幅", "交易日", "来源"]]
                for price in display_prices:
                    data.append([
                        *([Paragraph(html.escape(_price_kind_label(price.kind)), styles["small"])] if show_kind else []),
                        f"{_format_number(price.value)} {price.currency}",
                        _format_percent(price.change_pct),
                        str(instrument.trading_date or price.as_of.date()),
                        Paragraph(html.escape(_source_refs(price.source_ids, source_labels)), styles["small"]),
                    ])
                col_widths = ([20 * mm] if show_kind else []) + [38 * mm, 28 * mm, 34 * mm, 45 * mm]
                table = Table(data, colWidths=col_widths, repeatRows=1)
                table.setStyle(TableStyle([
                    ("FONTNAME", (0, 0), (-1, -1), font_name), ("FONTSIZE", (0, 0), (-1, -1), 7.5),
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F2F4F7")), ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#344054")),
                    ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#D0D5DD")), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 5), ("RIGHTPADDING", (0, 0), (-1, -1), 5), ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ]))
                story.append(KeepTogether([instrument_heading, table, Spacer(1, 3 * mm)]))
            else:
                story.append(instrument_heading)
            analysis = analyses_by_instrument.get(instrument.instrument_id)
            if analysis:
                source_links = _pdf_source_links(result, analysis.source_ids)
                story.append(Paragraph("A  |  当前判断", styles["h4"]))
                story.append(Paragraph(_reader_html(analysis.investment_view_zh), styles["body"]))
                for label, title, value in (
                    ("B", "市场正在定价什么", analysis.market_pricing_zh),
                    ("C", "关键分歧与增量信息", analysis.variant_view_zh),
                    ("D", "未来催化剂", analysis.catalysts_zh),
                    ("E", "关键价位与应对", analysis.levels_and_actions_zh or analysis.key_variable_zh),
                ):
                    if label == "D" and value.strip() in duplicated_catalysts:
                        continue
                    if value:
                        story.append(Paragraph(f"{label}  |  {title}", styles["h4"]))
                        story.append(Paragraph(_reader_html(value), styles["body"]))
                evidence_items = _reader_evidence(analysis.key_evidence_zh)
                if evidence_items:
                    story.append(Paragraph("证据依据", styles["h4"]))
                for index, evidence in enumerate(evidence_items, start=1):
                    story.append(Paragraph(f"{index}. {_reader_html(evidence)}", styles["body"]))
                if source_links:
                    story.append(Paragraph(f"观点来源：{source_links}", styles["small"]))
            if instrument.news:
                event_label = "F" if analysis else "A"
                story.append(Paragraph(f"{event_label}  |  重要事件", styles["h4"]))
                for news_number, item in enumerate(instrument.news, start=1):
                    impact_style = styles[item.impact.value]
                    source_links = _pdf_source_links(result, item.source_ids)
                    story.append(Paragraph(f"{event_label}.{news_number}  |  {IMPACT_ZH[item.impact.value]}  |  {html.escape(item.headline)}", impact_style))
                    if source_links:
                        story.append(Paragraph(f"来源：{source_links}", styles["small"]))
                    story.append(Paragraph(html.escape(item.summary_zh), styles["body"]))
                    story.extend(_pdf_rationale(item.rationale_zh, styles))
            elif not analysis:
                story.append(Paragraph("窗口内无重大新闻。", styles["meta"]))
        if result.section_news:
            section_news_group = len(result.instruments) + 1
            story.append(Paragraph(f"{section_number}.{section_news_group}  |  板块与行业新闻", styles["h3"]))
            for news_number, item in enumerate(result.section_news, start=1):
                impact_style = styles[item.impact.value]
                source_links = _pdf_source_links(result, item.source_ids)
                story.append(Paragraph(f"{section_number}.{section_news_group}.{news_number}  |  {IMPACT_ZH[item.impact.value]}  |  {html.escape(item.headline)}", impact_style))
                if source_links:
                    story.append(Paragraph(f"来源：{source_links}", styles["small"]))
                story.append(Paragraph(html.escape(item.summary_zh), styles["body"]))
                story.extend(_pdf_rationale(item.rationale_zh, styles))
        for item in result.macro_observations:
            story.append(Paragraph(f"{item.label}：{_format_number(item.value)} {item.unit}（{_reader_datetime_text(item.period)}）[{html.escape(_source_refs(item.source_ids, source_labels))}]", styles["body"]))
        for metric in result.relative_metrics:
            story.append(Paragraph(f"{metric.numerator}/{metric.denominator}：{_reader_html(metric.interpretation_zh)} [{html.escape(_source_refs(metric.source_ids, source_labels))}]", styles["body"]))
        story.append(Spacer(1, 4 * mm))
    upcoming_section_number = len(result_list) + 1
    upcoming_story: list[object] = [Paragraph(f"{upcoming_section_number}  未来一周关键事件", styles["h2"])]
    if upcoming_entries:
        for index, (owner, event) in enumerate(upcoming_entries, start=1):
            affected = "、".join(event.affected_assets_zh)
            source_links = _pdf_source_links(owner, event.source_ids)
            date_label = _event_date_label(event)
            upcoming_story.append(Paragraph(f"{upcoming_section_number}.{index}  |  {date_label}  |  {html.escape(event.title_zh)}", styles["h4"]))
            values = " / ".join(item for item in (
                f"预期 {event.consensus}" if event.consensus else "",
                f"前值 {event.prior}" if event.prior else "",
                f"实际 {event.actual}" if event.actual else "",
            ) if item)
            detail_parts = [f"影响：{affected}"]
            if event.transmission_variable_zh:
                detail_parts.append(f"传导：{event.transmission_variable_zh}")
            if values:
                detail_parts.append(f"数据：{values}")
            upcoming_story.append(Paragraph(_reader_html("；".join(detail_parts)), styles["small"]))
            if source_links:
                upcoming_story.append(Paragraph(f"来源：{source_links}", styles["small"]))
    else:
        upcoming_story.append(Paragraph("未来七天暂未取得日期与来源均可核实的重大事件。", styles["meta"]))
    story.append(KeepTogether(upcoming_story))
    doc.build(story, onFirstPage=decorate_page, onLaterPages=decorate_page)


def render_reports(report_dir: Path, context: RunContext, results: list[ResearchTaskResult], mode: str = "mock") -> dict[str, str]:
    report_dir.mkdir(parents=True, exist_ok=True)
    html_path = report_dir / "daily-report.html"
    pdf_path = report_dir / "daily-report.pdf"
    json_path = report_dir / "daily-report.json"
    html_path.write_text(_html_report(context, results, mode), encoding="utf-8")
    json_path.write_text(
        json.dumps(
            {"run_context": context.model_dump(mode="json"), "tasks": [item.model_dump(mode="json") for item in results]},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    _pdf_report(pdf_path, context, results, mode)
    return {"html": str(html_path), "pdf": str(pdf_path), "json": str(json_path)}

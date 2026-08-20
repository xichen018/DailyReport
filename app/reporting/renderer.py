from __future__ import annotations

import html
import json
from decimal import Decimal
from pathlib import Path
from typing import Iterable

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFError, TTFont
from reportlab.platypus import KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from app.schemas.models import ResearchTaskResult, RunContext, TaskStatus


STATUS_ZH = {
    TaskStatus.SUCCESS: "成功",
    TaskStatus.PARTIAL: "部分成功",
    TaskStatus.FAILED: "失败",
}
IMPACT_ZH = {"positive": "利好", "negative": "利空", "neutral": "中性"}


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


def _change_class(value: object) -> str:
    if value is None:
        return "flat"
    numeric = float(value)
    return "up" if numeric > 0 else "down" if numeric < 0 else "flat"


def _format_percent(value: object) -> str:
    return "-" if value is None else f"{_format_number(value)}%"


def _html_report(context: RunContext, results: Iterable[ResearchTaskResult], mode: str) -> str:
    result_list = list(results)
    delivered = sum(item.status != TaskStatus.FAILED for item in result_list)
    instruments = [instrument for result in result_list for instrument in result.instruments]
    news_items = [item for instrument in instruments for item in instrument.news]
    snapshot_rows: list[str] = []
    for instrument in instruments:
        if not instrument.prices:
            continue
        price = instrument.prices[0]
        owner = next(result for result in result_list if instrument in result.instruments)
        labels = _source_labels(owner)
        direction = _change_class(price.change_pct)
        snapshot_rows.append(
            "<tr>"
            f"<td><strong>{html.escape(instrument.symbol)}</strong><span>{html.escape(instrument.name)}</span></td>"
            f"<td class='numeric'>{_format_number(price.value)} <small>{html.escape(price.currency)}</small></td>"
            f"<td class='numeric {direction}'>{_format_percent(price.change_pct)}</td>"
            f"<td>{html.escape(instrument.trading_date.isoformat() if instrument.trading_date else price.as_of.date().isoformat())}</td>"
            f"<td class='source-ref'>{html.escape(_source_refs(price.source_ids, labels))}</td></tr>"
        )
    sections: list[str] = []
    for section_number, result in enumerate(result_list, start=1):
        status_class = "failed" if result.status == TaskStatus.FAILED else "ok"
        source_labels = _source_labels(result)
        body: list[str] = []
        if result.errors:
            issue_title = "板块执行失败" if result.status == TaskStatus.FAILED else "数据限制"
            body.append(f"<div class='failure'><strong>{issue_title}</strong>" + "".join(f"<p>{html.escape(error.message_zh)}</p>" for error in result.errors) + "</div>")
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
        for instrument in result.instruments:
            body.append(f"<div class='instrument-head'><h3>{html.escape(instrument.name)}</h3><span>{html.escape(instrument.symbol)}</span></div>")
            if instrument.prices:
                rows = "".join(
                    "<tr>"
                    f"<td>{html.escape(price.kind)}</td><td class='numeric'>{_format_number(price.value)} {html.escape(price.currency)}</td>"
                    f"<td class='numeric {_change_class(price.change_value)}'>{_format_number(price.change_value)}</td>"
                    f"<td class='numeric {_change_class(price.change_pct)}'>{_format_percent(price.change_pct)}</td>"
                    f"<td>{html.escape(price.as_of.strftime('%Y-%m-%d %H:%M'))}</td><td class='source-ref'>{html.escape(_source_refs(price.source_ids, source_labels))}</td></tr>"
                    for price in instrument.prices
                )
                body.append("<table class='data-table'><thead><tr><th>口径</th><th>价格</th><th>涨跌</th><th>涨跌幅</th><th>截至</th><th>来源</th></tr></thead><tbody>" + rows + "</tbody></table>")
            if instrument.news:
                body.append("<div class='news'>")
                for item in instrument.news:
                    body.append(
                        f"<article><div class='event-line'><span class='impact {html.escape(item.impact.value)}'>{IMPACT_ZH[item.impact.value]}</span>"
                        f"<h4>{html.escape(item.headline)}</h4><span class='source-ref'>{html.escape(_source_refs(item.source_ids, source_labels))}</span></div>"
                        f"<time>{html.escape(item.published_at.strftime('%Y-%m-%d %H:%M %Z'))}</time>"
                        f"<p>{html.escape(item.summary_zh)}</p><p class='rationale'><strong>投资含义：</strong>{html.escape(item.rationale_zh)}</p></article>"
                    )
                body.append("</div>")
            else:
                body.append("<p class='no-news'>研究窗口内未发现达到披露阈值的重大新闻。</p>")
        if result.macro_observations:
            rows = "".join(
                f"<tr><td>{html.escape(item.label)}</td><td>{html.escape(_format_number(item.value))}</td><td>{html.escape(item.unit)}</td><td>{html.escape(item.period)}</td><td>{html.escape(_source_refs(item.source_ids, source_labels))}</td></tr>"
                for item in result.macro_observations
            )
            body.append("<table class='data-table'><thead><tr><th>指标</th><th>数值</th><th>单位</th><th>期间</th><th>来源</th></tr></thead><tbody>" + rows + "</tbody></table>")
        for metric in result.relative_metrics:
            body.append(f"<div class='metric-note'><strong>{html.escape(metric.numerator)}/{html.escape(metric.denominator)}</strong><p>{html.escape(metric.interpretation_zh)} <span class='source-ref'>{html.escape(_source_refs(metric.source_ids, source_labels))}</span></p></div>")
        if result.warnings:
            body.append("<details class='quality'><summary>数据质量记录（{count}）</summary><ul>{items}</ul></details>".format(count=len(result.warnings), items="".join(f"<li>{html.escape(item.message_zh)}</li>" for item in result.warnings)))
        if result.sources:
            body.append("<div class='sources'><h3>来源与时间戳</h3><ol>")
            for source in result.sources:
                published = f"，发布时间 {html.escape(source.published_at.isoformat())}" if source.published_at else ""
                body.append(
                    f"<li id='{html.escape(result.task_id)}-{html.escape(source.source_id)}'><strong>{html.escape(source.publisher)}</strong> "
                    f"<span class='provider'>{html.escape(source.provider)}</span>{published}："
                    f"<a href='{html.escape(str(source.url), quote=True)}' rel='noopener noreferrer'>{html.escape(str(source.url))}</a></li>"
                )
            body.append("</ol></div>")
        sections.append(
            f"<section><div class='section-head'><div><span class='section-no'>{section_number:02d}</span><h2>{html.escape(result.title_zh)}</h2></div>"
            f"<span class='status {status_class}'>{STATUS_ZH[result.status]}</span></div>{''.join(body)}</section>"
        )

    mode_label = "真实数据" if mode == "real" else "模拟数据"
    footer_label = "自动化结构化研究 · 数据截至报告所示时间" if mode == "real" else "模拟数据 · 仅用于自动化流程验证"
    delivery_note = "无失败" if delivered == len(result_list) else f"失败 {len(result_list) - delivered}"
    return f"""<!doctype html>
<html lang="zh-Hans"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>金融日报 {context.scheduled_for.date().isoformat()}</title>
<style>
:root{{--ink:#182230;--muted:#667085;--line:#dfe3e8;--soft:#f6f8fa;--accent:#176b5b;--accent-soft:#e9f3f0;--up:#067647;--down:#b42318;--amber:#9c6500;--paper:#fff}}
*{{box-sizing:border-box}} html{{background:#eef1f3}} body{{margin:0;color:var(--ink);font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;font-variant-numeric:tabular-nums;letter-spacing:0}}
main{{max-width:1040px;margin:24px auto;background:var(--paper);padding:46px 54px 64px;box-shadow:0 1px 4px rgba(16,24,40,.08)}}
.masthead{{border-top:5px solid var(--ink);padding-top:22px;border-bottom:1px solid var(--ink);padding-bottom:18px}} .brand-row{{display:flex;justify-content:space-between;align-items:flex-start;gap:24px}}
.eyebrow{{font-size:11px;font-weight:700;color:var(--accent);text-transform:uppercase}} h1{{font-size:32px;line-height:1.15;margin:5px 0 0;font-weight:700}} .edition{{text-align:right;font-size:12px;color:var(--muted);line-height:1.7}}
.mode{{display:inline-block;margin-top:8px;padding:2px 7px;border:1px solid var(--amber);color:var(--amber);font-weight:700}} .meta-strip{{display:flex;gap:28px;margin-top:16px;font-size:12px;color:var(--muted)}} .meta-strip strong{{color:var(--ink)}}
.brief{{padding:22px 0 26px;border-bottom:2px solid var(--ink)}} .brief h2{{font-size:15px;margin:0 0 14px;text-transform:uppercase}} .brief-grid{{display:grid;grid-template-columns:repeat(3,1fr);border:1px solid var(--line)}} .brief-item{{padding:12px 14px;border-right:1px solid var(--line)}} .brief-item:last-child{{border:0}} .brief-item span{{display:block;font-size:11px;color:var(--muted)}} .brief-item strong{{display:block;font-size:20px;margin-top:4px}}
.snapshot{{margin-top:20px}} .snapshot h3{{font-size:13px;margin:0 0 8px}} section{{padding:30px 0;border-bottom:1px solid var(--line)}}
.section-head{{display:flex;align-items:center;justify-content:space-between;gap:16px;margin-bottom:18px}} .section-head>div{{display:flex;align-items:baseline;gap:12px}} .section-no{{font-size:12px;color:var(--accent);font-weight:700}} h2{{font-size:22px;margin:0}} .status{{font-size:11px;padding:2px 7px;border:1px solid var(--accent);color:var(--accent);font-weight:700}} .status.failed{{border-color:var(--down);color:var(--down)}}
.instrument-head{{display:flex;align-items:baseline;gap:10px;margin:22px 0 8px}} .instrument-head h3{{font-size:16px;margin:0}} .instrument-head span{{font-size:12px;color:var(--muted);font-weight:700}}
.coverage{{display:flex;justify-content:space-between;gap:12px;padding:9px 11px;background:var(--accent-soft);font-size:12px;margin-bottom:12px}}
table{{width:100%;border-collapse:collapse;font-size:12px;table-layout:fixed}} th,td{{padding:8px 9px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top;overflow-wrap:anywhere}} th{{background:var(--soft);font-size:10px;color:#475467;text-transform:uppercase}} td:first-child{{font-weight:500}} td span{{display:block;font-size:10px;color:var(--muted);font-weight:400;margin-top:2px}} .numeric{{text-align:right;white-space:nowrap}} .up{{color:var(--up);font-weight:700}} .down{{color:var(--down);font-weight:700}} .flat{{color:var(--muted)}} small{{font-size:9px;color:var(--muted)}}
.data-table{{margin-bottom:13px}} .news{{border-top:1px solid var(--line)}} article{{padding:13px 0;border-bottom:1px solid var(--line)}} .event-line{{display:flex;align-items:center;gap:8px}} h4{{font-size:14px;margin:0;flex:1}} .impact{{font-size:10px;font-weight:700;padding:2px 6px;border:1px solid}} .impact.positive{{color:var(--up)}} .impact.negative{{color:var(--down)}} .impact.neutral{{color:var(--muted)}}
time{{display:block;margin:5px 0;color:var(--muted);font-size:11px}} article p{{margin:4px 0;line-height:1.62;font-size:13px}} .rationale{{color:#344054}} .source-ref{{color:var(--accent);font-size:10px;font-weight:700}} .no-news{{font-size:12px;color:var(--muted);padding:10px 0}}
.metric-note{{border-left:3px solid var(--accent);padding:8px 12px;margin:12px 0;background:var(--soft)}} .metric-note p{{margin:4px 0;font-size:13px}} .failure{{border-left:3px solid var(--down);background:#fff5f4;padding:12px 14px;color:var(--down)}}
.quality{{margin-top:14px;color:var(--muted);font-size:11px}} .quality summary{{cursor:pointer}} .sources{{margin-top:20px;border-top:1px solid var(--line);padding-top:12px}} .sources h3{{font-size:12px;margin:0 0 8px}} .sources ol{{padding-left:20px;margin:0}} .sources li{{margin:6px 0;font-size:10px;line-height:1.45;color:var(--muted);overflow-wrap:anywhere}} .sources a{{color:#175cd3}} .provider{{display:inline;margin-left:5px;padding:1px 4px;background:var(--soft);color:var(--muted)}}
footer{{margin-top:28px;padding-top:14px;border-top:1px solid var(--ink);display:flex;justify-content:space-between;color:var(--muted);font-size:10px}}
@media(max-width:700px){{html{{background:#fff}} main{{margin:0;padding:26px 18px;box-shadow:none}} .brand-row,.meta-strip,footer{{align-items:flex-start;flex-direction:column;gap:8px}} .edition{{text-align:left}} .brief-grid{{grid-template-columns:1fr}} .brief-item{{border-right:0;border-bottom:1px solid var(--line)}} .section-head{{align-items:flex-start}} .snapshot,.data-table{{overflow-x:auto}} table{{min-width:690px}} h1{{font-size:27px}}}}
@media print{{html{{background:#fff}} main{{margin:0;max-width:none;box-shadow:none;padding:0}} a{{color:inherit;text-decoration:none}} .quality{{display:none}}}}
</style></head><body><main>
<header class="masthead"><div class="brand-row"><div><div class="eyebrow">Daily Investment Intelligence</div><h1>金融市场日报</h1></div><div class="edition">{context.scheduled_for.strftime('%Y年%m月%d日')}<br>香港时间 {context.scheduled_for.strftime('%H:%M')}<br><span class="mode">{mode_label}</span></div></div>
<div class="meta-strip"><span><strong>研究窗口</strong> {context.window.start_at.strftime('%m-%d %H:%M')} - {context.window.end_at.strftime('%m-%d %H:%M')} HKT</span><span><strong>覆盖</strong> 港股 · 美股 · 加密资产 · 能源 · 宏观</span></div></header>
<div class="brief"><h2>Executive Brief</h2><div class="brief-grid"><div class="brief-item"><span>板块交付</span><strong>{delivered}/{len(result_list)}</strong><small>{delivery_note}</small></div><div class="brief-item"><span>覆盖标的</span><strong>{len(instruments)}</strong></div><div class="brief-item"><span>窗口内事件</span><strong>{len(news_items)}</strong></div></div>
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
        "eyebrow": ParagraphStyle("Eyebrow", parent=base["BodyText"], fontName=font_name, fontSize=7.5, leading=10, textColor=colors.HexColor("#176B5B"), spaceAfter=3),
        "title": ParagraphStyle("ChineseTitle", parent=base["Title"], fontName=font_name, fontSize=24, leading=29, textColor=colors.HexColor("#182230"), alignment=TA_LEFT, spaceAfter=9),
        "meta": ParagraphStyle("ChineseMeta", parent=base["BodyText"], fontName=font_name, fontSize=8.5, leading=13, textColor=colors.HexColor("#667085")),
        "h2": ParagraphStyle("ChineseH2", parent=base["Heading2"], fontName=font_name, fontSize=15, leading=20, textColor=colors.HexColor("#176B5B"), spaceBefore=12, spaceAfter=8),
        "h3": ParagraphStyle("ChineseH3", parent=base["Heading3"], fontName=font_name, fontSize=11, leading=15, spaceBefore=8, spaceAfter=5, keepWithNext=True),
        "body": ParagraphStyle("ChineseBody", parent=base["BodyText"], fontName=font_name, fontSize=9, leading=14, spaceAfter=5),
        "small": ParagraphStyle("ChineseSmall", parent=base["BodyText"], fontName=font_name, fontSize=7.5, leading=11, textColor=colors.HexColor("#667085"), spaceAfter=3),
        "positive": ParagraphStyle("Positive", parent=base["BodyText"], fontName=font_name, fontSize=8.5, leading=13, textColor=colors.HexColor("#067647"), spaceAfter=4),
        "negative": ParagraphStyle("Negative", parent=base["BodyText"], fontName=font_name, fontSize=8.5, leading=13, textColor=colors.HexColor("#B42318"), spaceAfter=4),
        "neutral": ParagraphStyle("Neutral", parent=base["BodyText"], fontName=font_name, fontSize=8.5, leading=13, textColor=colors.HexColor("#475467"), spaceAfter=4),
        "error": ParagraphStyle("ChineseError", parent=base["BodyText"], fontName=font_name, fontSize=9, leading=14, textColor=colors.HexColor("#B42318")),
    }


def _pdf_report(path: Path, context: RunContext, results: Iterable[ResearchTaskResult], mode: str) -> None:
    font_name, styles = _pdf_styles()
    result_list = list(results)
    delivered = sum(item.status != TaskStatus.FAILED for item in result_list)
    instruments = [instrument for result in result_list for instrument in result.instruments]
    news_count = sum(len(instrument.news) for instrument in instruments)
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
        footer = "自动化结构化研究 · 数据截至报告所示时间" if mode == "real" else "模拟数据 · 仅用于自动化流程验证"
        canvas.drawString(17 * mm, 9 * mm, footer)
        canvas.drawRightString(A4[0] - 17 * mm, 9 * mm, f"{document.page}")
        canvas.restoreState()

    story: list[object] = [
        Paragraph("DAILY INVESTMENT INTELLIGENCE", styles["eyebrow"]),
        Paragraph("金融市场日报", styles["title"]),
        Paragraph(f"{context.scheduled_for.strftime('%Y年%m月%d日')} · 香港时间 {context.scheduled_for.strftime('%H:%M')} · 研究窗口 {context.window.start_at.strftime('%m-%d %H:%M')} 至 {context.window.end_at.strftime('%m-%d %H:%M')}", styles["meta"]),
        Spacer(1, 5 * mm),
    ]
    delivery_note = "无失败" if delivered == len(result_list) else f"失败 {len(result_list) - delivered}"
    brief_data = [["板块交付", "覆盖标的", "窗口内事件"], [f"{delivered}/{len(result_list)}  ({delivery_note})", str(len(instruments)), str(news_count)]]
    brief = Table(brief_data, colWidths=[53 * mm, 53 * mm, 53 * mm])
    brief.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), font_name), ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("FONTSIZE", (0, 0), (-1, 0), 7.5), ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#667085")),
        ("FONTSIZE", (0, 1), (-1, 1), 15), ("TEXTCOLOR", (0, 1), (-1, 1), colors.HexColor("#182230")),
        ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#D0D5DD")), ("INNERGRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#D0D5DD")),
        ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    story.extend([brief, Spacer(1, 5 * mm), Paragraph("市场快照", styles["h3"])])
    snapshot = [["标的", "价格", "涨跌幅", "交易日", "来源"]]
    for instrument in instruments:
        if not instrument.prices:
            continue
        price = instrument.prices[0]
        owner = next(result for result in result_list if instrument in result.instruments)
        labels = _source_labels(owner)
        snapshot.append([
            instrument.symbol,
            f"{_format_number(price.value)} {price.currency}",
            _format_percent(price.change_pct),
            str(instrument.trading_date or price.as_of.date()),
            Paragraph(html.escape(_source_refs(price.source_ids, labels)), styles["small"]),
        ])
    snapshot_table = Table(snapshot, colWidths=[25 * mm, 39 * mm, 24 * mm, 34 * mm, 37 * mm], repeatRows=1)
    snapshot_table.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), font_name), ("FONTSIZE", (0, 0), (-1, -1), 7.3),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F2F4F7")), ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#475467")),
        ("LINEBELOW", (0, 0), (-1, -1), 0.35, colors.HexColor("#D0D5DD")), ("ALIGN", (1, 1), (2, -1), "RIGHT"),
        ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.extend([snapshot_table, Spacer(1, 5 * mm)])

    for section_number, result in enumerate(result_list, start=1):
        if section_number == 6:
            story.append(PageBreak())
        status_suffix = f" · {STATUS_ZH[result.status]}" if result.status != TaskStatus.SUCCESS else ""
        source_labels = _source_labels(result)
        story.append(Paragraph(f"{section_number:02d}  {result.title_zh}{status_suffix}", styles["h2"]))
        for error in result.errors:
            issue_title = "失败原因" if result.status == TaskStatus.FAILED else "数据限制"
            story.append(Paragraph(f"{issue_title}：{html.escape(error.message_zh)}", styles["error"]))
        if result.research_checks:
            unavailable = sum(item.status.value == "data_unavailable" for item in result.research_checks)
            covered = len(result.research_checks) - unavailable
            story.append(Paragraph(f"已核查：{covered}/{len(result.research_checks)}；数据受限：{unavailable} 项", styles["meta"]))
            unavailable_items = [item for item in result.research_checks if item.status.value == "data_unavailable"]
            for item in unavailable_items[:3]:
                story.append(Paragraph(f"数据缺口：{html.escape(item.requirement_zh)} - {html.escape(item.conclusion_zh)}", styles["small"]))
            if len(unavailable_items) > 3:
                story.append(Paragraph(f"其余 {len(unavailable_items) - 3} 项缺口详见随附 JSON 审计记录。", styles["small"]))
        for instrument in result.instruments:
            instrument_heading = Paragraph(f"{instrument.name} ({instrument.symbol})", styles["h3"])
            if instrument.prices:
                data = [["类型", "价格", "涨跌", "涨跌幅", "时间", "来源"]]
                for price in instrument.prices:
                    data.append([
                        Paragraph(html.escape(price.kind), styles["small"]),
                        f"{_format_number(price.value)} {price.currency}",
                        _format_number(price.change_value),
                        _format_percent(price.change_pct),
                        price.as_of.strftime("%Y-%m-%d %H:%M"),
                        Paragraph(html.escape(_source_refs(price.source_ids, source_labels)), styles["small"]),
                    ])
                table = Table(data, colWidths=[22 * mm, 30 * mm, 19 * mm, 19 * mm, 37 * mm, 32 * mm], repeatRows=1)
                table.setStyle(TableStyle([
                    ("FONTNAME", (0, 0), (-1, -1), font_name), ("FONTSIZE", (0, 0), (-1, -1), 7.5),
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E6F4F1")), ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#0F4F49")),
                    ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#D0D5DD")), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 5), ("RIGHTPADDING", (0, 0), (-1, -1), 5), ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ]))
                story.append(KeepTogether([instrument_heading, table, Spacer(1, 3 * mm)]))
            else:
                story.append(instrument_heading)
            if instrument.news:
                for item in instrument.news:
                    impact_style = styles[item.impact.value]
                    story.append(Paragraph(f"{IMPACT_ZH[item.impact.value]}  |  {html.escape(item.headline)} [{html.escape(_source_refs(item.source_ids, source_labels))}]", impact_style))
                    story.append(Paragraph(f"{html.escape(item.summary_zh)} {html.escape(item.rationale_zh)}", styles["body"]))
            else:
                story.append(Paragraph("窗口内无重大新闻。", styles["meta"]))
        for item in result.macro_observations:
            story.append(Paragraph(f"{item.label}：{_format_number(item.value)} {item.unit}（{item.period}）[{html.escape(_source_refs(item.source_ids, source_labels))}]", styles["body"]))
        for metric in result.relative_metrics:
            story.append(Paragraph(f"{metric.numerator}/{metric.denominator}：{html.escape(metric.interpretation_zh)} [{html.escape(_source_refs(metric.source_ids, source_labels))}]", styles["body"]))
        for warning in result.warnings[:3]:
            story.append(Paragraph(f"数据质量：{html.escape(warning.message_zh)}", styles["small"]))
        if result.sources:
            story.append(Paragraph("来源与时间戳", styles["h3"]))
            for source in result.sources:
                published = f"，发布时间 {source.published_at.isoformat()}" if source.published_at else ""
                safe_url = html.escape(str(source.url), quote=True)
                story.append(
                    Paragraph(
                        f"{html.escape(source.publisher)} [{html.escape(source.provider)}]{published} · <link href=\"{safe_url}\" color=\"#175CD3\">查看原文</link>",
                        styles["small"],
                    )
                )
        story.append(Spacer(1, 4 * mm))
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

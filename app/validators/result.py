from __future__ import annotations

import re
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from app.modules.loader import ModuleConfig
from app.schemas.models import CheckStatus, ResearchTaskResult, TaskStatus, TaskWarning


class ValidationFailure(ValueError):
    pass


TRACKING_PARAMS = {"fbclid", "gclid", "mc_cid", "mc_eid"}


def required_research_check_plan(module: ModuleConfig) -> list[dict[str, str]]:
    plan: list[dict[str, str]] = []

    def add(requirement_type: str, scope_id: str, requirement_zh: str) -> None:
        plan.append(
            {
                "check_id": f"required_{len(plan) + 1}",
                "requirement_type": requirement_type,
                "scope_id": scope_id,
                "requirement_zh": requirement_zh,
            }
        )

    for item in module.price_checks:
        add("price", module.task_id, item)
    news_scopes = [item.instrument_id for item in module.instruments] or [module.task_id]
    for scope in news_scopes:
        for item in module.news_categories:
            add("news", scope, item)
    for item in module.industry_topics:
        add("industry", module.task_id, item)
    for item in module.triggered_checks:
        add("trigger", module.task_id, item)
    for instrument in module.instruments:
        for item in instrument.focus:
            add("instrument_focus", instrument.instrument_id, item)
    return plan


def required_research_checks(module: ModuleConfig) -> set[tuple[str, str, str]]:
    return {
        (item["requirement_type"], item["scope_id"], item["requirement_zh"])
        for item in required_research_check_plan(module)
    }


def canonical_url(value: str) -> str:
    parts = urlsplit(value)
    query = [
        (key, val)
        for key, val in parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in TRACKING_PARAMS
    ]
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), urlencode(sorted(query)), ""))


def normalized_headline(value: str) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", value.casefold())


def validate_result(
    result: ResearchTaskResult,
    module: ModuleConfig,
    provider_data: dict[str, Any] | None = None,
) -> ResearchTaskResult:
    if result.task_id != module.task_id:
        raise ValidationFailure("task_id does not match module")
    if result.status == TaskStatus.FAILED:
        return result

    registry = {item.instrument_id: item for item in module.instruments}
    source_ids = {source.source_id for source in result.sources}
    if len(source_ids) != len(result.sources):
        raise ValidationFailure("duplicate source_id")

    actual_checks = {
        (item.requirement_type, item.scope_id, item.requirement_zh)
        for item in result.research_checks
    }
    required_checks = required_research_checks(module)
    missing_checks = required_checks - actual_checks
    if missing_checks:
        raise ValidationFailure(f"required research checks missing: {sorted(missing_checks)}")
    unexpected_checks = actual_checks - required_checks
    if unexpected_checks:
        raise ValidationFailure(f"unexpected research checks: {sorted(unexpected_checks)}")
    if len(actual_checks) != len(result.research_checks):
        raise ValidationFailure("duplicate research check")
    for check in result.research_checks:
        if set(check.source_ids) - source_ids:
            raise ValidationFailure(f"research check has unknown source: {check.check_id}")

    warnings = list(result.warnings)
    market_candidates: dict[str, list[dict[str, Any]]] = {}
    news_urls: set[str] = set()
    macro_metric_ids: set[str] = set()
    relative_metric_ids: set[str] = set()
    if provider_data is not None:
        for record in provider_data.get("market", {}).get("records", []):
            market_candidates.setdefault(record["instrument_id"], []).append(record)
        news_urls = {
            canonical_url(str(item["url"]))
            for item in provider_data.get("news", {}).get("articles", [])
            if item.get("url")
        }
        macro_metric_ids = {
            str(item["metric_id"])
            for item in provider_data.get("macro", {}).get("observations", [])
            if item.get("metric_id")
        }
        relative_metric_ids = {
            str(item["metric_id"])
            for item in provider_data.get("macro", {}).get("relative_metrics", [])
            if item.get("metric_id")
        }
        google_queries = [
            item
            for item in provider_data.get("news", {}).get("queries", [])
            if item.get("provider") == "google-news-rss"
        ]
        mandatory_news_completed = bool(google_queries) and all(item.get("status") == "success" for item in google_queries)
        if mandatory_news_completed:
            for check in result.research_checks:
                if (
                    check.requirement_type in {"news", "industry", "instrument_focus"}
                    and check.status == CheckStatus.DATA_UNAVAILABLE
                ):
                    check.status = CheckStatus.NO_MATERIAL_FINDING
                    check.conclusion_zh = f"已完成必查新闻源检索；窗口内未发现相关重要新闻。{check.conclusion_zh}"
                    warnings.append(
                        TaskWarning(
                            code="NO_NEWS_IS_NOT_MISSING_DATA",
                            message_zh=f"已将无新闻候选规范为无重要发现：{check.scope_id} / {check.requirement_zh}",
                            field_path=f"research_checks.{check.check_id}",
                        )
                    )
    source_map = {source.source_id: source for source in result.sources}
    reported_news_urls: set[str] = set()

    def validated_news(items: list[Any], field_path: str) -> list[Any]:
        deduped = []
        seen_keys: set[tuple[str, str]] = set()
        for news in sorted(items, key=lambda item: item.published_at, reverse=True):
            missing = set(news.source_ids) - source_ids
            if missing:
                raise ValidationFailure(f"news has unknown source ids: {sorted(missing)}")
            if not news.outside_window and not (result.window.start_at <= news.published_at <= result.window.end_at):
                raise ValidationFailure(f"news outside research window: {news.headline}")
            urls = sorted(canonical_url(str(source_map[sid].url)) for sid in news.source_ids)
            if provider_data is not None and not any(url in news_urls for url in urls):
                raise ValidationFailure(f"news URL not found in provider data: {news.headline}")
            key = (normalized_headline(news.headline), "|".join(urls))
            if key in seen_keys or (urls and all(url in reported_news_urls for url in urls)):
                warnings.append(
                    TaskWarning(
                        code="DUPLICATE_NEWS_REMOVED",
                        message_zh=f"已移除重复新闻：{news.headline}",
                        field_path=field_path,
                    )
                )
                continue
            seen_keys.add(key)
            reported_news_urls.update(urls)
            deduped.append(news)
        return deduped

    for instrument in result.instruments:
        configured = registry.get(instrument.instrument_id)
        if configured is None:
            raise ValidationFailure(f"unknown instrument_id: {instrument.instrument_id}")
        expected = (configured.symbol, configured.exchange, configured.currency)
        actual = (instrument.symbol, instrument.exchange, instrument.currency)
        if actual != expected:
            raise ValidationFailure(f"instrument identity mismatch: {instrument.instrument_id}")
        for price in instrument.prices:
            missing = set(price.source_ids) - source_ids
            if missing:
                raise ValidationFailure(f"price has unknown source ids: {sorted(missing)}")
            if provider_data is not None:
                candidates = market_candidates.get(instrument.instrument_id, [])
                matched_candidate = None
                for item in candidates:
                    provider_value = item.get("previous_value") if price.kind == "previous_close" else item.get("value")
                    if provider_value is None or abs(price.value - Decimal(str(provider_value))) > Decimal("0.01"):
                        continue
                    if (
                        price.kind != "previous_close"
                        and price.previous_value is not None
                        and item.get("previous_value") is not None
                        and abs(price.previous_value - Decimal(str(item["previous_value"]))) > Decimal("0.01")
                    ):
                        continue
                    matched_candidate = item
                    break
                if matched_candidate is None:
                    raise ValidationFailure(f"price not found in provider data: {instrument.symbol}")

                authoritative_value = (
                    matched_candidate.get("previous_value")
                    if price.kind == "previous_close"
                    else matched_candidate.get("value")
                )
                normalized = price.value != Decimal(str(authoritative_value))
                price.value = Decimal(str(authoritative_value))
                if (
                    price.kind != "previous_close"
                    and price.previous_value is not None
                    and matched_candidate.get("previous_value") is not None
                ):
                    authoritative_previous = Decimal(str(matched_candidate["previous_value"]))
                    normalized = normalized or price.previous_value != authoritative_previous
                    price.previous_value = authoritative_previous
                if normalized:
                    warnings.append(
                        TaskWarning(
                            code="PRICE_PROVIDER_NORMALIZED",
                            message_zh=f"已按行情源原始精度规范化价格：{instrument.symbol} {price.kind}",
                            field_path=f"instruments.{instrument.instrument_id}.prices.{price.kind}",
                        )
                    )
            if price.previous_value is not None:
                expected_value = (price.value - price.previous_value).quantize(Decimal("0.01"))
                expected_pct = ((price.value - price.previous_value) / price.previous_value * Decimal("100")).quantize(Decimal("0.01"))
                if price.change_value != expected_value or price.change_pct != expected_pct:
                    value_close = price.change_value is not None and abs(price.change_value - expected_value) <= Decimal("0.01")
                    pct_close = price.change_pct is not None and abs(price.change_pct - expected_pct) <= Decimal("0.01")
                    if not (value_close and pct_close):
                        raise ValidationFailure(f"price change mismatch: {instrument.symbol}")
                    price.change_value = expected_value
                    price.change_pct = expected_pct
                    warnings.append(
                        TaskWarning(
                            code="PRICE_CHANGE_RECALCULATED",
                            message_zh=f"已按价格重算并规范化涨跌值与涨跌幅：{instrument.symbol} {price.kind}",
                            field_path=f"instruments.{instrument.instrument_id}.prices.{price.kind}",
                        )
                    )
        instrument.news = validated_news(instrument.news, f"instruments.{instrument.instrument_id}.news")

    result.section_news = validated_news(result.section_news, "section_news")
    if provider_data is not None:
        unreported_news = news_urls - reported_news_urls
        if unreported_news:
            raise ValidationFailure(f"provider news articles were not summarized: {len(unreported_news)}")

    for observation in result.macro_observations:
        if set(observation.source_ids) - source_ids:
            raise ValidationFailure(f"macro observation has unknown source: {observation.metric_id}")
        if provider_data is not None and observation.metric_id not in macro_metric_ids:
            raise ValidationFailure(f"macro observation not found in provider data: {observation.metric_id}")
    for metric in result.relative_metrics:
        if set(metric.source_ids) - source_ids:
            raise ValidationFailure(f"relative metric has unknown source: {metric.metric_id}")
        if provider_data is not None and metric.metric_id not in relative_metric_ids:
            raise ValidationFailure(f"relative metric not found in provider data: {metric.metric_id}")
        for observation in metric.observations:
            expected_ratio = (observation.numerator_value / observation.denominator_value).quantize(Decimal("0.0001"))
            if observation.ratio != expected_ratio:
                raise ValidationFailure(f"relative ratio mismatch: {metric.metric_id}")

    referenced_source_ids: set[str] = set()
    for instrument in result.instruments:
        for price in instrument.prices:
            referenced_source_ids.update(price.source_ids)
        for news in instrument.news:
            referenced_source_ids.update(news.source_ids)
    for news in result.section_news:
        referenced_source_ids.update(news.source_ids)
    for observation in result.macro_observations:
        referenced_source_ids.update(observation.source_ids)
    for metric in result.relative_metrics:
        referenced_source_ids.update(metric.source_ids)
    for check in result.research_checks:
        referenced_source_ids.update(check.source_ids)
    result.sources = [source for source in result.sources if source.source_id in referenced_source_ids]
    result.warnings = warnings
    result.status = (
        TaskStatus.PARTIAL
        if result.errors or any(check.status == CheckStatus.DATA_UNAVAILABLE for check in result.research_checks)
        else TaskStatus.SUCCESS
    )
    return result

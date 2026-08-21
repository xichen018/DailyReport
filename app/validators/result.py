from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import HttpUrl

from app.modules.loader import ModuleConfig
from app.schemas.models import CheckStatus, PricePoint, RelativeObservation, ResearchTaskResult, Source, TaskStatus, TaskWarning
from app.text.chinese import to_simplified_chinese


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
    for item in module.data_checks:
        add("data", module.task_id, item)
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


def _is_previous_close(kind: str) -> bool:
    normalized = kind.strip().casefold().replace("-", "_").replace(" ", "_")
    return normalized == "previous_close" or ("上一" in kind and "收盘" in kind)


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
    warnings = list(result.warnings)

    if provider_data is not None:
        provider_articles = [
            item
            for item in provider_data.get("news", {}).get("articles", [])
            if item.get("url") and item.get("published_at")
        ]
        news_items = [
            *[news for instrument in result.instruments for news in instrument.news],
            *result.section_news,
        ]
        for news in news_items:
            missing_source_ids = set(news.source_ids) - source_ids
            for missing_source_id in missing_source_ids:
                matches = [
                    article
                    for article in provider_articles
                    if datetime.fromisoformat(str(article["published_at"]).replace("Z", "+00:00"))
                    == news.published_at
                ]
                if len(matches) != 1:
                    continue
                article = matches[0]
                result.sources.append(
                    Source(
                        source_id=missing_source_id,
                        provider=str(article.get("provider") or "provider"),
                        publisher=str(article.get("publisher") or "Unknown"),
                        url=article["url"],
                        published_at=article["published_at"],
                        retrieved_at=result.window.end_at,
                    )
                )
                source_ids.add(missing_source_id)
                warnings.append(
                    TaskWarning(
                        code="NEWS_SOURCE_METADATA_RESTORED",
                        message_zh=f"已按唯一发布时间匹配恢复新闻来源：{news.headline}",
                        field_path=f"sources.{missing_source_id}",
                    )
                )

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
        unknown_source_ids = set(check.source_ids) - source_ids
        if unknown_source_ids:
            check.source_ids = [source_id for source_id in check.source_ids if source_id in source_ids]
            warnings.append(
                TaskWarning(
                    code="UNKNOWN_CHECK_SOURCE_REMOVED",
                    message_zh=f"已移除研究检查中的无效来源引用：{check.check_id} {sorted(unknown_source_ids)}",
                    field_path=f"research_checks.{check.check_id}.source_ids",
                )
            )
        if check.requirement_type == "data" and check.status in {
            CheckStatus.NO_MATERIAL_FINDING,
            CheckStatus.NOT_TRIGGERED,
        }:
            check.status = CheckStatus.DATA_UNAVAILABLE
            check.conclusion_zh = f"未能获取精确数据。{check.conclusion_zh}"

    market_candidates: dict[str, list[dict[str, Any]]] = {}
    provider_news_articles: list[dict[str, Any]] = []
    provider_news_urls: list[str] = []
    news_urls: set[str] = set()
    macro_metric_ids: set[str] = set()
    relative_metric_ids: set[str] = set()
    relative_metric_candidates: dict[str, dict[str, Any]] = {}
    if provider_data is not None:
        for record in provider_data.get("market", {}).get("records", []):
            market_candidates.setdefault(record["instrument_id"], []).append(record)
        provider_news_articles = [
            item
            for item in provider_data.get("news", {}).get("articles", [])
            if item.get("url")
        ]
        provider_news_urls = [
            str(item["url"])
            for item in provider_news_articles
        ]
        news_urls = {canonical_url(url) for url in provider_news_urls}
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
        relative_metric_candidates = {
            str(item["metric_id"]): item
            for item in provider_data.get("macro", {}).get("relative_metrics", [])
            if item.get("metric_id")
        }
        google_queries = [
            item
            for item in provider_data.get("news", {}).get("queries", [])
            if item.get("provider") == "google-news-rss" and not item.get("background", False)
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
    if provider_news_urls:
        for source in result.sources:
            if source.provider != "google-news-rss":
                continue
            current = canonical_url(str(source.url))
            if current in news_urls:
                continue
            current_parts = urlsplit(current)
            metadata_matches = []
            for article in provider_news_articles:
                published_at = article.get("published_at")
                article_time = None
                if published_at:
                    article_time = datetime.fromisoformat(str(published_at).replace("Z", "+00:00"))
                if (
                    source.published_at is not None
                    and article_time == source.published_at
                    and to_simplified_chinese(str(article.get("publisher", ""))) == source.publisher
                ):
                    metadata_matches.append(str(article["url"]))

            matches = metadata_matches if len(metadata_matches) == 1 else []
            if not matches:
                for candidate in provider_news_urls:
                    candidate_parts = urlsplit(canonical_url(candidate))
                    if (
                        current_parts.netloc == "news.google.com"
                        and candidate_parts.netloc == current_parts.netloc
                        and current_parts.path.startswith("/rss/articles/")
                        and (
                            candidate_parts.path.startswith(current_parts.path)
                            or current_parts.path.startswith(candidate_parts.path)
                        )
                    ):
                        matches.append(candidate)
            if len(matches) == 1:
                source.url = HttpUrl(matches[0])
                warnings.append(
                    TaskWarning(
                        code="NEWS_SOURCE_URL_RESTORED",
                        message_zh=f"已恢复 Google News 来源的完整 URL：{source.publisher}",
                        field_path=f"sources.{source.source_id}.url",
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
                matched_as_previous = False
                for item in candidates:
                    previous_close = _is_previous_close(price.kind)
                    if (
                        not previous_close
                        and price.previous_value is None
                        and item.get("previous_value") is not None
                        and item.get("previous_as_of") is not None
                        and abs(price.value - Decimal(str(item["previous_value"]))) <= Decimal("0.01")
                        and price.as_of == datetime.fromisoformat(str(item["previous_as_of"]).replace("Z", "+00:00"))
                    ):
                        previous_close = True
                    provider_value = item.get("previous_value") if previous_close else item.get("value")
                    if provider_value is None or abs(price.value - Decimal(str(provider_value))) > Decimal("0.01"):
                        continue
                    if (
                        not previous_close
                        and price.previous_value is not None
                        and item.get("previous_value") is not None
                        and abs(price.previous_value - Decimal(str(item["previous_value"]))) > Decimal("0.01")
                    ):
                        continue
                    matched_candidate = item
                    matched_as_previous = previous_close
                    break
                if matched_candidate is None:
                    raise ValidationFailure(f"price not found in provider data: {instrument.symbol}")

                previous_close = matched_as_previous
                authoritative_value = (
                    matched_candidate.get("previous_value")
                    if previous_close
                    else matched_candidate.get("value")
                )
                if previous_close:
                    price.kind = "previous_close"
                elif matched_candidate.get("session_date"):
                    instrument.trading_date = date.fromisoformat(str(matched_candidate["session_date"]))
                normalized = price.value != Decimal(str(authoritative_value))
                price.value = Decimal(str(authoritative_value))
                if (
                    not previous_close
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
        has_previous_close = any(_is_previous_close(price.kind) for price in instrument.prices)
        if provider_data is not None and not has_previous_close:
            yahoo_candidates = [
                item
                for item in market_candidates.get(instrument.instrument_id, [])
                if item.get("provider") == "yahoo-chart" and item.get("previous_value") is not None
            ]
            if len(yahoo_candidates) == 1:
                candidate = yahoo_candidates[0]
                candidate_url = canonical_url(str(candidate.get("source_url", "")))
                source_ids_for_previous = [
                    source.source_id
                    for source in result.sources
                    if canonical_url(str(source.url)) == candidate_url
                ]
                if not source_ids_for_previous:
                    yahoo_price = next(
                        (
                            price
                            for price in instrument.prices
                            if price.source_ids
                            and abs(price.value - Decimal(str(candidate["value"]))) <= Decimal("0.01")
                        ),
                        None,
                    )
                    if yahoo_price is not None:
                        source_ids_for_previous = list(yahoo_price.source_ids)
                if source_ids_for_previous:
                    instrument.prices.append(
                        PricePoint(
                            kind="previous_close",
                            value=Decimal(str(candidate["previous_value"])),
                            currency=str(candidate["currency"]),
                            as_of=candidate.get("previous_as_of") or candidate["as_of"],
                            source_ids=source_ids_for_previous,
                        )
                    )
                    warnings.append(
                        TaskWarning(
                            code="PREVIOUS_CLOSE_ADDED_FROM_PROVIDER",
                            message_zh=f"已从 Yahoo 连续交易日数据补全上一收盘价：{instrument.symbol}",
                            field_path=f"instruments.{instrument.instrument_id}.prices.previous_close",
                        )
                    )
        instrument.news = validated_news(instrument.news, f"instruments.{instrument.instrument_id}.news")

    result.section_news = validated_news(result.section_news, "section_news")

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
        candidate = relative_metric_candidates.get(metric.metric_id)
        if candidate is not None:
            normalized_observations = []
            for item in candidate.get("observations", []):
                numerator = Decimal(str(item["numerator_value"]))
                denominator = Decimal(str(item["denominator_value"]))
                normalized_observations.append(
                    RelativeObservation(
                        label=str(item["label"]),
                        as_of=item["as_of"],
                        numerator_value=numerator,
                        denominator_value=denominator,
                        ratio=Decimal(str(item.get("ratio") or (numerator / denominator))).quantize(Decimal("0.0001")),
                    )
                )
            normalized = (
                metric.numerator != str(candidate["numerator"])
                or metric.denominator != str(candidate["denominator"])
                or metric.observations != normalized_observations
            )
            metric.numerator = str(candidate["numerator"])
            metric.denominator = str(candidate["denominator"])
            metric.observations = normalized_observations
            if normalized:
                warnings.append(
                    TaskWarning(
                        code="RELATIVE_METRIC_PROVIDER_NORMALIZED",
                        message_zh=f"已按行情源候选值规范化相对比率：{metric.metric_id}",
                        field_path=f"relative_metrics.{metric.metric_id}",
                    )
                )
        for observation in metric.observations:
            expected_ratio = (observation.numerator_value / observation.denominator_value).quantize(Decimal("0.0001"))
            if observation.ratio != expected_ratio:
                if abs(observation.ratio - expected_ratio) > Decimal("0.0001"):
                    raise ValidationFailure(f"relative ratio mismatch: {metric.metric_id}")
                observation.ratio = expected_ratio
                warnings.append(
                    TaskWarning(
                        code="RELATIVE_RATIO_RECALCULATED",
                        message_zh=f"已按原始数值重算并规范化相对比率：{metric.metric_id} {observation.label}",
                        field_path=f"relative_metrics.{metric.metric_id}.{observation.label}.ratio",
                    )
                )

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

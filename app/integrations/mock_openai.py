from __future__ import annotations

import threading
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from app.modules.loader import ModuleConfig
from app.schemas.models import ResearchTaskResult, TaskError, TaskStatus


def _pct(current: Decimal, previous: Decimal) -> Decimal:
    return ((current - previous) / previous * Decimal("100")).quantize(Decimal("0.01"))


class MockResponsesClient:
    """Produces strict responses while preserving one-request-per-task isolation."""

    def __init__(self, fail_tasks: set[str] | None = None) -> None:
        self.fail_tasks = fail_tasks or set()
        self.calls: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def create(
        self,
        module: ModuleConfig,
        prompt: dict[str, object],
        provider_data: dict[str, Any],
    ) -> dict[str, Any]:
        request_id = f"mock_resp_{uuid.uuid4().hex}"
        with self._lock:
            self.calls.append({"request_id": request_id, "task_id": module.task_id, "prompt": prompt})

        context = prompt["run_context"]
        window = context["window"]
        if module.task_id in self.fail_tasks:
            return ResearchTaskResult(
                run_id=context["run_id"],
                request_id=request_id,
                task_id=module.task_id,
                title_zh=module.title_zh,
                status=TaskStatus.FAILED,
                window=window,
                errors=[
                    TaskError(
                        code="MOCK_TASK_FAILURE",
                        stage="responses_api",
                        message_zh="按测试参数注入的模拟任务失败。",
                        retryable=False,
                    )
                ],
            ).model_dump(mode="json")

        retrieved_at = context["scheduled_for"]
        sources: list[dict[str, Any]] = []
        source_by_url: dict[str, str] = {}

        def source_id(url: str, provider: str, publisher: str, published_at: str | None = None) -> str:
            if url in source_by_url:
                return source_by_url[url]
            identifier = f"src_{len(sources) + 1}"
            source_by_url[url] = identifier
            sources.append(
                {
                    "source_id": identifier,
                    "provider": provider,
                    "publisher": publisher,
                    "url": url,
                    "published_at": published_at,
                    "retrieved_at": retrieved_at,
                }
            )
            return identifier

        prices_by_instrument: dict[str, list[dict[str, Any]]] = {}
        for record in provider_data["market"]["records"]:
            sid = source_id(record["source_url"], provider_data["market"]["provider"], "Mock Exchange")
            current = Decimal(record["value"])
            previous = Decimal(record["previous_value"])
            prices_by_instrument.setdefault(record["instrument_id"], []).append(
                {
                    "kind": "latest" if record["symbol"] == "BTCUSDT" else "close",
                    "value": current,
                    "currency": record["currency"],
                    "as_of": record["as_of"],
                    "previous_value": previous,
                    "change_value": (current - previous).quantize(Decimal("0.01")),
                    "change_pct": _pct(current, previous),
                    "source_ids": [sid],
                }
            )

        news_by_instrument: dict[str, list[dict[str, Any]]] = {}
        for article in provider_data["news"]["articles"]:
            sid = source_id(
                article["url"],
                provider_data["news"]["provider"],
                article["publisher"],
                article["published_at"],
            )
            news_by_instrument.setdefault(article["instrument_id"], []).append(
                {
                    "headline": article["headline"],
                    "published_at": article["published_at"],
                    "summary_zh": article["summary_zh"],
                    "impact": article["impact"],
                    "rationale_zh": article["rationale_zh"],
                    "source_ids": [sid],
                    "outside_window": False,
                }
            )

        market_dates = context["market_dates"]
        instruments = []
        no_major_news = []
        for instrument in module.instruments:
            news = news_by_instrument.get(instrument.instrument_id, [])
            calendar_key = "HKEX" if instrument.exchange == "HKEX" else "NYMEX" if instrument.exchange == "NYMEX" else "US"
            if instrument.asset_class == "crypto":
                trading_date = datetime.fromisoformat(context["scheduled_for"]).date().isoformat()
            else:
                trading_date = market_dates[calendar_key]["latest_trading_day"]
            instruments.append(
                {
                    "instrument_id": instrument.instrument_id,
                    "symbol": instrument.symbol,
                    "name": instrument.name,
                    "asset_class": instrument.asset_class,
                    "exchange": instrument.exchange,
                    "currency": instrument.currency,
                    "trading_date": trading_date,
                    "prices": prices_by_instrument.get(instrument.instrument_id, []),
                    "news": news,
                }
            )
            if not news:
                no_major_news.append(
                    {
                        "instrument_id": instrument.instrument_id,
                        "checked_at": context["scheduled_for"],
                        "reason_zh": "模拟新闻源在研究窗口内未返回重大新闻。",
                    }
                )

        macro_observations = []
        for item in provider_data["macro"]["observations"]:
            sid = source_id(item["url"], provider_data["macro"]["provider"], "Mock Macro Authority")
            macro_observations.append({**{k: v for k, v in item.items() if k != "url"}, "source_ids": [sid]})

        relative_metrics = []
        for item in provider_data["macro"]["relative_metrics"]:
            sid = source_id(item["url"], provider_data["macro"]["provider"], "Mock Index Provider")
            observations = []
            for observation in item["observations"]:
                numerator = Decimal(observation["numerator_value"])
                denominator = Decimal(observation["denominator_value"])
                observations.append(
                    {
                        **observation,
                        "ratio": (numerator / denominator).quantize(Decimal("0.0001")),
                    }
                )
            relative_metrics.append(
                {
                    **{k: v for k, v in item.items() if k not in {"url", "observations"}},
                    "observations": observations,
                    "source_ids": [sid],
                }
            )

        research_checks: list[dict[str, Any]] = []

        def add_check(requirement_type: str, scope_id: str, requirement: str, status: str, conclusion: str) -> None:
            evidence_ids = [
                identifier
                for url, identifier in source_by_url.items()
                if "utm_" not in url and "fbclid=" not in url and "gclid=" not in url
            ]
            research_checks.append(
                {
                    "check_id": f"check_{len(research_checks) + 1}",
                    "requirement_type": requirement_type,
                    "scope_id": scope_id,
                    "requirement_zh": requirement,
                    "status": status,
                    "conclusion_zh": conclusion,
                    "source_ids": evidence_ids,
                }
            )

        for requirement in module.price_checks:
            add_check("price", module.task_id, requirement, "completed", "已用模拟行情数据完成该项核对。")
        for requirement in module.data_checks:
            add_check("data", module.task_id, requirement, "completed", "已用模拟宏观数据完成该项核对。")
        news_scopes = [item.instrument_id for item in module.instruments] or [module.task_id]
        for scope_id in news_scopes:
            has_news = bool(news_by_instrument.get(scope_id)) if module.instruments else bool(macro_observations)
            for index, requirement in enumerate(module.news_categories):
                status = "completed" if has_news and index == 0 else "no_material_finding"
                conclusion = "模拟数据包含符合该项的候选信息。" if status == "completed" else "模拟检索未发现该项重大信息。"
                add_check("news", scope_id, requirement, status, conclusion)
        for requirement in module.industry_topics:
            add_check("industry", module.task_id, requirement, "no_material_finding", "模拟检索未发现该专题重大信息。")
        for requirement in module.triggered_checks:
            add_check("trigger", module.task_id, requirement, "not_triggered", "模拟运行中触发条件未满足。")
        for instrument in module.instruments:
            for requirement in instrument.focus:
                add_check("instrument_focus", instrument.instrument_id, requirement, "no_material_finding", "已检查，模拟窗口内无重大信息。")

        return ResearchTaskResult(
            run_id=context["run_id"],
            request_id=request_id,
            task_id=module.task_id,
            title_zh=module.title_zh,
            status=TaskStatus.SUCCESS,
            window=window,
            instruments=instruments,
            macro_observations=macro_observations,
            relative_metrics=relative_metrics,
            research_checks=research_checks,
            sources=sources,
            no_major_news=no_major_news,
        ).model_dump(mode="json")

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class InstrumentConfig:
    instrument_id: str
    symbol: str
    name: str
    asset_class: str
    exchange: str
    currency: str
    aliases: tuple[str, ...]
    focus: tuple[str, ...]
    investment_context: tuple[str, ...]


@dataclass(frozen=True)
class ModuleConfig:
    task_id: str
    title_zh: str
    template: str
    research_context: tuple[str, ...]
    price_checks: tuple[str, ...]
    news_categories: tuple[str, ...]
    industry_topics: tuple[str, ...]
    triggered_checks: tuple[str, ...]
    data_checks: tuple[str, ...]
    source_requirements: tuple[str, ...]
    search_terms_zh: tuple[str, ...]
    search_terms_en: tuple[str, ...]
    background_search_terms_zh: tuple[str, ...]
    background_search_terms_en: tuple[str, ...]
    no_news_policy: str
    background_policy: str
    instruments: tuple[InstrumentConfig, ...]

    @property
    def topics(self) -> tuple[str, ...]:
        """Compatibility view for providers that still consume a flat topic list."""
        return self.news_categories + self.industry_topics


def load_module_configs(directory: Path) -> list[ModuleConfig]:
    configs: list[ModuleConfig] = []
    for path in sorted(directory.glob("*.toml")):
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        instruments = tuple(
            InstrumentConfig(
                instrument_id=item["instrument_id"],
                symbol=item["symbol"],
                name=item["name"],
                asset_class=item["asset_class"],
                exchange=item["exchange"],
                currency=item["currency"],
                aliases=tuple(item.get("aliases", [])),
                focus=tuple(item.get("focus", [])),
                investment_context=tuple(item.get("investment_context", [])),
            )
            for item in data.get("instruments", [])
        )
        configs.append(
            ModuleConfig(
                task_id=data["task_id"],
                title_zh=data["title_zh"],
                template=data["template"],
                research_context=tuple(data.get("research_context", [])),
                price_checks=tuple(data.get("price_checks", [])),
                news_categories=tuple(data.get("news_categories", [])),
                industry_topics=tuple(data.get("industry_topics", [])),
                triggered_checks=tuple(data.get("triggered_checks", [])),
                data_checks=tuple(data.get("data_checks", [])),
                source_requirements=tuple(data.get("source_requirements", [])),
                search_terms_zh=tuple(data.get("search_terms_zh", [])),
                search_terms_en=tuple(data.get("search_terms_en", [])),
                background_search_terms_zh=tuple(data.get("background_search_terms_zh", [])),
                background_search_terms_en=tuple(data.get("background_search_terms_en", [])),
                no_news_policy=data.get("no_news_policy", "窗口内无重大新闻时明确记录无重大新闻"),
                background_policy=data.get(
                    "background_policy",
                    "仅可补充窗口外但仍在发酵的重要背景，必须标注日期和 outside_window=true",
                ),
                instruments=instruments,
            )
        )
    task_ids = [config.task_id for config in configs]
    if len(task_ids) != len(set(task_ids)):
        raise ValueError("duplicate task_id in module configs")
    return configs

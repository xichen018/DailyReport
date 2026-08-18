from __future__ import annotations

from pathlib import Path

from app.modules.loader import ModuleConfig
from app.schemas.models import RunContext
from app.validators.result import required_research_check_plan


class PromptBuilder:
    def __init__(self, prompt_dir: Path) -> None:
        self.prompt_dir = prompt_dir

    def build(self, module: ModuleConfig, context: RunContext) -> dict[str, object]:
        common = (self.prompt_dir / "common_rules.md").read_text(encoding="utf-8")
        template = (self.prompt_dir / module.template).read_text(encoding="utf-8")
        return {
            "common_rules": common,
            "module_instructions": template,
            "module": {
                "task_id": module.task_id,
                "title_zh": module.title_zh,
                "price_checks": list(module.price_checks),
                "news_categories": list(module.news_categories),
                "industry_topics": list(module.industry_topics),
                "triggered_checks": list(module.triggered_checks),
                "source_requirements": list(module.source_requirements),
                "search_terms": {
                    "zh": list(module.search_terms_zh),
                    "en": list(module.search_terms_en),
                },
                "no_news_policy": module.no_news_policy,
                "background_policy": module.background_policy,
                "required_research_checks": required_research_check_plan(module),
                "instruments": [instrument.__dict__ for instrument in module.instruments],
            },
            "run_context": context.model_dump(mode="json"),
        }

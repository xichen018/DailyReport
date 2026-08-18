from __future__ import annotations

import json
from typing import Any

from app.modules.loader import ModuleConfig
from app.schemas.models import ResearchTaskResult


class OpenAIResponsesClient:
    def __init__(
        self,
        api_key: str,
        model: str,
        reasoning_effort: str = "low",
        base_url: str | None = None,
    ) -> None:
        from openai import OpenAI

        self.client = OpenAI(api_key=api_key, base_url=base_url)
        self.model = model
        self.reasoning_effort = reasoning_effort

    def create(
        self,
        module: ModuleConfig,
        prompt: dict[str, object],
        provider_data: dict[str, Any],
    ) -> dict[str, Any]:
        system_text = f"{prompt['common_rules']}\n\n{prompt['module_instructions']}"
        task_input = {
            "module": prompt["module"],
            "run_context": prompt["run_context"],
            "provider_data": provider_data,
        }
        response = self.client.responses.parse(
            model=self.model,
            reasoning={"effort": self.reasoning_effort},
            input=[
                {"role": "system", "content": system_text},
                {"role": "user", "content": json.dumps(task_input, ensure_ascii=False, default=str)},
            ],
            text_format=ResearchTaskResult,
        )
        parsed = response.output_parsed
        if parsed is None:
            raise RuntimeError("OpenAI response did not contain a parsed structured result")
        result = parsed.model_dump(mode="json")
        result["request_id"] = response.id
        return result

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

from app.modules.loader import ModuleConfig
from app.schemas.models import ResearchTaskResult


def _gateway_compatible_strict_schema() -> dict[str, Any]:
    """Keep strict structure while removing unsupported format annotations."""
    from openai.lib._pydantic import to_strict_json_schema

    schema = deepcopy(to_strict_json_schema(ResearchTaskResult))

    unsupported = {
        "format",
        "pattern",
        "minLength",
        "maxLength",
        "minItems",
        "maxItems",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "default",
    }

    def strip_unsupported_keywords(value: Any) -> None:
        if isinstance(value, dict):
            for keyword in unsupported:
                value.pop(keyword, None)
            for child in value.values():
                strip_unsupported_keywords(child)
        elif isinstance(value, list):
            for child in value:
                strip_unsupported_keywords(child)

    strip_unsupported_keywords(schema)
    return schema


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
        response = self.client.responses.create(
            model=self.model,
            reasoning={"effort": self.reasoning_effort},
            input=[
                {"role": "system", "content": system_text},
                {"role": "user", "content": json.dumps(task_input, ensure_ascii=False, default=str)},
            ],
            text={
                "format": {
                    "type": "json_schema",
                    "name": "ResearchTaskResult",
                    "schema": _gateway_compatible_strict_schema(),
                    "strict": True,
                }
            },
        )
        if not response.output_text:
            raise RuntimeError("OpenAI response did not contain structured JSON output")
        parsed = ResearchTaskResult.model_validate(json.loads(response.output_text))
        result = parsed.model_dump(mode="json")
        result["request_id"] = response.id
        return result

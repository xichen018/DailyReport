from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.modules.loader import ModuleConfig, load_module_configs
from app.orchestrator.context import build_run_context
from app.prompts.builder import PromptBuilder
from app.providers.base import ProviderBundle
from app.reporting.renderer import render_reports
from app.schemas.models import ResearchTaskResult, TaskError, TaskStatus
from app.validators.result import ValidationFailure, validate_result


LOGGER = logging.getLogger(__name__)


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


class DailyReportPipeline:
    def __init__(
        self,
        project_root: Path,
        providers: ProviderBundle,
        responses_client: Any,
        output_root: Path | None = None,
        max_workers: int = 6,
        task_ids: set[str] | None = None,
    ) -> None:
        self.project_root = project_root
        self.providers = providers
        self.responses_client = responses_client
        self.output_root = output_root or project_root / "data" / "runs"
        self.max_workers = max_workers
        self.prompt_builder = PromptBuilder(project_root / "app" / "prompts")
        all_modules = load_module_configs(project_root / "app" / "modules")
        if task_ids:
            known = {module.task_id for module in all_modules}
            unknown = task_ids - known
            if unknown:
                raise ValueError(f"unknown task ids: {sorted(unknown)}")
            self.modules = [module for module in all_modules if module.task_id in task_ids]
        else:
            self.modules = all_modules

    def _failed_result(self, module: ModuleConfig, context: Any, stage: str, exc: Exception) -> ResearchTaskResult:
        return ResearchTaskResult(
            run_id=context.run_id,
            request_id=f"failed_{module.task_id}",
            task_id=module.task_id,
            title_zh=module.title_zh,
            status=TaskStatus.FAILED,
            window=context.window,
            errors=[TaskError(code=type(exc).__name__.upper(), stage=stage, message_zh=str(exc), retryable=False)],
        )

    def _run_task(self, module: ModuleConfig, context: Any, run_dir: Path) -> ResearchTaskResult:
        try:
            provider_data = {
                "market": self.providers.market.get_task_data(module, context.scheduled_for),
                "news": self.providers.news.get_task_data(module, context.window.start_at, context.window.end_at),
                "macro": self.providers.macro.get_task_data(module, context.window.start_at, context.window.end_at),
            }
            _write_json(run_dir / "raw" / "providers" / module.task_id / "bundle.json", provider_data)
            prompt = self.prompt_builder.build(module, context)
            raw = self.responses_client.create(module, prompt, provider_data)
            _write_json(run_dir / "raw" / "openai" / f"{module.task_id}.json", raw)
            parsed = ResearchTaskResult.model_validate(raw)
            validated = validate_result(parsed, module, provider_data)
        except (ValidationError, ValidationFailure, Exception) as exc:
            LOGGER.exception("task failed: %s", module.task_id)
            validated = self._failed_result(module, context, "task_pipeline", exc)
        _write_json(run_dir / "validated" / f"{module.task_id}.json", validated.model_dump(mode="json"))
        return validated

    def run(self, scheduled_for: datetime | None = None) -> dict[str, Any]:
        context = build_run_context(self.providers.calendar, scheduled_for=scheduled_for)
        run_dir = self.output_root / context.run_id
        run_dir.mkdir(parents=True, exist_ok=False)
        _write_json(run_dir / "run_context.json", context.model_dump(mode="json"))

        result_by_task: dict[str, ResearchTaskResult] = {}
        with ThreadPoolExecutor(max_workers=min(self.max_workers, len(self.modules))) as executor:
            future_map = {executor.submit(self._run_task, module, context, run_dir): module for module in self.modules}
            for future in as_completed(future_map):
                module = future_map[future]
                result_by_task[module.task_id] = future.result()
        results = [result_by_task[module.task_id] for module in self.modules]

        merged = {
            "run_context": context.model_dump(mode="json"),
            "tasks": [result.model_dump(mode="json") for result in results],
        }
        _write_json(run_dir / "merged" / "report_input.json", merged)
        reports = render_reports(run_dir / "reports", context, results)
        status = "success" if all(item.status == TaskStatus.SUCCESS for item in results) else "partial_success" if any(item.status != TaskStatus.FAILED for item in results) else "failed"
        manifest = {
            "run_id": context.run_id,
            "status": status,
            "tasks": {item.task_id: item.status.value for item in results},
            "reports": reports,
            "request_ids": [item.request_id for item in results],
        }
        _write_json(run_dir / "run_manifest.json", manifest)
        log_dir = run_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / "run.jsonl").write_text(json.dumps(manifest, ensure_ascii=False) + "\n", encoding="utf-8")
        return {"run_dir": str(run_dir), **manifest}

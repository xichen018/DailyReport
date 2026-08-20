from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from app.integrations.mock_openai import MockResponsesClient
from app.integrations.openai_responses import OpenAIResponsesClient
from app.integrations.secrets import load_secrets, require_secret
from app.modules.loader import load_module_configs
from app.orchestrator.pipeline import DailyReportPipeline
from app.providers.mock import MockProviderBundle
from app.providers.http import build_free_provider_bundle
from app.schemas.models import ResearchTaskResult
from app.settings import Settings


PROJECT_ROOT = Path(__file__).resolve().parents[1]
HKT = ZoneInfo("Asia/Hong_Kong")


def _parse_as_of(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=HKT) if parsed.tzinfo is None else parsed.astimezone(HKT)


def command_run(args: argparse.Namespace) -> int:
    settings = Settings.from_env(args.mode)
    if settings.mode == "real":
        secrets = load_secrets(settings)
        providers = build_free_provider_bundle(secrets.get("marketaux_api_token"), settings.request_timeout_seconds)
        client = OpenAIResponsesClient(
            api_key=require_secret(secrets, "openai_api_key"),
            model=settings.openai_model,
            reasoning_effort=settings.openai_reasoning_effort,
            base_url=settings.openai_base_url,
        )
    else:
        providers = MockProviderBundle()
        client = MockResponsesClient(set(args.fail_task))
    pipeline = DailyReportPipeline(
        project_root=PROJECT_ROOT,
        providers=providers,
        responses_client=client,
        output_root=Path(args.output_root).resolve() if args.output_root else None,
        task_ids=set(args.task) if args.task else None,
        report_mode=settings.mode,
    )
    result = pipeline.run(scheduled_for=_parse_as_of(args.as_of))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] in {"success", "partial_success"} else 1


def command_healthcheck(args: argparse.Namespace) -> int:
    configs = load_module_configs(PROJECT_ROOT / "app" / "modules")
    if len(configs) != 6:
        raise RuntimeError(f"expected 6 modules, found {len(configs)}")
    output_root = Path(args.output_root).resolve() if args.output_root else PROJECT_ROOT / "data" / "runs"
    output_root.mkdir(parents=True, exist_ok=True)
    if not os.access(output_root, os.W_OK):
        raise RuntimeError(f"output directory is not writable: {output_root}")
    settings = Settings.from_env(args.mode)
    secret_status = None
    if settings.mode == "real":
        secrets = load_secrets(settings)
        secret_status = {
            "openai_api_key": bool(secrets.get("openai_api_key")),
            "marketaux_api_token": bool(secrets.get("marketaux_api_token")),
        }
    print(json.dumps({"status": "ok", "mode": settings.mode, "modules": len(configs), "output_root": str(output_root), "secrets_available": secret_status}, ensure_ascii=False))
    return 0


def command_schema(_: argparse.Namespace) -> int:
    print(json.dumps(ResearchTaskResult.model_json_schema(), ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Automated financial daily report")
    subparsers = parser.add_subparsers(dest="command", required=True)
    run = subparsers.add_parser("run", help="run the mock pipeline")
    run.add_argument("--mode", choices=("mock", "real"), default=None)
    run.add_argument("--as-of", help="ISO 8601 run time; defaults to current HKT")
    run.add_argument("--output-root")
    run.add_argument("--fail-task", action="append", default=[], help="inject a task failure")
    run.add_argument("--task", action="append", default=[], help="run only this task_id; may be repeated")
    run.set_defaults(func=command_run)
    health = subparsers.add_parser("healthcheck")
    health.add_argument("--mode", choices=("mock", "real"), default=None)
    health.add_argument("--output-root")
    health.set_defaults(func=command_healthcheck)
    schema = subparsers.add_parser("schema")
    schema.set_defaults(func=command_schema)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        return args.func(args)
    except Exception as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

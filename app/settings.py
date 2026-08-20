from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    mode: str = "mock"
    openai_model: str = "gpt-5.6-terra"
    openai_reasoning_effort: str = "low"
    openai_max_workers: int = 2
    openai_base_url: str | None = None
    request_timeout_seconds: float = 25.0
    aws_region: str = "ap-southeast-1"
    secret_id: str | None = None
    ses_sender: str = ""
    ses_recipients: tuple[str, ...] = ()

    @classmethod
    def from_env(cls, mode: str | None = None) -> "Settings":
        return cls(
            mode=mode or os.getenv("DAILY_REPORT_MODE", "mock"),
            openai_model=os.getenv("OPENAI_MODEL", "gpt-5.6-terra"),
            openai_reasoning_effort=os.getenv("OPENAI_REASONING_EFFORT", "low"),
            openai_max_workers=max(1, int(os.getenv("OPENAI_MAX_WORKERS", "2"))),
            openai_base_url=os.getenv("OPENAI_BASE_URL"),
            request_timeout_seconds=float(os.getenv("HTTP_TIMEOUT_SECONDS", "25")),
            aws_region=os.getenv("AWS_REGION", "ap-southeast-1"),
            secret_id=os.getenv("DAILY_REPORT_SECRET_ID"),
            ses_sender=os.getenv("SES_SENDER", "").strip(),
            ses_recipients=tuple(
                item.strip()
                for item in os.getenv("SES_RECIPIENTS", "").split(",")
                if item.strip()
            ),
        )

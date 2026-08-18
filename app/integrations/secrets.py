from __future__ import annotations

import json
import os
from typing import Mapping

from app.settings import Settings


SECRET_ENV_NAMES = {
    "openai_api_key": "OPENAI_API_KEY",
    "marketaux_api_token": "MARKETAUX_API_TOKEN",
}


def load_secrets(settings: Settings) -> dict[str, str]:
    """Load secret values without logging or persisting them."""
    if settings.secret_id:
        import boto3

        response = boto3.client("secretsmanager", region_name=settings.aws_region).get_secret_value(
            SecretId=settings.secret_id
        )
        payload = response.get("SecretString")
        if not payload:
            raise RuntimeError("Secrets Manager response did not contain SecretString")
        parsed = json.loads(payload)
        if not isinstance(parsed, dict):
            raise RuntimeError("Secrets Manager payload must be a JSON object")
        return {str(key): str(value) for key, value in parsed.items()}
    return {
        key: value
        for key, env_name in SECRET_ENV_NAMES.items()
        if (value := os.getenv(env_name))
    }


def require_secret(secrets: Mapping[str, str], name: str) -> str:
    value = secrets.get(name)
    if not value:
        raise RuntimeError(f"required secret is unavailable: {name}")
    return value

from __future__ import annotations

from functools import lru_cache
from typing import Any


@lru_cache(maxsize=1)
def _converter() -> Any:
    from opencc import OpenCC

    return OpenCC("t2s")


def to_simplified_chinese(value: str) -> str:
    return _converter().convert(value)


def simplify_strings(value: Any, *, _key: str | None = None) -> Any:
    if isinstance(value, str):
        if _key == "url":
            return value
        return to_simplified_chinese(value)
    if isinstance(value, dict):
        return {key: simplify_strings(item, _key=key) for key, item in value.items()}
    if isinstance(value, list):
        return [simplify_strings(item, _key=_key) for item in value]
    return value

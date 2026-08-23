from __future__ import annotations

import hashlib
import json
import re
import subprocess
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from xml.etree import ElementTree

from pydantic import BaseModel, ConfigDict, Field


class ResearchRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    record_id: str
    title: str
    source_name: str
    author: str | None = None
    published_on: date | None = None
    imported_at: datetime
    source_locator: str
    source_kind: Literal["user_material", "public_url"]
    assets: list[str] = Field(default_factory=list)
    topics: list[str] = Field(default_factory=list)
    horizon: Literal["tactical", "structural"] = "tactical"
    valid_until: date
    review_on: date
    status: Literal["active", "expired", "superseded"] = "active"
    verified_facts: list[str] = Field(default_factory=list)
    author_views: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    transmission: list[str] = Field(default_factory=list)
    bullish_conditions: list[str] = Field(default_factory=list)
    bearish_conditions: list[str] = Field(default_factory=list)
    catalysts: list[str] = Field(default_factory=list)
    invalidation_conditions: list[str] = Field(default_factory=list)
    excerpt: str = Field(max_length=2400)


def _extract_docx(path: Path) -> str:
    with zipfile.ZipFile(path) as archive:
        root = ElementTree.fromstring(archive.read("word/document.xml"))
    return "\n".join("".join(node.itertext()) for node in root.iter() if node.tag.endswith("}p"))


def _extract_text(path: Path) -> str:
    suffix = path.suffix.casefold()
    if suffix in {".txt", ".md"}:
        return path.read_text(encoding="utf-8")
    if suffix == ".docx":
        return _extract_docx(path)
    if suffix == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError:
            completed = subprocess.run(
                ["pdftotext", "-layout", str(path), "-"],
                check=True, capture_output=True, text=True, encoding="utf-8",
            )
            return completed.stdout
        return "\n".join(page.extract_text() or "" for page in PdfReader(path).pages)
    raise ValueError(f"unsupported research material: {suffix}")


def _sentences(text: str) -> list[str]:
    compact = re.sub(r"[ \t]+", " ", text)
    return [item.strip() for item in re.split(r"(?<=[。！？.!?])\s+|\n+", compact) if len(item.strip()) >= 12]


def _matching(sentences: list[str], keywords: tuple[str, ...], limit: int = 6) -> list[str]:
    return [sentence for sentence in sentences if any(keyword in sentence for keyword in keywords)][:limit]


def import_material(
    locator: str,
    library_root: Path,
    *,
    source_name: str,
    author: str | None = None,
    published_on: date | None = None,
    assets: list[str] | None = None,
    topics: list[str] | None = None,
    horizon: Literal["tactical", "structural"] = "tactical",
) -> ResearchRecord:
    parsed = urlsplit(locator)
    if parsed.scheme in {"http", "https"}:
        import httpx

        response = httpx.get(locator, timeout=25, follow_redirects=True, headers={"User-Agent": "DailyReport/0.3"})
        response.raise_for_status()
        text = re.sub(r"<[^>]+>", " ", response.text)
        source_kind = "public_url"
        title = parsed.netloc
    else:
        path = Path(locator).expanduser().resolve()
        text = _extract_text(path)
        source_kind = "user_material"
        title = path.stem
        stored_locator = path.name
    if source_kind == "public_url":
        stored_locator = locator
    sentences = _sentences(text)
    now = datetime.now(timezone.utc)
    lifetime = 30 if horizon == "tactical" else 180
    base_date = published_on or now.date()
    digest = hashlib.sha256(f"{locator}\n{text}".encode("utf-8")).hexdigest()[:16]
    record = ResearchRecord(
        record_id=f"research_{digest}", title=title, source_name=source_name, author=author,
        published_on=published_on, imported_at=now, source_locator=stored_locator, source_kind=source_kind,
        assets=assets or [], topics=topics or [], horizon=horizon,
        valid_until=base_date + timedelta(days=lifetime), review_on=base_date + timedelta(days=lifetime),
        author_views=sentences[:12],
        assumptions=_matching(sentences, ("假设", "前提")),
        transmission=_matching(sentences, ("驱动", "传导", "导致", "影响")),
        bullish_conditions=_matching(sentences, ("若走强", "站稳", "守住", "向上突破")),
        bearish_conditions=_matching(sentences, ("若走弱", "跌破", "探底", "下行")),
        catalysts=_matching(sentences, ("关键日期", "会议", "投票", "公布", "财报", "解锁")),
        invalidation_conditions=_matching(sentences, ("失效", "跌破", "不再成立")),
        excerpt="\n".join(sentences[:16])[:2400],
    )
    library_root.mkdir(parents=True, exist_ok=True)
    target = library_root / f"{record.record_id}.json"
    if not target.exists():
        target.write_text(json.dumps(record.model_dump(mode="json"), ensure_ascii=False, indent=2), encoding="utf-8")
    return record


class ResearchLibrary:
    def __init__(self, root: Path) -> None:
        self.root = root

    def records(self, as_of: date | None = None) -> list[ResearchRecord]:
        today = as_of or date.today()
        records: list[ResearchRecord] = []
        if not self.root.exists():
            return records
        for path in sorted(self.root.glob("research_*.json")):
            record = ResearchRecord.model_validate_json(path.read_text(encoding="utf-8"))
            if record.status == "active" and record.valid_until >= today:
                records.append(record)
        return records

    def relevant(self, terms: set[str], as_of: date, limit: int = 8) -> list[ResearchRecord]:
        normalized = {term.casefold() for term in terms if term}
        scored: list[tuple[int, ResearchRecord]] = []
        for record in self.records(as_of):
            haystack = " ".join([record.title, *record.assets, *record.topics, record.excerpt]).casefold()
            score = sum(term in haystack for term in normalized)
            if score:
                scored.append((score, record))
        return [item for _, item in sorted(scored, key=lambda pair: (pair[0], pair[1].imported_at), reverse=True)[:limit]]

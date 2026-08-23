from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit


@dataclass(frozen=True)
class SourcePolicy:
    tier: str
    content_access: str
    role: str


PRIMARY = SourcePolicy("primary", "public", "confirmed_fact")
PROFESSIONAL = SourcePolicy("professional_media", "snippet_only", "reported_fact_or_view")
SPECIALIST = SourcePolicy("specialist_media", "public", "reported_fact_or_view")
AGGREGATOR = SourcePolicy("aggregator", "snippet_only", "discovery_only")

SOURCE_POLICIES: dict[str, SourcePolicy] = {
    "bls.gov": PRIMARY,
    "bea.gov": PRIMARY,
    "census.gov": PRIMARY,
    "federalreserve.gov": PRIMARY,
    "sec.gov": PRIMARY,
    "eia.gov": PRIMARY,
    "opec.org": PRIMARY,
    "hkexnews.hk": PRIMARY,
    "wsj.com": PROFESSIONAL,
    "ft.com": PROFESSIONAL,
    "thedefiant.io": SPECIALIST,
    "chainfeeds.xyz": SPECIALIST,
    "investing.com": SourcePolicy("calendar_secondary", "public", "consensus_or_prior"),
    "news.google.com": AGGREGATOR,
    "gdeltproject.org": AGGREGATOR,
    "marketaux.com": AGGREGATOR,
}


def source_policy(url: str, publisher: str | None = None) -> SourcePolicy:
    host = (urlsplit(url).hostname or "").lower().removeprefix("www.")
    for domain, policy in SOURCE_POLICIES.items():
        if policy.tier == "aggregator":
            continue
        if host == domain or host.endswith(f".{domain}"):
            return policy
    normalized = (publisher or "").casefold()
    if any(name in normalized for name in (
        "bureau of labor statistics", "bureau of economic analysis", "federal reserve",
        "u.s. census", "energy information administration", "opec", "sec.gov",
        "securities and exchange commission", "hong kong exchanges",
    )):
        return SourcePolicy("primary", "snippet_only", "confirmed_fact")
    if "wall street journal" in normalized or normalized == "wsj" or "financial times" in normalized:
        return PROFESSIONAL
    if "the defiant" in normalized or "chainfeeds" in normalized:
        return SourcePolicy("specialist_media", "snippet_only", "reported_fact_or_view")
    for domain, policy in SOURCE_POLICIES.items():
        if host == domain or host.endswith(f".{domain}"):
            return policy
    return SourcePolicy("secondary", "snippet_only", "reported_fact")

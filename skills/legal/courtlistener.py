"""
CourtListener API client for legal research.
Provides access to US court opinions, PACER data, and legal citations.
https://www.courtlistener.com/api/
"""
from dataclasses import dataclass
import httpx

from skills.shared import get_logger, audit_log, retry

logger = get_logger("courtlistener")

COURTLISTENER_BASE = "https://www.courtlistener.com/api/rest/v4"


@dataclass
class CourtOpinion:
    case_name: str
    court: str
    date_filed: str
    citation: str
    docket_number: str
    summary: str
    url: str
    relevance_score: float


@dataclass
class LegalSearchResult:
    query: str
    total_results: int
    opinions: list[CourtOpinion]
    jurisdiction_note: str


@retry(max_attempts=3, base_delay=1.0, retryable_exceptions=(httpx.ConnectError, httpx.ReadTimeout))
async def search_opinions(
    query: str,
    court: str = None,
    filed_after: str = None,
    filed_before: str = None,
    limit: int = 10,
) -> LegalSearchResult:
    """
    Search court opinions on CourtListener.
    Free API — no auth required for basic searches.
    """
    params = {
        "q": query,
        "order_by": "score desc",
        "page_size": min(limit, 20),
    }

    if court:
        params["court"] = court
    if filed_after:
        params["filed_after"] = filed_after
    if filed_before:
        params["filed_before"] = filed_before

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            f"{COURTLISTENER_BASE}/search/",
            params=params,
            headers={
                "Accept": "application/json",
                "User-Agent": "FinTechBot-LegalResearch/1.0",
            },
        )
        resp.raise_for_status()
        data = resp.json()

    opinions = []
    for result in data.get("results", []):
        # Extract citation
        citation_parts = []
        if result.get("citation"):
            citation_parts.append(str(result["citation"]))
        elif result.get("caseName"):
            citation_parts.append(result["caseName"])

        opinions.append(CourtOpinion(
            case_name=result.get("caseName", "Unknown"),
            court=result.get("court", "Unknown Court"),
            date_filed=result.get("dateFiled", ""),
            citation=" ".join(citation_parts) or "No citation available",
            docket_number=result.get("docketNumber", ""),
            summary=result.get("snippet", "")[:500],
            url=f"https://www.courtlistener.com{result.get('absolute_url', '')}",
            relevance_score=float(result.get("score", 0)),
        ))

    audit_log("legal-agent", "courtlistener_search", {
        "query": query,
        "results": len(opinions),
        "court_filter": court,
    })

    return LegalSearchResult(
        query=query,
        total_results=data.get("count", 0),
        opinions=opinions,
        jurisdiction_note="Results are from US federal and state courts via CourtListener.",
    )


@retry(max_attempts=2, base_delay=1.0, retryable_exceptions=(httpx.ConnectError,))
async def get_opinion_detail(opinion_id: str) -> dict:
    """Fetch full details for a specific opinion."""
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            f"{COURTLISTENER_BASE}/opinions/{opinion_id}/",
            headers={
                "Accept": "application/json",
                "User-Agent": "FinTechBot-LegalResearch/1.0",
            },
        )
        resp.raise_for_status()
        return resp.json()


@retry(max_attempts=3, base_delay=1.0, retryable_exceptions=(httpx.ConnectError, httpx.ReadTimeout))
async def search_sec_dockets(
    query: str,
    limit: int = 10,
) -> list[dict]:
    """Search for SEC-related dockets on CourtListener."""
    params = {
        "q": query,
        "court": "scotus,ca1,ca2,ca3,ca4,ca5,ca6,ca7,ca8,ca9,ca10,ca11,cadc,cafc",
        "order_by": "score desc",
        "page_size": min(limit, 20),
    }

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            f"{COURTLISTENER_BASE}/dockets/",
            params=params,
            headers={
                "Accept": "application/json",
                "User-Agent": "FinTechBot-LegalResearch/1.0",
            },
        )
        resp.raise_for_status()
        data = resp.json()

    return [
        {
            "case_name": d.get("case_name", ""),
            "court": d.get("court", ""),
            "date_filed": d.get("date_filed", ""),
            "docket_number": d.get("docket_number", ""),
            "url": f"https://www.courtlistener.com{d.get('absolute_url', '')}",
        }
        for d in data.get("results", [])
    ]


def format_search_results(result: LegalSearchResult) -> str:
    """Format legal search results for messaging."""
    lines = [
        f"📚 Legal Research: \"{result.query}\"",
        f"Results: {result.total_results} total ({len(result.opinions)} shown)",
        f"Source: {result.jurisdiction_note}",
        "",
    ]

    for i, op in enumerate(result.opinions, 1):
        lines.append(f"**{i}. {op.case_name}**")
        lines.append(f"   Court: {op.court}")
        lines.append(f"   Filed: {op.date_filed}")
        lines.append(f"   Citation: {op.citation}")
        if op.summary:
            # Clean HTML from snippet
            import re
            clean_summary = re.sub(r'<[^>]+>', '', op.summary)
            lines.append(f"   Summary: {clean_summary[:200]}...")
        lines.append(f"   Link: {op.url}")
        lines.append("")

    lines.append("⚖️ This is an AI-assisted search, not legal advice. Verify all citations.")

    return "\n".join(lines)

"""
Governance integration via Snapshot.org GraphQL API.
Fetches active proposals and voting power for tracked protocols.
"""
from dataclasses import dataclass
from typing import Optional
import httpx

from skills.shared import get_logger, audit_log, retry

logger = get_logger("governance")

SNAPSHOT_GRAPHQL = "https://hub.snapshot.org/graphql"

# Snapshot space IDs for tracked protocols
PROTOCOL_SPACES = {
    "uniswap": "uniswapgovernance.eth",
    "aave": "aave.eth",
    "compound": "comp-vote.eth",
    "curve": "curve.eth",
    "lido": "lido-snapshot.eth",
    "sushiswap": "sushigov.eth",
}


@dataclass
class GovernanceProposal:
    protocol: str
    proposal_id: str
    title: str
    body_summary: str
    status: str  # active, closed, pending
    start: int  # unix timestamp
    end: int  # unix timestamp
    choices: list[str]
    scores: list[float]
    votes_count: int
    quorum: float
    author: str
    link: str


@retry(max_attempts=3, base_delay=1.0, retryable_exceptions=(httpx.ConnectError, httpx.ReadTimeout))
async def fetch_proposals(
    protocol: str,
    status: str = "active",
    limit: int = 10,
) -> list[GovernanceProposal]:
    """Fetch governance proposals from Snapshot.org."""
    space = PROTOCOL_SPACES.get(protocol.lower())
    if not space:
        logger.warning(f"Unknown protocol: {protocol}. Known: {list(PROTOCOL_SPACES.keys())}")
        return []

    query = """
    query Proposals($space: String!, $state: String!, $first: Int!) {
        proposals(
            where: { space: $space, state: $state }
            orderBy: "created"
            orderDirection: desc
            first: $first
        ) {
            id
            title
            body
            state
            start
            end
            choices
            scores
            votes
            quorum
            author
            link
        }
    }
    """

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            SNAPSHOT_GRAPHQL,
            json={
                "query": query,
                "variables": {
                    "space": space,
                    "state": status,
                    "first": limit,
                },
            },
        )
        resp.raise_for_status()
        data = resp.json()

    proposals = []
    for p in data.get("data", {}).get("proposals", []):
        # Truncate body for summary
        body = p.get("body", "")
        summary = body[:500] + "..." if len(body) > 500 else body

        proposals.append(GovernanceProposal(
            protocol=protocol,
            proposal_id=p["id"],
            title=p["title"],
            body_summary=summary,
            status=p["state"],
            start=p["start"],
            end=p["end"],
            choices=p.get("choices", []),
            scores=p.get("scores", []),
            votes_count=p.get("votes", 0),
            quorum=p.get("quorum", 0),
            author=p.get("author", ""),
            link=p.get("link", f"https://snapshot.org/#/{space}/proposal/{p['id']}"),
        ))

    audit_log("defi-agent", "governance_fetched", {
        "protocol": protocol,
        "space": space,
        "proposals_found": len(proposals),
        "status_filter": status,
    })

    return proposals


@retry(max_attempts=2, base_delay=1.0, retryable_exceptions=(httpx.ConnectError,))
async def get_voting_power(
    protocol: str,
    voter_address: str,
    proposal_id: str,
) -> float:
    """Get a wallet's voting power for a specific proposal."""
    space = PROTOCOL_SPACES.get(protocol.lower())
    if not space:
        return 0.0

    query = """
    query VotingPower($space: String!, $voter: String!, $proposal: String!) {
        vp(
            space: $space
            voter: $voter
            proposal: $proposal
        ) {
            vp
            vp_by_strategy
            vp_state
        }
    }
    """

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            SNAPSHOT_GRAPHQL,
            json={
                "query": query,
                "variables": {
                    "space": space,
                    "voter": voter_address,
                    "proposal": proposal_id,
                },
            },
        )
        resp.raise_for_status()
        data = resp.json()

    vp_data = data.get("data", {}).get("vp", {})
    return float(vp_data.get("vp", 0))


def format_proposal(proposal: GovernanceProposal, voting_power: float = 0) -> str:
    """Format a governance proposal for messaging."""
    from datetime import datetime

    end_dt = datetime.utcfromtimestamp(proposal.end).strftime("%Y-%m-%d %H:%M UTC")

    # Calculate vote percentages
    total_score = sum(proposal.scores) if proposal.scores else 1
    vote_bars = []
    for choice, score in zip(proposal.choices, proposal.scores):
        pct = (score / total_score * 100) if total_score > 0 else 0
        bar_len = int(pct / 5)  # 20 chars max
        bar = "█" * bar_len + "░" * (20 - bar_len)
        vote_bars.append(f"  {choice}: {bar} {pct:.1f}%")

    lines = [
        f"🗳️ Governance: {proposal.protocol.upper()}",
        f"Proposal: {proposal.title}",
        f"Status: {proposal.status}",
        f"Voting ends: {end_dt}",
        f"Votes: {proposal.votes_count}",
        f"",
        "Current results:",
        *vote_bars,
    ]

    if voting_power > 0:
        lines.append(f"\nYour voting power: {voting_power:,.0f}")

    lines.append(f"\nLink: {proposal.link}")
    lines.append(f"⏳ Reply with your vote choice to cast a vote.")

    return "\n".join(lines)


async def check_all_governance(
    protocols: list[str] = None,
    voter_address: str = None,
) -> list[dict]:
    """Check all tracked protocols for active governance proposals."""
    protocols = protocols or list(PROTOCOL_SPACES.keys())
    results = []

    for protocol in protocols:
        try:
            proposals = await fetch_proposals(protocol, status="active")
            for prop in proposals:
                vp = 0.0
                if voter_address:
                    try:
                        vp = await get_voting_power(protocol, voter_address, prop.proposal_id)
                    except Exception:
                        pass

                results.append({
                    "protocol": protocol,
                    "proposal_id": prop.proposal_id,
                    "title": prop.title,
                    "status": prop.status,
                    "end": prop.end,
                    "votes": prop.votes_count,
                    "voting_power": vp,
                    "message": format_proposal(prop, vp),
                })
        except Exception as e:
            logger.error(f"Governance check failed for {protocol}: {e}")

    return results

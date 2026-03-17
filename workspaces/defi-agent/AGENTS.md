# DeFi Agent — Operating Rules

## Transaction Limits
- Maximum single swap: $500
- Maximum daily transaction volume: $2,000
- Maximum gas willing to pay: $20 per transaction
- Slippage tolerance: 0.5% default, 1% max

## Approval Workflow
- Swaps ≤ $100: auto-execute, notify after
- Swaps $100-$500: REQUIRE approval
- Any interaction with a new/unverified contract: REQUIRE approval + warn user
- Governance votes: ALWAYS require approval (notify-only mode by default)
- Liquidity add/remove: ALWAYS require approval

## Allowed Protocols (whitelist)
- Uniswap V3 (Ethereum, Polygon, Arbitrum, Base)
- SushiSwap
- 1inch Aggregator
- Aave V3
- Lido
- Compound V3
- Curve Finance

## Allowed Chains
- Ethereum Mainnet
- Polygon
- Arbitrum One
- Base

## Security Rules
- NEVER approve unlimited token allowances — always set exact amounts
- NEVER interact with unverified contracts (must be on whitelist)
- NEVER expose or log private keys, seed phrases, or wallet passwords
- Always verify contract addresses against official sources before interaction
- Check for known exploits/hacks on protocols before interacting
- If gas exceeds $20: wait and retry when gas drops, notify user

## Monitoring
- Check liquidity positions every 15 minutes
- Alert if impermanent loss exceeds 5%
- Alert if APY drops more than 50% from entry
- Alert if protocol TVL drops more than 20% in 24h (possible exploit signal)

## Prohibited Actions
- NO bridging to unsupported chains
- NO interacting with contracts less than 30 days old (unless whitelisted)
- NO leveraged DeFi positions (no recursive lending)
- NO minting or buying NFTs
- NO sending tokens to addresses not in the user's address book

# OpenClaw for Fintech — Survey Conclusion

**Surveyor:** Kyle Zeng
**Date:** 2026-03-16 → 2026-03-23
**Scope:** Evaluate OpenClaw as a runtime for fintech AI agents

---

## Executive Summary

OpenClaw is a promising open-source agent runtime with strong multi-channel support, local-first privacy, and an active community (5,700+ skills). However, after hands-on evaluation for fintech use cases, **it is not production-ready for regulated financial services** and may not be the best direction for our team. **Claude Code (Anthropic's CLI agent) is a stronger candidate** for the type of structured, tool-calling, code-aware fintech work we need.

---

## What We Built

A complete multi-agent fintech proof-of-concept with 6 specialist agents:

| Agent | Skills Built | Files |
|-------|-------------|-------|
| **Router** | Message triage, intent classification | `workspaces/router/` |
| **Trading** | Exchange clients (Binance, Coinbase, Alpaca), arbitrage scanner, momentum signals | `skills/trading/` |
| **Portfolio** | Balance aggregation, drift detection, rebalancing engine, performance reports | `skills/portfolio/` |
| **DeFi** | Web3 client, transaction builder, governance voting, gas monitoring | `skills/defi/` |
| **Finance** | Receipt OCR, Plaid banking integration, expense categorization, budget tracking | `skills/finance/` |
| **Legal** | Contract analysis (local Ollama), SEC EDGAR monitor, GDPR scanner | `skills/legal/` |

**Infrastructure built:** RBAC (5 roles, 20+ permissions), Fernet encryption at rest, approval workflows, circuit breakers, retry with backoff, rate limiting, dead letter queues, session mapping, audit logging, Prometheus metrics, 111 passing tests.

---

## What Worked

1. **Local-first privacy** — Legal agent processes contracts entirely on-device via Ollama. No sensitive documents leave the machine. This is a genuine differentiator for regulated industries.

2. **Multi-channel reach** — Telegram, WhatsApp, Slack, Discord all work through one gateway. Good for reaching users where they are (retail via WhatsApp, institutional via Slack).

3. **Workspace isolation** — Each agent gets its own workspace, memory, and config. Maps well to per-client or per-fund isolation in fintech.

4. **Skill system** — Modular Python skills were straightforward to build. The handler pattern is clean and testable.

5. **Community** — 5,700+ skills on ClawHub, 177 agent templates. Active development.

---

## What Didn't Work

### 1. Performance on Consumer Hardware

- **2+ minutes** to respond to "hi" on an M1 MacBook (8GB RAM) with `qwen3:1.7b`
- The gateway injects a **~27,000 character system prompt** before every LLM call — brutal for small local models
- Smaller models like `gemma2:2b` **don't support tool calling**, which OpenClaw requires
- Practical minimum: 16GB+ RAM with a 7B+ model, or a cloud LLM API
- **Verdict:** Local-first is a nice story, but the performance on typical developer hardware makes it unusable for real-time fintech interactions

### 2. Cloud LLM Integration Is an Afterthought

- The config supports Ollama as primary provider, but pointing it at Anthropic/OpenAI requires workarounds
- No native Anthropic provider — you'd route through Ollama's OpenAI-compatible API layer
- Defeats the purpose: if you need a cloud LLM for acceptable latency, OpenClaw's local-first advantage evaporates

### 3. No Financial Primitives

- Zero built-in support for: order types, position tracking, P&L, risk models, margin, settlement
- Everything had to be built from scratch as custom Python skills
- The 5,700 skills on ClawHub are overwhelmingly consumer/personal assistant oriented — almost nothing for fintech

### 4. Deployment Complexity

- Full deployment requires Docker Compose (gateway + Prometheus + Grafana + Seq log viewer)
- The gateway runtime is a closed-source Docker image (`openclaw/openclaw:latest`) — cannot run natively without Docker or the `openclaw` CLI
- The `openclaw` CLI (installed via Homebrew/npm) manages a launchd service, which is convenient but opaque
- Config is split across `config.yaml`, `.env`, `openclaw.json`, workspace files, and `SOUL.md`/`AGENTS.md`/`IDENTITY.md` — too many surfaces

### 5. Founder Risk & Governance

- Peter Steinberger announced joining OpenAI in Feb 2026
- Project moving to an open-source foundation — governance is in flux
- For a fintech product that needs long-term stability, this is a concern

### 6. Compliance Gaps

- No SOC 2, PCI-DSS, or ISO 27001 certification
- Memory stored as plaintext Markdown files on disk
- Audit logs exist but lack the depth/format financial regulators expect
- MIT license = zero warranty. If the agent makes a bad trade, liability is entirely on the deployer

---

## Why Claude Code May Be a Better Direction

| Dimension | OpenClaw | Claude Code |
|-----------|----------|-------------|
| **LLM Quality** | Limited by local model size or requires API workaround | Claude Opus/Sonnet natively — state-of-the-art reasoning |
| **Tool Calling** | Requires models with tool support; small models often lack it | Native, reliable tool use with any Claude model |
| **Latency** | 2+ min on consumer hardware; seconds with cloud API | Sub-second to seconds, consistently |
| **Code Awareness** | Agents work in isolated Markdown workspaces | Full codebase access — reads, writes, searches, runs tests |
| **Fintech Skills** | Must build everything as custom Python skills | Can directly use any Python library, API, or CLI tool |
| **Deployment** | Docker + launchd + multiple config surfaces | Single CLI binary, runs anywhere |
| **MCP Servers** | Not supported | Native MCP support for extending capabilities |
| **Hooks & Automation** | Cron + heartbeat system (rigid) | Configurable hooks on any tool call event |
| **Cost** | Free (local) but slow; or pay for cloud LLM API | Anthropic API costs, but predictable and fast |
| **Compliance** | DIY | Anthropic's enterprise offerings, audit trails |

### Key Advantages of Claude Code for Fintech

1. **Direct code execution** — Claude Code can read your trading scripts, run tests, execute Python, and interact with APIs directly. No skill-wrapper boilerplate needed.

2. **Reliable tool calling** — Claude models have best-in-class function calling. No "this model doesn't support tools" errors.

3. **Agent SDK** — Anthropic's Claude Agent SDK allows building custom multi-agent systems in Python with full control over orchestration, without being locked into OpenClaw's workspace/gateway architecture.

4. **MCP ecosystem** — Model Context Protocol servers for databases, APIs, and services can be plugged in without writing custom skills.

5. **Enterprise path** — Anthropic offers enterprise plans with SOC 2 compliance, which matters for fintech.

---

## Recommendation

### Short-term (now)
**Use Claude Code** for fintech agent development. The skills we built (`skills/trading/`, `skills/portfolio/`, etc.) are plain Python — they work with or without OpenClaw. Wire them into Claude Code via MCP servers or direct tool definitions.

### Medium-term (Q2 2026)
Evaluate **Claude Agent SDK** for building a proper multi-agent fintech system. It gives us the multi-agent architecture OpenClaw provides, but with Claude-quality reasoning and without the deployment complexity.

### Keep from OpenClaw
- The **approval workflow pattern** (human-in-the-loop for trades > $X) — implement this in any agent system
- The **workspace isolation model** (per-client/per-fund agents) — good architectural pattern regardless of runtime
- The **local Ollama integration** for confidential document processing — keep this as a secondary model for contract analysis

### Retire
- The OpenClaw gateway deployment (Docker, launchd service)
- The `SOUL.md` / `AGENTS.md` / `IDENTITY.md` personality system (not needed for fintech)
- The multi-channel Telegram/WhatsApp/Slack routing (premature — focus on one channel first)

---

## Appendix: Repository Contents

```
openclaw-fintech/
├── skills/
│   ├── shared/        # RBAC, encryption, database, approval, resilience, metrics
│   ├── trading/       # Exchange clients, arbitrage, momentum, handlers
│   ├── portfolio/     # Rebalancing, performance, drift detection
│   ├── defi/          # Web3, swaps, governance, gas monitoring
│   ├── finance/       # Receipt OCR, Plaid, expense tracking
│   └── legal/         # Contract analysis, SEC EDGAR, GDPR scanner
├── workspaces/        # 6 agent workspace configs
├── gateway/           # config.yaml, .env
├── docker/            # docker-compose.yaml, Prometheus, Grafana
├── tests/             # 111 tests (14 test files)
├── scripts/           # setup.sh, add_sec_entity.sh
├── demo.py            # Direct skill execution demo
├── gateway_bot.py     # Lightweight Telegram bot
└── survey.html        # Full survey report (this conclusion's source)
```

**Tests:** 111 passing across encryption, database, RBAC, approval workflows, resilience, strategy, metrics, GDPR, OCR, session mapping, dead letter queue.

---

*Survey conducted 2026-03-16 through 2026-03-23. Hands-on evaluation performed on M1 MacBook Pro (8GB RAM) running macOS Darwin 25.0.0, Ollama 0.18.2, OpenClaw 2026.3.23-1.*

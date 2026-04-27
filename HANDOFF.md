# OpenClaw Quant Handoff

## Supported Surface

Ship and maintain these entry points:

- `python cli.py` for local operator commands.
- `python -m skills.orchestrator.scheduler` for the autonomous trading scheduler.
- `python gateway_bot.py` as an optional Telegram adapter over the same shipped handlers.
- `docker compose up -d --build trading-scheduler` for containerized scheduler deployment.

Legacy DeFi/finance/portfolio packages are not part of this repository's supported surface unless
those packages are added back with tests.

## Fresh Setup

```bash
git clone https://github.com/Kymi808/openclaw-fintech.git
cd openclaw-fintech
python -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
cp gateway/.env.example gateway/.env
```

Fill in `gateway/.env` before running anything that touches broker, market-data, LLM, alerting, or
messaging APIs.

Generate a stable encryption key once:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Put the output in `DATA_ENCRYPTION_KEY`. Without it, encrypted local data will not decrypt across
process restarts.

## Model Dependency

The production daily pipeline requires the external CS model repository:

```bash
export CS_SYSTEM_PATH=/absolute/path/to/CS_Multi_Model_Trading_System
```

For Docker, mount the model repo into the container:

```bash
CS_SYSTEM_PATH_HOST=/absolute/path/to/CS_Multi_Model_Trading_System \
  docker compose -f docker-compose.yml -f docker-compose.models.yml \
  up -d --build trading-scheduler
```

If no trained model can be loaded, the scheduler fails closed. For local smoke tests only:

```bash
export ALLOW_DUMMY_PREDICTIONS=1
```

Do not set `ALLOW_DUMMY_PREDICTIONS=1` in paper-trading or production deployments.

## Release Gates

Before handoff, release, or merge to `main`, run:

```bash
python -m ruff check .
python -m pytest
python -m compileall cli.py gateway_bot.py skills tests
docker compose config --services
CS_SYSTEM_PATH_HOST=/tmp docker compose -f docker-compose.yml -f docker-compose.models.yml config --services
docker compose -f docker/docker-compose.yaml config --services
docker build .
```

GitHub Actions runs the same core gates on push and pull request.

## Runtime State

These are generated locally and intentionally not tracked:

- `data/*.db`, `data/*.db-*`, `data/*.json`, `data/*.csv`
- `logs/`
- `.openclaw/`
- `workspaces/*/state.json`
- `workspaces/*/pending_execution.json`
- `workspaces/*/data/`
- `workspaces/orchestrator/checkpoints/`

If a runtime artifact is needed for a demo, document how to regenerate it instead of committing it.

## Security Handoff

- Keep `gateway/.env` untracked.
- Rotate development broker, LLM, messaging, and encryption credentials before external handoff.
- Use Alpaca paper trading by default. Switch to live endpoints only after explicit approval and a
  fresh risk review.
- Do not commit audit logs, local databases, session mappings, pending order queues, or API output
  caches.

## Known Product Limits

- No live P&L track record. Paper-trade for at least six months before any real capital allocation.
- The external model repo is required for real predictions.
- CrossMamba/TST are Linux-oriented fallbacks. Apple Silicon local runs should use LightGBM.
- Transaction-cost and slippage assumptions need calibration against actual paper fills.

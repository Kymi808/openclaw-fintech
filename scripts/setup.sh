#!/usr/bin/env bash
set -euo pipefail

echo "=== OpenClaw Quant setup ==="

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

command -v python >/dev/null 2>&1 || { echo "Error: python is required"; exit 1; }
command -v docker >/dev/null 2>&1 || { echo "Error: docker is required"; exit 1; }
docker compose version >/dev/null 2>&1 || { echo "Error: docker compose is required"; exit 1; }

if [ ! -f gateway/.env ]; then
    cp gateway/.env.example gateway/.env
    echo "Created gateway/.env from gateway/.env.example"
    echo "Edit gateway/.env with real paper-trading credentials before starting services."
fi

mkdir -p data logs workspaces/execution-agent workspaces/orchestrator/checkpoints

echo "Validating Compose files..."
docker compose config >/dev/null
docker compose -f docker/docker-compose.yaml config >/dev/null

echo ""
echo "Setup complete."
echo ""
echo "Next steps:"
echo "  1. Create a virtualenv and install dependencies:"
echo "     python -m venv .venv"
echo "     . .venv/bin/activate"
echo "     python -m pip install -r requirements.txt"
echo "  2. Fill in gateway/.env."
echo "  3. Run tests:"
echo "     python -m pytest"
echo "  4. Start the scheduler:"
echo "     docker compose up -d --build trading-scheduler"
echo ""
echo "Optional local CLI:"
echo "  docker compose --profile cli run --rm cli"

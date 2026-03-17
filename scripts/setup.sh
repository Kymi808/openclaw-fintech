#!/usr/bin/env bash
set -euo pipefail

echo "=== OpenClaw Fintech Agent Team — Setup ==="

# Check prerequisites
command -v docker >/dev/null 2>&1 || { echo "Error: docker is required"; exit 1; }
command -v docker compose >/dev/null 2>&1 || { echo "Error: docker compose is required"; exit 1; }

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

# 1. Create .env from template if not exists
if [ ! -f gateway/.env ]; then
    cp gateway/.env.example gateway/.env
    echo "Created gateway/.env from template"
    echo "⚠️  IMPORTANT: Edit gateway/.env with your API keys before starting!"
    echo ""
fi

# 2. Create data directories
mkdir -p logs
mkdir -p workspaces/{trading-agent,portfolio-agent,defi-agent,finance-agent,legal-agent}/data

# 3. Start Ollama first and pull the model
echo "Starting Ollama..."
docker compose -f docker/docker-compose.yaml up -d ollama
echo "Waiting for Ollama to be ready..."
sleep 5

# Pull the local LLM model for legal/confidential processing
echo "Pulling llama3.1:70b model (this may take a while on first run)..."
docker exec fintech-ollama ollama pull llama3.1:70b || {
    echo "Warning: Could not pull 70b model. Trying 8b instead..."
    docker exec fintech-ollama ollama pull llama3.1:8b
}

# 4. Start all services
echo "Starting all services..."
docker compose -f docker/docker-compose.yaml up -d

echo ""
echo "=== Setup Complete ==="
echo ""
echo "Services:"
echo "  Gateway:    ws://localhost:18789"
echo "  Web UI:     http://localhost:3000"
echo "  Ollama:     http://localhost:11434"
echo "  Log Viewer: http://localhost:8080"
echo ""
echo "Next steps:"
echo "  1. Edit gateway/.env with your API keys"
echo "  2. Configure target allocations in workspaces/portfolio-agent/config.json"
echo "  3. Add SEC entities to track in workspaces/legal-agent/data/sec_state.json"
echo "  4. Connect your messaging channels (Telegram, WhatsApp, Slack)"
echo "  5. Send a message to test routing!"
echo ""

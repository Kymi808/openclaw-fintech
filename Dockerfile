FROM python:3.13-slim

WORKDIR /app

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libffi-dev && \
    rm -rf /var/lib/apt/lists/*

# Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application code
COPY . .

# Create data directories
RUN mkdir -p data workspaces logs

# Health check
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "from skills.shared.health import health_checker; print('ok')" || exit 1

# Default: run the scheduler
ENV PYTHONPATH=/app
CMD ["python", "-m", "skills.orchestrator.scheduler"]

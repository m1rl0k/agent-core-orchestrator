# Slim runtime image for the orchestrator. Builds work on linux/amd64 and
# linux/arm64 (Apple Silicon, Graviton, ARM64 Windows under WSL).
FROM python:3.13-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# System deps for psycopg + tree-sitter native bits.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        build-essential libpq-dev git curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY src ./src
COPY agents ./agents

RUN pip install --upgrade pip \
 && pip install .

EXPOSE 8088
CMD ["uvicorn", "agentcore.orchestrator.app:app", "--host", "0.0.0.0", "--port", "8088"]

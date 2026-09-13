FROM ghcr.io/astral-sh/uv:0.11.19 AS uv

FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends libsndfile1 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 collog
COPY --from=uv /uv /uvx /bin/
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY app ./app
COPY scripts ./scripts
COPY alembic ./alembic
COPY alembic.ini ./
COPY docker-compose.yml /app/deploy-bundle/docker-compose.yml
COPY deploy/Caddyfile /app/deploy-bundle/deploy/Caddyfile
RUN uv sync --locked --no-dev
RUN mkdir -p /app/.data/audio && chown -R collog:collog /app/.data
ENV PATH="/app/.venv/bin:$PATH"
USER collog
EXPOSE 8080
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]

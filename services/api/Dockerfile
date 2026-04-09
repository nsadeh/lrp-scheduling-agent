FROM python:3.12-slim

WORKDIR /app

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Copy dependency files first for layer caching
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# Copy application code
COPY . .

ENV PYTHONPATH=/app/src

EXPOSE 8000

# Run migrations (idempotent, --batch = non-interactive) then start the server.
# Railway injects PORT; fall back to 8000 for local Docker usage.
CMD ["sh", "-c", "uv run yoyo apply --database \"$DATABASE_URL\" ./migrations --batch && uv run uvicorn api.main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips='*'"]

# ---------------------------------------------------------------------------
# Build stage – install dependencies with uv
# ---------------------------------------------------------------------------
FROM python:3.14-slim AS builder

WORKDIR /app

# Pull uv from its official image — no pip required
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Copy dependency files first for layer-cache efficiency
COPY pyproject.toml uv.lock ./

# Install dependencies into /app/.venv without installing the project itself
RUN uv sync --frozen --no-install-project --no-dev

# Copy source and run a final sync to install the project package
COPY api.py ./
RUN uv sync --frozen --no-dev

# ---------------------------------------------------------------------------
# Runtime stage – minimal image
# ---------------------------------------------------------------------------
FROM python:3.14-slim AS runtime

# Non-root user for security
RUN groupadd --gid 1001 appgroup \
    && useradd --uid 1001 --gid appgroup --shell /bin/bash --create-home appuser

WORKDIR /app

# Copy the virtual environment and application source from the builder
COPY --from=builder --chown=appuser:appgroup /app/.venv /app/.venv
COPY --from=builder --chown=appuser:appgroup /app/api.py /app/api.py

# Activate the venv by prepending it to PATH
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Drop privileges
USER appuser

EXPOSE 8000

# Gunicorn with UvicornWorker for production:
#   - Multiple workers = parallelism
#   - UvicornWorker = async ASGI support
#   - Graceful timeout, keep-alive, access log to stdout
CMD ["gunicorn", "api:app", \
    "--worker-class", "uvicorn.workers.UvicornWorker", \
    "--workers", "2", \
    "--bind", "0.0.0.0:8000", \
    "--timeout", "120", \
    "--keep-alive", "5", \
    "--access-logfile", "-", \
    "--error-logfile", "-", \
    "--log-level", "info"]

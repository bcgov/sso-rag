# ---------------------------------------------------------------------------
# Build stage – install dependencies with uv
# ---------------------------------------------------------------------------
FROM python:3.14-slim AS builder

WORKDIR /app

# Pull uv from its official image — no pip required
COPY --from=ghcr.io/astral-sh/uv:0.11.6 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# Copy dependency files first for layer-cache efficiency
COPY pyproject.toml uv.lock ./

# Install dependencies into /app/.venv (the project itself is not a package)
RUN uv sync --frozen --no-install-project --no-dev

# ---------------------------------------------------------------------------
# Runtime stage – minimal image
# ---------------------------------------------------------------------------
FROM python:3.14-slim AS runtime

# Non-root user for security
RUN groupadd --gid 1001 appgroup \
    && useradd --uid 1001 --gid appgroup --shell /bin/bash --create-home appuser

WORKDIR /app

# Copy the virtual environment from the builder and the application source
COPY --from=builder --chown=appuser:appgroup /app/.venv /app/.venv
COPY --chown=appuser:appgroup main.py ./
COPY --chown=appuser:appgroup app/ ./app/

# Activate the venv by prepending it to PATH
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Drop privileges
USER appuser

EXPOSE 8000

# Uvicorn with multiple worker processes:
#   - Keep-alive longer than the ALB idle timeout (120s) to avoid 502s from
#     the ALB reusing a connection uvicorn has just closed
#   - Proxy headers trusted from the ALB
CMD ["uvicorn", "main:app", \
    "--host", "0.0.0.0", \
    "--port", "8000", \
    "--workers", "2", \
    "--timeout-keep-alive", "125", \
    "--proxy-headers", \
    "--forwarded-allow-ips", "*", \
    "--log-level", "info"]

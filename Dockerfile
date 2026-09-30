FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.12.7 /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    OBS_HOST=0.0.0.0

# Dependencies first so code edits reuse the cached layer.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
# Install the deployable wheel, including SQL migrations and browser assets.
RUN uv sync --frozen --no-dev --no-editable

# The container never reads a .env file; configuration comes from the platform.
CMD ["/app/.venv/bin/observatory", "serve"]

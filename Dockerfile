# syntax=docker/dockerfile:1.7
#
# Production image: a non-root, source-free virtualenv on python:3.13-slim.
#   docker build -t cue .
#   docker run --rm -p 8000:8000 -e CUE_DATABASE__URL=... cuectl serve

ARG PYTHON_VERSION=3.13
# Optional features to install. Drop "ai" for a ~40% smaller image.
ARG EXTRAS="postgres fcm metrics ai mcp"

FROM python:${PYTHON_VERSION}-slim AS build
ARG EXTRAS
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /src

# Dependencies first, so source edits do not invalidate this layer.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-dev --no-install-project $(printf -- '--extra %s ' $EXTRAS)

COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable $(printf -- '--extra %s ' $EXTRAS)

FROM python:${PYTHON_VERSION}-slim AS runtime
LABEL org.opencontainers.image.source="https://github.com/murtazox04/Cue" \
      org.opencontainers.image.description="Self-hosted notification decision engine" \
      org.opencontainers.image.licenses="MIT"
RUN useradd --system --uid 10001 --home-dir /app --shell /usr/sbin/nologin cue \
    && install -d -o cue -g cue /app
COPY --from=build /opt/venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    CUE_LOG_FORMAT=json
WORKDIR /app
USER cue
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2)"]
ENTRYPOINT ["cuectl"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8000"]

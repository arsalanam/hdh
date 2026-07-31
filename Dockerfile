# syntax=docker/dockerfile:1
# Reproducible build from uv.lock: the image gets exactly the dependency
# versions the lockfile pins. Quality gates (tests, coverage, ruff, mypy,
# design-quality, security) run before this via `just build`.

FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS builder
WORKDIR /app
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable --extra risk --extra agent --extra api

FROM python:3.13-slim
LABEL org.opencontainers.image.title="hdh" \
      org.opencontainers.image.description="Health Data Hub — synthetic family-medicine EHR toolkit" \
      org.opencontainers.image.authors="Ajmal Mahmood" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.source="https://github.com/OWNER/hdh"

COPY --from=builder /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH"

# Non-root; /data holds the SQLite database (mount a volume there)
RUN useradd --create-home --uid 1000 hdh && mkdir /data && chown hdh /data
USER hdh
WORKDIR /data

EXPOSE 8000
ENTRYPOINT ["hdh"]
CMD ["--help"]
